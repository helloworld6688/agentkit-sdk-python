# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
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

"""Offline error-mapping tests for ``BaseServiceClient._invoke_api``.

Verifies the exception-discipline standard at the HTTP-owning layer:

* a transport error raised by ``self.json`` is wrapped as ``NetworkError``
  (typed, with the original cause chained), not surfaced raw;
* a malformed response body becomes a domain ``ApiError`` rather than a raw
  ``json.JSONDecodeError`` bubbling out of the SDK.

No network is performed: ``self.json`` is replaced with a stub. Explicit
credentials are supplied so the vefaas auto-refresh path is never taken.
"""

from __future__ import annotations

import json
import types

# Import the toolkit package first to fully initialise the import graph before
# touching ``agentkit.client`` (the package wiring is order-sensitive).
import agentkit.toolkit  # noqa: F401

import pytest
import requests

from agentkit.auth.errors import NetworkError
from agentkit.client.base_service_client import BaseServiceClient
from agentkit.platform.configuration import VolcConfiguration
from agentkit.toolkit.errors import ApiError


class _Req:
    """Minimal stand-in for a Pydantic request model."""

    def model_dump(self, *, by_alias: bool = True, exclude_none: bool = True):
        return {}


class _Resp:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _DummyClient(BaseServiceClient):
    API_ACTIONS = {"TestAction": "TestAction"}

    def __init__(self) -> None:
        # Explicit ak/sk => _explicit_credentials is True => no vefaas refresh,
        # so construction needs neither a credential file nor network.
        super().__init__(
            service="agentkit",
            access_key="AK_LOCAL_TEST_ONLY",
            secret_key="SK_LOCAL_TEST_ONLY",
            region="cn-beijing",
            service_name="dummy",
            platform_config=VolcConfiguration(),
        )


@pytest.fixture
def client():
    c = _DummyClient()
    # Sanity: the explicit-credential path must avoid auto-refresh entirely.
    assert c._explicit_credentials is True
    assert c._should_auto_refresh_vefaas_credentials() is False
    return c


def _set_json(client, fn):
    client.json = types.MethodType(fn, client)


def test_invoke_api_connection_error_becomes_networkerror(client):
    def _json(self, api, params, body):
        raise requests.exceptions.ConnectionError("socket reset")

    _set_json(client, _json)

    with pytest.raises(NetworkError) as excinfo:
        client._invoke_api("TestAction", _Req(), _Resp)

    assert isinstance(excinfo.value.__cause__, requests.exceptions.ConnectionError)
    # Transport detail must not leak into the domain message.
    assert "socket reset" not in str(excinfo.value)


def test_invoke_api_malformed_body_becomes_apierror_not_jsondecodeerror(client):
    def _json(self, api, params, body):
        return "this-is-not-valid-json{"

    _set_json(client, _json)

    with pytest.raises(ApiError) as excinfo:
        client._invoke_api("TestAction", _Req(), _Resp)

    # The raw decode error is chained but never the type that escapes the SDK.
    assert not isinstance(excinfo.value, json.JSONDecodeError)
    assert isinstance(excinfo.value.__cause__, (ValueError, TypeError))
    assert "JSONDecodeError" not in str(excinfo.value)


def test_invoke_api_error_metadata_becomes_apierror_with_code(client):
    def _json(self, api, params, body):
        return json.dumps(
            {
                "ResponseMetadata": {
                    "Action": "TestAction",
                    "RequestId": "req-123",
                    "Error": {"Code": "SomeBizError", "Message": "boom"},
                }
            }
        )

    _set_json(client, _json)

    with pytest.raises(ApiError) as excinfo:
        client._invoke_api("TestAction", _Req(), _Resp)

    assert excinfo.value.error_code == "SomeBizError"
    assert excinfo.value.request_id == "req-123"
    assert excinfo.value.action == "TestAction"
    assert "request_id=req-123" in str(excinfo.value)
    assert "action=TestAction" in str(excinfo.value)
