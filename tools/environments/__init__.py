"""Hermes execution environment backends.

Each backend provides the same interface (BaseEnvironment ABC) for running
shell commands in a specific execution context: local, Docker, SSH,
Singularity, Modal, Daytona, Vercel Sandbox, or AgentProxy. (Modal
additionally has direct and Nous-managed modes, selected via
terminal.modal_mode.  AgentProxy runs commands in a Docker container on a
remote AgentProxy agent, transported purely over the Dashboard task API —
no SSH.)

The terminal_tool.py factory (_create_environment) selects the backend
based on the TERMINAL_ENV configuration.
"""

from tools.environments.base import BaseEnvironment

__all__ = ["BaseEnvironment"]
