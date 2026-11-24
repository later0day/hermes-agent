"""``approval`` request params and answers for the ``access`` (folder) and ``network`` (site) kinds."""

import pytest
from pydantic import ValidationError

from tui_gateway.contracts import registry as contracts
from tui_gateway.contracts.server_requests import ApprovalRequestParams, ApprovalResult
from tui_gateway.contracts.canonical_sessions import CanonicalApprovalParams, CanonicalPendingPrompt

_IDS = {"session_id": "s1", "request_id": "r1"}


def _problem(params: dict) -> str | None:
    _, problem = contracts.validate_params(contracts.SERVER_REQUESTS["approval"], {**_IDS, **params})
    return problem


def test_command_approval_needs_no_kind():
    assert _problem({"command": "rm -rf build", "description": "dangerous command"}) is None
    assert ApprovalRequestParams.model_validate({**_IDS, "command": "ls"}).kind is None


def test_folder_and_site_cards_validate():
    assert _problem({"kind": "access", "path": "C:\\docs", "access": "read_write",
                     "risk": "The agent can change and delete files in this folder."}) is None
    assert _problem({"kind": "network", "host": "api.keenable.ai", "label": "Web search (api.keenable.ai)",
                     "choices": ["once", "always", "deny"]}) is None

    folder = ApprovalRequestParams.model_validate({**_IDS, "kind": "access", "path": "/srv", "access": "read"})
    assert (folder.kind, folder.path, folder.access) == ("access", "/srv", "read")
    site = ApprovalRequestParams.model_validate({**_IDS, "kind": "network", "host": "example.com", "label": "Example"})
    assert (site.kind, site.host, site.label) == ("network", "example.com", "Example")


@pytest.mark.parametrize("params", [{"kind": "printer"}, {"kind": "access", "path": "/srv", "access": "rw"}])
def test_values_outside_the_enums_are_refused(params):
    with pytest.raises(ValidationError):
        ApprovalRequestParams.model_validate({**_IDS, **params})


def test_a_deny_reason_rides_the_answer_and_approval_respond():
    assert ApprovalResult.model_validate({"choice": "deny", "reason": "Not this site"}).reason == "Not this site"
    assert ApprovalResult.model_validate({"choice": "once"}).reason is None

    respond = contracts.METHODS["approval.respond"]
    _, problem = contracts.validate_params(respond, {**_IDS, "choice": "deny", "reason": "Not this site"})
    assert problem is None
    _, problem = contracts.validate_params(respond, {**_IDS, "choice": "deny", "why": "x"})
    assert problem is not None and "why" in problem


@pytest.mark.parametrize("params", [{"kind": "access"}, {"kind": "access", "host": "example.com"},
                                    {"kind": "access", "path": " "},
                                    {"kind": "network"}, {"kind": "network", "path": "/srv"}])
def test_a_folder_or_site_card_without_its_target_is_refused(params):
    """An ``access`` card names a ``path`` and a ``network`` card names a ``host``; a request without
    it would offer Allow for something the user cannot read."""
    with pytest.raises(ValidationError):
        ApprovalRequestParams.model_validate({**_IDS, **params})


@pytest.mark.parametrize("kind, target", [
    ("access", {"path": "/srv/Project  Files", "access": "read_write", "risk": "Write risk"}),
    ("network", {"host": "downloads.example", "label": "Download  mirror", "risk": "Network risk"}),
])
def test_canonical_pending_prompt_retains_folder_and_site_card_fields(kind, target):
    prompt = CanonicalPendingPrompt.model_validate({"kind": "approval", "prompt_id": "card-1",
        "execution_generation": 3, "description": "E2E approval", "choices": ["once", "deny"],
        "approval_kind": kind, **target})

    assert prompt.approval_kind == kind
    assert {field: getattr(prompt, field) for field in target} == target


def test_canonical_access_value_and_unknown_fields_remain_closed():
    with pytest.raises(ValidationError):
        CanonicalPendingPrompt.model_validate({"kind": "approval", "prompt_id": "card-1", "execution_generation": 3,
            "choices": ["once", "deny"], "approval_kind": "access", "path": "/srv", "access": "execute"})
    with pytest.raises(ValidationError):
        CanonicalPendingPrompt.model_validate({"kind": "approval", "prompt_id": "card-1", "execution_generation": 3,
            "choices": ["once", "deny"], "approval_kind": "network", "pattern_key": "private"})


def test_canonical_answer_can_carry_a_reason_without_changing_command_answer_shape():
    answer = CanonicalApprovalParams.model_validate({"session_id": "s1", "execution_generation": 3,
        "prompt_id": "card-1", "choice": "deny", "reason": "Not allowed"})
    command = CanonicalApprovalParams.model_validate({"session_id": "s1", "execution_generation": 3,
        "prompt_id": "command-1", "choice": "once"})
    assert answer.reason == "Not allowed"
    assert command.model_dump(exclude_none=True) == {"session_id": "s1", "execution_generation": 3,
        "prompt_id": "command-1", "choice": "once"}
