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

"""AgentKit CLI - ``add`` commands.

``agentkit add harness`` writes a harness configuration file
``<name>.harness.json`` describing a deployable agent. The schema mirrors the
attributes of the VeADK harness (model / tools / skills / system prompt /
runtime, plus the knowledge-base and memory components and an optional OAuth2
auth block), serialized as a layered JSON document::

    {
      "harness_name": "my-harness",
      "model": {"name": "doubao-seed-1-6-250615"},
      "tools": ["web_search"],
      "skills": [],
      "system_prompt": "You are a helpful assistant.",
      "description": "A helpful assistant.",
      "runtime": "adk",
      "knowledgebase": {"type": "viking", "project": "...", "region": "..."},
      "long_term_memory": {"type": ""},
      "short_term_memory": {"type": "local"},
      "auth": {"discovery_url": "...", "allowed_ids": ["..."]}
    }

Re-running ``add harness`` for the same ``--name`` merges the supplied options
into the existing file, so configuration can be built up incrementally.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlunparse

import typer
from rich.console import Console

console = Console()

add_app = typer.Typer(
    name="add",
    help="Add and configure agent resources (e.g. a harness).",
    add_completion=False,
)

# Allowed values for the enumerated fields. ``""`` always means "leave unset"
# (the component is disabled / VeADK falls back to its own default).
_RUNTIMES = ("adk", "codex")
_KNOWLEDGEBASE_TYPES = ("viking", "opensearch", "redis", "tos_vector", "context_search")
_LONG_TERM_MEMORY_TYPES = ("viking", "opensearch", "redis", "mem0")
_SHORT_TERM_MEMORY_TYPES = ("local", "sqlite", "mysql", "postgresql")

# Harness name charset (matches `init`'s directory-name rule).
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_REGISTRY_QUERY_KEYS = {
    "id",
    "space_id",
    "space_name",
    "record_type",
    "top_k",
    "endpoint",
    "region",
}
_REGISTRY_INT_KEYS = {"top_k"}
_REGISTER_NETWORK_TYPES = {"public", "private"}
_REGISTER_DEFAULT_VERSION = "2025-10-30"
_DEFAULT_A2A_REGISTRY_URI = (
    "agentkit://a2a-registry?"
    "space_name=Default&region=cn-beijing&endpoint=https://open.volcengineapi.com/"
)


class _A2ARegisterError(Exception):
    def __init__(
        self, message: str, diagnostics: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.diagnostics = diagnostics or {}


def _split_csv(value: Optional[str]) -> Optional[list[str]]:
    """Parse a comma-separated option into a trimmed list, or None if unset."""
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _is_blank(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _derive_agent_name(harness_name: str) -> str:
    """ADK agent name derived from a harness name.

    Must mirror veadk's ``harness_app/utils.py::agent_name_from_harness``.
    """
    name = re.sub(r"[^0-9A-Za-z_]", "_", harness_name or "")
    if not name or name[0].isdigit():
        name = f"_{name}"
    return f"{name}_" if name == "user" else name


def _prune(data: dict) -> None:
    """Drop unset fields so the file holds only what is configured.

    Empty scalars/lists are removed; a component section left with no ``type``
    is dropped entirely. ``short_term_memory`` is always kept (its ``local``
    default is meaningful).
    """
    components = ("knowledgebase", "long_term_memory", "short_term_memory")
    for key in list(data):
        value = data[key]
        if isinstance(value, dict):
            for sub in list(value):
                if _is_blank(value[sub]):
                    del value[sub]
            keep_empty_type = key == "short_term_memory"
            if key in components and not value.get("type") and not keep_empty_type:
                del data[key]
            elif not value:
                del data[key]
        elif _is_blank(value):
            del data[key]


def _validate_choice(
    label: str, value: Optional[str], allowed: tuple[str, ...]
) -> None:
    """Fast-fail when an enumerated option is set to an unsupported value."""
    if value is None or value == "":
        return
    if value not in allowed:
        console.print(
            f"[red]Error: invalid {label} '{value}'. "
            f"Allowed: {', '.join(allowed)}[/red]"
        )
        raise typer.Exit(1)


def _parse_registry_int(key: str, value: object) -> object:
    if key not in _REGISTRY_INT_KEYS:
        return value
    try:
        return int(str(value))
    except ValueError as exc:
        raise ValueError(f"Registry param `{key}` must be an integer, got {value!r}.") from exc


def _expand_default_registry_uri(value: str) -> str:
    raw = value.strip()
    if raw.lower() == "default":
        return _DEFAULT_A2A_REGISTRY_URI
    return raw


def _parse_registry_uri(value: str) -> dict:
    """Parse a supported A2A registry URI into a Harness spec section."""
    raw = _expand_default_registry_uri(value)
    if raw.lower() == "disabled":
        return {"type": ""}

    parsed = urlparse(raw)
    if parsed.scheme == "uni-registry" and parsed.path in {"", "/"}:
        if not parsed.netloc:
            raise ValueError("UniRegistry URI requires a registry ID")
        scheme_type = "uni_registry"
    elif (
        parsed.scheme == "agentkit"
        and parsed.netloc == "a2a-registry"
        and parsed.path in {"", "/"}
    ):
        scheme_type = "agentkit_a2a"
    else:
        raise ValueError(
            "Unsupported registry URI. Use "
            "`agentkit://a2a-registry?space_id=xxx&top_k=3`, "
            "`uni-registry://<registry-id>?endpoint=https://...`, "
            "or `default` / `disabled`. Only `default` / `disabled` is supported "
            "as a non-URI registry value."
        )

    query = {
        key.replace("-", "_"): values[-1]
        for key, values in parse_qs(parsed.query, keep_blank_values=False).items()
        if values and values[-1] != ""
    }
    unknown = sorted(set(query) - _REGISTRY_QUERY_KEYS)
    if unknown:
        raise ValueError(
            f"Unsupported registry query param(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(_REGISTRY_QUERY_KEYS))}"
        )

    section: dict = {"type": scheme_type}
    if scheme_type == "uni_registry":
        section["id"] = parsed.netloc
    for key, raw_value in query.items():
        section[key] = _parse_registry_int(key, raw_value)
    return section


def _set_registry_value(section: dict, key: str, value: object | None) -> None:
    if value is not None:
        section[key] = _parse_registry_int(key, value)


def _apply_registry_config(
    data: dict,
    registry: Optional[str],
    registry_id: Optional[str],
    registry_record_type: Optional[str],
    registry_space_id: Optional[str],
    registry_space_name: Optional[str],
    registry_top_k: Optional[int],
    registry_endpoint: Optional[str],
    registry_region: Optional[str],
) -> None:
    has_registry_update = any(
        value is not None
        for value in [
            registry,
            registry_id,
            registry_record_type,
            registry_space_id,
            registry_space_name,
            registry_top_k,
            registry_endpoint,
            registry_region,
        ]
    )
    if not has_registry_update:
        return

    section = data.get("registry")
    if not isinstance(section, dict):
        section = {}

    if registry is not None:
        parsed_registry = _parse_registry_uri(registry)
        if parsed_registry.get("space_name") and "space_id" not in parsed_registry:
            section.pop("space_id", None)
        section.update(parsed_registry)

    _set_registry_value(section, "id", registry_id)
    _set_registry_value(section, "record_type", registry_record_type)
    if registry_space_name is not None:
        section.pop("space_id", None)
    _set_registry_value(section, "space_id", registry_space_id)
    _set_registry_value(section, "space_name", registry_space_name)
    _set_registry_value(section, "top_k", registry_top_k)
    _set_registry_value(section, "endpoint", registry_endpoint)
    _set_registry_value(section, "region", registry_region)

    if section.get("type") not in {"", "uni_registry"}:
        section["type"] = "agentkit_a2a"

    if section.get("type") == "agentkit_a2a" and not section.get("space_id"):
        space_name = section.pop("space_name", None)
        if space_name:
            resolved_region = (
                registry_region
                or section.get("region")
                or os.getenv("AGENTKIT_REGION")
                or os.getenv("VOLCENGINE_REGION")
                or "cn-beijing"
            )
            resolved_endpoint = (
                registry_endpoint
                or section.get("endpoint")
                or _default_agentkit_endpoint(resolved_region)
            )
            section["space_id"] = _resolve_a2a_space_id_by_name(
                str(space_name),
                endpoint=str(resolved_endpoint),
                region=str(resolved_region),
            )
    else:
        section.pop("space_name", None)

    if section.get("type") == "agentkit_a2a" and not section.get("space_id"):
        raise ValueError(
            "Registry space_id is required. Use "
            '`--registry "agentkit://a2a-registry?space_id=xxx"` '
            "or `--registry-space-id xxx` / `--registry-space-name name`."
        )

    data["registry"] = section
    if section.get("type") == "agentkit_a2a":
        resolved_endpoint, resolved_region = _resolve_agentkit_openapi_target(
            endpoint=str(section["endpoint"]) if section.get("endpoint") else None,
            region=str(section["region"]) if section.get("region") else None,
        )
        _enable_a2a_space_intent(
            str(section["space_id"]),
            endpoint=resolved_endpoint,
            region=resolved_region,
        )
    elif section.get("type") == "uni_registry":
        if not section.get("id"):
            raise ValueError(
                "UniRegistry id is required. Use "
                '`--registry "uni-registry://<registry-id>?endpoint=https://..."` '
                "or `--registry-id`."
            )
        if not section.get("endpoint"):
            raise ValueError(
                "UniRegistry endpoint is required. Set it in the URI or pass "
                "`--registry-endpoint`."
            )


def _load_spec(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _write_spec(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def _parse_register_tags(values: list[str]) -> list[dict[str, str]]:
    tags = []
    for value in values:
        if "=" not in value:
            console.print(
                f"[red]Error: invalid --register-tag {value!r}. "
                "Expected KEY=VALUE.[/red]"
            )
            raise typer.Exit(1)
        key, tag_value = value.split("=", 1)
        key = key.strip()
        tag_value = tag_value.strip()
        if not key:
            console.print("[red]Error: --register-tag key must not be empty.[/red]")
            raise typer.Exit(1)
        tags.append({"Key": key, "Value": tag_value})
    return tags


def _default_agentkit_endpoint(region: str) -> str:
    if region == "cn-beijing":
        return "https://agentkit.cn-beijing.volcengineapi.com/"
    return f"https://agentkit.{region}.volcengineapi.com/"


def _resolve_agentkit_openapi_target(
    *,
    endpoint: Optional[str],
    region: Optional[str],
) -> tuple[str, str]:
    resolved_region = (
        region
        or os.getenv("AGENTKIT_REGION")
        or os.getenv("VOLCENGINE_REGION")
        or "cn-beijing"
    )
    resolved_endpoint = endpoint or _default_agentkit_endpoint(str(resolved_region))
    parsed = urlparse(resolved_endpoint)
    if (
        parsed.scheme
        and parsed.netloc
        and (parsed.query or parsed.params or parsed.fragment)
    ):
        resolved_endpoint = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path or "/", "", "", "")
        )
    return resolved_endpoint, str(resolved_region)


def _request_id(response: dict[str, Any]) -> str | None:
    return (response.get("ResponseMetadata") or {}).get("RequestId")


def _resolve_agentkit_credentials():
    from agentkit.platform import VolcConfiguration, resolve_credentials

    return resolve_credentials("agentkit", platform_config=VolcConfiguration())


def _agentkit_post(
    *,
    endpoint: str,
    version: str,
    region: str,
    action: str,
    body: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    import time

    import requests

    from agentkit.auth._sigv4 import sign_headers

    credentials = _resolve_agentkit_credentials()
    started = time.monotonic()
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    parsed = urlparse(endpoint)
    path = parsed.path or "/"
    query = {"Action": action, "Version": version}
    headers = sign_headers(
        "POST",
        parsed.netloc,
        query,
        body_bytes,
        access_key=credentials.access_key,
        secret_key=credentials.secret_key,
        service="agentkit",
        region=region,
        session_token=credentials.session_token or None,
        path=path,
    )

    response = None
    try:
        response = requests.post(
            endpoint,
            params=query,
            headers=headers,
            data=body_bytes,
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        diagnostics = {}
        if response is not None:
            diagnostics["status_code"] = response.status_code
            try:
                diagnostics["response"] = response.json()
            except ValueError:
                pass
        raise _A2ARegisterError(
            f"AgentKit OpenAPI request failed: {exc}", diagnostics
        ) from exc
    except ValueError as exc:
        raise _A2ARegisterError(
            "AgentKit OpenAPI returned non-JSON response"
        ) from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    if data.get("Error"):
        raise _A2ARegisterError(
            "AgentKit OpenAPI returned an error", {"response": data.get("Error")}
        )
    if "Result" not in data:
        raise _A2ARegisterError("AgentKit OpenAPI response missing Result")
    return data, duration_ms


def _enable_a2a_space_intent(
    space_id: str,
    *,
    endpoint: str,
    region: str,
) -> dict[str, Any]:
    response, request_duration_ms = _agentkit_post(
        endpoint=endpoint,
        version=_REGISTER_DEFAULT_VERSION,
        region=region,
        action="UpdateA2aSpace",
        body={"Id": space_id, "IntentEnabled": True},
    )
    return {
        "request_id": _request_id(response),
        "request_duration_ms": request_duration_ms,
    }


def _resolve_a2a_space_id_by_name(
    space_name: str,
    *,
    endpoint: str,
    region: str,
) -> str:
    normalized_name = space_name.strip()
    if not normalized_name:
        raise ValueError("A2A space name must not be empty.")

    matches: list[dict[str, Any]] = []
    page_number = 1
    page_size = 100
    while True:
        response, _ = _agentkit_post(
            endpoint=endpoint,
            version=_REGISTER_DEFAULT_VERSION,
            region=region,
            action="ListA2aSpaces",
            body={"PageNumber": page_number, "PageSize": page_size},
        )
        result = response.get("Result") or {}
        items = result.get("Items") or []
        if not isinstance(items, list):
            raise _A2ARegisterError("ListA2aSpaces response Items is not a list")
        matches.extend(
            item
            for item in items
            if isinstance(item, dict) and item.get("Name") == normalized_name
        )

        total_count = int(result.get("TotalCount") or 0)
        if page_number * page_size >= total_count or not items:
            break
        page_number += 1

    if not matches:
        raise ValueError(f"A2A space name '{normalized_name}' was not found.")
    if len(matches) > 1:
        ids = ", ".join(str(item.get("Id", "")) for item in matches if item.get("Id"))
        raise ValueError(
            f"A2A space name '{normalized_name}' matched multiple spaces"
            + (f": {ids}" if ids else ".")
        )

    space_id = matches[0].get("Id")
    if not space_id:
        raise _A2ARegisterError(
            f"ListA2aSpaces result for '{normalized_name}' is missing Id"
        )
    return str(space_id)


def _create_a2a_agent(
    *,
    a2a_space_id: str,
    runtime_id: str,
    network_type: str,
    tags: list[dict[str, str]] | None,
    endpoint: str,
    region: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "Source": "Runtime",
        "A2aSpaceId": a2a_space_id,
        "RuntimeConfig": {
            "RuntimeId": runtime_id,
            "NetworkType": network_type,
        },
        "SetDefaultVersion": True,
    }
    if tags:
        body["Tags"] = tags

    response, request_duration_ms = _agentkit_post(
        endpoint=endpoint,
        version=_REGISTER_DEFAULT_VERSION,
        region=region,
        action="CreateA2aAgent",
        body=body,
    )
    result = response.get("Result") or {}
    return {
        "outcome": "success",
        "agent_id": result.get("Id", ""),
        "tags": result.get("Tags") or [],
        "diagnostics": {
            "request_id": _request_id(response),
            "request_duration_ms": request_duration_ms,
        },
    }


def _resolve_self_register_entry(directory: str, name: str) -> dict[str, Any]:
    registry_path = Path(directory).resolve() / "harness.json"
    registry = _load_json(registry_path)
    entry = registry.get(name)
    if not isinstance(entry, dict):
        console.print(
            f"[red]Error: cannot register harness '{name}'. "
            f"{registry_path} does not contain an entry for '{name}'. "
            "Deploy the harness first or check --directory.[/red]"
        )
        raise typer.Exit(1)

    missing = [field for field in ("url", "runtime_id") if not entry.get(field)]
    if missing:
        console.print(
            f"[red]Error: cannot register harness '{name}'. "
            f"{registry_path} entry is missing required field(s): "
            f"{', '.join(missing)}. Deploy the harness again so harness.json "
            "records url and runtime_id.[/red]"
        )
        raise typer.Exit(1)
    return entry


def _resolve_register_space_id(
    data: dict,
    space_id: Optional[str],
    space_name: Optional[str],
    *,
    endpoint: str,
    region: str,
) -> str:
    if space_id:
        return space_id
    if space_name:
        return _resolve_a2a_space_id_by_name(
            space_name,
            endpoint=endpoint,
            region=region,
        )
    registry = data.get("registry") if isinstance(data, dict) else None
    if isinstance(registry, dict) and registry.get("space_id"):
        return str(registry["space_id"])
    if isinstance(registry, dict) and registry.get("space_name"):
        return _resolve_a2a_space_id_by_name(
            str(registry["space_name"]),
            endpoint=endpoint,
            region=region,
        )
    console.print(
        "[red]Error: A2A space id is required for registration. Pass "
        "--register-space-id / --register-space-name, --registry-space-id / "
        "--registry-space-name, or set `registry.space_id` in the harness spec.[/red]"
    )
    raise typer.Exit(1)


def _register_a2a_runtime_agent(
    *,
    subject: str,
    space_id: str,
    runtime_id: str,
    network_type: str,
    tags: list[str],
    endpoint: Optional[str],
    region: Optional[str],
) -> None:
    normalized_network_type = network_type.strip().lower()
    if normalized_network_type not in _REGISTER_NETWORK_TYPES:
        console.print(
            "[red]Error: --register-network-type must be one of: public, private.[/red]"
        )
        raise typer.Exit(1)

    resolved_region = (
        region
        or os.getenv("AGENTKIT_REGION")
        or os.getenv("VOLCENGINE_REGION")
        or "cn-beijing"
    )
    resolved_endpoint = endpoint or _default_agentkit_endpoint(resolved_region)
    parsed_tags = _parse_register_tags(tags)

    console.print(
        f"[cyan]Registering {subject} runtime {runtime_id} "
        f"to A2A space {space_id}...[/cyan]"
    )
    try:
        result = _create_a2a_agent(
            a2a_space_id=space_id,
            runtime_id=runtime_id,
            network_type=normalized_network_type,
            tags=parsed_tags or None,
            endpoint=resolved_endpoint,
            region=resolved_region,
        )
    except _A2ARegisterError as exc:
        console.print(f"[red]❌ A2A registration failed: {exc.message}[/red]")
        if exc.diagnostics:
            console.print(json.dumps(exc.diagnostics, ensure_ascii=False, indent=2))
        raise typer.Exit(1) from exc

    console.print("[green]✅ A2A agent registered[/green]")
    console.print(f"[cyan]AgentId:[/cyan] {result.get('agent_id', '')}")
    diagnostics = result.get("diagnostics") or {}
    if diagnostics.get("request_id"):
        console.print(f"[cyan]RequestId:[/cyan] {diagnostics['request_id']}")


@add_app.command("harness")
def harness_command(
    name: str = typer.Option(
        ...,
        "--name",
        help="Harness name; the config is written to <name>.harness.json.",
    ),
    # --- core agent parameters ----------------------------------------------
    system_prompt: Optional[str] = typer.Option(
        None, "--system-prompt", help="Agent system prompt / instruction."
    ),
    description: Optional[str] = typer.Option(
        None,
        "--description",
        "--desc",
        help="Agent description (used at agent init, e.g. for A2A discovery).",
    ),
    model_name: Optional[str] = typer.Option(
        None, "--model-name", help="Reasoning model name."
    ),
    tools: Optional[str] = typer.Option(
        None, "--tools", help="Comma-separated built-in tool names (e.g. web_search)."
    ),
    skills: Optional[str] = typer.Option(
        None, "--skills", help="Comma-separated skill hub names."
    ),
    runtime: Optional[str] = typer.Option(
        None, "--runtime", help=f"Agent runtime backend ({' | '.join(_RUNTIMES)})."
    ),
    structured_tool_calls: Optional[bool] = typer.Option(
        None,
        "--structured-tool-calls/--no-structured-tool-calls",
        help="Ask the model to return executable structured tool calls.",
    ),
    include_tools_every_turn: Optional[bool] = typer.Option(
        None,
        "--include-tools-every-turn/--reuse-tool-context",
        help="Include tool definitions on every model turn.",
    ),
    registry: Optional[str] = typer.Option(
        None,
        "--registry",
        help=(
            'A2A registry URI, "default", or "disabled", e.g. '
            '"agentkit://a2a-registry?space_id=xxx&top_k=3" or '
            '"uni-registry://reg-xxx?endpoint=https://registry.example".'
        ),
    ),
    registry_id: Optional[str] = typer.Option(
        None, "--registry-id", help="UniRegistry instance ID."
    ),
    registry_record_type: Optional[str] = typer.Option(
        None,
        "--registry-record-type",
        help="UniRegistry record type for A2A Agent Cards.",
    ),
    registry_space_id: Optional[str] = typer.Option(
        None, "--registry-space-id", help="AgentKit A2A SpaceId."
    ),
    registry_space_name: Optional[str] = typer.Option(
        None,
        "--registry-space-name",
        help="AgentKit A2A space name. Resolved to space_id via ListA2aSpaces.",
    ),
    registry_top_k: Optional[int] = typer.Option(
        None,
        "--registry-top-k",
        help="Number of candidate AgentCards to retrieve from the registry.",
    ),
    registry_endpoint: Optional[str] = typer.Option(
        None, "--registry-endpoint", help="AgentKit OpenAPI endpoint for A2A registry."
    ),
    registry_region: Optional[str] = typer.Option(
        None, "--registry-region", help="AgentKit OpenAPI region."
    ),
    # --- A2A registry registration action -----------------------------------
    register_self: bool = typer.Option(
        False,
        "--register-self",
        help="Register this deployed harness Runtime Agent from harness.json.",
    ),
    register_space_id: Optional[str] = typer.Option(
        None,
        "--register-space-id",
        help="A2A registry space id for registration. Defaults to registry.space_id.",
    ),
    register_space_name: Optional[str] = typer.Option(
        None,
        "--register-space-name",
        help="A2A registry space name for registration. Resolved via ListA2aSpaces.",
    ),
    register_network_type: str = typer.Option(
        "public",
        "--register-network-type",
        help="Runtime network address to register: public or private.",
    ),
    register_tag: list[str] = typer.Option(
        [],
        "--register-tag",
        help="A2A agent tag in KEY=VALUE form. Can be repeated.",
    ),
    register_endpoint: Optional[str] = typer.Option(
        None,
        "--register-endpoint",
        help="AgentKit OpenAPI endpoint for CreateA2aAgent.",
    ),
    register_region: Optional[str] = typer.Option(
        None,
        "--register-region",
        help="AgentKit OpenAPI region. Defaults to AGENTKIT_REGION/VOLCENGINE_REGION/cn-beijing.",
    ),
    # --- component backend types --------------------------------------------
    knowledgebase_type: Optional[str] = typer.Option(
        None, "--knowledgebase-type", help="Knowledge base backend."
    ),
    long_term_memory_type: Optional[str] = typer.Option(
        None, "--long-term-memory-type", help="Long-term memory backend."
    ),
    short_term_memory_type: Optional[str] = typer.Option(
        None, "--short-term-memory-type", help="Short-term memory backend."
    ),
    # --- knowledge base connection params -----------------------------------
    knowledgebase_project: Optional[str] = typer.Option(
        None, "--knowledgebase-project", help="Knowledge base `project`."
    ),
    knowledgebase_region: Optional[str] = typer.Option(
        None, "--knowledgebase-region", help="Knowledge base `region`."
    ),
    knowledgebase_host: Optional[str] = typer.Option(
        None, "--knowledgebase-host", help="Knowledge base `host`."
    ),
    knowledgebase_port: Optional[str] = typer.Option(
        None, "--knowledgebase-port", help="Knowledge base `port`."
    ),
    knowledgebase_username: Optional[str] = typer.Option(
        None, "--knowledgebase-username", help="Knowledge base `username`."
    ),
    knowledgebase_password: Optional[str] = typer.Option(
        None, "--knowledgebase-password", help="Knowledge base `password`."
    ),
    knowledgebase_use_ssl: Optional[str] = typer.Option(
        None, "--knowledgebase-use-ssl", help="Knowledge base `use_ssl` (true/false)."
    ),
    knowledgebase_cert_path: Optional[str] = typer.Option(
        None, "--knowledgebase-cert-path", help="Knowledge base `cert_path`."
    ),
    knowledgebase_secret_token: Optional[str] = typer.Option(
        None, "--knowledgebase-secret-token", help="Knowledge base `secret_token`."
    ),
    knowledgebase_db: Optional[str] = typer.Option(
        None, "--knowledgebase-db", help="Knowledge base `db`."
    ),
    # --- long-term memory connection params ---------------------------------
    long_term_memory_project: Optional[str] = typer.Option(
        None, "--long-term-memory-project", help="Long-term memory `project`."
    ),
    long_term_memory_region: Optional[str] = typer.Option(
        None, "--long-term-memory-region", help="Long-term memory `region`."
    ),
    long_term_memory_host: Optional[str] = typer.Option(
        None, "--long-term-memory-host", help="Long-term memory `host`."
    ),
    long_term_memory_port: Optional[str] = typer.Option(
        None, "--long-term-memory-port", help="Long-term memory `port`."
    ),
    long_term_memory_username: Optional[str] = typer.Option(
        None, "--long-term-memory-username", help="Long-term memory `username`."
    ),
    long_term_memory_password: Optional[str] = typer.Option(
        None, "--long-term-memory-password", help="Long-term memory `password`."
    ),
    long_term_memory_db: Optional[str] = typer.Option(
        None, "--long-term-memory-db", help="Long-term memory `db`."
    ),
    long_term_memory_api_key: Optional[str] = typer.Option(
        None, "--long-term-memory-api-key", help="Long-term memory `api_key`."
    ),
    long_term_memory_api_key_id: Optional[str] = typer.Option(
        None, "--long-term-memory-api-key-id", help="Long-term memory `api_key_id`."
    ),
    long_term_memory_project_id: Optional[str] = typer.Option(
        None, "--long-term-memory-project-id", help="Long-term memory `project_id`."
    ),
    long_term_memory_base_url: Optional[str] = typer.Option(
        None, "--long-term-memory-base-url", help="Long-term memory `base_url`."
    ),
    # --- short-term memory connection params --------------------------------
    short_term_memory_host: Optional[str] = typer.Option(
        None, "--short-term-memory-host", help="Short-term memory `host`."
    ),
    short_term_memory_port: Optional[str] = typer.Option(
        None, "--short-term-memory-port", help="Short-term memory `port`."
    ),
    short_term_memory_user: Optional[str] = typer.Option(
        None, "--short-term-memory-user", help="Short-term memory `user`."
    ),
    short_term_memory_password: Optional[str] = typer.Option(
        None, "--short-term-memory-password", help="Short-term memory `password`."
    ),
    short_term_memory_database: Optional[str] = typer.Option(
        None, "--short-term-memory-database", help="Short-term memory `database`."
    ),
    short_term_memory_charset: Optional[str] = typer.Option(
        None, "--short-term-memory-charset", help="Short-term memory `charset`."
    ),
    # --- optional OAuth2 / JWT auth -----------------------------------------
    discovery_url: Optional[str] = typer.Option(
        None, "--discovery-url", help="OIDC discovery URL; enables OAuth2/JWT auth."
    ),
    allowed_id: Optional[str] = typer.Option(
        None, "--allowed-id", help="Comma-separated allowed client IDs for OAuth2/JWT."
    ),
    directory: str = typer.Option(
        ".",
        "--directory",
        help="Directory to write <name>.harness.json into.",
    ),
):
    """Create or update a harness config file ``<name>.harness.json``.

    Each option SETS its value; ``--tools`` / ``--skills`` / ``--allowed-id``
    take comma-separated lists. Connection params are written under their
    component section alongside its ``type``. Re-running for the same ``--name``
    merges options into the existing file.
    """
    if not _NAME_RE.match(name):
        console.print(
            f"[red]Error: harness name '{name}' contains invalid characters. "
            "Only letters, numbers, hyphens, and underscores are allowed.[/red]"
        )
        raise typer.Exit(1)

    agent_name = _derive_agent_name(name)
    if agent_name != name:
        console.print(
            f"[yellow]ℹ Once deployed, the agent will be named "
            f"'{agent_name}' instead of '{name}'.[/yellow]"
        )

    _validate_choice("--runtime", runtime, _RUNTIMES)
    _validate_choice("--knowledgebase-type", knowledgebase_type, _KNOWLEDGEBASE_TYPES)
    _validate_choice(
        "--long-term-memory-type", long_term_memory_type, _LONG_TERM_MEMORY_TYPES
    )
    _validate_choice(
        "--short-term-memory-type", short_term_memory_type, _SHORT_TERM_MEMORY_TYPES
    )

    target = Path(directory).resolve() / f"{name}.harness.json"
    if target.exists() and not target.is_file():
        console.print(f"[red]Error: '{target}' exists but is not a file.[/red]")
        raise typer.Exit(1)

    # Start from the existing file (merge) or a minimal default scaffold.
    data = _load_spec(target)
    if not data:
        data = {
            "harness_name": name,
            "runtime": "adk",
            "short_term_memory": {"type": "local"},
        }
    data["harness_name"] = name

    if model_name is not None:
        model = data.get("model")
        data["model"] = model if isinstance(model, dict) else {}
        data["model"]["name"] = model_name
    if system_prompt is not None:
        data["system_prompt"] = system_prompt
    if description is not None:
        data["description"] = description
    if runtime is not None:
        data["runtime"] = runtime
    if structured_tool_calls is not None:
        data["structured_tool_calls"] = structured_tool_calls
    if include_tools_every_turn is not None:
        data["include_tools_every_turn"] = include_tools_every_turn
    tools_list = _split_csv(tools)
    if tools_list is not None:
        data["tools"] = tools_list
    skills_list = _split_csv(skills)
    if skills_list is not None:
        data["skills"] = skills_list

    # Component sections: backend `type` plus its connection params.
    component_params: dict[str, dict[str, Optional[str]]] = {
        "knowledgebase": {
            "type": knowledgebase_type,
            "project": knowledgebase_project,
            "region": knowledgebase_region,
            "host": knowledgebase_host,
            "port": knowledgebase_port,
            "username": knowledgebase_username,
            "password": knowledgebase_password,
            "use_ssl": knowledgebase_use_ssl,
            "cert_path": knowledgebase_cert_path,
            "secret_token": knowledgebase_secret_token,
            "db": knowledgebase_db,
        },
        "long_term_memory": {
            "type": long_term_memory_type,
            "project": long_term_memory_project,
            "region": long_term_memory_region,
            "host": long_term_memory_host,
            "port": long_term_memory_port,
            "username": long_term_memory_username,
            "password": long_term_memory_password,
            "db": long_term_memory_db,
            "api_key": long_term_memory_api_key,
            "api_key_id": long_term_memory_api_key_id,
            "project_id": long_term_memory_project_id,
            "base_url": long_term_memory_base_url,
        },
        "short_term_memory": {
            "type": short_term_memory_type,
            "host": short_term_memory_host,
            "port": short_term_memory_port,
            "user": short_term_memory_user,
            "password": short_term_memory_password,
            "database": short_term_memory_database,
            "charset": short_term_memory_charset,
        },
    }
    for component, params in component_params.items():
        for param, value in params.items():
            if value is None:
                continue
            section = data.get(component)
            if not isinstance(section, dict):
                section = {}
                data[component] = section
            section[param] = value

    # Optional auth block (presence of either flag enables OAuth2/JWT).
    allowed_ids = _split_csv(allowed_id)
    if discovery_url is not None or allowed_ids is not None:
        auth = data.get("auth")
        auth = auth if isinstance(auth, dict) else {}
        if discovery_url is not None:
            auth["discovery_url"] = discovery_url
        if allowed_ids is not None:
            auth["allowed_ids"] = allowed_ids
        data["auth"] = auth

    try:
        _apply_registry_config(
            data,
            registry,
            registry_id,
            registry_record_type,
            registry_space_id,
            registry_space_name,
            registry_top_k,
            registry_endpoint,
            registry_region,
        )
    except _A2ARegisterError as exc:
        console.print(
            f"[red]Error: failed to configure A2A registry: {exc.message}[/red]"
        )
        if exc.diagnostics:
            console.print(json.dumps(exc.diagnostics, ensure_ascii=False, indent=2))
        raise typer.Exit(1) from exc
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc

    _prune(data)
    _write_spec(target, data)
    console.print(f"[green]✓ Wrote harness config: {target}[/green]")

    if register_self:
        entry = _resolve_self_register_entry(directory, name)
        register_resolved_region = (
            register_region
            or os.getenv("AGENTKIT_REGION")
            or os.getenv("VOLCENGINE_REGION")
            or "cn-beijing"
        )
        register_resolved_endpoint = register_endpoint or _default_agentkit_endpoint(
            register_resolved_region
        )
        try:
            resolved_space_id = _resolve_register_space_id(
                data,
                register_space_id,
                register_space_name,
                endpoint=register_resolved_endpoint,
                region=register_resolved_region,
            )
        except _A2ARegisterError as exc:
            console.print(
                f"[red]Error: failed to resolve A2A space name: {exc.message}[/red]"
            )
            if exc.diagnostics:
                console.print(json.dumps(exc.diagnostics, ensure_ascii=False, indent=2))
            raise typer.Exit(1) from exc
        except ValueError as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(f"[cyan]Harness URL:[/cyan] {entry['url']}")
        _register_a2a_runtime_agent(
            subject=f"harness '{name}'",
            space_id=resolved_space_id,
            runtime_id=str(entry["runtime_id"]),
            network_type=register_network_type,
            tags=register_tag,
            endpoint=register_resolved_endpoint,
            region=register_resolved_region,
        )


# Credential types accepted by ``agentkit add credential``. Only ``api-key`` is
# wired up today; the value maps to the API's ``AuthType`` field.
_CREDENTIAL_AUTH_TYPES = {"api-key": "ApiKey"}


@add_app.command("credential")
def credential_command(
    type_: str = typer.Option(
        ...,
        "--type",
        help=f"Credential type ({' | '.join(_CREDENTIAL_AUTH_TYPES)}).",
    ),
    name: str = typer.Option(
        ..., "--name", help="Credential name (used as the API key name)."
    ),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", help="API key value (required for --type api-key)."
    ),
    region: Optional[str] = typer.Option(
        None,
        "--region",
        help=(
            "Region override for this command (e.g. cn-beijing, cn-shanghai). "
            "Defaults to VOLCENGINE_AGENTKIT_REGION/VOLCENGINE_REGION/global config."
        ),
    ),
):
    """Add a credential by creating an inbound auth config.

    ``--type api-key`` registers ``--api-key`` under ``--name`` so deployed
    agents can authenticate inbound requests with that key.
    """
    auth_type = _CREDENTIAL_AUTH_TYPES.get(type_)
    if auth_type is None:
        console.print(
            f"[red]Error: invalid --type '{type_}'. "
            f"Allowed: {', '.join(_CREDENTIAL_AUTH_TYPES)}[/red]"
        )
        raise typer.Exit(1)
    if not name.strip():
        console.print("[red]Error: --name must not be empty.[/red]")
        raise typer.Exit(1)
    if not api_key:
        console.print("[red]Error: --api-key is required for --type api-key.[/red]")
        raise typer.Exit(1)

    from agentkit.sdk.identity.client import AgentkitIdentityClient
    from agentkit.sdk.identity import types as it

    client = AgentkitIdentityClient(region=(region or "").strip())
    request = it.CreateInboundAuthConfigRequest(
        auth_type=auth_type,
        config_name=name,
        api_key_auth_configs=[it.ApiKeyAuthConfig(api_key_name=name, api_key=api_key)],
    )
    response = client.create_inbound_auth_config(request)

    console.print(f"[green]✓ Created credential '{name}'[/green]")
    if response.inbound_auth_config_id:
        console.print(f"  InboundAuthConfigId: {response.inbound_auth_config_id}")
