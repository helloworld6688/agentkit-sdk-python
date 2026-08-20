# Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for ``agentkit add harness``."""

import json

import pytest
from typer.testing import CliRunner

from agentkit.toolkit.cli.cli import app
from agentkit.toolkit.cli import cli_add
from agentkit.toolkit.harness.env_mapping import to_runtime_env

runner = CliRunner()


def _run(args):
    return runner.invoke(app, ["add", *args])


def _squash_output(value: str) -> str:
    return " ".join(value.split())


@pytest.fixture(autouse=True)
def _fake_update_a2a_space_intent(monkeypatch):
    def fake_agentkit_post(*, endpoint, version, region, action, body):
        if action != "UpdateA2aSpace":
            raise AssertionError(f"unexpected AgentKit action: {action}")
        return {"ResponseMetadata": {"RequestId": "req-update"}, "Result": {}}, 1

    monkeypatch.setattr(cli_add, "_agentkit_post", fake_agentkit_post)


def test_creates_harness_json_with_layered_structure(tmp_path):
    result = _run(
        [
            "harness",
            "--name",
            "my-harness",
            "--model-name",
            "doubao-seed-1-6-250615",
            "--tools",
            "web_search, web_fetch",
            "--system-prompt",
            "You are helpful.",
            "--runtime",
            "codex",
            "--knowledgebase-type",
            "viking",
            "--knowledgebase-project",
            "myproj",
            "--knowledgebase-region",
            "cn-beijing",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "my-harness.harness.json").read_text())
    assert data == {
        "harness_name": "my-harness",
        "runtime": "codex",
        "short_term_memory": {"type": "local"},
        "model": {"name": "doubao-seed-1-6-250615"},
        "system_prompt": "You are helpful.",
        "tools": ["web_search", "web_fetch"],
        "knowledgebase": {
            "type": "viking",
            "project": "myproj",
            "region": "cn-beijing",
        },
    }


def test_description_written_and_flattens_to_env(tmp_path):
    # `--desc` is an alias for `--description`; the value lands in the spec and
    # flattens to the DESCRIPTION env var the veadk harness reads at agent init.
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--desc",
            "A travel planning assistant.",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert data["description"] == "A travel planning assistant."
    assert to_runtime_env(data)["DESCRIPTION"] == "A travel planning assistant."


def test_rerun_merges_into_existing_file(tmp_path):
    assert _run(["harness", "--name", "h", "--directory", str(tmp_path)]).exit_code == 0
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--skills",
            "code_review",
            "--discovery-url",
            "https://x/.well-known/openid-configuration",
            "--allowed-id",
            "cid1,cid2",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    # Default scaffold fields survive the merge ...
    assert data["harness_name"] == "h"
    assert data["short_term_memory"] == {"type": "local"}
    # ... and the new options are written.
    assert data["skills"] == ["code_review"]
    assert data["auth"] == {
        "discovery_url": "https://x/.well-known/openid-configuration",
        "allowed_ids": ["cid1", "cid2"],
    }


def test_blank_component_type_is_pruned(tmp_path):
    # A connection param without a backend `type` leaves no orphan section.
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--long-term-memory-project",
            "p",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert "long_term_memory" not in data


def test_invalid_runtime_fails(tmp_path):
    result = _run(
        ["harness", "--name", "h", "--runtime", "foo", "--directory", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert not (tmp_path / "h.harness.json").exists()


def test_invalid_name_fails(tmp_path):
    result = _run(["harness", "--name", "bad name", "--directory", str(tmp_path)])
    assert result.exit_code == 1


def test_name_normalization_notice_shown_for_non_identifier_name(tmp_path):
    result = _run(
        ["harness", "--name", "oauth-test", "--directory", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "oauth_test" in _squash_output(result.output)


def test_no_normalization_notice_for_clean_identifier_name(tmp_path):
    result = _run(["harness", "--name", "myagent", "--directory", str(tmp_path)])

    assert result.exit_code == 0, result.output
    output = _squash_output(result.output)
    assert "instead of" not in output


def test_registry_flags_write_agentkit_a2a_section(tmp_path):
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry",
            "agentkit://a2a-registry?space_id=space-test&top_k=2",
            "--registry-top-k",
            "7",
            "--registry-region",
            "cn-beijing",
            "--structured-tool-calls",
            "--include-tools-every-turn",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert data["registry"] == {
        "type": "agentkit_a2a",
        "space_id": "space-test",
        "top_k": 7,
        "region": "cn-beijing",
    }
    assert data["structured_tool_calls"] is True
    assert data["include_tools_every_turn"] is True


def test_registry_uri_writes_uni_registry_section(tmp_path):
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry",
            (
                "uni-registry://reg-test?"
                "endpoint=https%3A%2F%2Fregistry.example&record_type=agent_card&top_k=5"
            ),
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert data["registry"] == {
        "type": "uni_registry",
        "id": "reg-test",
        "endpoint": "https://registry.example",
        "record_type": "agent_card",
        "top_k": 5,
    }
    env = to_runtime_env(data)
    assert env["REGISTRY_TYPE"] == "uni_registry"
    assert env["REGISTRY_ID"] == "reg-test"
    assert env["REGISTRY_RECORD_TYPE"] == "agent_card"


def test_registry_add_enables_a2a_space_intent(tmp_path, monkeypatch):
    calls = []

    def fake_agentkit_post(*, endpoint, version, region, action, body):
        calls.append(
            {
                "endpoint": endpoint,
                "version": version,
                "region": region,
                "action": action,
                "body": body,
            }
        )
        return {"ResponseMetadata": {"RequestId": "req-update"}, "Result": {}}, 1

    monkeypatch.setattr(cli_add, "_agentkit_post", fake_agentkit_post)

    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry-space-id",
            "as-test",
            "--registry-endpoint",
            "https://open.volcengineapi.com/?unused=1",
            "--registry-region",
            "cn-beijing",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "endpoint": "https://open.volcengineapi.com/",
            "version": "2025-10-30",
            "region": "cn-beijing",
            "action": "UpdateA2aSpace",
            "body": {"Id": "as-test", "IntentEnabled": True},
        }
    ]


def test_registry_space_name_resolves_to_space_id(tmp_path, monkeypatch):
    captured = {}

    def fake_resolve_space_name(space_name, *, endpoint, region):
        captured.update({"space_name": space_name, "endpoint": endpoint, "region": region})
        return "space-from-name"

    monkeypatch.setattr(
        "agentkit.toolkit.cli.cli_add._resolve_a2a_space_id_by_name",
        fake_resolve_space_name,
    )

    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry-space-name",
            "space-name",
            "--registry-endpoint",
            "https://agentkit.cn-beijing.volcengineapi.com/",
            "--registry-region",
            "cn-beijing",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert data["registry"] == {
        "type": "agentkit_a2a",
        "space_id": "space-from-name",
        "endpoint": "https://agentkit.cn-beijing.volcengineapi.com/",
        "region": "cn-beijing",
    }
    assert captured == {
        "space_name": "space-name",
        "endpoint": "https://agentkit.cn-beijing.volcengineapi.com/",
        "region": "cn-beijing",
    }


def test_registry_uri_space_name_uses_uri_endpoint_and_region(tmp_path, monkeypatch):
    captured = {}

    def fake_resolve_space_name(space_name, *, endpoint, region):
        captured.update({"space_name": space_name, "endpoint": endpoint, "region": region})
        return "space-from-uri-name"

    monkeypatch.setattr(
        "agentkit.toolkit.cli.cli_add._resolve_a2a_space_id_by_name",
        fake_resolve_space_name,
    )

    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry",
            "agentkit://a2a-registry?space_name=space-name&endpoint=https%3A%2F%2Fopen.volcengineapi.com%2F&region=cn-beijing",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert data["registry"] == {
        "type": "agentkit_a2a",
        "space_id": "space-from-uri-name",
        "endpoint": "https://open.volcengineapi.com/",
        "region": "cn-beijing",
    }
    assert captured == {
        "space_name": "space-name",
        "endpoint": "https://open.volcengineapi.com/",
        "region": "cn-beijing",
    }


def test_registry_default_resolves_default_space(tmp_path, monkeypatch):
    captured = {}

    def fake_resolve_space_name(space_name, *, endpoint, region):
        captured.update({"space_name": space_name, "endpoint": endpoint, "region": region})
        return "space-default"

    monkeypatch.setattr(
        "agentkit.toolkit.cli.cli_add._resolve_a2a_space_id_by_name",
        fake_resolve_space_name,
    )

    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry",
            "default",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert data["registry"] == {
        "type": "agentkit_a2a",
        "space_id": "space-default",
        "endpoint": "https://open.volcengineapi.com/",
        "region": "cn-beijing",
    }
    assert captured == {
        "space_name": "Default",
        "endpoint": "https://open.volcengineapi.com/",
        "region": "cn-beijing",
    }


def test_registry_default_overrides_existing_space_id(tmp_path, monkeypatch):
    (tmp_path / "h.harness.json").write_text(
        json.dumps(
            {
                "harness_name": "h",
                "runtime": "adk",
                "short_term_memory": {"type": "local"},
                "registry": {
                    "type": "agentkit_a2a",
                    "space_id": "as-old",
                    "endpoint": "https://open.volcengineapi.com/",
                    "region": "cn-beijing",
                },
            }
        )
    )
    captured = {}

    def fake_resolve_space_name(space_name, *, endpoint, region):
        captured.update({"space_name": space_name, "endpoint": endpoint, "region": region})
        return "as-default"

    monkeypatch.setattr(
        "agentkit.toolkit.cli.cli_add._resolve_a2a_space_id_by_name",
        fake_resolve_space_name,
    )

    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry",
            "default",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert data["registry"] == {
        "type": "agentkit_a2a",
        "endpoint": "https://open.volcengineapi.com/",
        "region": "cn-beijing",
        "space_id": "as-default",
    }
    assert captured == {
        "space_name": "Default",
        "endpoint": "https://open.volcengineapi.com/",
        "region": "cn-beijing",
    }


def test_resolve_a2a_space_id_by_name_paginates_all_spaces(monkeypatch):
    calls = []

    def fake_agentkit_post(*, endpoint, version, region, action, body):
        calls.append(body)
        if body["PageNumber"] == 1:
            return (
                {
                    "Result": {
                        "TotalCount": 101,
                        "Items": [
                            {
                                "Id": f"as-{idx}",
                                "Name": f"space-{idx}",
                            }
                            for idx in range(100)
                        ],
                    }
                },
                1,
            )
        return (
            {
                "Result": {
                    "TotalCount": 101,
                    "Items": [{"Id": "as-target", "Name": "target-space"}],
                }
            },
            1,
        )

    monkeypatch.setattr(
        "agentkit.toolkit.cli.cli_add._agentkit_post",
        fake_agentkit_post,
    )

    space_id = cli_add._resolve_a2a_space_id_by_name(
        "target-space",
        endpoint="https://agentkit.cn-beijing.volcengineapi.com/",
        region="cn-beijing",
    )

    assert space_id == "as-target"
    assert calls == [
        {"PageNumber": 1, "PageSize": 100},
        {"PageNumber": 2, "PageSize": 100},
    ]


def test_registry_disabled_is_pruned_from_harness_spec(tmp_path):
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry",
            "disabled",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "h.harness.json").read_text())
    assert "registry" not in data


def test_registry_off_is_not_supported(tmp_path):
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry",
            "off",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 1
    assert "`default` / `disabled` is supported" in _squash_output(result.output)


def test_registry_config_maps_to_runtime_env():
    env = to_runtime_env(
        {
            "registry": {
                "type": "agentkit_a2a",
                "space_id": "space-test",
                "top_k": 7,
                "region": "cn-beijing",
            },
            "structured_tool_calls": True,
            "include_tools_every_turn": True,
        }
    )

    assert env["REGISTRY_TYPE"] == "agentkit_a2a"
    assert env["REGISTRY_SPACE_ID"] == "space-test"
    assert env["REGISTRY_TOP_K"] == "7"
    assert env["REGISTRY_REGION"] == "cn-beijing"
    assert env["STRUCTURED_TOOL_CALLS"] == "true"
    assert env["INCLUDE_TOOLS_EVERY_TURN"] == "true"


def test_add_harness_register_self_resolves_runtime_and_space(
    tmp_path, monkeypatch
):
    (tmp_path / "harness.json").write_text(
        json.dumps({"h": {"url": "https://x", "runtime_id": "r-test"}})
    )
    captured = {}

    def fake_create_a2a_agent(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "success",
            "agent_id": "a-test",
            "tags": [],
            "diagnostics": {"request_id": "req-1"},
        }

    monkeypatch.setattr(
        "agentkit.toolkit.cli.cli_add._create_a2a_agent",
        fake_create_a2a_agent,
    )

    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry-space-id",
            "space-test",
            "--register-self",
            "--register-tag",
            "env=test",
            "--register-endpoint",
            "https://agentkit.cn-beijing.volcengineapi.com/",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    assert "A2A agent registered" in result.output
    assert "Harness URL:" in result.output
    assert captured["a2a_space_id"] == "space-test"
    assert captured["runtime_id"] == "r-test"
    assert captured["network_type"] == "public"
    assert captured["tags"] == [{"Key": "env", "Value": "test"}]
    assert captured["endpoint"] == "https://agentkit.cn-beijing.volcengineapi.com/"
    assert captured["region"] == "cn-beijing"


def test_add_harness_register_self_resolves_space_name(
    tmp_path, monkeypatch
):
    (tmp_path / "harness.json").write_text(
        json.dumps({"h": {"url": "https://x", "runtime_id": "r-test"}})
    )
    captured = {}

    def fake_resolve_space_name(space_name, *, endpoint, region):
        captured["resolved_space"] = {
            "space_name": space_name,
            "endpoint": endpoint,
            "region": region,
        }
        return "space-from-name"

    def fake_create_a2a_agent(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "success",
            "agent_id": "a-test",
            "tags": [],
            "diagnostics": {"request_id": "req-1"},
        }

    monkeypatch.setattr(
        "agentkit.toolkit.cli.cli_add._resolve_a2a_space_id_by_name",
        fake_resolve_space_name,
    )
    monkeypatch.setattr(
        "agentkit.toolkit.cli.cli_add._create_a2a_agent",
        fake_create_a2a_agent,
    )

    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--register-self",
            "--register-space-name",
            "space-name",
            "--register-endpoint",
            "https://agentkit.cn-beijing.volcengineapi.com/",
            "--register-region",
            "cn-beijing",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    assert captured["resolved_space"] == {
        "space_name": "space-name",
        "endpoint": "https://agentkit.cn-beijing.volcengineapi.com/",
        "region": "cn-beijing",
    }
    assert captured["a2a_space_id"] == "space-from-name"
    assert captured["runtime_id"] == "r-test"


def test_add_harness_register_self_requires_harness_json_entry(tmp_path):
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry-space-id",
            "space-test",
            "--register-self",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 1
    assert "does not contain an entry for 'h'" in _squash_output(result.output)


def test_add_harness_register_self_requires_url_and_runtime_id(tmp_path):
    (tmp_path / "harness.json").write_text(json.dumps({"h": {"url": "https://x"}}))

    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--registry-space-id",
            "space-test",
            "--register-self",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 1
    assert "missing required field(s): runtime_id" in _squash_output(result.output)


def test_add_harness_register_self_rejects_invalid_network_type(tmp_path):
    (tmp_path / "harness.json").write_text(
        json.dumps({"h": {"url": "https://x", "runtime_id": "r-test"}})
    )
    result = _run(
        [
            "harness",
            "--name",
            "h",
            "--register-space-id",
            "space-test",
            "--register-self",
            "--register-network-type",
            "internet",
            "--directory",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 1
    assert "--register-network-type must be one of" in result.output
