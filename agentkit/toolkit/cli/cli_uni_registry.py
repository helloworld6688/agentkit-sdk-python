# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""CLI commands for uni-reg records and managed registry resources."""

# ruff: noqa: B008

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import click
import requests
import typer
from rich.console import Console
from rich.table import Table

from agentkit.utils.global_config_io import (
    read_global_config_dict,
    write_global_config_dict,
)

console = Console()
error_console = Console(stderr=True)

uni_registry_app = typer.Typer(
    name="uni-reg",
    help="Manage UniRegistry records and managed registry resources.",
    add_completion=False,
)
record_app = typer.Typer(help="Manage records stored in UniRegistry.")
registry_app = typer.Typer(help="Manage cloud UniRegistry instances.")
binding_app = typer.Typer(help="Manage local UniRegistry connection bindings.")

_REGISTRY_CONFIG_CACHE: dict[str, dict[str, Any]] | None = None
_A2A_SYNCER_TOTAL_TIMEOUT_SECONDS = 30 * 60


class UniRegistryAPIError(click.ClickException):
    """An unsuccessful response from a UniRegistry endpoint."""


class UniRegistryNetworkError(UniRegistryAPIError):
    """A request did not receive a response from the registry instance."""


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _json_object(value: str | None, option_name: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{option_name} must be valid JSON") from exc
    if not isinstance(result, dict):
        raise typer.BadParameter(f"{option_name} must be a JSON object")
    return result


def _json_request(
    json_body: str | None,
    json_file: str | None,
    option_name: str = "--json",
) -> dict[str, Any]:
    if json_body and json_file:
        raise typer.BadParameter(f"provide only one of {option_name} or --json-file")
    if json_file:
        try:
            value = Path(json_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"cannot read --json-file: {exc}") from exc
        return _json_object(value, "--json-file")
    return _json_object(json_body, option_name)


def _has_any_key(payload: dict[str, Any], *keys: str) -> bool:
    return any(key in payload for key in keys)


def _has_non_empty_any_key(payload: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _require_payload_keys(payload: dict[str, Any], option_name: str, *keys: str) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        joined = ", ".join(missing)
        raise typer.BadParameter(f"{option_name} must include: {joined}")


def _stringify_record_json_fields(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("data", "network_config", "extensions"):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            payload[key] = json.dumps(value, ensure_ascii=False)
    return payload


def _format_api_error(status_code: int, text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        body = text.strip()
        return f"HTTP {status_code}: {body[:1000]}" if body else f"HTTP {status_code}"

    if isinstance(payload, dict):
        metadata = payload.get("ResponseMetadata")
        if isinstance(metadata, dict):
            error = metadata.get("Error")
            if isinstance(error, dict):
                code = str(error.get("Code") or "").strip()
                message = str(error.get("Message") or "").strip()
                request_id = _request_id_from_response(payload) or ""
                action = str(metadata.get("Action") or "").strip()
                parts = [f"HTTP {status_code}"]
                if code:
                    parts.append(code)
                if message:
                    parts.append(message)
                suffix = []
                if action:
                    suffix.append(f"action={action}")
                if request_id:
                    suffix.append(f"request_id={request_id}")
                result = ": ".join(parts)
                if suffix:
                    result = f"{result} ({', '.join(suffix)})"
                return result

        request_id = _request_id_from_response(payload)
        message = payload.get("message") or payload.get("Message") or payload.get("error")
        if isinstance(message, str) and message.strip():
            result = f"HTTP {status_code}: {message.strip()}"
            if request_id:
                result = f"{result} (request_id={request_id})"
            return result

    return f"HTTP {status_code}: {json.dumps(_mask_sensitive(payload), ensure_ascii=False)[:1000]}"


def _api_error_message(exc: UniRegistryAPIError) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    return str(exc)


def _handle_api_errors(func: Any) -> Any:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except UniRegistryAPIError as exc:
            error_console.print("Error:", highlight=False, soft_wrap=True)
            message = _api_error_message(exc)
            for line in message.splitlines() or [message]:
                error_console.print(f"  {line}", highlight=False, soft_wrap=True)
            raise typer.Exit(1) from exc

    return wrapper


def _data_value(data: str | None, data_file: str | None) -> str:
    if bool(data) == bool(data_file):
        raise typer.BadParameter("provide exactly one of --data or --data-file")
    if data_file:
        try:
            value = Path(data_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"cannot read --data-file: {exc}") from exc
    else:
        value = data or ""
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("--data must contain valid JSON") from exc
    return value


def _tags(values: list[str] | None) -> list[dict[str, str]]:
    result = []
    for value in values or []:
        if "=" not in value:
            raise typer.BadParameter("--tag must use KEY=VALUE")
        key, tag_value = value.split("=", 1)
        if not key or not tag_value:
            raise typer.BadParameter("--tag must use non-empty KEY=VALUE")
        result.append({"key": key, "value": tag_value})
    return result


def _acl_entries(
    values: list[str] | None = None, comma_separated: str | None = None
) -> list[str]:
    entries: list[str] = []
    raw_values = list(values or [])
    if comma_separated is not None:
        raw_values.extend(comma_separated.split(","))
    for raw_value in raw_values:
        entry = raw_value.strip()
        if not entry:
            raise typer.BadParameter("--acl-entry/--acl-entries must be non-empty")
        if entry not in entries:
            entries.append(entry)
    return entries


def _normalize_server(value: str) -> str:
    server = value.strip().rstrip("/")
    parsed = urlparse(server)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise typer.BadParameter("server must be an http(s) URL")
    return server


def _uni_registry_section(force_reload: bool = False) -> dict[str, Any]:
    data = read_global_config_dict(force_reload=force_reload)
    section = data.get("uni_registry")
    return section if isinstance(section, dict) else {}


def _uni_registry_defaults() -> dict[str, Any]:
    section = _uni_registry_section()
    defaults = section.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    result = dict(defaults)
    for key in ("server", "region", "service", "username", "password", "registry_id"):
        value = section.get(key)
        if value not in (None, "") and key not in result:
            result[key] = value
    value = section.get("default_registry_id")
    if value not in (None, "") and "registry_id" not in result:
        result["registry_id"] = value
    return result


def _resolve_connection_options(options: dict[str, Any]) -> dict[str, Any]:
    defaults = _uni_registry_defaults()
    return {
        **options,
        "server": (
            options.get("server")
            or os.getenv("UNI_REGISTRY_SERVER_URL")
            or defaults.get("server")
        ),
        "username": (
            options.get("username")
            or os.getenv("UNI_USERNAME")
            or defaults.get("username")
        ),
        "password": (
            options.get("password")
            or os.getenv("UNI_PASSWORD")
            or defaults.get("password")
        ),
        "region": options.get("region") or defaults.get("region"),
        "service": options.get("service") or defaults.get("service") or "agentkit",
    }


def _default_registry_id() -> str | None:
    resolved = str(_uni_registry_defaults().get("registry_id") or "").strip()
    return resolved or None


def _default_gateway_id() -> str | None:
    resolved = str(_uni_registry_defaults().get("gateway_id") or "").strip()
    return resolved or None


def _resolve_registry_id(
    registry_id: str | None, *, source_label: str = "--registry-id"
) -> str:
    resolved = (registry_id or _default_registry_id() or "").strip()
    if not resolved:
        raise typer.BadParameter(
            f"provide {source_label} or set uni_registry.defaults.registry_id"
        )
    return resolved


def _resolve_gateway_id(gateway_id: str | None) -> str:
    if gateway_id is not None:
        resolved = gateway_id.strip()
        if resolved:
            return resolved
        raise typer.BadParameter("--gateway-id is required")
    resolved = _default_gateway_id()
    if not resolved:
        raise typer.BadParameter(
            "provide --gateway-id or set uni_registry.defaults.gateway_id"
        )
    return resolved


def _set_default_registry_id(registry_id: str) -> None:
    normalized_id = registry_id.strip()
    if not normalized_id:
        return
    data = read_global_config_dict(force_reload=True)
    section = data.setdefault("uni_registry", {})
    if not isinstance(section, dict):
        section = {}
        data["uni_registry"] = section
    defaults = section.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
        section["defaults"] = defaults
    defaults["registry_id"] = normalized_id
    section["default_registry_id"] = normalized_id
    write_global_config_dict(data)


def _registry_configs(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    global _REGISTRY_CONFIG_CACHE
    if _REGISTRY_CONFIG_CACHE is not None and not force_reload:
        return _REGISTRY_CONFIG_CACHE
    section = _uni_registry_section(force_reload=force_reload)
    registries = section.get("registries")
    if not isinstance(registries, dict):
        registries = {}
    _REGISTRY_CONFIG_CACHE = {
        str(key): value for key, value in registries.items() if isinstance(value, dict)
    }
    return _REGISTRY_CONFIG_CACHE


def _save_registry_configs(registries: dict[str, dict[str, Any]]) -> None:
    global _REGISTRY_CONFIG_CACHE
    data = read_global_config_dict(force_reload=True)
    section = data.setdefault("uni_registry", {})
    if not isinstance(section, dict):
        section = {}
        data["uni_registry"] = section
    section["registries"] = registries
    write_global_config_dict(data)
    _REGISTRY_CONFIG_CACHE = registries


def _upsert_registry_config(registry_id: str, updates: dict[str, Any]) -> None:
    normalized_id = registry_id.strip()
    if not normalized_id:
        return
    registries = dict(_registry_configs())
    current = dict(registries.get(normalized_id, {}))
    for key, value in updates.items():
        if value is not None and value != "":
            current[key] = value
    if current:
        registries[normalized_id] = current
        _save_registry_configs(registries)


def _registry_config(registry_id: str) -> dict[str, Any] | None:
    value = _registry_configs().get(registry_id.strip())
    return dict(value) if isinstance(value, dict) else None


def _mask_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if (
                normalized_key in {
                    "key",
                    "api_key",
                    "apikey",
                }
                or "password" in normalized_key
                or "secret" in normalized_key
                or "access_key" in normalized_key
            ) and isinstance(item, str):
                result[key] = "******"
            else:
                result[key] = _mask_sensitive(item)
        return result
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value


def _masked_registry_config(registry_id: str, value: dict[str, Any]) -> dict[str, Any]:
    return _mask_sensitive({"registry_id": registry_id, **value})


def _request_id_from_response(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("request_id", "requestId", "RequestId"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict):
        for key in ("RequestId", "requestId", "request_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _registry_id_from_response(response: Any) -> str | None:
    registry = _registry_from_response(response)
    candidates: list[dict[str, Any]] = []
    if registry:
        candidates.append(registry)
    if isinstance(response, dict):
        result = response.get("Result")
        if isinstance(result, dict):
            candidates.append(result)
    for candidate in candidates:
        for key in ("id", "Id", "registry_id", "RegistryId"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _registry_status_from_response(response: Any) -> str | None:
    registry = _registry_from_response(response)
    if registry:
        for key in ("status", "Status"):
            value = registry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(response, dict):
        result = response.get("Result")
        if isinstance(result, dict):
            for key in ("status", "Status"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _registry_field_from_response(response: Any, *keys: str) -> Any:
    registry = _registry_from_response(response)
    if registry:
        for key in keys:
            if key in registry:
                return registry[key]
    if isinstance(response, dict):
        result = response.get("Result")
        if isinstance(result, dict):
            for key in keys:
                if key in result:
                    return result[key]
    return None


def _first_registry_field(response: Any, *keys: str) -> Any:
    for key in keys:
        value = _registry_field_from_response(response, key)
        if value is not None:
            return value
    return None


def _registry_nested_field(response: Any, container_keys: tuple[str, ...], *keys: str) -> Any:
    registry = _registry_from_response(response)
    if not registry:
        return None
    container = None
    for container_key in container_keys:
        value = registry.get(container_key)
        if isinstance(value, dict):
            container = value
            break
    if not container:
        return None
    for key in keys:
        value = container.get(key)
        if value is not None:
            return value
    return None


def _compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _registry_ui_url(public_address: Any) -> str | None:
    if not isinstance(public_address, str) or not public_address.strip():
        return None
    address = public_address.strip().rstrip("/")
    parsed = urlparse(address if "://" in address else f"http://{address}")
    if not parsed.netloc:
        return None
    return f"http://{parsed.netloc}/ui"


def _registry_create_summary(
    response: Any,
    wait_result: dict[str, Any] | None = None,
    migration_result: dict[str, Any] | None = None,
    skill_migration_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = _compact_dict(
        {
            "id": _registry_id_from_response(response),
            "request_id": _request_id_from_response(response),
        }
    )
    if wait_result is not None:
        summary["wait"] = _compact_dict(
            {
                "status": wait_result.get("Status"),
                "ready": wait_result.get("Ready"),
                "timed_out": wait_result.get("TimedOut"),
                "attempts": wait_result.get("Attempts"),
                "request_id": wait_result.get("RequestId"),
            }
        )
    if migration_result is not None:
        summary["a2a_syncer"] = _compact_dict(
            {
                "id": migration_result.get("Id"),
                "status": migration_result.get("Status"),
                "ready": migration_result.get("Ready"),
                "timed_out": migration_result.get("TimedOut"),
                "attempts": migration_result.get("Attempts"),
                "request_id": migration_result.get("RequestId"),
                "message": migration_result.get("Message"),
            }
        )
    if skill_migration_result is not None:
        summary["skill_syncer"] = _compact_dict(
            {
                "id": skill_migration_result.get("Id"),
                "status": skill_migration_result.get("Status"),
                "ready": skill_migration_result.get("Ready"),
                "timed_out": skill_migration_result.get("TimedOut"),
                "attempts": skill_migration_result.get("Attempts"),
                "request_id": skill_migration_result.get("RequestId"),
                "message": skill_migration_result.get("Message"),
            }
        )
    return summary


def _registry_detail_summary(response: Any, fallback_id: str | None = None) -> dict[str, Any]:
    public_address = _first_registry_field(response, "public_address", "PublicAddress")
    return _compact_dict(
        {
            "id": _registry_id_from_response(response) or fallback_id,
            "request_id": _request_id_from_response(response),
            "status": _registry_status_from_response(response),
            "public_address": public_address,
            "private_address": _first_registry_field(
                response, "private_address", "PrivateAddress"
            ),
            "ui_url": _registry_ui_url(public_address),
            "username": _first_registry_field(
                response, "username", "Username", "user_name", "UserName"
            )
            or "uni",
            "password": _first_registry_field(
                response, "initial_password", "InitialPassword", "password", "Password"
            ),
        }
    )


def _registry_update_summary(response: Any, fallback_id: str | None = None) -> dict[str, Any]:
    return _compact_dict(
        {
            "id": _registry_id_from_response(response) or fallback_id,
            "request_id": _request_id_from_response(response),
            "status": _registry_status_from_response(response),
            "public_address": _first_registry_field(
                response, "public_address", "PublicAddress"
            ),
            "private_address": _first_registry_field(
                response, "private_address", "PrivateAddress"
            ),
            "acl_entries": _first_registry_field(
                response, "acl_entries", "AclEntries", "ACLEntries"
            )
            or _registry_nested_field(
                response, ("network_spec", "NetworkSpec"), "acl_entries", "AclEntries"
            ),
        }
    )


def _registry_syncer_summary(
    *,
    registry_id: str,
    created: bool,
    create_response: Any | None,
    wait_result: dict[str, Any] | None,
    acl_update_response: Any | None = None,
    registry_response: Any | None = None,
    migration_result: dict[str, Any] | None = None,
    skill_migration_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = _compact_dict(
        {
            "id": registry_id,
            "created": created,
            "request_id": _request_id_from_response(create_response),
        }
    )
    if acl_update_response is not None:
        summary["acl_update"] = _registry_update_summary(
            acl_update_response, registry_id
        )
    if wait_result is not None:
        summary["wait"] = _compact_dict(
            {
                "status": wait_result.get("Status"),
                "ready": wait_result.get("Ready"),
                "timed_out": wait_result.get("TimedOut"),
                "attempts": wait_result.get("Attempts"),
                "request_id": wait_result.get("RequestId"),
            }
        )
    if created and registry_response is not None:
        summary["registry"] = _registry_detail_summary(
            registry_response, fallback_id=registry_id
        )
    if migration_result is not None:
        summary["a2a_syncer"] = _compact_dict(
            {
                "id": migration_result.get("Id"),
                "status": migration_result.get("Status"),
                "ready": migration_result.get("Ready"),
                "timed_out": migration_result.get("TimedOut"),
                "attempts": migration_result.get("Attempts"),
                "request_id": migration_result.get("RequestId"),
                "message": migration_result.get("Message"),
            }
        )
    if skill_migration_result is not None:
        summary["skill_syncer"] = _compact_dict(
            {
                "id": skill_migration_result.get("Id"),
                "status": skill_migration_result.get("Status"),
                "ready": skill_migration_result.get("Ready"),
                "timed_out": skill_migration_result.get("TimedOut"),
                "attempts": skill_migration_result.get("Attempts"),
                "request_id": skill_migration_result.get("RequestId"),
                "message": skill_migration_result.get("Message"),
            }
        )
    return summary


def _uni_migration_summary(
    response: Any,
    *,
    fallback_id: str | None = None,
    registry_id: str | None = None,
) -> dict[str, Any]:
    return _compact_dict(
        {
            "id": _migration_id_from_response(response) or fallback_id,
            "registry_id": registry_id,
            "request_id": _request_id_from_response(response),
            "status": _migration_status_from_response(response),
            "progress": _migration_field_from_response(response, "progress", "Progress"),
            "message": _migration_field_from_response(response, "message", "Message"),
        }
    )


def _first_result_field(response: Any, *keys: str) -> Any:
    if not isinstance(response, dict):
        return None
    candidates = [response]
    result = response.get("Result")
    if isinstance(result, dict):
        candidates.insert(0, result)
    for candidate in candidates:
        for key in keys:
            if key in candidate:
                return candidate[key]
    return None


def _registries_from_list_response(response: Any) -> list[dict[str, Any]]:
    result: Any = response
    if isinstance(response, dict) and isinstance(response.get("Result"), dict):
        result = response["Result"]
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    for key in (
        "registries",
        "Registries",
        "uni_registries",
        "UniRegistries",
        "registry_list",
        "RegistryList",
        "items",
        "Items",
        "list",
        "List",
    ):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _registry_list_summary(response: Any) -> dict[str, Any]:
    items = [
        _registry_detail_summary({"Registry": registry})
        for registry in _registries_from_list_response(response)
    ]
    return _compact_dict(
        {
            "request_id": _request_id_from_response(response),
            "page": _first_result_field(
                response, "page", "Page", "PageNumber", "page_number"
            ),
            "page_size": _first_result_field(
                response, "page_size", "PageSize", "pageSize"
            ),
            "total": _first_result_field(
                response, "total", "Total", "TotalCount", "total_count"
            ),
            "items": items,
        }
    )


def _is_registry_ready_status(status: str | None) -> bool:
    return (status or "").lower() in {"running", "ready"}


def _is_registry_failed_status(status: str | None) -> bool:
    return (status or "").lower() in {"failed", "error", "abnormal"}


def _migration_from_response(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    result = response.get("Result", response)
    if not isinstance(result, dict):
        return None
    for key in (
        "migration",
        "Migration",
        "a2a_uni_migration",
        "A2aUniMigration",
        "task",
        "Task",
    ):
        value = result.get(key)
        if isinstance(value, dict):
            return value
    return result


def _migration_field_from_response(response: Any, *keys: str) -> Any:
    migration = _migration_from_response(response)
    if migration:
        for key in keys:
            if key in migration:
                return migration[key]
    return None


def _migration_id_from_response(response: Any) -> str | None:
    for key in ("id", "Id", "migration_id", "MigrationId", "task_id", "TaskId"):
        value = _migration_field_from_response(response, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _migration_status_from_response(response: Any) -> str | None:
    for key in ("status", "Status", "state", "State"):
        value = _migration_field_from_response(response, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_migration_ready_status(status: str | None) -> bool:
    return (status or "").lower() in {
        "succeeded",
        "success",
        "completed",
        "complete",
        "finished",
        "ready",
    }


def _is_migration_failed_status(status: str | None) -> bool:
    return (status or "").lower() in {
        "failed",
        "error",
        "abnormal",
        "canceled",
        "cancelled",
        "terminated",
    }


def _wait_for_registry_ready(
    client: UniRegistryClient,
    resource_id: str,
    *,
    interval: float,
    timeout: float,
    top: dict[str, Any],
) -> dict[str, Any]:
    error_console.print(
        f"Polling UniRegistry {resource_id}: interval={interval}s timeout={timeout}s"
    )
    deadline = time.monotonic() + timeout
    last_response: Any = None
    last_status: str | None = None
    attempts = 0
    started_at = time.monotonic()
    while True:
        attempts += 1
        last_response = client.get_resource(resource_id, top)
        last_status = _registry_status_from_response(last_response)
        request_id = _request_id_from_response(last_response)
        request_id_text = f" request_id={request_id}" if request_id else ""
        ready_replicas = _registry_field_from_response(
            last_response, "ready_replicas", "ReadyReplicas"
        )
        replicas = _registry_field_from_response(last_response, "replicas", "Replicas")
        elapsed = time.monotonic() - started_at
        if _is_registry_ready_status(last_status):
            error_console.print(
                f"Polling UniRegistry {resource_id}: attempt={attempts} "
                f"status={last_status or 'Unknown'} ready_replicas={ready_replicas} "
                f"replicas={replicas} elapsed={elapsed:.1f}s result=ready{request_id_text}"
            )
            result = {
                "Status": last_status,
                "Ready": True,
                "TimedOut": False,
                "Attempts": attempts,
                "GetUniRegistryResponse": last_response,
            }
            if request_id:
                result["RequestId"] = request_id
            return result
        if _is_registry_failed_status(last_status):
            error_console.print(
                f"Polling UniRegistry {resource_id}: attempt={attempts} "
                f"status={last_status or 'Unknown'} ready_replicas={ready_replicas} "
                f"replicas={replicas} elapsed={elapsed:.1f}s result=failed{request_id_text}"
            )
            result = {
                "Status": last_status,
                "Ready": False,
                "TimedOut": False,
                "Attempts": attempts,
                "GetUniRegistryResponse": last_response,
            }
            if request_id:
                result["RequestId"] = request_id
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error_console.print(
                f"Polling UniRegistry {resource_id}: attempt={attempts} "
                f"status={last_status or 'Unknown'} ready_replicas={ready_replicas} "
                f"replicas={replicas} elapsed={elapsed:.1f}s result=timeout{request_id_text}"
            )
            result = {
                "Status": last_status,
                "Ready": False,
                "TimedOut": True,
                "Attempts": attempts,
                "GetUniRegistryResponse": last_response,
            }
            if request_id:
                result["RequestId"] = request_id
            return result
        sleep_seconds = min(interval, remaining)
        error_console.print(
            f"Polling UniRegistry {resource_id}: attempt={attempts} "
            f"status={last_status or 'Unknown'} ready_replicas={ready_replicas} "
            f"replicas={replicas} elapsed={elapsed:.1f}s next_poll_in={sleep_seconds:.1f}s"
            f"{request_id_text}"
        )
        time.sleep(sleep_seconds)


def _wait_for_uni_migration(
    migration_id: str,
    *,
    label: str,
    response_key: str,
    get_migration: Any,
    registry_id: str | None = None,
    with_id: bool = False,
    get_kwargs: dict[str, Any] | None = None,
    interval: float,
    deadline: float,
) -> dict[str, Any]:
    error_console.print(f"Polling {label} {migration_id}: interval={interval}s")
    last_response: Any = None
    last_status: str | None = None
    attempts = 0
    started_at = time.monotonic()
    while True:
        attempts += 1
        last_response = get_migration(
            migration_id,
            registry_id=registry_id,
            with_id=with_id,
            **(get_kwargs or {}),
        )
        last_status = _migration_status_from_response(last_response)
        request_id = _request_id_from_response(last_response)
        request_id_text = f" request_id={request_id}" if request_id else ""
        progress = _migration_field_from_response(last_response, "progress", "Progress")
        message = _migration_field_from_response(last_response, "message", "Message")
        elapsed = time.monotonic() - started_at
        if _is_migration_ready_status(last_status):
            error_console.print(
                f"Polling {label} {migration_id}: attempt={attempts} "
                f"status={last_status or 'Unknown'} progress={progress} "
                f"elapsed={elapsed:.1f}s result=ready{request_id_text}"
            )
            result = {
                "Id": migration_id,
                "Status": last_status,
                "Ready": True,
                "TimedOut": False,
                "Attempts": attempts,
                response_key: last_response,
            }
            if request_id:
                result["RequestId"] = request_id
            return result
        if _is_migration_failed_status(last_status):
            error_console.print(
                f"Polling {label} {migration_id}: attempt={attempts} "
                f"status={last_status or 'Unknown'} progress={progress} "
                f"elapsed={elapsed:.1f}s result=failed{request_id_text}"
            )
            result = {
                "Id": migration_id,
                "Status": last_status,
                "Ready": False,
                "TimedOut": False,
                "Attempts": attempts,
                response_key: last_response,
            }
            if request_id:
                result["RequestId"] = request_id
            if isinstance(message, str) and message.strip():
                result["Message"] = message.strip()
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error_console.print(
                f"Polling {label} {migration_id}: attempt={attempts} "
                f"status={last_status or 'Unknown'} progress={progress} "
                f"elapsed={elapsed:.1f}s result=timeout{request_id_text}"
            )
            result = {
                "Id": migration_id,
                "Status": last_status,
                "Ready": False,
                "TimedOut": True,
                "Attempts": attempts,
                response_key: last_response,
            }
            if request_id:
                result["RequestId"] = request_id
            return result
        sleep_seconds = min(interval, remaining)
        error_console.print(
            f"Polling {label} {migration_id}: attempt={attempts} "
            f"status={last_status or 'Unknown'} progress={progress} "
            f"elapsed={elapsed:.1f}s next_poll_in={sleep_seconds:.1f}s"
            f"{request_id_text}"
        )
        time.sleep(sleep_seconds)


def _wait_for_a2a_uni_migration(
    client: UniRegistryClient,
    migration_id: str,
    *,
    registry_id: str | None = None,
    with_id: bool = False,
    interval: float,
    deadline: float,
) -> dict[str, Any]:
    return _wait_for_uni_migration(
        migration_id,
        label="A2A Uni migration",
        response_key="GetA2aUniMigrationResponse",
        get_migration=client.get_a2a_uni_migration,
        registry_id=registry_id,
        with_id=with_id,
        interval=interval,
        deadline=deadline,
    )


def _wait_for_skill_uni_migration(
    client: UniRegistryClient,
    migration_id: str,
    *,
    workspace_id: str,
    registry_id: str | None = None,
    with_id: bool = False,
    interval: float,
    deadline: float,
) -> dict[str, Any]:
    return _wait_for_uni_migration(
        migration_id,
        label="Skill Uni migration",
        response_key="GetSkillUniMigrationResponse",
        get_migration=client.get_skill_uni_migration,
        registry_id=registry_id,
        with_id=with_id,
        get_kwargs={"workspace_id": workspace_id},
        interval=interval,
        deadline=deadline,
    )


def _start_and_wait_uni_migration(
    client: UniRegistryClient,
    *,
    label: str,
    registry_id: str,
    top: dict[str, Any],
    start_migration: Any,
    wait_migration: Any,
    start_kwargs: dict[str, Any] | None = None,
    wait_kwargs: dict[str, Any] | None = None,
    interval: float,
    deadline: float,
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UniRegistryAPIError(
            f"{label} was not started because the timeout was exhausted "
            "while waiting for UniRegistry readiness"
        )
    error_console.print(f"Starting {label}: registry_id={registry_id}")
    start_response = start_migration(
        registry_id, top, with_id=True, **(start_kwargs or {})
    )
    migration_id = _migration_id_from_response(start_response) or registry_id
    start_request_id = _request_id_from_response(start_response)
    request_id_text = f" request_id={start_request_id}" if start_request_id else ""
    error_console.print(f"Started {label}: id={migration_id}{request_id_text}")
    return wait_migration(
        client,
        migration_id,
        registry_id=registry_id,
        with_id=True,
        **(wait_kwargs or {}),
        interval=interval,
        deadline=deadline,
    )


def _registry_from_response(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    result = response.get("Result", response)
    if not isinstance(result, dict):
        return None
    for key in ("registry", "Registry", "uni_registry", "UniRegistry"):
        value = result.get(key)
        if isinstance(value, dict):
            return value
    return None


def _top_payload(top: dict[str, Any]) -> dict[str, Any]:
    return {"Top": top} if top else {}


def _uni_registry_id_header(resource_id: str) -> dict[str, str]:
    return {"X-Mse-Uni-Registry-Id": resource_id}


def _uni_test_suffix_header() -> dict[str, str]:
    return {"X-Mse-Uni-Test-Suffix": "test"}


def _default_syncer_create_payload(gateway_id: str) -> dict[str, Any]:
    return {
        "Name": f"registry-{secrets.token_hex(4)}",
        "Replicas": 2,
        "DeletionProtectionEnabled": False,
        "NetworkSpec": {
            "NetworkType": ["PUBLIC"],
            "EipBandwidth": 1,
            "IpVersion": "IPv4",
        },
        "GatewayId": gateway_id,
    }


def _registry_id_header_from_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    for key in ("id", "Id", "registry_id", "RegistryId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _uni_registry_id_header(value.strip())
    return None


def _resource_payload(payload: dict[str, Any]) -> dict[str, Any]:
    field_names = {
        "id": "Id",
        "name": "Name",
        "replicas": "Replicas",
        "gateway_id": "GatewayId",
        "network_spec": "NetworkSpec",
        "monitor_spec": "MonitorSpec",
        "project_name": "ProjectName",
        "tags": "Tags",
        "metadata": "Metadata",
        "deletion_protection_enabled": "DeletionProtectionEnabled",
        "top": "Top",
        "page_number": "PageNumber",
        "page_size": "PageSize",
        "filter": "Filter",
        "tag_filters": "TagFilters",
    }
    filter_names = {
        "id": "Id",
        "name": "Name",
        "status": "Status",
        "vpc_id": "VpcId",
        "project_name": "ProjectName",
    }
    result: dict[str, Any] = {}
    for key, value in payload.items():
        target = field_names.get(key, key)
        if key == "filter" and isinstance(value, dict):
            result[target] = {
                filter_names.get(filter_key, filter_key): filter_value
                for filter_key, filter_value in value.items()
            }
        else:
            result[target] = value
    return result


class UniRegistryClient:
    """HTTP client shared by record and managed-resource CLI commands."""

    def __init__(
        self,
        server: str,
        username: str | None = None,
        password: str | None = None,
        region: str | None = None,
        service: str = "agentkit",
        timeout: int = 30,
        api_version: str = "2025-10-30",
    ) -> None:
        self.server = server.rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self.service = service
        self.api_version = api_version
        self.access_key = ""
        self.secret_key = ""
        self.session_token = ""
        self.region = region or ""

    def _load_credentials(self) -> None:
        if self.access_key and self.secret_key:
            return
        from agentkit.platform import VolcConfiguration

        config = VolcConfiguration(region=self.region or None)
        credentials = config.get_service_credentials("agentkit")
        endpoint = config.get_service_endpoint("agentkit")
        self.access_key = credentials.access_key
        self.secret_key = credentials.secret_key
        self.session_token = credentials.session_token or ""
        self.region = self.region or endpoint.region

    def _signed_headers(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        body: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        if not self.access_key or not self.secret_key:
            raise UniRegistryAPIError("Volcengine credentials are required")

        now = dt.datetime.now(dt.timezone.utc)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        date = timestamp[:8]
        payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical_query = urlencode(
            sorted(params.items()), quote_via=quote, safe="-_.~"
        )
        host = self.server.split("://", 1)[-1].split("/", 1)[0]
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Host": host,
            "X-Date": timestamp,
            "X-Content-Sha256": payload_hash,
        }
        if extra_headers:
            headers.update(extra_headers)
        signed_names = ["content-type", "host", "x-content-sha256", "x-date"]
        if extra_headers:
            signed_names.extend(
                sorted(
                    name.lower()
                    for name in extra_headers
                    if name.lower() not in signed_names
                )
            )
        if self.session_token:
            headers["X-Security-Token"] = self.session_token
            signed_names.append("x-security-token")
        canonical_headers = "\n".join(
            f"{name}:{headers[next(k for k in headers if k.lower() == name)].strip()}"
            for name in signed_names
        )
        canonical_request = "\n".join(
            [
                method.upper(),
                path,
                canonical_query,
                canonical_headers + "\n",
                ";".join(signed_names),
                payload_hash,
            ]
        )
        scope = f"{date}/{self.region}/{self.service}/request"
        string_to_sign = "\n".join(
            [
                "HMAC-SHA256",
                timestamp,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = _hmac_sha256(self.secret_key.encode("utf-8"), date)
        signing_key = _hmac_sha256(signing_key, self.region)
        signing_key = _hmac_sha256(signing_key, self.service)
        signing_key = _hmac_sha256(signing_key, "request")
        signature = _hmac_sha256(signing_key, string_to_sign).hex()
        headers["Authorization"] = (
            f"HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={';'.join(signed_names)}, Signature={signature}"
        )
        return headers

    def _uses_openapi_action_query(self) -> bool:
        host = urlparse(self.server).hostname or ""
        return host.endswith(
            ("volcengineapi.com", "volcengineapi.byted.org", "byteplusapi.com")
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        query = params or {}
        request_path = path
        if (
            not path.startswith("/api/v1/")
            and path != "/"
            and self._uses_openapi_action_query()
        ):
            query = {
                "Action": path.lstrip("/"),
                "Version": self.api_version,
                **query,
            }
            request_path = "/"
        request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        auth = None
        if self.username or self.password:
            auth = (self.username, self.password)
        elif not path.startswith("/api/v1/"):
            self._load_credentials()
            request_headers = self._signed_headers(
                method, request_path, query, body_text, headers
            )
        try:
            response = requests.request(
                method,
                f"{self.server}{request_path}",
                params=query,
                data=body_text or None,
                headers=request_headers,
                auth=auth,
                timeout=self.timeout,
            )
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError,
        ) as exc:
            raise UniRegistryNetworkError(f"request failed: {exc}") from exc
        except requests.RequestException as exc:
            raise UniRegistryAPIError(f"request failed: {exc}") from exc
        if not response.ok:
            raise UniRegistryAPIError(
                _format_api_error(response.status_code, response.text)
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise UniRegistryAPIError("response is not valid JSON") from exc

    def create_record(self, payload: dict[str, Any]) -> Any:
        return self.request("POST", "/api/v1/records", payload)

    def get_record(self, record_id: str) -> Any:
        return self.request("GET", f"/api/v1/records/{quote(record_id, safe='')}")

    def list_records(self, params: dict[str, Any]) -> Any:
        return self.request("GET", "/api/v1/records", params=params)

    def update_record(self, record_id: str, payload: dict[str, Any]) -> Any:
        return self.request(
            "PUT", f"/api/v1/records/{quote(record_id, safe='')}", payload
        )

    def delete_record(self, record_id: str) -> Any:
        return self.request("DELETE", f"/api/v1/records/{quote(record_id, safe='')}")

    def create_resource(self, payload: dict[str, Any], *, with_id: bool = False) -> Any:
        headers = _uni_test_suffix_header() if with_id else None
        return self.request(
            "POST", "/CreateUniRegistry", _resource_payload(payload), headers=headers
        )

    def get_resource(self, resource_id: str, top: dict[str, Any]) -> Any:
        headers = _uni_registry_id_header(resource_id)
        return self.request(
            "POST",
            "/GetUniRegistry",
            {"Id": resource_id, **_top_payload(top)},
            headers=headers,
        )

    def list_resources(self, payload: dict[str, Any]) -> Any:
        return self.request("POST", "/ListUniRegistries", _resource_payload(payload))

    def update_resource(self, payload: dict[str, Any]) -> Any:
        headers = _registry_id_header_from_payload(payload)
        return self.request(
            "POST", "/UpdateUniRegistry", _resource_payload(payload), headers=headers
        )

    def delete_resource(self, resource_id: str, top: dict[str, Any]) -> Any:
        headers = _uni_registry_id_header(resource_id)
        return self.request(
            "POST",
            "/DeleteUniRegistry",
            {"Id": resource_id, **_top_payload(top)},
            headers=headers,
        )

    def start_a2a_uni_migration(
        self, resource_id: str, top: dict[str, Any], *, with_id: bool = False
    ) -> Any:
        headers = _uni_registry_id_header(resource_id) if with_id else None
        return self.request(
            "POST",
            "/StartA2aUniMigration",
            {"Id": resource_id, **_top_payload(top)},
            headers=headers,
        )

    def get_a2a_uni_migration(
        self,
        migration_id: str,
        *,
        registry_id: str | None = None,
        with_id: bool = False,
    ) -> Any:
        headers = _uni_registry_id_header(registry_id) if with_id and registry_id else None
        return self.request(
            "POST",
            "/GetA2aUniMigration",
            {"Id": migration_id},
            headers=headers,
        )

    def retry_a2a_uni_migration(
        self,
        migration_id: str,
        *,
        version: int,
        registry_id: str | None = None,
        with_id: bool = False,
    ) -> Any:
        headers = _uni_registry_id_header(registry_id) if with_id and registry_id else None
        return self.request(
            "POST",
            "/RetryA2aUniMigration",
            {"Id": migration_id, "Version": version},
            headers=headers,
        )

    def start_skill_uni_migration(
        self,
        resource_id: str,
        top: dict[str, Any],
        *,
        workspace_id: str,
        with_id: bool = False,
    ) -> Any:
        headers = _uni_registry_id_header(resource_id) if with_id else None
        return self.request(
            "POST",
            "/StartSkillUniMigration",
            {"Id": resource_id, "WorkspaceId": workspace_id, **_top_payload(top)},
            headers=headers,
        )

    def get_skill_uni_migration(
        self,
        migration_id: str,
        *,
        workspace_id: str,
        registry_id: str | None = None,
        with_id: bool = False,
    ) -> Any:
        headers = _uni_registry_id_header(registry_id) if with_id and registry_id else None
        return self.request(
            "POST",
            "/GetSkillUniMigration",
            {"Id": migration_id, "WorkspaceId": workspace_id},
            headers=headers,
        )

    def retry_skill_uni_migration(
        self,
        migration_id: str,
        *,
        workspace_id: str,
        version: int,
        registry_id: str | None = None,
        with_id: bool = False,
    ) -> Any:
        headers = _uni_registry_id_header(registry_id) if with_id and registry_id else None
        return self.request(
            "POST",
            "/RetrySkillUniMigration",
            {"Id": migration_id, "WorkspaceId": workspace_id, "Version": version},
            headers=headers,
        )

    def record_clients(self, registry_id: str) -> list[UniRegistryClient]:
        cached = _registry_config(registry_id)
        if cached:
            clients = _clients_from_registry_config(cached, self)
            if clients:
                return clients

        registry_response = self.get_resource(registry_id, {})
        registry = _registry_from_response(registry_response)
        if not isinstance(registry, dict):
            raise UniRegistryAPIError(
                f"GetUniRegistry({registry_id}) did not return a registry"
            )

        updates = _registry_updates_from_registry_response(
            registry,
            username=self.username or None,
            password=self.password or None,
            region=self.region or None,
            service=self.service or None,
        )
        _upsert_registry_config(registry_id, updates)
        clients = _clients_from_registry_config(updates, self)
        if not clients:
            raise UniRegistryAPIError(
                f"UniRegistry {registry_id} has no private_address or public_address"
            )
        return clients

    def record_client(self, registry_id: str) -> UniRegistryClient:
        return self.record_clients(registry_id)[0]


def _registry_updates_from_registry_response(
    registry: dict[str, Any],
    *,
    username: str | None,
    password: str | None,
    region: str | None,
    service: str | None,
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for source, target in [
        ("server", "server"),
        ("private_address", "private_address"),
        ("PrivateAddress", "private_address"),
        ("public_address", "public_address"),
        ("PublicAddress", "public_address"),
    ]:
        value = registry.get(source)
        if isinstance(value, str) and value.strip():
            try:
                updates[target] = _normalize_server(value)
            except typer.BadParameter:
                # A provisioning instance can expose an address before it is usable.
                continue
    for key, value in {
        "username": username,
        "password": password,
        "region": region,
        "service": service,
    }.items():
        if value:
            updates[key] = value
    return updates


def _clients_from_registry_config(
    config: dict[str, Any], fallback: UniRegistryClient
) -> list[UniRegistryClient]:
    endpoints: list[str] = []
    for field in ("private_address", "server", "public_address"):
        address = str(config.get(field) or "").strip().rstrip("/")
        if not address or address in endpoints:
            continue
        parsed = urlparse(address)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise UniRegistryAPIError(f"registry config has invalid {field}: {address!r}")
        endpoints.append(address)
    return [
        UniRegistryClient(
            endpoint,
            str(config.get("username") or fallback.username or "") or None,
            str(config.get("password") or fallback.password or "") or None,
            str(config.get("region") or fallback.region or "") or None,
            str(config.get("service") or fallback.service or "agentkit"),
            fallback.timeout,
        )
        for endpoint in endpoints
    ]


def _client(options: dict[str, Any]) -> UniRegistryClient:
    resolved = _resolve_connection_options(options)
    server = resolved["server"]
    if not server:
        from agentkit.platform import VolcConfiguration

        endpoint = VolcConfiguration(region=resolved["region"]).get_service_endpoint(
            "agentkit"
        )
        server = f"{endpoint.scheme}://{endpoint.host}"
    return UniRegistryClient(
        server,
        resolved["username"],
        resolved["password"],
        resolved["region"],
        resolved["service"],
    )


def _registry_data_clients(
    options: dict[str, Any], registry_id: str | None
) -> list[UniRegistryClient]:
    base_client = _client(options)
    if registry_id:
        cached = _registry_config(registry_id)
        if cached:
            clients = _clients_from_registry_config(cached, base_client)
            if clients:
                return clients
        return base_client.record_clients(registry_id)
    return [base_client]


def _record_client(options: dict[str, Any], registry_id: str) -> UniRegistryClient:
    return _client(options).record_client(registry_id)


def _call_registry_data(
    options: dict[str, Any], registry_id: str | None, operation: Any
) -> Any:
    last_error: UniRegistryNetworkError | None = None
    for client in _registry_data_clients(options, registry_id):
        try:
            return operation(client)
        except UniRegistryNetworkError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise UniRegistryAPIError(f"UniRegistry {registry_id} has no data-plane endpoint")


def _call_record(options: dict[str, Any], registry_id: str, operation: Any) -> Any:
    return _call_registry_data(options, registry_id, operation)


def _cache_resource_response(
    response: Any,
    options: dict[str, Any],
    explicit_registry_id: str | None = None,
) -> None:
    registry = _registry_from_response(response)
    registry_id = explicit_registry_id or _registry_id_from_response(response)
    if not registry or not registry_id:
        return
    resolved = _resolve_connection_options(options)
    updates = _registry_updates_from_registry_response(
        registry,
        username=resolved.get("username"),
        password=resolved.get("password"),
        region=resolved.get("region"),
        service=resolved.get("service"),
    )
    if updates:
        try:
            _upsert_registry_config(registry_id, updates)
        except OSError:
            # A read-only local config must not invalidate a control-plane result.
            return


def _print(value: Any, output: str) -> None:
    if output == "table" and isinstance(value, dict):
        table = Table(show_header=False)
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                item = json.dumps(item, ensure_ascii=False)
            table.add_row(str(key), str(item))
        console.print(table)
        return
    console.print_json(json.dumps(value, ensure_ascii=False, default=str))


def _options(ctx: typer.Context) -> dict[str, Any]:
    if not isinstance(ctx.obj, dict):
        raise typer.BadParameter("uni-reg command context is unavailable")
    return ctx.obj


@uni_registry_app.callback()
def configure_uni_registry(
    ctx: typer.Context,
    server: str | None = typer.Option(
        None, "--server", help="UniRegistry control-plane URL."
    ),
    username: str | None = typer.Option(
        None, "--username", help="Basic-auth username."
    ),
    password: str | None = typer.Option(
        None, "--password", help="Basic-auth password."
    ),
    region: str | None = typer.Option(
        None, "--region", help="Volcengine signing region."
    ),
    service: str | None = typer.Option(
        None, "--service", help="Volcengine signing service."
    ),
    output: str = typer.Option(
        "json", "--output", "-o", help="Output format: json|table."
    ),
) -> None:
    """Configure the UniRegistry connection for a subcommand."""
    if output not in {"json", "table"}:
        raise typer.BadParameter("--output must be json or table")
    ctx.obj = {
        "server": server,
        "username": username,
        "password": password,
        "region": region,
        "service": service,
        "output": output,
    }


@record_app.command("create")
@_handle_api_errors
def create_record(
    ctx: typer.Context,
    registry_id: str | None = typer.Option(None, "--registry-id"),
    name: str | None = typer.Option(None, "--name"),
    record_type: str | None = typer.Option(None, "--type"),
    record_version: str | None = typer.Option(None, "--record-version"),
    data: str | None = typer.Option(None, "--data"),
    data_file: str | None = typer.Option(None, "--data-file"),
    description: str | None = typer.Option(None, "--description"),
    network_config: str | None = typer.Option(None, "--network-config"),
    extensions: str | None = typer.Option(None, "--extensions"),
    json_body: str | None = typer.Option(None, "--json", help="Full request JSON."),
    json_file: str | None = typer.Option(
        None, "--json-file", help="Read full request JSON from a file."
    ),
) -> None:
    """Create a record."""
    payload = _json_request(json_body, json_file)
    for key, value in {
        "name": name,
        "type": record_type,
        "record_version": record_version,
        "description": description,
        "network_config": network_config,
        "extensions": extensions,
    }.items():
        if value is not None:
            payload[key] = value
    if data is not None or data_file is not None:
        payload["data"] = _data_value(data, data_file)
    elif "data" not in payload:
        raise typer.BadParameter("provide --data, --data-file, or data in --json")
    _require_payload_keys(payload, "--json", "name", "type", "record_version", "data")
    _stringify_record_json_fields(payload)
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    _print(
        _call_record(
            options, resolved_registry_id, lambda client: client.create_record(payload)
        ),
        options["output"],
    )


@record_app.command("get")
@_handle_api_errors
def get_record(
    ctx: typer.Context,
    record_id: str,
    registry_id: str | None = typer.Option(None, "--registry-id"),
) -> None:
    """Get a record by ID."""
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    _print(
        _call_record(
            options, resolved_registry_id, lambda client: client.get_record(record_id)
        ),
        options["output"],
    )


@record_app.command("list")
@_handle_api_errors
def list_records(
    ctx: typer.Context,
    registry_id: str | None = typer.Option(None, "--registry-id"),
    page: int = typer.Option(1, "--page"),
    page_size: int = typer.Option(10, "--page-size"),
    name_contains: str | None = typer.Option(None, "--name-contains"),
    record_type: str | None = typer.Option(None, "--type"),
    record_version: str | None = typer.Option(None, "--record-version"),
    status: str | None = typer.Option(None, "--status"),
) -> None:
    """List records."""
    params = {"page_number": page, "page_size": page_size}
    for key, value in {
        "name_contains": name_contains,
        "type": record_type,
        "record_version": record_version,
        "status": status,
    }.items():
        if value is not None:
            params[key] = value
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    _print(
        _call_record(
            options, resolved_registry_id, lambda client: client.list_records(params)
        ),
        options["output"],
    )


@record_app.command("update")
@_handle_api_errors
def update_record(
    ctx: typer.Context,
    record_id: str,
    registry_id: str | None = typer.Option(None, "--registry-id"),
    name: str | None = typer.Option(None, "--name"),
    description: str | None = typer.Option(None, "--description"),
    record_type: str | None = typer.Option(None, "--type"),
    record_version: str | None = typer.Option(None, "--record-version"),
    data: str | None = typer.Option(None, "--data"),
    network_config: str | None = typer.Option(None, "--network-config"),
    extensions: str | None = typer.Option(None, "--extensions"),
    status: str | None = typer.Option(None, "--status"),
    error_message: str | None = typer.Option(None, "--error-message"),
    json_body: str | None = typer.Option(None, "--json", help="Additional request JSON."),
    json_file: str | None = typer.Option(
        None, "--json-file", help="Read additional request JSON from a file."
    ),
) -> None:
    """Update only the supplied fields of a record."""
    payload = _json_request(json_body, json_file)
    payload.update(
        {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "type": record_type,
                "record_version": record_version,
                "data": data,
                "network_config": network_config,
                "extensions": extensions,
                "status": status,
                "error_message": error_message,
            }.items()
            if value is not None
        }
    )
    if not payload:
        raise typer.BadParameter("provide at least one field to update")
    _stringify_record_json_fields(payload)
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    _print(
        _call_record(
            options,
            resolved_registry_id,
            lambda client: client.update_record(record_id, payload),
        ),
        options["output"],
    )


@record_app.command("delete")
@_handle_api_errors
def delete_record(
    ctx: typer.Context,
    record_id: str,
    registry_id: str | None = typer.Option(None, "--registry-id"),
) -> None:
    """Delete a record by ID."""
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    _print(
        _call_record(
            options,
            resolved_registry_id,
            lambda client: client.delete_record(record_id),
        ),
        options["output"],
    )


@registry_app.command("create")
@_handle_api_errors
def create_resource(
    ctx: typer.Context,
    name: str | None = typer.Option(None, "--name"),
    replicas: int | None = typer.Option(None, "--replicas", min=1),
    network_spec: str | None = typer.Option(None, "--network-spec"),
    monitor_spec: str | None = typer.Option(None, "--monitor-spec"),
    project_name: str | None = typer.Option(None, "--project-name"),
    tag: list[str] | None = typer.Option(None, "--tag"),
    metadata: str | None = typer.Option(None, "--metadata"),
    deletion_protection_enabled: bool | None = typer.Option(
        None, "--deletion-protection-enabled/--no-deletion-protection"
    ),
    gateway_id: str | None = typer.Option(
        None,
        "--gateway-id",
        help="Gateway ID hosting the UniRegistry. Defaults to uni_registry.defaults.gateway_id.",
    ),
    top: str | None = typer.Option(None, "--top"),
    json_body: str | None = typer.Option(None, "--json", help="Full request JSON."),
    json_file: str | None = typer.Option(
        None, "--json-file", help="Read full request JSON from a file."
    ),
    wait: bool = typer.Option(
        False,
        "--wait/--no-wait",
        help="Poll GetUniRegistry after creation until the instance is ready.",
    ),
    wait_interval: float = typer.Option(
        10.0,
        "--wait-interval",
        min=0.1,
        help="Polling interval in seconds when --wait is enabled.",
    ),
    wait_timeout: float = typer.Option(
        900.0,
        "--wait-timeout",
        min=1.0,
        help="Maximum polling time in seconds when only --wait is enabled.",
    ),
    with_id: bool = typer.Option(
        False,
        "--with-id/--no-with-id",
        help="Add X-Mse-Uni-Test-Suffix to CreateUniRegistry.",
    ),
) -> None:
    """Create a managed UniRegistry resource."""
    payload = _json_request(json_body, json_file)
    if name is not None:
        payload["name"] = name
    if replicas is not None:
        payload["replicas"] = replicas
    mappings = {
        "network_spec": _json_object(network_spec, "--network-spec"),
        "monitor_spec": _json_object(monitor_spec, "--monitor-spec"),
        "metadata": _json_object(metadata, "--metadata"),
        "top": _json_object(top, "--top"),
    }
    payload.update({key: value for key, value in mappings.items() if value})
    if project_name is not None:
        payload["project_name"] = project_name
    if tag:
        payload["tags"] = _tags(tag)
    if deletion_protection_enabled is not None:
        payload["deletion_protection_enabled"] = deletion_protection_enabled
    if gateway_id is not None:
        resolved_gateway_id = gateway_id.strip()
        if not resolved_gateway_id:
            raise typer.BadParameter("--gateway-id is required")
        payload["gateway_id"] = resolved_gateway_id
    elif _has_any_key(payload, "gateway_id", "GatewayId"):
        if not _has_non_empty_any_key(payload, "gateway_id", "GatewayId"):
            raise typer.BadParameter(
                "provide --gateway-id or GatewayId/gateway_id in --json"
            )
    else:
        default_gateway_id = _default_gateway_id()
        if default_gateway_id:
            payload["gateway_id"] = default_gateway_id
    if not _has_any_key(payload, "name", "Name"):
        raise typer.BadParameter("provide --name or Name/name in --json")
    if not _has_any_key(payload, "replicas", "Replicas"):
        raise typer.BadParameter("provide --replicas or Replicas/replicas in --json")
    if not _has_non_empty_any_key(payload, "gateway_id", "GatewayId"):
        raise typer.BadParameter(
            "provide --gateway-id, GatewayId/gateway_id in --json, or set uni_registry.defaults.gateway_id"
        )
    options = _options(ctx)
    client = _client(options)
    parsed_top = _json_object(top, "--top")
    response = client.create_resource(payload, with_id=with_id)
    _cache_resource_response(response, options)
    resource_id = _registry_id_from_response(response)
    if resource_id:
        request_id = _request_id_from_response(response)
        request_id_text = f" request_id={request_id}" if request_id else ""
        error_console.print(f"Created UniRegistry: id={resource_id}{request_id_text}")
    if wait:
        if not resource_id:
            raise UniRegistryAPIError(
                "CreateUniRegistry response does not include a registry ID to poll"
            )
        wait_result = _wait_for_registry_ready(
            client,
            resource_id,
            interval=wait_interval,
            timeout=wait_timeout,
            top=parsed_top,
        )
        final_response = wait_result.get("GetUniRegistryResponse")
        _cache_resource_response(final_response, options, resource_id)
        if wait_result.get("Ready"):
            default_registry_id = _registry_id_from_response(final_response) or resource_id
            _set_default_registry_id(default_registry_id)
            error_console.print(f"Set default UniRegistry id: {default_registry_id}")
    else:
        wait_result = None
    _print(
        _registry_create_summary(response, wait_result),
        options["output"],
    )


@registry_app.command("syncer")
@_handle_api_errors
def syncer_resource(
    ctx: typer.Context,
    registry_id: str | None = typer.Option(
        None,
        "--registry-id",
        help="Existing UniRegistry ID. Defaults to uni_registry.defaults.registry_id.",
    ),
    sync_type: str | None = typer.Option(
        None,
        "--type",
        help="Migration type: a2a or skill. Defaults to both.",
    ),
    gateway_id: str | None = typer.Option(
        None,
        "--gateway-id",
        help="Gateway ID hosting the UniRegistry. Defaults to uni_registry.defaults.gateway_id.",
    ),
    top: str | None = typer.Option(None, "--top"),
    wait_interval: float = typer.Option(
        10.0,
        "--wait-interval",
        min=0.1,
        help="Polling interval in seconds.",
    ),
    timeout: float = typer.Option(
        _A2A_SYNCER_TOTAL_TIMEOUT_SECONDS,
        "--timeout",
        min=1.0,
        help="Maximum time in seconds for registry readiness and migration polling.",
    ),
    workspace_id: str | None = typer.Option(
        None,
        "--workspace-id",
        help="Workspace ID required by Skill Uni migration APIs.",
    ),
    acl_entry: list[str] | None = typer.Option(
        None,
        "--acl-entry",
        help="CIDR whitelist entry to set when syncer creates a UniRegistry. Repeatable.",
    ),
    acl_entries: str | None = typer.Option(
        None,
        "--acl-entries",
        help="Comma-separated CIDR whitelist entries to set when syncer creates a UniRegistry.",
    ),
    with_id: bool = typer.Option(
        False,
        "--with-id/--no-with-id",
        help="Add X-Mse-Uni-Test-Suffix to CreateUniRegistry when creation is needed.",
    ),
) -> None:
    """Create or reuse a UniRegistry, then run migration syncers."""
    normalized_type = (sync_type or "").strip().lower()
    if normalized_type and normalized_type not in {"a2a", "skill"}:
        raise typer.BadParameter("--type must be a2a or skill")
    gateway_id = _resolve_gateway_id(gateway_id)
    run_a2a_syncer = normalized_type in {"", "a2a"}
    run_skill_syncer = normalized_type in {"", "skill"}
    if run_skill_syncer and not workspace_id:
        raise typer.BadParameter("--workspace-id is required for Skill migration")
    resolved_acl_entries = _acl_entries(acl_entry, acl_entries)

    options = _options(ctx)
    client = _client(options)
    parsed_top = _json_object(top, "--top")
    create_response: Any | None = None
    acl_update_response: Any | None = None
    created = False
    resource_id = (registry_id or _default_registry_id() or "").strip()
    if resource_id:
        error_console.print(f"Using UniRegistry: id={resource_id}")
    else:
        payload = _default_syncer_create_payload(gateway_id)
        create_response = client.create_resource(payload, with_id=with_id)
        _cache_resource_response(create_response, options)
        resource_id = _registry_id_from_response(create_response) or ""
        if not resource_id:
            raise UniRegistryAPIError(
                "CreateUniRegistry response does not include a registry ID to poll"
            )
        created = True
        request_id = _request_id_from_response(create_response)
        request_id_text = f" request_id={request_id}" if request_id else ""
        error_console.print(f"Created UniRegistry: id={resource_id}{request_id_text}")

    deadline = time.monotonic() + timeout
    wait_result = _wait_for_registry_ready(
        client,
        resource_id,
        interval=wait_interval,
        timeout=timeout,
        top=parsed_top,
    )
    final_response = wait_result.get("GetUniRegistryResponse")
    _cache_resource_response(final_response, options, resource_id)
    if not wait_result.get("Ready"):
        raise UniRegistryAPIError("UniRegistry did not become ready; skip Uni migration")
    migration_registry_id = _registry_id_from_response(final_response) or resource_id
    if created:
        _set_default_registry_id(migration_registry_id)
        error_console.print(f"Set default UniRegistry id: {migration_registry_id}")
        if resolved_acl_entries:
            error_console.print(
                f"Updating UniRegistry ACL entries: id={migration_registry_id} entries={len(resolved_acl_entries)}"
            )
            acl_update_response = client.update_resource(
                {
                    "Id": migration_registry_id,
                    "NetworkSpec": {
                        "NetworkType": ["PUBLIC"],
                        "AclEntries": resolved_acl_entries,
                    },
                }
            )
            _cache_resource_response(
                acl_update_response, options, migration_registry_id
            )
    migration_result = None
    skill_migration_result = None
    if run_a2a_syncer:
        migration_result = _start_and_wait_uni_migration(
            client,
            label="A2A Uni migration",
            registry_id=migration_registry_id,
            top=parsed_top,
            start_migration=client.start_a2a_uni_migration,
            wait_migration=_wait_for_a2a_uni_migration,
            interval=wait_interval,
            deadline=deadline,
        )
    if run_skill_syncer:
        skill_migration_result = _start_and_wait_uni_migration(
            client,
            label="Skill Uni migration",
            registry_id=migration_registry_id,
            top=parsed_top,
            start_migration=client.start_skill_uni_migration,
            wait_migration=_wait_for_skill_uni_migration,
            start_kwargs={"workspace_id": workspace_id},
            wait_kwargs={"workspace_id": workspace_id},
            interval=wait_interval,
            deadline=deadline,
        )
    _print(
        _registry_syncer_summary(
            registry_id=resource_id,
            created=created,
            create_response=create_response,
            wait_result=wait_result,
            acl_update_response=acl_update_response,
            registry_response=final_response if created else None,
            migration_result=migration_result,
            skill_migration_result=skill_migration_result,
        ),
        options["output"],
    )


@registry_app.command("start-a2a-migration")
@_handle_api_errors
def start_a2a_migration(
    ctx: typer.Context,
    resource_id: str | None = typer.Argument(
        None,
        help="UniRegistry ID. Defaults to uni_registry.defaults.registry_id.",
    ),
    top: str | None = typer.Option(None, "--top"),
) -> None:
    """Start A2A Uni migration for a UniRegistry."""
    options = _options(ctx)
    resolved_resource_id = _resolve_registry_id(resource_id, source_label="RESOURCE_ID")
    response = _client(options).start_a2a_uni_migration(
        resolved_resource_id,
        _json_object(top, "--top"),
        with_id=True,
    )
    _print(
        _uni_migration_summary(
            response, fallback_id=resolved_resource_id, registry_id=resolved_resource_id
        ),
        options["output"],
    )


@registry_app.command("get-a2a-migration")
@_handle_api_errors
def get_a2a_migration(
    ctx: typer.Context,
    migration_id: str | None = typer.Argument(
        None,
        help=(
            "A2A Uni migration ID. Defaults to the resolved UniRegistry ID when omitted."
        ),
    ),
    registry_id: str | None = typer.Option(
        None,
        "--registry-id",
        help="UniRegistry ID used for X-Mse-Uni-Registry-Id. Defaults to configured registry_id.",
    ),
) -> None:
    """Get A2A Uni migration status."""
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    resolved_migration_id = (migration_id or resolved_registry_id).strip()
    response = _client(options).get_a2a_uni_migration(
        resolved_migration_id,
        registry_id=resolved_registry_id,
        with_id=True,
    )
    _print(
        _uni_migration_summary(
            response,
            fallback_id=resolved_migration_id,
            registry_id=resolved_registry_id,
        ),
        options["output"],
    )


@registry_app.command("retry-a2a-migration")
@_handle_api_errors
def retry_a2a_migration(
    ctx: typer.Context,
    migration_id: str | None = typer.Argument(
        None,
        help="A2A Uni migration ID. Defaults to the resolved UniRegistry ID when omitted.",
    ),
    version: int = typer.Option(
        ...,
        "--version",
        min=0,
        help="Migration version.",
    ),
    registry_id: str | None = typer.Option(
        None,
        "--registry-id",
        help="UniRegistry ID used for X-Mse-Uni-Registry-Id. Defaults to configured registry_id.",
    ),
) -> None:
    """Retry A2A Uni migration."""
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    resolved_migration_id = (migration_id or resolved_registry_id).strip()
    response = _client(options).retry_a2a_uni_migration(
        resolved_migration_id,
        version=version,
        registry_id=resolved_registry_id,
        with_id=True,
    )
    _print(
        _uni_migration_summary(
            response,
            fallback_id=resolved_migration_id,
            registry_id=resolved_registry_id,
        ),
        options["output"],
    )


@registry_app.command("start-skill-migration")
@_handle_api_errors
def start_skill_migration(
    ctx: typer.Context,
    resource_id: str | None = typer.Argument(
        None,
        help="UniRegistry ID. Defaults to uni_registry.defaults.registry_id.",
    ),
    top: str | None = typer.Option(None, "--top"),
    workspace_id: str = typer.Option(
        ...,
        "--workspace-id",
        help="Workspace ID required by Skill Uni migration APIs.",
    ),
) -> None:
    """Start Skill Uni migration for a UniRegistry."""
    options = _options(ctx)
    resolved_resource_id = _resolve_registry_id(resource_id, source_label="RESOURCE_ID")
    response = _client(options).start_skill_uni_migration(
        resolved_resource_id,
        _json_object(top, "--top"),
        workspace_id=workspace_id,
        with_id=True,
    )
    _print(
        _uni_migration_summary(
            response, fallback_id=resolved_resource_id, registry_id=resolved_resource_id
        ),
        options["output"],
    )


@registry_app.command("get-skill-migration")
@_handle_api_errors
def get_skill_migration(
    ctx: typer.Context,
    migration_id: str | None = typer.Argument(
        None,
        help=(
            "Skill Uni migration ID. Defaults to the resolved UniRegistry ID when omitted."
        ),
    ),
    registry_id: str | None = typer.Option(
        None,
        "--registry-id",
        help="UniRegistry ID used for X-Mse-Uni-Registry-Id. Defaults to configured registry_id.",
    ),
    workspace_id: str = typer.Option(
        ...,
        "--workspace-id",
        help="Workspace ID required by Skill Uni migration APIs.",
    ),
) -> None:
    """Get Skill Uni migration status."""
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    resolved_migration_id = (migration_id or resolved_registry_id).strip()
    response = _client(options).get_skill_uni_migration(
        resolved_migration_id,
        workspace_id=workspace_id,
        registry_id=resolved_registry_id,
        with_id=True,
    )
    _print(
        _uni_migration_summary(
            response,
            fallback_id=resolved_migration_id,
            registry_id=resolved_registry_id,
        ),
        options["output"],
    )


@registry_app.command("retry-skill-migration")
@_handle_api_errors
def retry_skill_migration(
    ctx: typer.Context,
    migration_id: str | None = typer.Argument(
        None,
        help="Skill Uni migration ID. Defaults to the resolved UniRegistry ID when omitted.",
    ),
    version: int = typer.Option(
        ...,
        "--version",
        min=0,
        help="Migration version.",
    ),
    registry_id: str | None = typer.Option(
        None,
        "--registry-id",
        help="UniRegistry ID used for X-Mse-Uni-Registry-Id. Defaults to configured registry_id.",
    ),
    workspace_id: str = typer.Option(
        ...,
        "--workspace-id",
        help="Workspace ID required by Skill Uni migration APIs.",
    ),
) -> None:
    """Retry Skill Uni migration."""
    options = _options(ctx)
    resolved_registry_id = _resolve_registry_id(registry_id)
    resolved_migration_id = (migration_id or resolved_registry_id).strip()
    response = _client(options).retry_skill_uni_migration(
        resolved_migration_id,
        workspace_id=workspace_id,
        version=version,
        registry_id=resolved_registry_id,
        with_id=True,
    )
    _print(
        _uni_migration_summary(
            response,
            fallback_id=resolved_migration_id,
            registry_id=resolved_registry_id,
        ),
        options["output"],
    )


@registry_app.command("get")
@_handle_api_errors
def get_resource(
    ctx: typer.Context,
    resource_id: str | None = typer.Argument(
        None,
        help="UniRegistry ID. Defaults to uni_registry.defaults.registry_id.",
    ),
    top: str | None = typer.Option(None, "--top"),
) -> None:
    """Get a managed UniRegistry resource."""
    options = _options(ctx)
    resolved_resource_id = _resolve_registry_id(resource_id, source_label="RESOURCE_ID")
    response = _client(options).get_resource(
        resolved_resource_id, _json_object(top, "--top")
    )
    _cache_resource_response(response, options, resolved_resource_id)
    _print(_registry_detail_summary(response, resolved_resource_id), options["output"])


@registry_app.command("list")
@_handle_api_errors
def list_resources(
    ctx: typer.Context,
    page: int = typer.Option(1, "--page"),
    page_size: int = typer.Option(10, "--page-size"),
    resource_id: list[str] | None = typer.Option(None, "--id"),
    name: str | None = typer.Option(None, "--name"),
    status: list[str] | None = typer.Option(None, "--status"),
    vpc_id: str | None = typer.Option(None, "--vpc-id"),
    project_name: str | None = typer.Option(None, "--project-name"),
    tag: list[str] | None = typer.Option(None, "--tag-filter"),
    top: str | None = typer.Option(None, "--top"),
) -> None:
    """List managed UniRegistry resources."""
    resource_filter = {
        key: value
        for key, value in {
            "id": resource_id,
            "name": name,
            "status": status,
            "vpc_id": vpc_id,
            "project_name": project_name,
        }.items()
        if value
    }
    payload: dict[str, Any] = {"page_number": page, "page_size": page_size}
    if resource_filter:
        payload["filter"] = resource_filter
    if project_name:
        payload["project_name"] = project_name
    if tag:
        payload["tag_filters"] = _tags(tag)
    parsed_top = _json_object(top, "--top")
    if parsed_top:
        payload["top"] = parsed_top
    options = _options(ctx)
    response = _client(options).list_resources(payload)
    _print(_registry_list_summary(response), options["output"])


@registry_app.command("update")
@_handle_api_errors
def update_resource(
    ctx: typer.Context,
    registry_id: str | None = typer.Argument(
        None,
        help="UniRegistry ID. Defaults to uni_registry.defaults.registry_id.",
    ),
    replicas: int | None = typer.Option(None, "--replicas", min=1),
    network_spec: str | None = typer.Option(None, "--network-spec"),
    monitor_spec: str | None = typer.Option(None, "--monitor-spec"),
    metadata: str | None = typer.Option(None, "--metadata"),
    top: str | None = typer.Option(None, "--top"),
    json_body: str | None = typer.Option(
        None, "--json", help="Additional request JSON."
    ),
    json_file: str | None = typer.Option(
        None, "--json-file", help="Read additional request JSON from a file."
    ),
) -> None:
    """Update a managed UniRegistry resource."""
    resolved_resource_id = _resolve_registry_id(registry_id, source_label="REGISTRY_ID")
    payload = _json_request(json_body, json_file)
    payload["id"] = resolved_resource_id
    if replicas is not None:
        payload["replicas"] = replicas
    for key, value, option in [
        ("network_spec", network_spec, "--network-spec"),
        ("monitor_spec", monitor_spec, "--monitor-spec"),
        ("metadata", metadata, "--metadata"),
        ("top", top, "--top"),
    ]:
        if value is not None:
            payload[key] = _json_object(value, option)
    if set(payload) == {"id"}:
        raise typer.BadParameter("provide at least one field to update")
    options = _options(ctx)
    response = _client(options).update_resource(payload)
    _cache_resource_response(response, options, resolved_resource_id)
    _print(_registry_update_summary(response, resolved_resource_id), options["output"])


@registry_app.command("delete")
@_handle_api_errors
def delete_resource(
    ctx: typer.Context,
    resource_id: str,
    top: str | None = typer.Option(None, "--top"),
) -> None:
    """Delete a managed UniRegistry resource."""
    options = _options(ctx)
    _print(
        _mask_sensitive(
            _client(options).delete_resource(
                resource_id, _json_object(top, "--top")
            )
        ),
        options["output"],
    )


@binding_app.command("bind")
@_handle_api_errors
def bind_registry(
    ctx: typer.Context,
    registry_id: str,
    server: str | None = typer.Option(
        None, "--server", help="Data-plane server URL."
    ),
    private_address: str | None = typer.Option(
        None, "--private-address", help="Private data-plane URL."
    ),
    public_address: str | None = typer.Option(
        None, "--public-address", help="Public data-plane URL."
    ),
    username: str | None = typer.Option(None, "--username", help="Basic-auth username."),
    password: str | None = typer.Option(None, "--password", help="Basic-auth password."),
    region: str | None = typer.Option(None, "--region", help="Signing region."),
    service: str | None = typer.Option(None, "--service", help="Signing service."),
) -> None:
    """Bind a registry ID to cached data-plane connection settings."""
    if not any([server, private_address, public_address]):
        raise typer.BadParameter(
            "provide at least one of --server, --private-address, or --public-address"
        )
    updates: dict[str, Any] = {}
    if server:
        updates["server"] = _normalize_server(server)
    if private_address:
        updates["private_address"] = _normalize_server(private_address)
    if public_address:
        updates["public_address"] = _normalize_server(public_address)
    for key, value in {
        "username": username,
        "password": password,
        "region": region,
        "service": service,
    }.items():
        if value:
            updates[key] = value
    _upsert_registry_config(registry_id, updates)
    _print(
        {"registry": _masked_registry_config(registry_id, _registry_config(registry_id) or {})},
        _options(ctx)["output"],
    )


@binding_app.command("get")
@_handle_api_errors
def get_registry_binding(ctx: typer.Context, registry_id: str) -> None:
    """Show cached connection settings for a registry ID."""
    value = _registry_config(registry_id)
    if not value:
        raise typer.BadParameter(f"registry {registry_id!r} is not bound")
    _print({"registry": _masked_registry_config(registry_id, value)}, _options(ctx)["output"])


@binding_app.command("list")
@_handle_api_errors
def list_registry_bindings(ctx: typer.Context) -> None:
    """List cached registry connection settings."""
    registries = _registry_configs()
    items = [
        _masked_registry_config(registry_id, value)
        for registry_id, value in sorted(registries.items())
    ]
    _print({"total_count": len(items), "items": items}, _options(ctx)["output"])


@binding_app.command("remove")
@_handle_api_errors
def remove_registry_binding(ctx: typer.Context, registry_id: str) -> None:
    """Remove cached connection settings for a registry ID."""
    registries = dict(_registry_configs())
    removed = registries.pop(registry_id, None) is not None
    _save_registry_configs(registries)
    _print({"success": removed}, _options(ctx)["output"])


uni_registry_app.add_typer(record_app, name="record")
registry_app.add_typer(binding_app, name="binding")
uni_registry_app.add_typer(registry_app, name="registry")
