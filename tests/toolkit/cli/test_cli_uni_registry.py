# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_uni_registry_config(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry

    store: dict[str, object] = {}

    def read_config(*, force_reload: bool = False):
        return json.loads(json.dumps(store))

    def write_config(data):
        store.clear()
        store.update(json.loads(json.dumps(data)))

    monkeypatch.setattr(registry, "_REGISTRY_CONFIG_CACHE", None)
    monkeypatch.setattr(registry, "read_global_config_dict", read_config)
    monkeypatch.setattr(registry, "write_global_config_dict", write_config)
    yield store


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
    assert json.loads(calls[0][2]["data"]) == {"Id": "ur-1"}
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


def test_record_list_uses_default_registry_id_from_config(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {"registry_id": "ur-default"},
                "registries": {
                    "ur-default": {
                        "server": "https://data.registry.test",
                        "username": "admin",
                        "password": "secret",
                    }
                },
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response({"items": []})

    monkeypatch.setattr(registry.requests, "request", request)

    result = runner.invoke(app, ["uni-reg", "record", "list"])

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("GET", "https://data.registry.test/api/v1/records")


def test_record_create_accepts_full_json_body(monkeypatch, isolated_uni_registry_config):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {"registry_id": "ur-default"},
                "registries": {"ur-default": {"server": "https://data.registry.test"}},
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response({"id": "record-1"})

    monkeypatch.setattr(registry.requests, "request", request)

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "record",
            "create",
            "--json",
            json.dumps(
                {
                    "name": "agent",
                    "type": "mcp",
                    "record_version": "v1",
                    "data": {"endpoint": "https://agent.test/mcp"},
                    "network_config": {"url": "https://agent.test/mcp"},
                }
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://data.registry.test/api/v1/records")
    payload = json.loads(calls[0][2]["data"])
    assert payload == {
        "name": "agent",
        "type": "mcp",
        "record_version": "v1",
        "data": '{"endpoint": "https://agent.test/mcp"}',
        "network_config": '{"url": "https://agent.test/mcp"}',
    }


def test_record_create_accepts_json_file(
    monkeypatch, isolated_uni_registry_config, tmp_path
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {"registry_id": "ur-default"},
                "registries": {"ur-default": {"server": "https://data.registry.test"}},
            }
        }
    )
    body_path = tmp_path / "record.json"
    body_path.write_text(
        json.dumps(
            {
                "name": "agent",
                "type": "mcp",
                "record_version": "v1",
                "data": {"endpoint": "https://agent.test/mcp"},
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response({"id": "record-1"})

    monkeypatch.setattr(registry.requests, "request", request)

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "record",
            "create",
            "--json-file",
            str(body_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(calls[0][2]["data"]) == {
        "name": "agent",
        "type": "mcp",
        "record_version": "v1",
        "data": '{"endpoint": "https://agent.test/mcp"}',
    }


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
            "registry",
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
    assert json.loads(calls[0][2]["data"]) == {
        "PageNumber": 2,
        "PageSize": 20,
        "Filter": {"Id": ["ur-1"], "Status": ["RUNNING"]},
    }
    assert calls[0][2]["headers"]["Authorization"].startswith("HMAC-SHA256 ")


def test_resource_create_accepts_full_json_body(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response({"Result": {"Id": "ur-1"}})

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
            "registry",
            "create",
            "--json",
            json.dumps(
                {
                    "Name": "uni-public-demo",
                    "Replicas": 1,
                    "DeletionProtectionEnabled": True,
                    "NetworkSpec": {
                        "NetworkType": ["PUBLIC"],
                        "EipBandwidth": 1,
                        "IpVersion": "IPv4",
                    },
                }
            ),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://open.volcengineapi.com/")
    assert json.loads(calls[0][2]["data"]) == {
        "Name": "uni-public-demo",
        "Replicas": 1,
        "DeletionProtectionEnabled": True,
        "NetworkSpec": {
            "NetworkType": ["PUBLIC"],
            "EipBandwidth": 1,
            "IpVersion": "IPv4",
        },
    }


def test_resource_list_reads_connection_defaults_from_config(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "server": "https://registry-config.test",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
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
        ),
    )

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "registry",
            "list",
            "--page",
            "1",
            "--page-size",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == (
        "POST",
        "https://registry-config.test/ListUniRegistries",
    )
    assert calls[0][2]["headers"]["Authorization"].startswith(
        "HMAC-SHA256 Credential=ak/"
    )
    assert "/cn-beijing/agentkit_stg/request" in calls[0][2]["headers"][
        "Authorization"
    ]


def test_resource_list_cli_options_override_connection_defaults(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "server": "https://registry-config.test",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
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
        ),
    )

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://registry-cli.test",
            "--region",
            "cn-shanghai",
            "--service",
            "agentkit",
            "registry",
            "list",
            "--page",
            "1",
            "--page-size",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://registry-cli.test/ListUniRegistries")
    assert "/cn-shanghai/agentkit/request" in calls[0][2]["headers"][
        "Authorization"
    ]


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
            "registry",
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
            "registry",
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


def test_registry_get_masks_initial_password_and_skips_unusable_address(
    monkeypatch,
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    monkeypatch.setattr(
        registry.requests,
        "request",
        lambda *_args, **_kwargs: _Response(
            {
                "Result": {
                    "Registry": {
                        "Id": "ur-1",
                        "PublicAddress": "115.190.137.250:80",
                        "InitialPassword": "should-not-be-printed",
                    }
                }
            }
        ),
    )

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
            "registry",
            "get",
            "ur-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "should-not-be-printed" not in result.output
    assert '"InitialPassword": "******"' in result.output
    assert registry._registry_config("ur-1") == {
        "username": "admin",
        "password": "secret",
        "service": "agentkit",
    }


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


def test_search_set_create_persists_mcp_route_under_registry(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    registry._upsert_registry_config(
        "ur-direct",
        {"server": "http://registry.test", "username": "uni", "password": "secret"},
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        action = kwargs.get("params", {}).get("Action")
        if action == "CreateMCPService":
            return _Response({"Result": {"MCPServiceId": "m-1", "MCPToolsetId": "mt-1"}})
        if action == "GetMCPService":
            return _Response(
                {
                    "Result": {
                        "MCPService": {
                            "MCPServiceId": "m-1",
                            "Status": "Ready",
                            "NetworkConfigurations": [
                                {"NetworkType": "public", "Endpoint": "https://svc.test"}
                            ],
                            "InboundAuthorizerConfiguration": {
                                "Authorizer": {
                                    "KeyAuth": {
                                        "ApiKeys": [{"Name": "s1", "Key": "service-key"}]
                                    }
                                }
                            },
                        }
                    }
                }
            )
        if action == "GetMCPToolset":
            return _Response(
                {
                    "Result": {
                        "MCPToolset": {
                            "MCPToolsetId": "mt-1",
                            "Status": "Ready",
                            "NetworkConfigurations": [
                                {"NetworkType": "public", "Endpoint": "https://gw.test"}
                            ],
                            "AuthorizerConfiguration": {
                                "Authorizer": {
                                    "KeyAuth": {
                                        "ApiKeys": [{"Name": "k1", "Key": "plain-key"}]
                                    }
                                }
                            },
                            "MCPServices": [{"MCPServiceId": "m-1", "Status": "Ready"}],
                        }
                    }
                }
            )
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

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://control.test",
            "search-set",
            "create",
            "--registry-id",
            "ur-direct",
            "--name",
            "finance",
            "--resource-id",
            "record-1",
            "--mcp-service-json",
            json.dumps({"Name": "mcp-finance"}),
            "--wait-interval",
            "0.1",
        ],
    )

    assert result.exit_code == 0, result.output
    cached = registry._registry_config("ur-direct")
    route = cached["search_sets"]["finance"]["mcp_route"]
    assert route["create_mcp_service_response"]["Result"]["MCPServiceId"] == "m-1"
    assert route["get_mcp_service_response"]["Result"]["MCPService"]["Status"] == "Ready"
    assert route["get_mcp_toolset_response"]["Result"]["MCPToolset"]["Status"] == "Ready"

    shown = runner.invoke(
        app, ["uni-reg", "registry", "binding", "get", "ur-direct"]
    )
    assert shown.exit_code == 0, shown.output
    assert "plain-key" not in shown.output
    assert "service-key" not in shown.output
    assert "******" in shown.output


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


def test_registry_binding_persists_connection_and_masks_password():
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "registry",
            "binding",
            "bind",
            "ur-direct",
            "--server",
            "http://registry.test/",
            "--username",
            "uni",
            "--password",
            "secret",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "******" in result.output
    assert "secret" not in result.output
    assert registry._registry_config("ur-direct") == {
        "server": "http://registry.test",
        "username": "uni",
        "password": "secret",
    }


def test_search_set_search_uses_cached_registry_connection(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    registry._upsert_registry_config(
        "ur-direct",
        {"server": "http://registry.test", "username": "uni", "password": "secret"},
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response({"resources": []})

    monkeypatch.setattr(registry.requests, "request", request)

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "search-set",
            "search",
            "finance",
            "export financial report",
            "--registry-id",
            "ur-direct",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == (
        "POST",
        "http://registry.test/api/v1/search-sets/finance/search",
    )
    assert calls[0][2]["auth"] == ("uni", "secret")
