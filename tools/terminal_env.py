"""Scope-local resolution of the ``TERMINAL_*`` settings.

Historically every terminal setting lived in process-global ``os.environ``
(``TERMINAL_ENV``, ``TERMINAL_CWD``, ``TERMINAL_DOCKER_*`` ...).  That is fine
for a classic single-profile process (CLI, TUI, desktop, one gateway), but the
gateway *multiplexer* serves many profiles from ONE process, and their turns
run CONCURRENTLY in ``run_in_executor`` threads.  With a single process-global
``TERMINAL_ENV`` whichever profile touched the terminal first locked its backend
for the entire process — a ``docker`` profile and an ``agentproxy`` profile
would hijack each other's backend (the multiplex TERMINAL_ENV race).

This module adds a ContextVar overlay.  ``_profile_runtime_scope`` (gateway)
installs the scoped profile's fully-resolved ``TERMINAL_*`` map alongside the
existing ``HERMES_HOME`` override and secret scope, and every terminal read
goes through :func:`terminal_env_get`, which prefers the overlay and falls
back to ``os.environ``.  Because the overlay is a ContextVar it must be set
*inside* the worker/executor thread's re-entered profile scope (ContextVars do
not follow ``run_in_executor`` threads automatically).

Single-profile paths install no overlay, so :func:`terminal_env_get` reads
``os.environ`` exactly as before — their behavior is unchanged.
"""
from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar, Token
from typing import Dict, Iterator, Mapping, Optional

# None => no scoped overlay active; read straight from os.environ (legacy
# single-profile behavior). A dict => the scoped profile's resolved TERMINAL_*
# values take precedence over os.environ.
_terminal_env_overlay: ContextVar[Optional[Dict[str, str]]] = ContextVar(
    "terminal_env_overlay", default=None
)


def set_terminal_env_overlay(mapping: Optional[Mapping[str, str]]) -> Token:
    """Install *mapping* as the active scope's TERMINAL_* overlay.

    A snapshot copy is stored so later mutation of the caller's dict cannot
    bleed across scopes. Returns a token for :func:`reset_terminal_env_overlay`.
    """
    snapshot = dict(mapping) if mapping is not None else None
    return _terminal_env_overlay.set(snapshot)


def reset_terminal_env_overlay(token: Token) -> None:
    """Restore the overlay to its value before the matching ``set`` call."""
    _terminal_env_overlay.reset(token)


def get_terminal_env_overlay() -> Optional[Dict[str, str]]:
    """Return the active overlay dict (or ``None`` when unscoped)."""
    return _terminal_env_overlay.get()


def terminal_env_get(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a ``TERMINAL_*`` variable, honoring the active scope overlay.

    Drop-in replacement for ``os.getenv(name, default)`` /
    ``os.environ.get(name, default)`` for terminal settings. When a scope
    overlay is active and contains *name*, the overlay value wins; otherwise
    it falls back to ``os.environ`` so vars the overlay doesn't carry (and all
    single-profile callers) keep working unchanged.
    """
    overlay = _terminal_env_overlay.get()
    if overlay is not None and name in overlay:
        return overlay[name]
    return os.getenv(name, default)


@contextlib.contextmanager
def terminal_env_scope(mapping: Optional[Mapping[str, str]]) -> Iterator[None]:
    """Context manager installing *mapping* as the TERMINAL_* overlay."""
    token = set_terminal_env_overlay(mapping)
    try:
        yield
    finally:
        reset_terminal_env_overlay(token)


def build_scoped_terminal_env() -> Dict[str, str]:
    """Resolve the current profile scope's full ``TERMINAL_*`` map.

    Must be called while the profile's ``HERMES_HOME`` override is active (e.g.
    inside ``_profile_runtime_scope``): the config lookups follow
    ``get_hermes_home()`` and its ``read_raw_config`` cache is keyed on the
    resolved config path, so this returns THIS profile's terminal settings.

    Returns a fresh dict (merged defaults + explicit ``terminal.*`` keys) with
    no reference to ``os.environ`` — safe to hand to
    :func:`set_terminal_env_overlay`. On any failure returns an empty dict so
    the caller simply installs no overlay and legacy ``os.environ`` reads apply.
    """
    try:
        from hermes_cli.config import apply_terminal_config_to_env

        # env={} => write into a fresh dict, never os.environ. override=True so
        # explicit config.yaml terminal keys are authoritative for this scope.
        return apply_terminal_config_to_env(env={}, override=True)
    except Exception:  # pragma: no cover - defensive; keep terminal usable
        return {}
