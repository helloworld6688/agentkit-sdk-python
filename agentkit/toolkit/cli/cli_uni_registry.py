# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""CLI commands for uni-reg records and managed registry resources."""

# ruff: noqa: B008

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import requests
import typer
from rich.console import Console
from rich.table import Table

console = Console()

uni_registry_app = typer.Typer(
    name="uni-reg",
    help="Manage UniRegistry records and managed registry resources.",
    add_completion=False,
)
record_app = typer.Typer(help="Manage records stored in UniRegistry.")
resource_app = typer.Typer(help="Manage UniRegistry resources.")


class UniRegistryAPIError(RuntimeError):
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
        self, method: str, path: str, params: dict[str, Any], body: str
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
        signed_names = ["content-type", "host", "x-content-sha256", "x-date"]
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
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = None
        if self.username or self.password:
            auth = (self.username, self.password)
        elif not path.startswith("/api/v1/"):
            self._load_credentials()
            headers = self._signed_headers(method, request_path, query, body_text)
        try:
            response = requests.request(
                method,
                f"{self.server}{request_path}",
                params=query,
                data=body_text or None,
                headers=headers,
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
                f"HTTP {response.status_code}: {response.text[:1000]}"
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

    def create_resource(self, payload: dict[str, Any]) -> Any:
        return self.request("POST", "/CreateUniRegistry", payload)

    def get_resource(self, resource_id: str, top: dict[str, Any]) -> Any:
        return self.request(
            "POST", "/GetUniRegistry", {"id": resource_id, "top": top or None}
        )

    def list_resources(self, payload: dict[str, Any]) -> Any:
        return self.request("POST", "/ListUniRegistries", payload)

    def update_resource(self, payload: dict[str, Any]) -> Any:
        return self.request("POST", "/UpdateUniRegistry", payload)

    def delete_resource(self, resource_id: str, top: dict[str, Any]) -> Any:
        return self.request(
            "POST", "/DeleteUniRegistry", {"id": resource_id, "top": top or None}
        )

    def record_clients(self, registry_id: str) -> list[UniRegistryClient]:
        registry_response = self.get_resource(registry_id, {})
        registry = registry_response.get("registry")
        if not isinstance(registry, dict):
            raise UniRegistryAPIError(
                f"GetUniRegistry({registry_id}) did not return a registry"
            )

        endpoints: list[str] = []
        for field in ("private_address", "public_address"):
            address = str(registry.get(field) or "").strip().rstrip("/")
            if not address or address in endpoints:
                continue
            parsed = urlparse(address)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise UniRegistryAPIError(
                    f"UniRegistry {registry_id} has invalid {field}: {address!r}"
                )
            endpoints.append(address)

        if not endpoints:
            raise UniRegistryAPIError(
                f"UniRegistry {registry_id} has no private_address or public_address"
            )

        return [
            UniRegistryClient(
                endpoint,
                self.username or None,
                self.password or None,
                self.region or None,
                self.service,
                self.timeout,
            )
            for endpoint in endpoints
        ]

    def record_client(self, registry_id: str) -> UniRegistryClient:
        return self.record_clients(registry_id)[0]


def _client(options: dict[str, Any]) -> UniRegistryClient:
    server = options["server"] or os.getenv("UNI_REGISTRY_SERVER_URL")
    if not server:
        from agentkit.platform import VolcConfiguration

        endpoint = VolcConfiguration(region=options["region"]).get_service_endpoint(
            "agentkit"
        )
        server = f"{endpoint.scheme}://{endpoint.host}"
    return UniRegistryClient(
        server,
        options["username"] or os.getenv("UNI_USERNAME"),
        options["password"] or os.getenv("UNI_PASSWORD"),
        options["region"],
        options["service"],
    )


def _record_client(options: dict[str, Any], registry_id: str) -> UniRegistryClient:
    return _client(options).record_client(registry_id)


def _call_record(options: dict[str, Any], registry_id: str, operation: Any) -> Any:
    last_error: UniRegistryNetworkError | None = None
    for client in _client(options).record_clients(registry_id):
        try:
            return operation(client)
        except UniRegistryNetworkError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise UniRegistryAPIError(f"UniRegistry {registry_id} has no record endpoint")


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
    service: str = typer.Option(
        "agentkit", "--service", help="Volcengine signing service."
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
def create_record(
    ctx: typer.Context,
    registry_id: str = typer.Option(..., "--registry-id"),
    name: str = typer.Option(..., "--name"),
    record_type: str = typer.Option(..., "--type"),
    record_version: str = typer.Option(..., "--record-version"),
    data: str | None = typer.Option(None, "--data"),
    data_file: str | None = typer.Option(None, "--data-file"),
    description: str | None = typer.Option(None, "--description"),
    network_config: str | None = typer.Option(None, "--network-config"),
    extensions: str | None = typer.Option(None, "--extensions"),
) -> None:
    """Create a record."""
    payload: dict[str, Any] = {
        "name": name,
        "type": record_type,
        "record_version": record_version,
        "data": _data_value(data, data_file),
    }
    for key, value in {
        "description": description,
        "network_config": network_config,
        "extensions": extensions,
    }.items():
        if value is not None:
            payload[key] = value
    options = _options(ctx)
    _print(
        _call_record(
            options, registry_id, lambda client: client.create_record(payload)
        ),
        options["output"],
    )


@record_app.command("get")
def get_record(
    ctx: typer.Context,
    record_id: str,
    registry_id: str = typer.Option(..., "--registry-id"),
) -> None:
    """Get a record by ID."""
    options = _options(ctx)
    _print(
        _call_record(options, registry_id, lambda client: client.get_record(record_id)),
        options["output"],
    )


@record_app.command("list")
def list_records(
    ctx: typer.Context,
    registry_id: str = typer.Option(..., "--registry-id"),
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
    _print(
        _call_record(options, registry_id, lambda client: client.list_records(params)),
        options["output"],
    )


@record_app.command("update")
def update_record(
    ctx: typer.Context,
    record_id: str,
    registry_id: str = typer.Option(..., "--registry-id"),
    name: str | None = typer.Option(None, "--name"),
    description: str | None = typer.Option(None, "--description"),
    record_type: str | None = typer.Option(None, "--type"),
    record_version: str | None = typer.Option(None, "--record-version"),
    data: str | None = typer.Option(None, "--data"),
    network_config: str | None = typer.Option(None, "--network-config"),
    extensions: str | None = typer.Option(None, "--extensions"),
    status: str | None = typer.Option(None, "--status"),
    error_message: str | None = typer.Option(None, "--error-message"),
) -> None:
    """Update only the supplied fields of a record."""
    payload = {
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
    if not payload:
        raise typer.BadParameter("provide at least one field to update")
    options = _options(ctx)
    _print(
        _call_record(
            options,
            registry_id,
            lambda client: client.update_record(record_id, payload),
        ),
        options["output"],
    )


@record_app.command("delete")
def delete_record(
    ctx: typer.Context,
    record_id: str,
    registry_id: str = typer.Option(..., "--registry-id"),
) -> None:
    """Delete a record by ID."""
    options = _options(ctx)
    _print(
        _call_record(
            options, registry_id, lambda client: client.delete_record(record_id)
        ),
        options["output"],
    )


@resource_app.command("create")
def create_resource(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name"),
    replicas: int = typer.Option(..., "--replicas", min=1),
    network_spec: str | None = typer.Option(None, "--network-spec"),
    monitor_spec: str | None = typer.Option(None, "--monitor-spec"),
    project_name: str | None = typer.Option(None, "--project-name"),
    tag: list[str] | None = typer.Option(None, "--tag"),
    metadata: str | None = typer.Option(None, "--metadata"),
    deletion_protection_enabled: bool | None = typer.Option(
        None, "--deletion-protection-enabled/--no-deletion-protection"
    ),
    top: str | None = typer.Option(None, "--top"),
    json_body: str | None = typer.Option(None, "--json", help="Full request JSON."),
) -> None:
    """Create a managed UniRegistry resource."""
    payload = _json_object(json_body, "--json")
    payload.update({"name": name, "replicas": replicas})
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
    options = _options(ctx)
    _print(_client(options).create_resource(payload), options["output"])


@resource_app.command("get")
def get_resource(
    ctx: typer.Context,
    resource_id: str,
    top: str | None = typer.Option(None, "--top"),
) -> None:
    """Get a managed UniRegistry resource."""
    options = _options(ctx)
    _print(
        _client(options).get_resource(resource_id, _json_object(top, "--top")),
        options["output"],
    )


@resource_app.command("list")
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
    _print(_client(options).list_resources(payload), options["output"])


@resource_app.command("update")
def update_resource(
    ctx: typer.Context,
    resource_id: str,
    replicas: int | None = typer.Option(None, "--replicas", min=1),
    network_spec: str | None = typer.Option(None, "--network-spec"),
    monitor_spec: str | None = typer.Option(None, "--monitor-spec"),
    metadata: str | None = typer.Option(None, "--metadata"),
    top: str | None = typer.Option(None, "--top"),
    json_body: str | None = typer.Option(
        None, "--json", help="Additional request JSON."
    ),
) -> None:
    """Update a managed UniRegistry resource."""
    payload = _json_object(json_body, "--json")
    payload["id"] = resource_id
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
    _print(_client(options).update_resource(payload), options["output"])


@resource_app.command("delete")
def delete_resource(
    ctx: typer.Context,
    resource_id: str,
    top: str | None = typer.Option(None, "--top"),
) -> None:
    """Delete a managed UniRegistry resource."""
    options = _options(ctx)
    _print(
        _client(options).delete_resource(
            resource_id, _json_object(top, "--top")
        ),
        options["output"],
    )


uni_registry_app.add_typer(record_app, name="record")
uni_registry_app.add_typer(resource_app, name="resource")
