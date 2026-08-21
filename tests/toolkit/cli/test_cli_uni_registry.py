# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.

from __future__ import annotations

import json

from typer.testing import CliRunner

runner = CliRunner()


class _Response:
    ok = True
    status_code = 200

    def __init__(self, payload: dict[str, object] | None = None):
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode()
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def test_record_create_resolves_private_address_from_registry_id(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == "https://control.test/GetUniRegistry":
            return _Response(
                {
                    "registry": {
                        "private_address": "https://private.registry.test",
                        "public_address": "https://public.registry.test",
                    }
                }
            )
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://control.test",
            "--username",
            "admin",
            "--password",
            "secret",
            "record",
            "create",
            "--registry-id",
            "ur-1",
            "--name",
            "agent",
            "--type",
            "a2a",
            "--record-version",
            "v1",
            "--data",
            '{"endpoint":"http://agent.test"}',
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://control.test/GetUniRegistry")
    assert json.loads(calls[0][2]["data"]) == {"id": "ur-1", "top": None}
    assert calls[1][0:2] == ("POST", "https://private.registry.test/api/v1/records")
    assert json.loads(calls[1][2]["data"]) == {
        "name": "agent",
        "type": "a2a",
        "record_version": "v1",
        "data": '{"endpoint":"http://agent.test"}',
    }


def test_record_list_falls_back_to_public_address_when_private_unreachable(
    monkeypatch,
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == "https://control.test/GetUniRegistry":
            return _Response(
                {
                    "registry": {
                        "private_address": "https://private.registry.test",
                        "public_address": "https://public.registry.test",
                    }
                }
            )
        if url == "https://private.registry.test/api/v1/records":
            raise registry.requests.exceptions.ConnectionError(
                "private network unreachable"
            )
        return _Response({"items": []})

    monkeypatch.setattr(registry.requests, "request", request)

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://control.test",
            "--username",
            "admin",
            "--password",
            "secret",
            "record",
            "list",
            "--registry-id",
            "ur-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[1][0:2] == ("GET", "https://private.registry.test/api/v1/records")
    assert calls[2][0:2] == ("GET", "https://public.registry.test/api/v1/records")


def test_resource_list_uses_rpc_path_and_signs_request(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(
        registry.UniRegistryClient,
        "_load_credentials",
        lambda self: (
            setattr(self, "access_key", "ak"),
            setattr(self, "secret_key", "sk"),
            setattr(self, "region", "cn-beijing"),
        ),
    )

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://registry.test",
            "resource",
            "list",
            "--page",
            "2",
            "--page-size",
            "20",
            "--id",
            "ur-1",
            "--status",
            "RUNNING",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://registry.test/ListUniRegistries")
    assert calls[0][2]["headers"]["Authorization"].startswith("HMAC-SHA256 ")


def test_resource_list_uses_openapi_action_query_for_volcengine_endpoint(
    monkeypatch,
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(
        registry.UniRegistryClient,
        "_load_credentials",
        lambda self: (
            setattr(self, "access_key", "ak"),
            setattr(self, "secret_key", "sk"),
            setattr(self, "region", "cn-beijing"),
        ),
    )

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://open.volcengineapi.com",
            "resource",
            "list",
            "--page",
            "1",
            "--page-size",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://open.volcengineapi.com/")
    assert calls[0][2]["params"] == {
        "Action": "ListUniRegistries",
        "Version": "2025-10-30",
    }
    assert calls[0][2]["headers"]["Authorization"].startswith("HMAC-SHA256 ")


def test_resource_list_uses_openapi_action_query_for_byted_endpoint(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(
        registry.UniRegistryClient,
        "_load_credentials",
        lambda self: (
            setattr(self, "access_key", "ak"),
            setattr(self, "secret_key", "sk"),
            setattr(self, "region", "cn-beijing"),
        ),
    )

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://volcengineapi.byted.org",
            "--service",
            "agentkit_stg",
            "resource",
            "list",
            "--page",
            "1",
            "--page-size",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://volcengineapi.byted.org/")
    assert calls[0][2]["params"] == {
        "Action": "ListUniRegistries",
        "Version": "2025-10-30",
    }
    assert calls[0][2]["headers"]["Authorization"].startswith(
        "HMAC-SHA256 Credential=ak/"
    )


def test_search_set_create_then_provisions_mcp_and_reads_toolset(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if kwargs.get("params", {}).get("Action") == "GetMCPToolset":
            return _Response({"Result": {"MCPToolset": {"Status": "Ready"}}})
        return _Response({"search_set": {"name": "finance"}})

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(
        registry.UniRegistryClient,
        "_load_credentials",
        lambda self: (
            setattr(self, "access_key", "ak"),
            setattr(self, "secret_key", "sk"),
            setattr(self, "region", "cn-beijing"),
        ),
    )

    service = {
        "Name": "mcp-finance",
        "Path": "/mcp",
        "ProtocolType": "MCP",
        "NetworkConfigurations": [{"NetworkType": "Public"}],
        "BackendType": "Function",
        "BackendConfiguration": {"FunctionConfiguration": {"FunctionId": "fn-1"}},
    }
    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://control.test",
            "search-set",
            "create",
            "--name",
            "finance",
            "--resource-id",
            "record-1",
            "--mcp-service-json",
            json.dumps(service),
            "--mcp-toolset-id",
            "mt-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://control.test/api/v1/search-sets")
    assert calls[1][2]["params"] == {
        "Action": "CreateMCPService",
        "Version": "2025-10-30",
    }
    assert json.loads(calls[1][2]["data"]) == service
    assert calls[2][2]["params"] == {
        "Action": "GetMCPToolset",
        "Version": "2025-10-30",
    }
    assert json.loads(calls[2][2]["data"]) == {"MCPToolsetId": "mt-1"}


def test_search_set_search_uses_scoped_search_endpoint(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "http://registry.test",
            "search-set",
            "search",
            "finance",
            "export financial report",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == (
        "POST",
        "http://registry.test/api/v1/search-sets/finance/search",
    )
    assert json.loads(calls[0][2]["data"]) == {"query": "export financial report"}
