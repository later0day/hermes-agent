"""AgentProxy terminal backend plugin — bundled, auto-loaded; selected by ``terminal.backend: agentproxy``."""

from __future__ import annotations

from plugins.terminal.agentproxy.provider import AgentProxyTerminalProvider


def register(ctx) -> None:
    ctx.register_terminal_environment_provider(AgentProxyTerminalProvider())
