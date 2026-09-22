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
    def __init__(self, payload: dict[str, object] | None = None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
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
        return _Response(
            {
                "ResponseMetadata": {"RequestId": "req-list"},
                "Result": {
                    "PageNumber": 2,
                    "PageSize": 20,
                    "Total": 1,
                    "Registries": [
                        {
                            "Id": "ur-1",
                            "Status": "Running",
                            "PublicAddress": "115.190.139.100:80",
                            "PrivateAddress": "192.168.0.10:80",
                            "InitialPassword": "initial-secret",
                            "Replicas": 1,
                        }
                    ],
                },
            }
        )

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
    output = json.loads(result.output)
    assert output == {
        "request_id": "req-list",
        "page": 2,
        "page_size": 20,
        "total": 1,
        "items": [
            {
                "id": "ur-1",
                "status": "Running",
                "public_address": "115.190.139.100:80",
                "private_address": "192.168.0.10:80",
                "ui_url": "http://115.190.139.100:80/ui",
                "username": "uni",
                "password": "initial-secret",
            }
        ],
    }
    assert "ResponseMetadata" not in result.output
    assert "Replicas" not in result.output


def test_resource_create_accepts_full_json_body(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(
            {
                "ResponseMetadata": {"RequestId": "req-create"},
                "Result": {"Id": "ur-1"},
            }
        )

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
            "--service",
            "agentkit_stg",
            "registry",
            "create",
            "--json",
            json.dumps(
                {
                    "Name": "uni-public-demo",
                    "Replicas": 1,
                    "GatewayId": "g-1",
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
    assert len(calls) == 1
    assert "Created UniRegistry: id=ur-1 request_id=req-create" in result.output
    output = json.loads(result.output[result.output.index("{") :])
    assert output == {"id": "ur-1", "request_id": "req-create"}
    assert "registry_id" not in isolated_uni_registry_config.get("uni_registry", {}).get(
        "defaults", {}
    )
    assert "default_registry_id" not in isolated_uni_registry_config.get(
        "uni_registry", {}
    )
    assert calls[0][0:2] == ("POST", "https://open.volcengineapi.com/")
    assert json.loads(calls[0][2]["data"]) == {
        "Name": "uni-public-demo",
        "Replicas": 1,
        "GatewayId": "g-1",
        "DeletionProtectionEnabled": True,
        "NetworkSpec": {
            "NetworkType": ["PUBLIC"],
            "EipBandwidth": 1,
            "IpVersion": "IPv4",
        },
    }


def test_resource_create_waits_until_registry_running(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []
    get_statuses = iter(["Creating", "Running"])

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        action = kwargs.get("params", {}).get("Action")
        if action == "CreateUniRegistry":
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": "req-create"},
                    "Result": {"Id": "ur-1"},
                }
            )
        if action == "GetUniRegistry":
            status = next(get_statuses)
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": f"req-get-{status.lower()}"},
                    "Result": {
                        "Registry": {
                            "Id": "ur-1",
                            "Status": status,
                            "PublicAddress": "public.registry.test:80",
                        }
                    }
                }
            )
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(registry.time, "sleep", lambda _seconds: None)
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
            "--service",
            "agentkit_stg",
            "registry",
            "create",
            "--json",
            json.dumps({"Name": "uni-public-demo", "Replicas": 1}),
            "--gateway-id",
            "g-1",
            "--wait",
            "--wait-interval",
            "0.1",
            "--wait-timeout",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [call[1] for call in calls] == ["https://open.volcengineapi.com/"] * 3
    assert [call[2]["params"]["Action"] for call in calls] == [
        "CreateUniRegistry",
        "GetUniRegistry",
        "GetUniRegistry",
    ]
    output = json.loads(result.output[result.output.index("{") :])
    assert output == {
        "id": "ur-1",
        "request_id": "req-create",
        "wait": {
            "status": "Running",
            "ready": True,
            "timed_out": False,
            "attempts": 2,
            "request_id": "req-get-running",
        },
    }
    assert "Created UniRegistry: id=ur-1 request_id=req-create" in result.output
    assert "Polling UniRegistry ur-1" in result.output
    assert "request_id=req-get-creating" in result.output
    assert "request_id=req-get-running" in result.output
    assert "status=Creating" in result.output
    assert "status=Running" in result.output
    assert isolated_uni_registry_config["uni_registry"]["defaults"]["registry_id"] == "ur-1"


def test_resource_create_uses_default_gateway_id(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "gateway_id": "g-default",
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
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
        ),
    )

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "registry",
            "create",
            "--json",
            json.dumps({"Name": "uni-public-demo", "Replicas": 1}),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(calls[0][2]["data"])["GatewayId"] == "g-default"


def test_resource_create_help_does_not_include_syncer_options():
    from agentkit.toolkit.cli.cli import app

    result = runner.invoke(app, ["uni-reg", "registry", "create", "--help"])

    assert result.exit_code == 0, result.output
    assert "--a2a-syncer" not in result.output
    assert "--skill-syncer" not in result.output
    assert "--syncer" not in result.output
    assert "--workspace-id" not in result.output
    assert "--gateway-id" in result.output


def test_resource_create_requires_gateway_id():
    from agentkit.toolkit.cli.cli import app

    result = runner.invoke(
        app,
        [
            "uni-reg",
            "--server",
            "https://open.volcengineapi.com",
            "--service",
            "agentkit_stg",
            "registry",
            "create",
            "--json",
            json.dumps({"Name": "uni-public-demo", "Replicas": 1}),
        ],
    )

    assert result.exit_code == 2
    assert "provide --gateway-id" in result.output
    assert "uni_registry.defaults.gateway_id" in result.output


def test_resource_syncer_uses_existing_default_and_requested_type(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "registry_id": "ur-default",
                    "gateway_id": "g-default",
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        action = kwargs.get("params", {}).get("Action")
        if action == "GetUniRegistry":
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": "req-get"},
                    "Result": {
                        "Registry": {
                            "Id": "ur-ready",
                            "Status": "Running",
                            "PublicAddress": "115.190.137.250:80",
                            "PrivateAddress": "192.168.0.10:80",
                            "Username": "uni",
                            "InitialPassword": "initial-secret",
                        }
                    },
                }
            )
        if action == "StartA2aUniMigration":
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": "req-start-a2a"},
                    "Result": {"MigrationId": "mig-a2a"},
                }
            )
        if action == "GetA2aUniMigration":
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": "req-get-a2a"},
                    "Result": {
                        "Migration": {
                            "Id": "mig-a2a",
                            "Status": "Succeeded",
                            "Progress": 100,
                        }
                    },
                }
            )
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
            "syncer",
            "--type",
            "a2a",
            "--acl-entry",
            "0.0.0.0/0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [call[2]["params"]["Action"] for call in calls] == [
        "GetUniRegistry",
        "StartA2aUniMigration",
        "GetA2aUniMigration",
    ]
    assert json.loads(calls[0][2]["data"]) == {"Id": "ur-default"}
    assert json.loads(calls[1][2]["data"]) == {"Id": "ur-ready"}
    output = json.loads(result.output[result.output.index("{") :])
    assert output["id"] == "ur-default"
    assert output["created"] is False
    assert output["a2a_syncer"]["id"] == "mig-a2a"
    assert "skill_syncer" not in output


def test_resource_syncer_updates_acl_entries_only_when_created(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        action = kwargs.get("params", {}).get("Action")
        if action == "CreateUniRegistry":
            return _Response({"Result": {"Id": "ur-1"}})
        if action == "UpdateUniRegistry":
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": "req-update"},
                    "Result": {
                        "Registry": {
                            "Id": "ur-1",
                            "NetworkSpec": {"AclEntries": ["0.0.0.0/0", "1.1.1.1/32"]},
                        }
                    },
                }
            )
        if action == "GetUniRegistry":
            return _Response(
                {"Result": {"Registry": {"Id": "ur-ready", "Status": "Running"}}}
            )
        if action == "StartA2aUniMigration":
            return _Response({"Result": {"MigrationId": "mig-a2a"}})
        if action == "GetA2aUniMigration":
            return _Response(
                {"Result": {"Migration": {"Id": "mig-a2a", "Status": "Succeeded"}}}
            )
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(registry.secrets, "token_hex", lambda _size: "abc123ef")
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
            "syncer",
            "--type",
            "a2a",
            "--gateway-id",
            "g-1",
            "--acl-entry",
            "0.0.0.0/0",
            "--acl-entry",
            "1.1.1.1/32",
            "--acl-entries",
            "0.0.0.0/0",
            "--wait-interval",
            "0.1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [call[2]["params"]["Action"] for call in calls] == [
        "CreateUniRegistry",
        "UpdateUniRegistry",
        "GetUniRegistry",
        "StartA2aUniMigration",
        "GetA2aUniMigration",
    ]
    assert json.loads(calls[1][2]["data"]) == {
        "Id": "ur-1",
        "NetworkSpec": {
            "NetworkType": ["PUBLIC"],
            "AclEntries": ["0.0.0.0/0", "1.1.1.1/32"],
        },
    }
    assert calls[1][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-1"
    output = json.loads(result.output[result.output.index("{") :])
    assert output["acl_update"] == {
        "id": "ur-1",
        "request_id": "req-update",
        "acl_entries": ["0.0.0.0/0", "1.1.1.1/32"],
    }


def test_resource_syncer_gateway_id_option_overrides_default(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "gateway_id": "g-default",
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        action = kwargs.get("params", {}).get("Action")
        if action == "CreateUniRegistry":
            return _Response({"Result": {"Id": "ur-1"}})
        if action == "GetUniRegistry":
            return _Response(
                {"Result": {"Registry": {"Id": "ur-ready", "Status": "Running"}}}
            )
        if action == "StartA2aUniMigration":
            return _Response({"Result": {"MigrationId": "mig-a2a"}})
        if action == "GetA2aUniMigration":
            return _Response(
                {"Result": {"Migration": {"Id": "mig-a2a", "Status": "Succeeded"}}}
            )
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(registry.secrets, "token_hex", lambda _size: "abc123ef")
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
            "syncer",
            "--type",
            "a2a",
            "--gateway-id",
            "g-explicit",
            "--wait-interval",
            "0.1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(calls[0][2]["data"])["GatewayId"] == "g-explicit"


def test_resource_syncer_creates_when_no_default_and_runs_both(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        action = kwargs.get("params", {}).get("Action")
        if action == "CreateUniRegistry":
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": "req-create"},
                    "Result": {"Id": "ur-1"},
                }
            )
        if action == "GetUniRegistry":
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": "req-get"},
                    "Result": {
                        "Registry": {
                            "Id": "ur-ready",
                            "Status": "Running",
                            "PublicAddress": "115.190.137.250:80",
                            "PrivateAddress": "192.168.0.10:80",
                            "Username": "uni",
                            "InitialPassword": "initial-secret",
                        }
                    },
                }
            )
        if action == "StartA2aUniMigration":
            return _Response({"Result": {"MigrationId": "mig-a2a"}})
        if action == "GetA2aUniMigration":
            return _Response(
                {"Result": {"Migration": {"Id": "mig-a2a", "Status": "Succeeded"}}}
            )
        if action == "StartSkillUniMigration":
            return _Response({"Result": {"MigrationId": "mig-skill"}})
        if action == "GetSkillUniMigration":
            return _Response(
                {"Result": {"Migration": {"Id": "mig-skill", "Status": "Succeeded"}}}
            )
        return _Response()

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(registry.secrets, "token_hex", lambda _size: "abc123ef")
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
            "syncer",
            "--gateway-id",
            "g-1",
            "--workspace-id",
            "ws-1",
            "--wait-interval",
            "0.1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [call[2]["params"]["Action"] for call in calls] == [
        "CreateUniRegistry",
        "GetUniRegistry",
        "StartA2aUniMigration",
        "GetA2aUniMigration",
        "StartSkillUniMigration",
        "GetSkillUniMigration",
    ]
    assert json.loads(calls[0][2]["data"]) == {
        "Name": "registry-abc123ef",
        "Replicas": 2,
        "DeletionProtectionEnabled": False,
        "NetworkSpec": {
            "NetworkType": ["PUBLIC"],
            "EipBandwidth": 1,
            "IpVersion": "IPv4",
        },
        "GatewayId": "g-1",
    }
    assert json.loads(calls[4][2]["data"]) == {
        "Id": "ur-ready",
        "WorkspaceId": "ws-1",
    }
    output = json.loads(result.output[result.output.index("{") :])
    assert output["id"] == "ur-1"
    assert output["created"] is True
    assert output["registry"] == {
        "id": "ur-ready",
        "request_id": "req-get",
        "status": "Running",
        "public_address": "115.190.137.250:80",
        "private_address": "192.168.0.10:80",
        "ui_url": "http://115.190.137.250:80/ui",
        "username": "uni",
        "password": "initial-secret",
    }
    assert output["a2a_syncer"]["id"] == "mig-a2a"
    assert output["skill_syncer"]["id"] == "mig-skill"
    assert isolated_uni_registry_config["uni_registry"]["defaults"]["registry_id"] == "ur-ready"


def test_resource_syncer_requires_gateway_id(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "registry_id": "ur-default",
                    "server": "https://open.volcengineapi.com",
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

    missing = runner.invoke(
        app, ["uni-reg", "registry", "syncer", "--type", "a2a"]
    )

    assert missing.exit_code == 2
    assert (
        "provide --gateway-id or set uni_registry.defaults.gateway_id"
        in missing.output
    )
    assert not calls

    empty = runner.invoke(
        app,
        [
            "uni-reg",
            "registry",
            "syncer",
            "--type",
            "a2a",
            "--gateway-id",
            "",
        ],
    )

    assert empty.exit_code == 2
    assert "--gateway-id is required" in empty.output
    assert not calls


def test_resource_start_a2a_migration_uses_default_registry_id(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "registry_id": "ur-default",
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(
            {
                "ResponseMetadata": {"RequestId": "req-start"},
                "Result": {"MigrationId": "mig-1", "Status": "Running"},
            }
        )

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(
        registry.UniRegistryClient,
        "_load_credentials",
        lambda self: (
            setattr(self, "access_key", "ak"),
            setattr(self, "secret_key", "sk"),
        ),
    )

    result = runner.invoke(app, ["uni-reg", "registry", "start-a2a-migration"])

    assert result.exit_code == 0, result.output
    assert calls[0][2]["params"]["Action"] == "StartA2aUniMigration"
    assert json.loads(calls[0][2]["data"]) == {"Id": "ur-default"}
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-default"
    output = json.loads(result.output)
    assert output == {
        "id": "mig-1",
        "registry_id": "ur-default",
        "request_id": "req-start",
        "status": "Running",
    }


def test_resource_get_a2a_migration_uses_migration_id_and_registry_header(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "registry_id": "ur-default",
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(
            {
                "ResponseMetadata": {"RequestId": "req-get-migration"},
                "Result": {
                    "Migration": {
                        "Id": "mig-1",
                        "Status": "Succeeded",
                        "Progress": 100,
                    }
                },
            }
        )

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
            "get-a2a-migration",
            "mig-1",
            "--registry-id",
            "ur-explicit",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["params"]["Action"] == "GetA2aUniMigration"
    assert json.loads(calls[0][2]["data"]) == {"Id": "mig-1"}
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-explicit"
    output = json.loads(result.output)
    assert output == {
        "id": "mig-1",
        "registry_id": "ur-explicit",
        "request_id": "req-get-migration",
        "status": "Succeeded",
        "progress": 100,
    }

    calls.clear()
    result = runner.invoke(
        app,
        [
            "uni-reg",
            "registry",
            "get-a2a-migration",
            "--registry-id",
            "ur-explicit",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(calls[0][2]["data"]) == {"Id": "ur-explicit"}
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-explicit"


def test_resource_skill_migration_commands_use_registry_id_header(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "registry_id": "ur-default",
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        action = kwargs.get("params", {}).get("Action")
        if action == "StartSkillUniMigration":
            return _Response(
                {
                    "ResponseMetadata": {"RequestId": "req-start-skill"},
                    "Result": {"MigrationId": "mig-skill", "Status": "Running"},
                }
            )
        return _Response(
            {
                "ResponseMetadata": {"RequestId": "req-get-skill"},
                "Result": {
                    "Migration": {
                        "Id": "mig-skill",
                        "Status": "Succeeded",
                        "Progress": 100,
                    }
                },
            }
        )

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
            "start-skill-migration",
            "--workspace-id",
            "ws-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["params"]["Action"] == "StartSkillUniMigration"
    assert json.loads(calls[0][2]["data"]) == {
        "Id": "ur-default",
        "WorkspaceId": "ws-1",
    }
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-default"
    output = json.loads(result.output)
    assert output["id"] == "mig-skill"
    assert output["registry_id"] == "ur-default"

    calls.clear()
    result = runner.invoke(
        app,
        [
            "uni-reg",
            "registry",
            "get-skill-migration",
            "mig-skill",
            "--registry-id",
            "ur-explicit",
            "--workspace-id",
            "ws-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["params"]["Action"] == "GetSkillUniMigration"
    assert json.loads(calls[0][2]["data"]) == {
        "Id": "mig-skill",
        "WorkspaceId": "ws-1",
    }
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-explicit"
    output = json.loads(result.output)
    assert output["id"] == "mig-skill"
    assert output["registry_id"] == "ur-explicit"
    assert output["status"] == "Succeeded"


def test_resource_retry_migration_commands_include_version_and_workspace(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "registry_id": "ur-default",
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        action = kwargs.get("params", {}).get("Action")
        return _Response(
            {
                "ResponseMetadata": {"RequestId": f"req-{action}"},
                "Result": {"MigrationId": "mig-retry", "Status": "Running"},
            }
        )

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
            "retry-a2a-migration",
            "mig-a2a",
            "--version",
            "7",
            "--registry-id",
            "ur-explicit",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["params"]["Action"] == "RetryA2aUniMigration"
    assert json.loads(calls[0][2]["data"]) == {"Id": "mig-a2a", "Version": 7}
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-explicit"

    calls.clear()
    result = runner.invoke(
        app,
        [
            "uni-reg",
            "registry",
            "retry-skill-migration",
            "mig-skill",
            "--version",
            "8",
            "--workspace-id",
            "ws-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["params"]["Action"] == "RetrySkillUniMigration"
    assert json.loads(calls[0][2]["data"]) == {
        "Id": "mig-skill",
        "WorkspaceId": "ws-1",
        "Version": 8,
    }
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-default"


def test_resource_get_prints_api_error_without_traceback(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    def request(_method, _url, **_kwargs):
        return _Response(
            {
                "ResponseMetadata": {
                    "RequestId": "req-404",
                    "Action": "GetUniRegistry",
                    "Version": "2025-10-30",
                    "Service": "agentkit_stg",
                    "Error": {
                        "HTTPCode": 404,
                        "Code": "ResourceNotFound.Id",
                        "Message": "The specified resource Id ur-missing cannot be found.",
                    },
                }
            },
            status_code=404,
        )

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
            "get",
            "ur-missing",
        ],
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "cli_uni_registry.py" not in result.output
    assert "ResourceNotFound.Id" in result.output
    assert "The specified resource Id ur-missing cannot be found." in result.output
    assert "request_id=req-404" in result.output


def test_resource_get_uses_default_registry_id_from_config(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {
            "uni_registry": {
                "defaults": {
                    "registry_id": "ur-default",
                    "server": "https://open.volcengineapi.com",
                    "region": "cn-beijing",
                    "service": "agentkit_stg",
                }
            }
        }
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(
            {
                "ResponseMetadata": {"RequestId": "req-get"},
                "Result": {
                    "Registry": {
                        "Id": "ur-default",
                        "Status": "Running",
                        "PublicAddress": "public.registry.test:80",
                    }
                },
            }
        )

    monkeypatch.setattr(registry.requests, "request", request)
    monkeypatch.setattr(
        registry.UniRegistryClient,
        "_load_credentials",
        lambda self: (
            setattr(self, "access_key", "ak"),
            setattr(self, "secret_key", "sk"),
        ),
    )

    result = runner.invoke(app, ["uni-reg", "registry", "get"])

    assert result.exit_code == 0, result.output
    assert calls[0][2]["params"]["Action"] == "GetUniRegistry"
    assert json.loads(calls[0][2]["data"]) == {"Id": "ur-default"}
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-default"
    output = json.loads(result.output[result.output.index("{") :])
    assert output["id"] == "ur-default"
    assert output["status"] == "Running"


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


def test_registry_get_prints_initial_password_and_skips_unusable_address(
    monkeypatch,
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    monkeypatch.setattr(
        registry.requests,
        "request",
        lambda *_args, **_kwargs: _Response(
            {
                "ResponseMetadata": {"RequestId": "req-get"},
                "Result": {
                    "Registry": {
                        "Id": "ur-1",
                        "Status": "Running",
                        "PublicAddress": "115.190.137.250:80",
                        "PrivateAddress": "192.168.0.10:80",
                        "InitialPassword": "should-not-be-printed",
                        "Replicas": 1,
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
    output = json.loads(result.output)
    assert output == {
        "id": "ur-1",
        "request_id": "req-get",
        "status": "Running",
        "public_address": "115.190.137.250:80",
        "private_address": "192.168.0.10:80",
        "ui_url": "http://115.190.137.250:80/ui",
        "username": "uni",
        "password": "should-not-be-printed",
    }
    assert "Replicas" not in result.output
    assert "ResponseMetadata" not in result.output
    assert registry._registry_config("ur-1") == {
        "username": "admin",
        "password": "secret",
        "service": "agentkit",
    }


def test_resource_update_prints_compact_registry_summary(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(
            {
                "ResponseMetadata": {"RequestId": "req-update"},
                "Result": {
                    "Registry": {
                        "Id": "ur-1",
                        "Status": "Running",
                        "PublicAddress": "115.190.137.250:80",
                        "PrivateAddress": "192.168.0.10:80",
                        "InitialPassword": "password-not-in-update-summary",
                        "NetworkSpec": {"AclEntries": ["203.0.113.10/32"]},
                        "Replicas": 1,
                    }
                },
            }
        )

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
            "registry",
            "update",
            "ur-1",
            "--json",
            '{"NetworkSpec":{"NetworkType":["PUBLIC"],"AclEntries":["203.0.113.10/32"]}}',
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-1"
    output = json.loads(result.output)
    assert output == {
        "id": "ur-1",
        "request_id": "req-update",
        "status": "Running",
        "public_address": "115.190.137.250:80",
        "private_address": "192.168.0.10:80",
        "acl_entries": ["203.0.113.10/32"],
    }
    assert "password-not-in-update-summary" not in result.output
    assert "ResponseMetadata" not in result.output


def test_resource_update_uses_default_registry_id(
    monkeypatch, isolated_uni_registry_config
):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    isolated_uni_registry_config.update(
        {"uni_registry": {"defaults": {"registry_id": "ur-default"}}}
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(
            {
                "ResponseMetadata": {"RequestId": "req-update"},
                "Result": {
                    "Registry": {
                        "Id": "ur-default",
                        "Status": "Running",
                        "NetworkSpec": {"AclEntries": ["203.0.113.10/32"]},
                    }
                },
            }
        )

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
            "registry",
            "update",
            "--json",
            '{"NetworkSpec":{"AclEntries":["203.0.113.10/32"]}}',
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://control.test/UpdateUniRegistry")
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-default"
    assert json.loads(calls[0][2]["data"]) == {
        "Id": "ur-default",
        "NetworkSpec": {"AclEntries": ["203.0.113.10/32"]},
    }
    output = json.loads(result.output)
    assert output["id"] == "ur-default"
    assert output["acl_entries"] == ["203.0.113.10/32"]


def test_resource_update_help_uses_registry_id_argument_name():
    from agentkit.toolkit.cli.cli import app

    result = runner.invoke(app, ["uni-reg", "registry", "update", "--help"])

    assert result.exit_code == 0, result.output
    assert "[registry_id]" in result.output
    assert "[resource_id]" not in result.output
    assert "REGISTRY_ID" in result.output
    assert "RESOURCE_ID" not in result.output


def test_resource_delete_adds_registry_id_header(monkeypatch):
    import agentkit.toolkit.cli.cli_uni_registry as registry
    from agentkit.toolkit.cli.cli import app

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response({"ResponseMetadata": {"RequestId": "req-delete"}})

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
            "registry",
            "delete",
            "ur-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == ("POST", "https://control.test/DeleteUniRegistry")
    assert json.loads(calls[0][2]["data"]) == {"Id": "ur-1"}
    assert calls[0][2]["headers"]["X-Mse-Uni-Registry-Id"] == "ur-1"


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
