"""Profile-scoped gateway runtime context."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root


@dataclass
class ProfileRuntimeContext:
    profile_name: str
    agent_id: str
    profile_home: Path
    config: dict[str, Any]
    session_store: Any
    session_db: Any
    session_key: str
    source_binding_key: str
    workspace_cwd: Path
    binding: Any = None
    config_mtime: float = 0.0


def profile_agent_id(profile_name: str) -> str:
    profile = str(profile_name or "default").strip()
    if profile in {"", "default"}:
        return "main"
    return profile


def resolve_profile_home(profile_name: str) -> Path:
    profile = str(profile_name or "default").strip()
    if profile in {"", "default"}:
        return get_default_hermes_root()
    from hermes_cli.profiles import get_profile_dir

    return get_profile_dir(profile)


def profile_config_mtime(profile_home: Path) -> float:
    try:
        return (profile_home / "config.yaml").stat().st_mtime
    except OSError:
        return 0.0


def load_profile_config(profile_home: Path) -> dict[str, Any]:
    config_path = profile_home / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml

        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _configured_terminal_cwd(config: dict[str, Any]) -> str:
    terminal_cfg = config.get("terminal")
    if isinstance(terminal_cfg, dict):
        raw = terminal_cfg.get("cwd")
    else:
        raw = config.get("cwd")
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or value.lower() in {"auto", "cwd"} or value == ".":
        return ""
    return value


def _expand_cwd(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def resolve_profile_workspace_cwd(
    profile_name: str,
    profile_home: Path,
    config: dict[str, Any],
) -> Path:
    """Return the default tool cwd for a profile-scoped gateway run."""
    profile = str(profile_name or "default").strip() or "default"
    configured = _configured_terminal_cwd(config)

    if profile == "default":
        if configured:
            return _expand_cwd(configured)
        env_cwd = os.getenv("TERMINAL_CWD", "").strip()
        if env_cwd:
            return _expand_cwd(env_cwd)
        return profile_home / "workspace"

    if configured:
        expanded = _expand_cwd(configured)
        try:
            expanded.relative_to(profile_home.resolve())
            return expanded
        except ValueError:
            pass
    return profile_home / "workspace"
