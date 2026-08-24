"""CLI commands for control-plane SearchSet and MCP service orchestration."""

# ruff: noqa: B008

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import typer

from .cli_uni_registry import (
    UniRegistryAPIError,
    UniRegistryClient,
    _call_registry_data,
    _client,
    _json_object,
    _json_request,
    _mask_sensitive,
    _options,
    _print,
    _registry_config,
    _default_registry_id,
    _resolve_connection_options,
    _upsert_registry_config,
)

search_set_app = typer.Typer(help="Manage SearchSets and their MCP integration.")


class MCPControlClient:
    """Raw AgentKit MCP OpenAPI client for asynchronous service provisioning."""

    def __init__(self, options: dict[str, Any]) -> None:
        control_client = _client(options)
        resolved = _resolve_connection_options(options)
        self._client = UniRegistryClient(
            control_client.server,
            region=resolved["region"],
            service=resolved["service"],
        )

    def create_mcp_service(self, payload: dict[str, Any]) -> Any:
        return self._client.request(
            "POST",
            "/",
            payload,
            {"Action": "CreateMCPService", "Version": "2025-10-30"},
        )

    def get_mcp_toolset(self, toolset_id: str) -> Any:
        return self._client.request(
            "POST",
            "/",
            {"MCPToolsetId": toolset_id},
            {"Action": "GetMCPToolset", "Version": "2025-10-30"},
        )

    def get_mcp_service(self, service_id: str) -> Any:
        return self._client.request(
            "POST",
            "/",
            {"MCPServiceId": service_id},
            {"Action": "GetMCPService", "Version": "2025-10-30"},
        )

    def wait_for_service(
        self, service_id: str, interval_seconds: float, timeout_seconds: float
    ) -> Any:
        deadline = time.monotonic() + timeout_seconds
        while True:
            response = self.get_mcp_service(service_id)
            service = _mcp_service(response)
            status = str(service.get("Status") or service.get("status") or "")
            if status.lower() == "ready":
                return response
            if status.lower() in {"failed", "error"}:
                raise UniRegistryAPIError(f"MCPService {service_id} failed: {service}")
            if time.monotonic() >= deadline:
                raise UniRegistryAPIError(
                    f"timed out waiting for MCPService {service_id}; status={status!r}"
                )
            time.sleep(interval_seconds)

    def wait_for_toolset(
        self, toolset_id: str, interval_seconds: float, timeout_seconds: float
    ) -> Any:
        deadline = time.monotonic() + timeout_seconds
        while True:
            response = self.get_mcp_toolset(toolset_id)
            toolset = _mcp_toolset(response)
            status = str(toolset.get("Status") or toolset.get("status") or "")
            if status.lower() == "ready":
                return response
            if status.lower() in {"failed", "error"}:
                raise UniRegistryAPIError(f"MCPToolset {toolset_id} failed: {toolset}")
            if time.monotonic() >= deadline:
                raise UniRegistryAPIError(
                    f"timed out waiting for MCPToolset {toolset_id}; status={status!r}"
                )
            time.sleep(interval_seconds)


def _mcp_toolset(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise UniRegistryAPIError("GetMCPToolset response is not an object")
    result = response.get("Result", response)
    if not isinstance(result, dict):
        raise UniRegistryAPIError("GetMCPToolset response has no Result object")
    toolset = result.get("MCPToolset", result.get("mcp_toolset"))
    if not isinstance(toolset, dict):
        raise UniRegistryAPIError("GetMCPToolset response has no MCPToolset")
    return toolset


def _mcp_service(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise UniRegistryAPIError("GetMCPService response is not an object")
    result = response.get("Result", response)
    if not isinstance(result, dict):
        raise UniRegistryAPIError("GetMCPService response has no Result object")
    service = result.get("MCPService", result.get("mcp_service"))
    if not isinstance(service, dict):
        raise UniRegistryAPIError("GetMCPService response has no MCPService")
    return service


def _search_set_payload(
    name: str | None,
    resource_ids: list[str] | None,
    description: str | None,
    search_config: str | None,
    json_body: str | None = None,
    json_file: str | None = None,
) -> dict[str, Any]:
    payload = _json_request(json_body, json_file)
    if name is not None:
        payload["name"] = name
    if resource_ids is not None:
        normalized_ids = [resource_id.strip() for resource_id in resource_ids]
        if not normalized_ids or any(not resource_id for resource_id in normalized_ids):
            raise typer.BadParameter("provide at least one non-empty --resource-id")
        payload["resource_ids"] = normalized_ids
    if description is not None:
        payload["description"] = description
    parsed_config = _json_object(search_config, "--search-config")
    if parsed_config:
        payload["search_config"] = parsed_config
    if "name" not in payload:
        raise typer.BadParameter("provide --name or name in --json")
    if "resource_ids" not in payload:
        raise typer.BadParameter("provide --resource-id or resource_ids in --json")
    return payload


def _call_search_set(
    options: dict[str, Any], registry_id: str | None, operation: Any
) -> Any:
    return _call_registry_data(options, registry_id or _default_registry_id(), operation)


def _toolset_id_from_search_set(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    search_set = response.get("search_set")
    if not isinstance(search_set, dict):
        return None
    for key in ("mcp_toolset_id", "MCPToolsetId"):
        value = search_set.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _toolset_id_from_mcp_service(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    result = response.get("Result", response)
    if not isinstance(result, dict):
        return None
    for key in ("MCPToolsetId", "mcp_toolset_id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _service_id_from_mcp_service(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    result = response.get("Result", response)
    if not isinstance(result, dict):
        return None
    for key in ("MCPServiceId", "mcp_service_id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    service = result.get("MCPService", result.get("mcp_service"))
    if isinstance(service, dict):
        for key in ("MCPServiceId", "mcp_service_id"):
            value = service.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _persist_mcp_route(
    registry_id: str | None,
    search_set_name: str,
    *,
    mcp_service: Any | None = None,
    mcp_service_detail: Any | None = None,
    mcp_toolset: Any | None = None,
) -> None:
    if not registry_id:
        return
    current = _registry_config(registry_id) or {}
    search_sets = current.get("search_sets")
    if not isinstance(search_sets, dict):
        search_sets = {}
    current_set = search_sets.get(search_set_name)
    if not isinstance(current_set, dict):
        current_set = {}
    route = current_set.get("mcp_route")
    if not isinstance(route, dict):
        route = {}
    if mcp_service is not None:
        route["create_mcp_service_response"] = mcp_service
    if mcp_service_detail is not None:
        route["get_mcp_service_response"] = mcp_service_detail
    if mcp_toolset is not None:
        route["get_mcp_toolset_response"] = mcp_toolset
    route["updated_at"] = datetime.now(timezone.utc).isoformat()
    current_set["mcp_route"] = route
    search_sets[search_set_name] = current_set
    _upsert_registry_config(registry_id, {"search_sets": search_sets})


def _provision_mcp_route(
    options: dict[str, Any],
    registry_id: str | None,
    search_set_name: str,
    mcp_service: str,
    mcp_toolset_id: str | None,
    wait_interval: float,
    wait_timeout: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    control_client = MCPControlClient(options)
    mcp_service_response = control_client.create_mcp_service(
        _json_object(mcp_service, "--mcp-service-json")
    )
    result["mcp_service"] = mcp_service_response
    _persist_mcp_route(registry_id, search_set_name, mcp_service=mcp_service_response)

    service_id = _service_id_from_mcp_service(mcp_service_response)
    if service_id:
        mcp_service_detail = control_client.wait_for_service(
            service_id, wait_interval, wait_timeout
        )
        result["mcp_service_detail"] = mcp_service_detail
        _persist_mcp_route(
            registry_id, search_set_name, mcp_service_detail=mcp_service_detail
        )

    toolset_id = mcp_toolset_id or _toolset_id_from_mcp_service(mcp_service_response)
    if toolset_id:
        mcp_toolset_response = control_client.wait_for_toolset(
            toolset_id, wait_interval, wait_timeout
        )
        result["mcp_toolset"] = mcp_toolset_response
        _persist_mcp_route(
            registry_id, search_set_name, mcp_toolset=mcp_toolset_response
        )
    return result


@search_set_app.command("create")
def create_search_set(
    ctx: typer.Context,
    name: str | None = typer.Option(None, "--name"),
    resource_id: list[str] | None = typer.Option(None, "--resource-id"),
    registry_id: str | None = typer.Option(
        None, "--registry-id", help="Cached UniRegistry ID for data-plane access."
    ),
    description: str | None = typer.Option(None, "--description"),
    search_config: str | None = typer.Option(
        None, "--search-config", help="Search config JSON."
    ),
    mcp_service: str | None = typer.Option(
        None,
        "--mcp-service-json",
        help="CreateMCPService request JSON, submitted after SearchSet creation.",
    ),
    mcp_toolset_id: str | None = typer.Option(
        None,
        "--mcp-toolset-id",
        help="MCPToolset ID to query after MCP Service creation.",
    ),
    json_body: str | None = typer.Option(None, "--json", help="Full request JSON."),
    json_file: str | None = typer.Option(
        None, "--json-file", help="Read full request JSON from a file."
    ),
    wait_timeout: float = typer.Option(
        180, "--wait-timeout", min=1, help="MCPToolset readiness timeout in seconds."
    ),
    wait_interval: float = typer.Option(
        3, "--wait-interval", min=0.1, help="MCPToolset polling interval in seconds."
    ),
) -> None:
    """Create a SearchSet, then provision its MCP Service and query its Toolset."""
    options = _options(ctx)
    resolved_registry_id = registry_id or _default_registry_id()
    payload = _search_set_payload(
        name, resource_id, description, search_config, json_body, json_file
    )
    search_set = _call_search_set(
        options,
        resolved_registry_id,
        lambda client: client.request("POST", "/api/v1/search-sets", payload),
    )

    result: dict[str, Any] = {"search_set": search_set}
    if mcp_service is not None:
        result.update(
            _provision_mcp_route(
                options,
                resolved_registry_id,
                str(payload.get("name") or name),
                mcp_service,
                mcp_toolset_id,
                wait_interval,
                wait_timeout,
            )
        )
    toolset_id = None if mcp_service is not None else _toolset_id_from_search_set(search_set)
    if toolset_id:
        mcp_toolset_response = MCPControlClient(options).wait_for_toolset(
            toolset_id, wait_interval, wait_timeout
        )
        result["mcp_toolset"] = mcp_toolset_response
        _persist_mcp_route(
            resolved_registry_id,
            str(payload.get("name") or name),
            mcp_toolset=mcp_toolset_response,
        )
    _print(_mask_sensitive(result), options["output"])


@search_set_app.command("provision-mcp")
def provision_mcp(
    ctx: typer.Context,
    name: str,
    registry_id: str | None = typer.Option(
        None, "--registry-id", help="Cached UniRegistry ID for route metadata storage."
    ),
    mcp_service: str = typer.Option(
        ...,
        "--mcp-service-json",
        help="CreateMCPService request JSON for this SearchSet.",
    ),
    mcp_toolset_id: str | None = typer.Option(
        None,
        "--mcp-toolset-id",
        help="MCPToolset ID to query after MCP Service creation.",
    ),
    wait_timeout: float = typer.Option(
        180, "--wait-timeout", min=1, help="MCP readiness timeout in seconds."
    ),
    wait_interval: float = typer.Option(
        3, "--wait-interval", min=0.1, help="MCP polling interval in seconds."
    ),
) -> None:
    """Provision a gateway MCP Service for an existing SearchSet."""
    options = _options(ctx)
    resolved_registry_id = registry_id or _default_registry_id()
    _print(
        _mask_sensitive(
            _provision_mcp_route(
                options,
                resolved_registry_id,
                name,
                mcp_service,
                mcp_toolset_id,
                wait_interval,
                wait_timeout,
            )
        ),
        options["output"],
    )


@search_set_app.command("get")
def get_search_set(
    ctx: typer.Context,
    name: str,
    registry_id: str | None = typer.Option(
        None, "--registry-id", help="Cached UniRegistry ID for data-plane access."
    ),
) -> None:
    """Get a SearchSet by name."""
    options = _options(ctx)
    _print(
        _call_search_set(
            options,
            registry_id,
            lambda client: client.request(
                "GET", f"/api/v1/search-sets/{quote(name, safe='')}"
            ),
        ),
        options["output"],
    )


@search_set_app.command("list")
def list_search_sets(
    ctx: typer.Context,
    registry_id: str | None = typer.Option(
        None, "--registry-id", help="Cached UniRegistry ID for data-plane access."
    ),
    page: int = typer.Option(1, "--page"),
    page_size: int = typer.Option(10, "--page-size"),
) -> None:
    """List SearchSets."""
    options = _options(ctx)
    _print(
        _call_search_set(
            options,
            registry_id,
            lambda client: client.request(
                "GET",
                "/api/v1/search-sets",
                params={"page_number": page, "page_size": page_size},
            ),
        ),
        options["output"],
    )


@search_set_app.command("update")
def update_search_set(
    ctx: typer.Context,
    name: str,
    resource_id: list[str] | None = typer.Option(None, "--resource-id"),
    registry_id: str | None = typer.Option(
        None, "--registry-id", help="Cached UniRegistry ID for data-plane access."
    ),
    description: str | None = typer.Option(None, "--description"),
    search_config: str | None = typer.Option(
        None, "--search-config", help="Search config JSON."
    ),
    json_body: str | None = typer.Option(None, "--json", help="Additional request JSON."),
    json_file: str | None = typer.Option(
        None, "--json-file", help="Read additional request JSON from a file."
    ),
) -> None:
    """Replace the records and configuration of a SearchSet."""
    options = _options(ctx)
    payload = _search_set_payload(
        name, resource_id, description, search_config, json_body, json_file
    )
    payload.pop("name")
    _print(
        _call_search_set(
            options,
            registry_id,
            lambda client: client.request(
                "PUT", f"/api/v1/search-sets/{quote(name, safe='')}", payload
            ),
        ),
        options["output"],
    )


@search_set_app.command("delete")
def delete_search_set(
    ctx: typer.Context,
    name: str,
    registry_id: str | None = typer.Option(
        None, "--registry-id", help="Cached UniRegistry ID for data-plane access."
    ),
) -> None:
    """Delete a SearchSet."""
    options = _options(ctx)
    _print(
        _call_search_set(
            options,
            registry_id,
            lambda client: client.request(
                "DELETE", f"/api/v1/search-sets/{quote(name, safe='')}"
            ),
        ),
        options["output"],
    )


@search_set_app.command("search")
def search_in_set(
    ctx: typer.Context,
    name: str,
    query: str,
    registry_id: str | None = typer.Option(
        None, "--registry-id", help="Cached UniRegistry ID for data-plane access."
    ),
) -> None:
    """Search only within a SearchSet."""
    options = _options(ctx)
    _print(
        _call_search_set(
            options,
            registry_id,
            lambda client: client.request(
                "POST",
                f"/api/v1/search-sets/{quote(name, safe='')}/search",
                {"query": query},
            ),
        ),
        options["output"],
    )
