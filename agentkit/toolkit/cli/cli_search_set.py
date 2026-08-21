"""CLI commands for control-plane SearchSet and MCP service orchestration."""

# ruff: noqa: B008

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import typer

from .cli_uni_registry import (
    UniRegistryAPIError,
    UniRegistryClient,
    _client,
    _json_object,
    _options,
    _print,
)

search_set_app = typer.Typer(help="Manage SearchSets and their MCP integration.")


class MCPControlClient:
    """Raw AgentKit MCP OpenAPI client for asynchronous service provisioning."""

    def __init__(self, options: dict[str, Any]) -> None:
        control_client = _client(options)
        self._client = UniRegistryClient(
            control_client.server,
            region=options["region"],
            service="agentkit",
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


def _search_set_payload(
    name: str,
    resource_ids: list[str],
    description: str | None,
    search_config: str | None,
) -> dict[str, Any]:
    normalized_ids = [resource_id.strip() for resource_id in resource_ids]
    if not normalized_ids or any(not resource_id for resource_id in normalized_ids):
        raise typer.BadParameter("provide at least one non-empty --resource-id")
    payload: dict[str, Any] = {"name": name, "resource_ids": normalized_ids}
    if description is not None:
        payload["description"] = description
    parsed_config = _json_object(search_config, "--search-config")
    if parsed_config:
        payload["search_config"] = parsed_config
    return payload


def _search_set_client(options: dict[str, Any]) -> UniRegistryClient:
    return _client(options)


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


@search_set_app.command("create")
def create_search_set(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name"),
    resource_id: list[str] = typer.Option(..., "--resource-id"),
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
    wait_timeout: float = typer.Option(
        180, "--wait-timeout", min=1, help="MCPToolset readiness timeout in seconds."
    ),
    wait_interval: float = typer.Option(
        3, "--wait-interval", min=0.1, help="MCPToolset polling interval in seconds."
    ),
) -> None:
    """Create a SearchSet, then provision its MCP Service and query its Toolset."""
    options = _options(ctx)
    payload = _search_set_payload(name, resource_id, description, search_config)
    search_set = _search_set_client(options).request(
        "POST", "/api/v1/search-sets", payload
    )

    result: dict[str, Any] = {"search_set": search_set}
    if mcp_service is not None:
        result["mcp_service"] = MCPControlClient(options).create_mcp_service(
            _json_object(mcp_service, "--mcp-service-json")
        )

    toolset_id = mcp_toolset_id or _toolset_id_from_search_set(search_set)
    if toolset_id:
        result["mcp_toolset"] = MCPControlClient(options).wait_for_toolset(
            toolset_id, wait_interval, wait_timeout
        )
    _print(result, options["output"])


@search_set_app.command("get")
def get_search_set(ctx: typer.Context, name: str) -> None:
    """Get a SearchSet by name."""
    options = _options(ctx)
    _print(
        _search_set_client(options).request(
            "GET", f"/api/v1/search-sets/{quote(name, safe='')}"
        ),
        options["output"],
    )


@search_set_app.command("list")
def list_search_sets(
    ctx: typer.Context,
    page: int = typer.Option(1, "--page"),
    page_size: int = typer.Option(10, "--page-size"),
) -> None:
    """List SearchSets."""
    options = _options(ctx)
    _print(
        _search_set_client(options).request(
            "GET",
            "/api/v1/search-sets",
            params={"page_number": page, "page_size": page_size},
        ),
        options["output"],
    )


@search_set_app.command("update")
def update_search_set(
    ctx: typer.Context,
    name: str,
    resource_id: list[str] = typer.Option(..., "--resource-id"),
    description: str | None = typer.Option(None, "--description"),
    search_config: str | None = typer.Option(
        None, "--search-config", help="Search config JSON."
    ),
) -> None:
    """Replace the records and configuration of a SearchSet."""
    options = _options(ctx)
    payload = _search_set_payload(name, resource_id, description, search_config)
    payload.pop("name")
    _print(
        _search_set_client(options).request(
            "PUT", f"/api/v1/search-sets/{quote(name, safe='')}", payload
        ),
        options["output"],
    )


@search_set_app.command("delete")
def delete_search_set(ctx: typer.Context, name: str) -> None:
    """Delete a SearchSet."""
    options = _options(ctx)
    _print(
        _search_set_client(options).request(
            "DELETE", f"/api/v1/search-sets/{quote(name, safe='')}"
        ),
        options["output"],
    )


@search_set_app.command("search")
def search_in_set(ctx: typer.Context, name: str, query: str) -> None:
    """Search only within a SearchSet."""
    options = _options(ctx)
    _print(
        _search_set_client(options).request(
            "POST",
            f"/api/v1/search-sets/{quote(name, safe='')}/search",
            {"query": query},
        ),
        options["output"],
    )
