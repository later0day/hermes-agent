"""Profile-scoped helpers: profile discovery fallback, profile dir/MCP-server writes,
the profile/config scope context managers, skills-hub and tools/analytics catalog helpers.
"""

import logging
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import threading
import time

from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException
from hermes_yaml import YAMLError

from hermes_cli.config import DEFAULT_CONFIG, get_process_hermes_home
from hermes_cli.config_backend import config_exists, read_config_doc
from hermes_cli.web_server_gateway import _ACTION_LOG_FILES

# Same logger the code used before extraction (record parity).
_log = logging.getLogger("hermes_cli.web_server")


def _safe(callable_: Callable[[], Any], default: Any) -> Any:
    try:
        return callable_()
    except Exception:
        return default


def _is_current_profile(profile: Optional[str]) -> bool:
    """None/""/"current" all mean the dashboard's own profile."""
    requested = (profile or "").strip()
    return not requested or requested.lower() == "current"


@contextmanager
def _hermes_home_scope(path) -> Any:
    """Scope ``load_config``/``save_config`` (anything resolving ``get_hermes_home()`` at call
    time) to ``path`` for the block via the context-local HERMES_HOME override."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(path))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def serving_profile_name() -> str:
    """This process's OWN profile name — but only when that name provably resolves back
    to the process home.

    The dashboard SPA needs an explicit scope for requests it fires before the profile
    switcher has resolved: a destructive route now 400s on an unnamed profile as soon as
    the host serves more than one, and "" would otherwise mean "whichever home this
    process launched with" anyway. Naming it is only safe if the name cannot resolve
    ELSEWHERE, so a custom HERMES_HOME outside ``profiles/`` (``get_active_profile_name()``
    answers ``"custom"``) returns "" and keeps the old unnamed behaviour rather than
    risking a wrong-profile write.
    """
    from hermes_cli import profiles as profiles_mod
    try:
        name = (profiles_mod.get_active_profile_name() or "").strip()
        if not name or name == "custom":
            return ""
        return name if profiles_mod.profile_matches_home(name, get_process_hermes_home()) else ""
    except Exception:
        return ""


def _is_other_profile(profile: Optional[str]) -> bool:
    """True when ``profile`` names a profile other than this process's own."""
    if _is_current_profile(profile):
        return False
    try:
        target = _resolve_profile_dir(profile.strip())
    except HTTPException:
        return True
    return target.resolve() != get_process_hermes_home().resolve()


def _approval_mode_of(config: dict[str, Any]) -> str:
    """Normalize approvals.mode from an in-memory config document. Both sides of the
    broadcast comparison use in-memory documents: re-reading through the config cache after
    a save can serve the pre-save document when the replacement file collides on the
    (mtime_ns, size) cache key, suppressing the broadcast exactly when the mode changed."""
    from tools.approval_context import _normalize_approval_mode
    approvals = config.get("approvals")
    default_mode = (DEFAULT_CONFIG.get("approvals") or {}).get("mode", "manual")
    mode = approvals.get("mode", default_mode) if isinstance(approvals, dict) else default_mode
    return _normalize_approval_mode(mode)


def _broadcast_gateway_session_info() -> None:
    """Broadcast session.info on the in-process gateway when it's loaded (``sys.modules``
    guard, not an import: gateway never imported means no live sessions to notify)."""
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return
    try:
        server.broadcast_session_info()
    except Exception:
        _log.exception("session.info broadcast after config save failed")


_MODEL_ENTRY_METADATA = ("canonical_model", "reasoning_effort")


def _parse_model_entries(resp: Any) -> list[dict[str, str]]:
    """Model rows from an OpenAI-compatible ``/v1/models`` response as ``{"id": ..}`` dicts,
    keeping the alias metadata a gateway may advertise (``canonical_model``,
    ``reasoning_effort``). Flattening to bare ids lost that, so Desktop stored a reasoning
    alias as the literal upstream model (#93622). Accepts ``{"data": [{"id": ..}]}`` or a
    bare ``{"data": ["id", ..]}``; ``[]`` on any parse/HTTP error so a slightly
    non-standard endpoint never hard-blocks."""
    try:
        if not resp.is_success:
            return []
        payload = resp.json()
    except Exception:
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    entries: list[dict[str, str]] = []
    for item in data:
        model_id = str((item.get("id") if isinstance(item, dict) else item) or "").strip()
        if not model_id:
            continue
        entry = {"id": model_id}
        if isinstance(item, dict):
            for key in _MODEL_ENTRY_METADATA:
                value = str(item.get(key) or "").strip()
                if value:
                    entry[key] = value
        entries.append(entry)
    return entries


def _parse_model_ids(resp: Any) -> list[str]:
    """Bare model ids from a ``/v1/models`` response (see :func:`_parse_model_entries`)."""
    return [entry["id"] for entry in _parse_model_entries(resp)]


def _fallback_profile_entry(profiles_mod, name: str, home: Path, *, is_default: bool,
                            has_env: bool, gateway_running: Callable[[], bool]) -> dict[str, Any]:
    model, provider = _safe(lambda: profiles_mod._read_config_model(home), (None, None))
    meta = _safe(lambda: profiles_mod.read_profile_meta(home), {}).get
    return {
        "name": name, "path": str(home), "is_default": is_default, "model": model,
        "provider": provider, "has_env": has_env,
        "skill_count": _safe(lambda: profiles_mod._cached_skill_count(home), 0),
        "gateway_running": _safe(gateway_running, False),
        "description": meta("description", ""), "description_auto": meta("description_auto", False),
        "bot_title": meta("bot_title", ""), "ui_meta": meta("ui_meta", {}),
        "distribution_name": None, "distribution_version": None, "distribution_source": None,
        "has_alias": False}


def _fallback_profile_dicts(profiles_mod) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    default_home = profiles_mod._get_default_hermes_home()
    if default_home.is_dir():
        profiles.append(_fallback_profile_entry(
            profiles_mod, "default", default_home, is_default=True,
            has_env=(default_home / ".env").exists(),
            gateway_running=lambda: profiles_mod._check_gateway_running(default_home)))

    profiles_root = profiles_mod._get_profiles_root()
    if profiles_root.is_dir():
        # os.scandir (context-managed) rather than Path.iterdir: an exception mid-iteration
        # must not leak the directory fd — the sidebar polls every few seconds, so a leak
        # exhausts RLIMIT_NOFILE within days.
        with os.scandir(profiles_root) as scan:
            entries = sorted(scan, key=lambda e: e.name)
        for entry in entries:
            home = Path(entry.path)
            if not entry.is_dir() or not profiles_mod._PROFILE_ID_RE.match(entry.name):
                continue
            profiles.append(_fallback_profile_entry(
                profiles_mod, entry.name, home, is_default=False,
                has_env=_safe(lambda: (home / ".env").exists(), False),
                gateway_running=lambda home=home, name=entry.name: (
                    profiles_mod._check_gateway_running(home)
                    or profiles_mod._served_by_running_multiplexer(name))))
    return profiles


def _resolve_profile_dir(name: str) -> Path:
    """Validate ``name`` and resolve to its directory or raise an HTTPException."""
    from hermes_cli import profiles as profiles_mod
    try:
        profiles_mod.validate_profile_name(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(name):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' does not exist.")
    return profiles_mod.get_profile_dir(name)


# Skills & Tools endpoints accept an optional ``profile`` query param so the dashboard can
# manage ANY profile's skills/toolsets ("Set as active" only flips the sticky active_profile
# file for FUTURE invocations, which misled users into thinking toggles landed there).


_SKILLS_PROFILE_LOCK = threading.RLock()


@contextmanager
def _profile_scope(profile: Optional[str]):
    """``_config_profile_scope(profile)`` held under ``_SKILLS_PROFILE_LOCK``. Yields the profile dir
    for a named profile, None for the current one.

    The lock serializes every scoped request in this process. Several bodies still do a bare
    ``load_config``/``save_config`` read-modify-write without ``CONFIG_MUTATION_LOCK`` (MCP catalog
    install, platform toggles, the raw config editor, credential mirror scrubs), and the lock is
    what keeps them from losing a concurrent ``config_write_scope`` update.
    """
    with _config_profile_scope(profile) as scoped, _SKILLS_PROFILE_LOCK:
        yield scoped


@contextmanager
def _config_profile_scope(profile: Optional[str]):
    """Await-safe profile scope: the task-local HERMES_HOME contextvar PLUS the profile's secret
    scope, without the process-wide lock ``_profile_scope`` adds. None/""/"current" = no override.

    Home alone left ``get_secret`` on the dashboard process's ``os.environ`` - the DEFAULT
    profile's values - so ``GET /api/config?profile=B`` expanded B's ``${VAR}`` refs to the default
    profile's plaintext credentials and ``load_gateway_config()`` under B's home bridged B's YAML
    settings into the shared process env (``yaml_env_setter`` skips only under a secret scope).
    A request for another profile's home also flips this process to fail-closed multi-profile
    hosting (``tui_gateway/launch_profile_policy.py``), so any remaining unscoped read raises
    instead of borrowing; the dashboard's own profile then runs under its frozen-launch-env scope.

    Explicit names resolving to the process home retain current-profile semantics.
    Still enter the requested home so a nested scope cannot retain another profile.
    None/""/"current" mean the request's EFFECTIVE home: a task-local override (a secondary
    profile's native ticket, ``native_profile_scope``) is that profile, so it gets that profile's
    secret scope rather than the launch profile's.
    """
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from hermes_constants import get_hermes_home, get_hermes_home_override
    from tui_gateway.launch_profile_policy import activate_multi_profile_hosting, launch_secret_scope

    process_home = get_process_hermes_home()
    if _is_current_profile(profile):
        profile_dir = get_hermes_home() if get_hermes_home_override() else None
    else:
        profile_dir = _resolve_profile_dir(profile.strip())
    scoped = None if profile_dir is None or profile_dir.resolve() == process_home.resolve() else profile_dir
    if scoped is not None:
        activate_multi_profile_hosting()
        hydrate_profile_secret_sources(scoped)  # first call may block on the source's fetch
        secrets = build_profile_secret_scope(scoped)
    else:
        # The dashboard's own profile: its launch-env scope (live env + .env while single-profile, so
        # systemd / op-run injection keeps resolving; frozen at activation afterwards). Bound even
        # before any secondary is served so the request's credential source is decided HERE: a
        # concurrent first ``?profile=B`` request flips ``get_secret`` to fail closed mid-request,
        # and an unscoped launch request would then raise ``UnscopedSecretError`` on its next read.
        secrets = launch_secret_scope(process_home)
    with (_hermes_home_scope(profile_dir) if profile_dir is not None else nullcontext()):
        token = set_secret_scope(secrets, profile_home=str(profile_dir or process_home))
        try:
            yield scoped
        finally:
            reset_secret_scope(token)


# Terminal backend picker rows — GUI counterpart of terminal.backend. Keep in sync with
# tools/terminal_tool.py::_create_environment and the terminal.backend enum.

# --------------------------------------------------------------------------- Terminal execution backend
# picker — the GUI counterpart of terminal.backend in config.yaml. Each row carries a fast, defensive health
# probe (Docker daemon reachable, SSH host configured, Modal/Daytona credentials present) so the
# Capabilities panel can render Ready / Needs setup guidance instead of a bare enum (issues #57738 /
# #63783). Probes must never raise — a probe failure renders as a status, not a 500.
# ---------------------------------------------------------------------------
_TERMINAL_BACKENDS: list[dict[str, str]] = [
    dict(zip(("name", "label", "description"), row)) for row in (
        ("local", "Local", "Run commands directly on this machine. No isolation."),
        ("docker", "Docker / Podman",
         "Run commands in an isolated Docker or Podman container with a persistent workspace."),
        ("modal", "Modal", "Run commands in a Modal cloud sandbox."),
        ("daytona", "Daytona", "Run commands in a Daytona cloud sandbox."),
        ("ssh", "SSH", "Run commands on a remote host over SSH."))]


def _plugin_terminal_backend_rows() -> list[dict[str, str]]:
    """Picker rows for plugin-registered terminal backends (fail-soft)."""
    rows: list[dict[str, str]] = []
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()  # idempotent — plugin state may not be loaded yet
    except Exception:
        pass
    try:
        from agent.terminal_env_registry import list_providers
        for provider in list_providers():
            try:
                rows.append({"name": provider.name.strip().lower(), "label": provider.display_name,
                             "description": provider.description})
            except Exception:
                continue
    except Exception:
        return rows
    return rows


# Token / cost analytics helpers.
_AUX_COUNTERS = ("input_tokens", "output_tokens", "estimated_cost", "api_calls")


def _token_volume(row: dict[str, Any]) -> Any:
    return (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0)


def _aux_usage_rows(persistence, cutoff: float) -> list[dict[str, Any]]:
    """Per-(model, task) auxiliary usage within the window: the task-dimension rows
    (task != '') record_auxiliary_usage writes. [] when the store predates the task
    column (older DB opened read-only by newer code) or the read fails.

    See #23270.
    """
    try:
        return persistence.usage_analytics.auxiliary_usage_by_model_task(started_after=cutoff)
    except Exception:
        return []


def _merge_aux_into_by_model(
    by_model: list[dict[str, Any]], aux_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold aux usage rows into the sessions-derived per-model list. Aux usage lives only in
    session_model_usage (never in the sessions counters), so this cannot double-count;
    models that ONLY appear via aux calls (e.g. a vision model) get their own entry."""
    if not aux_rows:
        return by_model
    merged: dict[str, dict[str, Any]] = {row.get("model") or "unknown": row for row in by_model}
    for aux in aux_rows:
        model = aux.get("model") or "unknown"
        target = merged.setdefault(model, {"model": model, "input_tokens": 0, "output_tokens": 0,
                                           "estimated_cost": 0, "sessions": 0, "api_calls": 0})
        for key in _AUX_COUNTERS:
            target[key] = (target.get(key) or 0) + (aux.get(key) or 0)
        target.setdefault("aux_tasks", []).append(
            {"task": aux.get("task") or "", **{key: aux.get(key) or 0 for key in _AUX_COUNTERS}})
    return sorted(merged.values(), key=_token_volume, reverse=True)


def _aux_task_summary(aux_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate aux usage rows across models into a per-task summary."""
    by_task: dict[str, dict[str, Any]] = {}
    for aux in aux_rows:
        task = aux.get("task") or ""
        d = by_task.setdefault(task, {"task": task, "input_tokens": 0, "output_tokens": 0,
                                      "estimated_cost": 0, "api_calls": 0, "models": []})
        for key in _AUX_COUNTERS:
            d[key] += aux.get(key) or 0
        model = aux.get("model") or "unknown"
        if model not in d["models"]:
            d["models"].append(model)
    return sorted(by_task.values(), key=_token_volume, reverse=True)


def _profile_cli_args(profile: Optional[str]) -> list[str]:
    """``["-p", <name>]`` for a validated named profile, else ``[]``. Hub actions run in
    a fresh ``hermes`` subprocess whose ``_apply_profile_override()`` reads ``-p`` from argv —
    the only mechanism that reaches import-time-bound globals like ``skills_hub.SKILLS_DIR``.
    ``default`` is a real named target, not an alias for the dashboard's own profile:
    selector-less argv would make the child resolve the ambient ``HERMES_HOME`` (the launch
    profile under a pooled ``-p X serve``), not the default home."""
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return []
    from hermes_cli import profiles as profiles_mod
    _resolve_profile_dir(requested)
    return ["-p", profiles_mod.normalize_profile_name(requested)]


def _hub_action_name(verb: str, key: str, profile: Optional[str] = None) -> str:
    """Unique per-skill hub action name (+ registered log file): ``_spawn_hermes_action``
    tracks one process/log per name, so a shared "skills-install" would make concurrent
    row-level actions overwrite each other. Slug (readable) + hash (collision-proof)."""
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:48] or "skill"
    profile_key = (profile or "current").strip().lower()
    digest = hashlib.sha1(f"{profile_key}:{key}".encode()).hexdigest()[:8]
    name = f"skills-{verb}-{slug}-{digest}"
    _ACTION_LOG_FILES.setdefault(name, f"action-{name}.log")
    return name


def _installed_hub_identifiers(profile: Optional[str] = None) -> dict:
    """identifier -> installed lock entry for hub-installed skills (UI marks installed search
    results and catalog cards). Scoped to ``profile``'s skills/.hub/lock.json when given —
    HubLockFile takes an explicit path, sidestepping the import-time LOCK_FILE binding. {} when
    unreadable.

    Keyed by ``HubLockFile.by_identifier``, so a ClawHub install also answers to the
    ``clawhub/<slug>`` id a catalog card installs. Search lists a ClawHub skill by its bare
    slug; a client looks such a hit up under that card id, going by the hit's source."""
    try:
        from tools.skills_hub import HubLockFile
        if _is_current_profile(profile):
            lock = HubLockFile()
        else:
            profile_dir = _resolve_profile_dir(profile.strip())
            lock = HubLockFile(profile_dir / "skills" / ".hub" / "lock.json")
        keys = ("name", "trust_level", "scan_verdict")
        return {identifier: {k: entry.get(k) for k in keys}
                for identifier, entry in lock.by_identifier().items()}
    except HTTPException:
        raise  # an unknown profile is the scope's 404, not an unreadable lock file
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Profile Dashboard summaries and source-binding serialization
# ---------------------------------------------------------------------------


def _load_profile_config_raw_with_error(name: str) -> tuple[dict[str, Any], Optional[str]]:
    config_path = _resolve_profile_dir(name) / "config.yaml"
    try:
        loaded = read_config_doc(config_path) or {}
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        _log.debug("Profile %s config read failed: %s", name, exc)
        return {}, f"Could not read profile config: {exc}"
    except YAMLError as exc:
        _log.debug("Profile %s config parse failed: %s", name, exc)
        return {}, f"Invalid profile config: {exc}"
    if not isinstance(loaded, dict):
        return {}, "Profile config must be a YAML mapping."
    return loaded, None


def _load_profile_config_raw(name: str) -> dict[str, Any]:
    cfg, _error = _load_profile_config_raw_with_error(name)
    return cfg


def _profile_model_summary_from_config(cfg: dict[str, Any]) -> dict[str, str]:
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        return {
            "provider": str(model_cfg.get("provider", "") or ""),
            "model": str(model_cfg.get("default", model_cfg.get("name", "")) or ""),
        }
    return {"provider": "", "model": str(model_cfg or "")}


def _profile_model_summary(name: str) -> dict[str, str]:
    return _profile_model_summary_from_config(_load_profile_config_raw(name))


def _safe_binding_fallback_extra(extra: Any) -> Optional[dict[str, Any]]:
    """Serialize binding metadata without returning webhook URLs or credentials."""
    if not isinstance(extra, dict):
        return None
    safe: dict[str, Any] = {}
    for key, value in extra.items():
        key_str = str(key)
        key_lower = key_str.lower()
        if key_lower == "session_webhook":
            safe["session_webhook_configured"] = bool(value)
            continue
        if key_lower == "session_webhook_expired_time":
            safe[key_str] = value
            continue
        if any(part in key_lower for part in ("webhook", "token", "secret", "password", "key")):
            safe[key_str] = "[REDACTED]" if value else value
            continue
        safe[key_str] = value
    return safe


def _binding_webhook_status(extra: Any) -> dict[str, Any]:
    missing = {
        "configured": False, "state": "missing", "kind": "none",
        "expires_at": None, "expired": False, "label": "missing",
    }
    if not isinstance(extra, dict) or not str(extra.get("session_webhook") or "").strip():
        return missing
    try:
        expires_ms = int(extra.get("session_webhook_expired_time") or 0)
    except (TypeError, ValueError):
        expires_ms = 0
    if expires_ms and expires_ms <= int(time.time() * 1000):
        return {
            "configured": True, "state": "expired", "kind": "temporary",
            "expires_at": expires_ms, "expired": True, "label": "expired",
        }
    if expires_ms:
        return {
            "configured": True, "state": "configured", "kind": "temporary",
            "expires_at": expires_ms, "expired": False, "label": "temporary",
        }
    return {
        "configured": True, "state": "configured", "kind": "permanent",
        "expires_at": None, "expired": False, "label": "permanent",
    }


def _binding_target_summary(target: Any, source_key: str) -> dict[str, Any]:
    target_dict = target if isinstance(target, dict) else {}
    parts = str(source_key or "").split(":")
    platform = str(target_dict.get("platform") or "").strip()
    chat_type = str(target_dict.get("chat_type") or "").strip()
    chat_id = str(target_dict.get("chat_id") or "").strip()
    thread_id = target_dict.get("thread_id")
    user_name = str(target_dict.get("user_name") or "").strip()
    user_id = str(target_dict.get("user_id_alt") or target_dict.get("user_id") or "").strip()
    if not platform and len(parts) >= 2 and parts[0] == "source":
        platform = parts[1]
    if not chat_type and len(parts) >= 3 and parts[0] == "source":
        chat_type = parts[2]
    if not chat_id and len(parts) >= 4 and parts[0] == "source":
        chat_id = parts[3]
    chat_name = str(target_dict.get("chat_name") or "").strip()
    scope_parts = [part for part in (chat_type, user_name or user_id) if part]
    if thread_id:
        scope_parts.append(f"thread {thread_id}")
    return {
        "platform": platform or "unknown",
        "chat_type": chat_type or "unknown",
        "chat_id": chat_id or None,
        "chat_name": chat_name or None,
        "thread_id": thread_id,
        "user_name": user_name or None,
        "user_id": user_id or None,
        "label": chat_name or chat_id or source_key,
        "scope": " / ".join(scope_parts) if scope_parts else "source",
    }


def _binding_to_dict(binding: Any) -> dict[str, Any]:
    return {
        "source_binding_key": binding.source_binding_key,
        "profile_name": binding.profile_name,
        "agent_id": binding.agent_id,
        "fallback_target": binding.fallback_target,
        "fallback_extra": _safe_binding_fallback_extra(binding.fallback_extra),
        "target_summary": _binding_target_summary(binding.fallback_target, binding.source_binding_key),
        "webhook_status": _binding_webhook_status(binding.fallback_extra),
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
        "created_by": binding.created_by,
        "updated_by": binding.updated_by,
    }


def _source_binding_store():
    from gateway.source_agent_binding import SourceAgentBindingStore
    return SourceAgentBindingStore()


def _profile_raw_bindings(name: str) -> list[Any]:
    if not name:
        return []
    store = _source_binding_store()
    try:
        return list(store.list_bindings(profile_name=name))
    except (OSError, sqlite3.Error):
        _log.exception("Failed to list source bindings for profile %s", name)
        return []
    finally:
        store.close()


def _profile_bindings(name: str) -> list[dict[str, Any]]:
    return [_binding_to_dict(binding) for binding in _profile_raw_bindings(name)]


def _empty_binding_summary() -> dict[str, int]:
    return {
        "total": 0, "webhook_configured": 0, "webhook_expired": 0,
        "webhook_permanent": 0, "webhook_temporary": 0,
    }


def _profile_binding_summary(name: str) -> dict[str, int]:
    if not name:
        return _empty_binding_summary()
    store = _source_binding_store()
    try:
        bindings = store.list_bindings(profile_name=name)
    except (OSError, sqlite3.Error):
        _log.exception("Failed to summarize source bindings for profile %s", name)
        return _empty_binding_summary()
    finally:
        store.close()
    statuses = [_binding_webhook_status(binding.fallback_extra) for binding in bindings]
    return {
        "total": len(bindings),
        "webhook_configured": sum(1 for status in statuses if status["configured"]),
        "webhook_expired": sum(1 for status in statuses if status["expired"]),
        "webhook_permanent": sum(1 for status in statuses if status["kind"] == "permanent"),
        "webhook_temporary": sum(
            1 for status in statuses if status["kind"] == "temporary" and not status["expired"]
        ),
    }


def _source_from_binding_or_key(binding: Any, source_key: str):
    """Best-effort source recovery for Dashboard-created Kanban tasks."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    target = getattr(binding, "fallback_target", None)
    if isinstance(target, dict) and target.get("platform") and target.get("chat_id"):
        try:
            return SessionSource.from_dict(target)
        except (KeyError, TypeError, ValueError):
            _log.debug("Invalid binding fallback target; using source key", exc_info=True)
    parts = str(source_key or "").split(":")
    if len(parts) < 4 or parts[0] != "source":
        return None
    try:
        platform = Platform(parts[1])
    except ValueError:
        return None
    chat_type = parts[2] or "dm"
    chat_id = parts[3]
    if not chat_id:
        return None
    thread_id = None
    user_id = None
    if chat_type == "dm":
        thread_id = parts[4] if len(parts) >= 5 else None
    elif len(parts) >= 6:
        thread_id, user_id = parts[4], parts[5]
    elif len(parts) == 5:
        user_id = parts[4]
    return SessionSource(
        platform=platform, chat_id=chat_id, chat_type=chat_type,
        user_id=user_id, thread_id=thread_id,
    )


def _active_dashboard_notifier_profile() -> str:
    from hermes_cli.profiles import get_active_profile_name
    return get_active_profile_name() or "default"


def _profile_kanban_summary(name: str) -> dict[str, Any]:
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        conn = kbc.connect()
        try:
            tasks = kb.list_tasks(conn, assignee=name, include_archived=False)
        finally:
            conn.close()
        by_status: dict[str, int] = {}
        for task in tasks:
            status = str(getattr(task, "status", "") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "total": len(tasks), "by_status": by_status,
            "active": sum(count for status, count in by_status.items()
                          if status not in {"done", "archived"}),
        }
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return {"total": 0, "by_status": {}, "active": 0, "error": str(exc)}


_LOG_RECORD_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]\d{3}\s+")


def _iter_log_records(lines: list[str]) -> list[str]:
    records: list[str] = []
    current: list[str] = []
    for line in lines:
        if _LOG_RECORD_START_RE.match(line) and current:
            text = "\n".join(current).strip()
            if text:
                records.append(text)
            current = [line]
        else:
            current.append(line)
    text = "\n".join(current).strip()
    if text:
        records.append(text)
    return records


def _is_non_critical_profile_warning(line: str, *, config_valid: bool = True) -> bool:
    """Warnings that should not make an agent/profile look unhealthy."""
    if config_valid and (
        "gateway.config: Failed to process config.yaml" in line
        or "hermes_cli.config: Failed to parse" in line
        or ("hermes_cli.web_server: Profile " in line and " config parse failed:" in line)
    ):
        return True
    if ("tools.terminal_tool: Terminal requirements check failed: [Errno 2] No such file or directory"
            in line and "os.getcwd()" in line):
        return True
    if (
        "dingtalk_stream.client:" in line
        or "gateway.platforms.dingtalk:" in line
        or "gateway.platforms.base: [Dingtalk]" in line
        or "gateway.run: kanban notifier tick failed:" in line
        or "gateway.run: kanban notifier: send failed" in line
        or "gateway.run: kanban notifier: dropping subscription" in line
        or "gateway.run: Shutdown context:" in line
        or "asyncio: Exception in callback Connection.connection_lost" in line
    ):
        return True
    return (
        "agent.title_generator: Title generation failed" in line
        or "agent.auxiliary_client: Auxiliary" in line
        or "agent.stream_diag: Stream drop mid tool-call" in line
    )


def _profile_recent_error(profile_dir: Path, *, config_valid: bool = True) -> Optional[str]:
    errors_path = profile_dir / "logs" / "errors.log"
    if not errors_path.exists():
        return None
    try:
        lines = errors_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return None
    for text in reversed(_iter_log_records(lines)):
        if text and not _is_non_critical_profile_warning(text, config_valid=config_valid):
            return text[-500:]
    return None


def _profile_health(name: str, profile_dir: Path) -> dict[str, Any]:
    cfg, config_error = _load_profile_config_raw_with_error(name)
    model = _profile_model_summary_from_config(cfg)
    checks = {
        "config_exists": config_exists(profile_dir / "config.yaml"),
        "config_valid": config_error is None,
        "env_exists": (profile_dir / ".env").exists(),
        "model_configured": bool(model.get("model")),
        "soul_exists": (profile_dir / "SOUL.md").exists(),
        "workspace_exists": (profile_dir / "workspace").exists(),
    }
    recent_error = _profile_recent_error(profile_dir, config_valid=config_error is None)
    status = "ok"
    if not checks["config_exists"] or not checks["model_configured"] or config_error:
        status = "warning"
    if recent_error and " ERROR " in recent_error:
        status = "warning"
    return {
        "status": status, "checks": checks,
        "recent_error": recent_error, "config_error": config_error,
    }


def _profile_workspace_descriptor(profile_dir: Path) -> dict[str, Any]:
    workspace_path = profile_dir / "workspace"
    return {
        "provider": "local", "kind": "profile", "ref": str(workspace_path),
        "display_path": str(workspace_path), "sandbox_id": None,
        "capabilities": {"local_path": True, "open_path": True, "sandbox": False},
    }


def _profile_skills_summary(profile_dir: Path, limit: Optional[int] = 100) -> dict[str, Any]:
    skills_dir = profile_dir / "skills"
    names: list[str] = []
    if skills_dir.is_dir():
        for skill_md in skills_dir.rglob("SKILL.md"):
            rel_text = str(skill_md.parent.relative_to(skills_dir))
            if rel_text.startswith(".hub") or "/.git/" in f"/{rel_text}/":
                continue
            names.append(rel_text)
    names = sorted(set(names))
    visible_names = names if limit is None else names[:limit]
    return {
        "count": len(names), "names": visible_names,
        "truncated": limit is not None and len(names) > limit,
    }


def _normalize_skill_copy_name(name: str) -> str:
    skill = str(name or "").strip().replace("\\", "/")
    parts = [part for part in skill.split("/") if part]
    if not parts or skill.startswith("/") or any(part in {".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail=f"Invalid skill name: {name}")
    return "/".join(parts)


def _copy_profile_skill(source_skills: Path, target_skills: Path, skill_name: str) -> str:
    safe_name = _normalize_skill_copy_name(skill_name)
    parts = safe_name.split("/")
    src = source_skills.joinpath(*parts)
    if not src.is_dir() or not (src / "SKILL.md").is_file():
        raise HTTPException(status_code=404, detail=f"Skill '{safe_name}' not found in source profile")
    if any(part in {".hub", ".git"} for part in parts):
        raise HTTPException(status_code=400, detail=f"Skill '{safe_name}' cannot be copied")
    dest = target_skills.joinpath(*parts)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not copy skill '{safe_name}': {exc}")
    return safe_name


def _delete_profile_skill(profile_dir: Path, skill_name: str) -> str:
    safe_name = _normalize_skill_copy_name(skill_name)
    parts = safe_name.split("/")
    if any(part in {".hub", ".git"} for part in parts):
        raise HTTPException(status_code=400, detail=f"Skill '{safe_name}' cannot be deleted")
    skills_dir = profile_dir / "skills"
    target = skills_dir.joinpath(*parts)
    if not target.is_dir() or not (target / "SKILL.md").is_file():
        raise HTTPException(status_code=404, detail=f"Skill '{safe_name}' not found in profile")
    try:
        shutil.rmtree(target)
        parent = target.parent
        while parent != skills_dir and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not delete skill '{safe_name}': {exc}")
    return safe_name


def _resolve_profile_skill_manifest(
    profile_dir: Path, skill_name: str, *, require_exists: bool = True,
) -> tuple[str, Path]:
    safe_name = _normalize_skill_copy_name(skill_name)
    parts = safe_name.split("/")
    if any(part in {".hub", ".git"} for part in parts):
        raise HTTPException(status_code=400, detail=f"Skill '{safe_name}' cannot be edited")
    manifest = profile_dir / "skills"
    manifest = manifest.joinpath(*parts) / "SKILL.md"
    if require_exists and not manifest.is_file():
        raise HTTPException(status_code=404, detail=f"Skill '{safe_name}' not found in profile")
    return safe_name, manifest


def _profile_memory_summary(name: str, profile_dir: Path) -> dict[str, Any]:
    memory_cfg = _load_profile_config_raw(name).get("memory", {})
    provider = str(memory_cfg.get("provider") or "") if isinstance(memory_cfg, dict) else ""
    memory_dir = profile_dir / "memories"
    memory_file_count = 0
    memory_bytes = 0
    if memory_dir.is_dir():
        for item in memory_dir.rglob("*"):
            if not item.is_file() or item.name.endswith(".lock"):
                continue
            memory_file_count += 1
            try:
                memory_bytes += item.stat().st_size
            except OSError:
                pass
    state_db = profile_dir / "state.db"
    try:
        state_db_size = state_db.stat().st_size if state_db.exists() else 0
    except OSError:
        state_db_size = 0

    def _preview_file(filename: str, limit: int = 2000) -> dict[str, Any]:
        path = memory_dir / filename
        exists = path.is_file()
        size = 0
        content = ""
        truncated = False
        if exists:
            try:
                size = path.stat().st_size
                raw = path.read_text(encoding="utf-8-sig", errors="replace")
                truncated = len(raw) > limit
                content = raw[:limit]
                try:
                    from agent.redact import redact_sensitive_text
                    content = redact_sensitive_text(content, force=True)
                except Exception:
                    pass
            except OSError:
                exists = False
        return {
            "name": filename, "path": str(path), "exists": exists,
            "bytes": size, "content": content, "truncated": truncated,
        }

    return {
        "provider": provider, "memory_dir": str(memory_dir),
        "memory_dir_exists": memory_dir.is_dir(), "memory_file_count": memory_file_count,
        "memory_bytes": memory_bytes, "state_db": str(state_db),
        "state_db_exists": state_db.exists(), "state_db_bytes": state_db_size,
        "previews": [_preview_file("MEMORY.md"), _preview_file("USER.md")],
    }


def _profile_audit_events(name: str, limit: int = 5) -> list[dict[str, Any]]:
    from gateway.agent_audit import list_agent_audit_events
    return list_agent_audit_events(profile_name=name, limit=limit)
