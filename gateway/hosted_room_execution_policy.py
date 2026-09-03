"""Target-issued execution authority for RoomLink member turns."""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


POLICY_VERSION = 1
MAX_POLICY_TOOLSETS = 128
MAX_POLICY_ITERATIONS = (1 << 53) - 1
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


# The Hermes analogue of Claude Code's ``COORDINATOR_MODE_ALLOWED_TOOLS``
# (decoded from the binary: ``{Agent, SendMessage, ListAgents, Workflow,
# TaskStop, StructuredOutput}``). CC runs its coordinator through
# ``applyCoordinatorToolFilter`` — a runtime *intersection* of the session's
# tools with this allow-set, so the coordinator is *orchestration-only*: it can
# delegate, message teammates, orchestrate and stop tasks, and NOTHING else (no
# Read/Bash/Grep/Edit/Write — strictly stronger than read-only). Our decider is
# the same role, so its member turn is restricted to the messaging/delegation/
# orchestration toolsets below and can never touch the filesystem, a terminal,
# code execution, a browser, or computer-use. ``bot_room`` (the room voice) is
# mandatory — it is the SendMessage analogue that lets the decider @mention
# teammates and speak the single external voice, and dispatch is expressed by
# that message, not by a write tool.
ORCHESTRATION_ONLY_TOOLSETS = frozenset(
    {
        "bot_room",  # SendMessage — the room voice; @mentions ARE the dispatch
        "delegation",  # Agent — delegate_task
        "todo",  # Workflow — plan/track the orchestration
        "clarify",  # steer teammates with clarifying questions
    }
)


def orchestration_only_toolsets(base: Iterable[str] | None) -> list[str]:
    """Intersect a member's toolset with the orchestration-only allow-set.

    Faithful to CC's ``applyCoordinatorToolFilter``: a decider keeps only the
    orchestration toolsets it already had (never gaining new ones), and always
    retains ``bot_room`` so it can still speak/@mention. ``base is None`` means
    "every toolset enabled", which collapses to exactly the allow-set.
    """

    if base is None:
        selected = set(ORCHESTRATION_ONLY_TOOLSETS)
    else:
        selected = {
            str(name) for name in base if str(name) in ORCHESTRATION_ONLY_TOOLSETS
        }
    selected.add("bot_room")
    return sorted(selected)


class RoomExecutionPolicyError(ValueError):
    """A RoomLink execution policy is malformed or no longer current."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _identifier(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 128
        or _IDENTIFIER_RE.fullmatch(normalized) is None
    ):
        raise RoomExecutionPolicyError(f"{field} is invalid")
    return normalized


@dataclass(frozen=True)
class RoomExecutionPolicy:
    """Immutable target policy applied at the agent and approval boundaries."""

    version: int
    target_profile: str
    enabled_toolsets: tuple[str, ...]
    approval_mode: str
    max_iterations: int
    policy_digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RoomExecutionPolicy":
        required = {
            "version",
            "target_profile",
            "enabled_toolsets",
            "approval_mode",
            "max_iterations",
            "policy_digest",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise RoomExecutionPolicyError("execution policy fields are invalid")
        if value["version"] != POLICY_VERSION:
            raise RoomExecutionPolicyError("execution policy version is unsupported")
        target_profile = _identifier(value["target_profile"], field="target_profile")
        raw_toolsets = value["enabled_toolsets"]
        if (
            not isinstance(raw_toolsets, list)
            or not raw_toolsets
            or len(raw_toolsets) > MAX_POLICY_TOOLSETS
        ):
            raise RoomExecutionPolicyError("enabled_toolsets are invalid")
        toolsets = tuple(
            sorted(_identifier(item, field="enabled_toolset") for item in raw_toolsets)
        )
        if len(set(toolsets)) != len(toolsets) or "bot_room" not in toolsets:
            raise RoomExecutionPolicyError("enabled_toolsets are invalid")
        approval_mode = str(value["approval_mode"] or "").strip().lower()
        if approval_mode not in {"manual", "smart", "off"}:
            raise RoomExecutionPolicyError("approval_mode is invalid")
        max_iterations = value["max_iterations"]
        if (
            isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or not 1 <= max_iterations <= MAX_POLICY_ITERATIONS
        ):
            raise RoomExecutionPolicyError("max_iterations is invalid")
        unsigned = {
            "version": POLICY_VERSION,
            "target_profile": target_profile,
            "enabled_toolsets": list(toolsets),
            "approval_mode": approval_mode,
            "max_iterations": max_iterations,
        }
        expected = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
        supplied = str(value["policy_digest"] or "").strip().lower()
        if supplied != expected:
            raise RoomExecutionPolicyError(
                "policy_digest does not match the execution policy"
            )
        return cls(**unsigned, policy_digest=supplied)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "target_profile": self.target_profile,
            "enabled_toolsets": list(self.enabled_toolsets),
            "approval_mode": self.approval_mode,
            "max_iterations": self.max_iterations,
            "policy_digest": self.policy_digest,
        }


def execution_policy_mapping(
    *,
    target_profile: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the effective API-server policy from the target's own config."""

    if config is None:
        from gateway.run import _load_gateway_config

        config = _load_gateway_config()
    if not isinstance(config, Mapping):
        raise RoomExecutionPolicyError("gateway config is invalid")

    from hermes_cli.config import resolve_turn_limit
    from hermes_cli.tools_config import _get_platform_tools
    from tools.approval import _YOLO_MODE_FROZEN, _normalize_approval_mode

    toolsets = sorted({*_get_platform_tools(dict(config), "api_server"), "bot_room"})
    agent = config.get("agent") if isinstance(config.get("agent"), Mapping) else {}
    approvals = (
        config.get("approvals") if isinstance(config.get("approvals"), Mapping) else {}
    )
    max_iterations = min(
        resolve_turn_limit(agent.get("max_turns")),
        MAX_POLICY_ITERATIONS,
    )
    approval_mode = (
        "off"
        if _YOLO_MODE_FROZEN
        else _normalize_approval_mode(approvals.get("mode", "manual"))
    )
    unsigned = {
        "version": POLICY_VERSION,
        "target_profile": _identifier(target_profile, field="target_profile"),
        "enabled_toolsets": toolsets,
        "approval_mode": approval_mode,
        "max_iterations": max_iterations,
    }
    value = {
        **unsigned,
        "policy_digest": hashlib.sha256(_canonical_json(unsigned)).hexdigest(),
    }
    return RoomExecutionPolicy.from_mapping(value).as_mapping()


_CURRENT_POLICY: ContextVar[RoomExecutionPolicy | None] = ContextVar(
    "hosted_room_execution_policy",
    default=None,
)


def bind_room_execution_policy(policy: RoomExecutionPolicy) -> Token:
    return _CURRENT_POLICY.set(policy)


def reset_room_execution_policy(token: Token) -> None:
    _CURRENT_POLICY.reset(token)


def current_room_execution_policy() -> RoomExecutionPolicy | None:
    return _CURRENT_POLICY.get()


__all__ = [
    "MAX_POLICY_ITERATIONS",
    "ORCHESTRATION_ONLY_TOOLSETS",
    "POLICY_VERSION",
    "RoomExecutionPolicy",
    "RoomExecutionPolicyError",
    "bind_room_execution_policy",
    "current_room_execution_policy",
    "execution_policy_mapping",
    "orchestration_only_toolsets",
    "reset_room_execution_policy",
]
