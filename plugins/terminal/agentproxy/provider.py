"""AgentProxy terminal backend: commands run in a Docker container on a remote AgentProxy agent.

Selected with ``terminal.backend: agentproxy`` (``TERMINAL_ENV=agentproxy``). Each setting comes from the
active profile's ``terminal.ap_*`` key in config.yaml, else its ``TERMINAL_AP_*`` variable (read through
the per-turn terminal scope so a multiplexed gateway never hands one profile's agent/container to
another), else the default.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

logger = logging.getLogger(__name__)

# (setting, TERMINAL_* variable, default)
_SETTINGS = (
    ("agent_id", "TERMINAL_AP_AGENT", "home"),
    ("container", "TERMINAL_AP_CONTAINER", "hermes-reverse"),
    ("image", "TERMINAL_AP_IMAGE", "nikolaik/python-nodejs:python3.11-nodejs20"),
    ("cloud_url", "TERMINAL_AP_CLOUD_URL", "https://127.0.0.1:8080"),
    ("env_file", "TERMINAL_AP_ENV_FILE", "/opt/agentproxy/.env"),
    ("path_prefix", "TERMINAL_AP_PATH_PREFIX", "/usr/local/bin"),
    ("docker_run_args", "TERMINAL_AP_DOCKER_RUN_ARGS", ""),
)


def agentproxy_settings() -> dict[str, str]:
    from hermes_cli.config import load_config_readonly
    from tools.terminal_scope import terminal_env

    # ``terminal.ap_agent`` etc. are plugin keys, so core's TERMINAL_CONFIG_ENV_MAP never bridges them to
    # the environment: read them from the (profile-scoped) config here or a YAML edit silently does nothing.
    terminal_cfg = (load_config_readonly() or {}).get("terminal") or {}
    settings = {}
    for key, var, default in _SETTINGS:
        configured = terminal_cfg.get(var.removeprefix("TERMINAL_").lower())
        settings[key] = str(configured) if configured not in (None, "") else terminal_env(var, default)
    return settings


class AgentProxyTerminalProvider(TerminalEnvironmentProvider):
    """Remote, containerised, and still approval-gated: the container lives on the operator's own
    machine (``home``), so dangerous commands keep their approval prompt."""

    is_remote = True
    is_container = True

    @property
    def name(self) -> str:
        return "agentproxy"

    @property
    def display_name(self) -> str:
        return "AgentProxy"

    @property
    def description(self) -> str:
        return "Run commands in a Docker container on a remote AgentProxy agent (Dashboard task API, no SSH)."

    @property
    def skip_container_guards(self) -> bool:
        return False

    @property
    def strip_env_keys(self) -> frozenset:
        return frozenset({"DASHBOARD_TOKEN"})

    @property
    def env_description(self) -> str:
        return "a Linux Docker container on a remote AgentProxy agent"

    def is_available(self) -> bool:
        from agent.secret_scope import get_secret

        return bool(get_secret("DASHBOARD_TOKEN")) or os.path.exists(agentproxy_settings()["env_file"])

    def check_requirements(self, config: dict[str, Any]) -> bool:
        if self.is_available():
            return True
        logger.error(
            "agentproxy backend selected but no Dashboard token found: set DASHBOARD_TOKEN or provide the "
            "AgentProxy env file (%s).", agentproxy_settings()["env_file"])
        return False

    def setup_instructions(self) -> list[str]:
        return [
            "Set DASHBOARD_TOKEN in this profile's .env (or keep it in the AgentProxy env file,",
            "default /opt/agentproxy/.env). Optional: TERMINAL_AP_AGENT, TERMINAL_AP_CONTAINER,",
            "TERMINAL_AP_IMAGE, TERMINAL_AP_CLOUD_URL, TERMINAL_AP_PATH_PREFIX, TERMINAL_AP_DOCKER_RUN_ARGS.",
        ]

    def create_environment(
        self, *, cwd: str, timeout: int, task_id: str = "default", image: Optional[str] = None,
        container_config: Optional[dict[str, Any]] = None, **kwargs: Any,
    ):
        from plugins.terminal.agentproxy.environment import AgentProxyEnvironment

        settings = agentproxy_settings()
        if image:
            settings["image"] = image
        return AgentProxyEnvironment(cwd=cwd, timeout=timeout, **settings)
