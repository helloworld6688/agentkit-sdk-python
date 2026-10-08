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

"""Offline regression guards for the unified exception hierarchy.

The engineering-standards refactor introduced a single stdlib-only leaf root,
``agentkit.errors.AgentKitError``. Every domain error -- auth (``AuthError`` /
``NetworkError``) and toolkit/API (``AgentKitError`` / ``ApiError``) -- must
descend from that single root so a caller can ``except agentkit.errors.AgentKitError``
to catch any AgentKit-originated failure, while ``agentkit.auth`` keeps its
dependency-free posture (importing only ``agentkit.errors``, never ``toolkit``).

These tests also pin the ``agentkit.auth._redact`` -> ``agentkit.utils.redact``
re-export shim so existing importers keep working.
"""

from __future__ import annotations

import agentkit.errors as root
from agentkit.auth.errors import AuthError, NetworkError, SsoError
from agentkit.toolkit.errors import AgentKitError as ToolkitAgentKitError
from agentkit.toolkit.errors import ApiError


def test_all_domain_errors_descend_from_the_single_root():
    # The true single root is agentkit.errors.AgentKitError.
    assert issubclass(AuthError, root.AgentKitError)
    assert issubclass(NetworkError, root.AgentKitError)
    assert issubclass(SsoError, root.AgentKitError)
    assert issubclass(ApiError, root.AgentKitError)
    assert issubclass(ToolkitAgentKitError, root.AgentKitError)


def test_toolkit_root_is_a_subclass_not_the_root_itself():
    # toolkit.errors.AgentKitError specialises the leaf root (adds error_code /
    # message), so it is a child of -- not identical to -- agentkit.errors.AgentKitError.
    assert ToolkitAgentKitError is not root.AgentKitError
    assert issubclass(ToolkitAgentKitError, root.AgentKitError)


def test_auth_errors_are_siblings_of_toolkit_root_under_the_shared_root():
    # auth errors descend from the shared root directly, NOT from the toolkit
    # specialisation -- this is what keeps agentkit.auth free of any toolkit import.
    assert not issubclass(AuthError, ToolkitAgentKitError)
    assert root.AgentKitError in AuthError.__mro__


def test_catching_the_root_catches_every_domain_error():
    for exc in (
        AuthError("a"),
        NetworkError("b"),
        ApiError("c"),
        ToolkitAgentKitError("d"),
    ):
        try:
            raise exc
        except root.AgentKitError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError(f"{type(exc).__name__} not caught by root")


def test_api_error_carries_optional_error_code():
    assert ApiError("boom").error_code is None
    assert ApiError("boom", error_code="SomeBizError").error_code == "SomeBizError"
    error = ApiError("boom", request_id="req-123", action="TestAction")
    assert error.request_id == "req-123"
    assert error.action == "TestAction"


def test_redact_shim_re_exports_canonical_implementation():
    from agentkit.auth._redact import mask as shim_mask
    from agentkit.auth._redact import redact as shim_redact
    from agentkit.utils.redact import mask as canon_mask
    from agentkit.utils.redact import redact as canon_redact

    assert shim_redact is canon_redact
    assert shim_mask is canon_mask
