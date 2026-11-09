"""Production coordinator for same-gateway hosted Discussion rooms."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import weakref
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

from gateway import hosted_room_actions
from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_room_links
from gateway import hosted_rooms
from gateway import hosted_room_router as router
from gateway.hosted_room_context import ROUTING_MODES, room_context
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint, PolicySnapshot
from gateway.hosted_room_peer import (
    GatewayRoomCatalog, HostedMemberDispatch, PROTOCOL_VERSION, room_grant_hard_expiry,
    room_grant_needs_dispatch_refresh)
from tui_gateway import hosted_room_dag
from tui_gateway import hosted_room_prepare_stall as prepare_stall
from tui_gateway import hosted_room_router_call as router_call
from tui_gateway.hosted_room_driver import (
    ROOM_SESSION_SOURCE, HostedRoomBinding, HostedRoomRuntime, OperatorSafeRoomError,
    room_session_title)
from tui_gateway.hosted_room_member_activity import (
    forget_task_progress, record_task_progress, task_is_active, task_progress)
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC
from tui_gateway.hosted_room_peer_http import (
    PeerRunsHTTPClient, PeerRunsHTTPError, digest_reauthorization_error)
from tui_gateway.hosted_room_peer_transport import (
    HostedRoomPeerClient, PeerHostedRoomTransport, PeerMemberRoute, build_member_dispatch)

logger = logging.getLogger("gateway.run")

_HOSTED_ROOM_IDLE_FALLBACK_SECONDS = 5.0
_HOSTED_ROOM_ACTIVE_POLL_SECONDS = 0.25
_HOSTED_ROOM_TERMINAL_GRACE_SECONDS = 30.0

_TERMINAL_STATUSES = ("deferred", "settled", "failed", "cancelled")
_LIVE_STATUSES = ("queued", "running", "stopping")
_STOPPABLE_STATUSES = ("queued", "running", "indeterminate", "deferred", "stopping")
_RETRYABLE_STATUSES = ("indeterminate", "deferred")
# Continue and the router answer re-read the log when an event lands between their read and their write.
_FENCED_APPEND_ATTEMPTS = 3
_APPROVABLE_STATUSES = ("running", "indeterminate", "deferred")
_PEER_RUN_ENDED = frozenset({"completed", "failed", "interrupted", "cancelled"})
# Owed grant revokes run on the room supervisor's cycle, so one short attempt per cycle, then backoff
# up to an hour: a member gateway that accepts and never answers holds the supervisor only briefly.
_OWED_REVOKES_PER_CYCLE = 1
_OWED_REVOKE_TIMEOUT_SECONDS = 3.0
_OWED_REVOKE_FIRST_RETRY_SECONDS = 5.0
_OWED_REVOKE_MAX_RETRY_SECONDS = 3600.0
logger = logging.getLogger(__name__)


class RoomDisbandRefusedError(hosted_rooms.HostedRoomError, RuntimeError):
    """An expected Disband refusal shared by both RPC surfaces."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def task_member_id(task: Mapping[str, Any]) -> str:
    return str(task["payload"].get("target_member_id") or task["payload"]["target_profile"])


def _approvable_attempts(tasks) -> set[tuple[str, str, int]]:
    return {(task_member_id(task), task["identity"].task_id, task["execution_generation"])
            for task in tasks if task["status"] in _APPROVABLE_STATUSES}


def attempt_action(kind: str, task: Mapping[str, Any]) -> dict[str, Any]:
    """A pending action naming the exact attempt ``groups.retry``/``discard`` fence on."""
    return {"kind": kind, "member_id": task_member_id(task), "task_id": task["identity"].task_id,
            "execution_generation": task["execution_generation"]}


def _member_activity(task: Mapping[str, Any]) -> dict[str, Any]:
    identity = task["identity"]
    # The thread and round let a client scope outcomes and the round strip to the discussion that owns the task.
    row = {"member_id": task_member_id(task), "status": str(task["status"]), "thread_id": identity.thread_id}
    if (round_index := discussion.turn_round(identity.turn_id)) is not None:
        row["round_index"] = round_index
    if task.get("started_at") is not None:
        row["started_at"] = task["started_at"]
    return {**row, **task_progress(identity.room_id, identity.task_id, int(task["execution_generation"]))}


def _discussion_limits(root: Path) -> discussion.DiscussionLimits:
    """Read the room owner's config, never the current member's profile scope. A malformed value
    keeps today's defaults (logged) rather than taking every room on this store offline."""
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(root))
    try:
        config = load_user_config_effective(root / "config.yaml")
    finally:
        reset_hermes_home_override(token)
    try:
        rooms = config.get("hosted_rooms", {})
        if not isinstance(rooms, Mapping):
            raise discussion.DiscussionValidationError("hosted_rooms must be a mapping")
        settings = rooms.get("discussion", {})
        if not isinstance(settings, Mapping):
            raise discussion.DiscussionValidationError("hosted_rooms.discussion must be a mapping")
        return discussion.DiscussionLimits(
            max_rounds=settings.get("max_rounds", discussion.MAX_DISCUSSION_ROUNDS),
            max_messages=settings.get("max_messages", discussion.MAX_DISCUSSION_MESSAGES))
    except discussion.DiscussionValidationError as exc:
        logger.warning("ignoring hosted_rooms.discussion in %s/config.yaml: %s", root, exc)
        return discussion.DiscussionLimits()


def _hosted_room_turn_timeout_seconds() -> float:
    try:
        agent_timeout = float(os.getenv("HERMES_AGENT_TIMEOUT", "1800"))
    except (TypeError, ValueError):
        agent_timeout = 0.0
    return (agent_timeout if agent_timeout > 0 else 1800.0) + _HOSTED_ROOM_TERMINAL_GRACE_SECONDS


def _grant_revoke_is_terminal(exc: PeerRunsHTTPError) -> bool:
    """Return whether the peer proves the scoped grant is already unusable."""
    return exc.status_code in {401, 403} and exc.error_code in {
        "invalid_room_grant", "room_reauthorization_required"}


def disband_fence(service: Any, room_id: str):
    """The service's Disband fence on new peer routes; a duck-typed service without one has none."""
    fence = getattr(service, "disbanding", None)
    return fence(room_id) if callable(fence) else contextlib.nullcontext()


def route_publication_lock(service: Any):
    """The lock that orders peer-route publication with Disband, when the service has one."""
    lock = getattr(service, "peer_route_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _member_of(task: Mapping[str, Any]) -> str:
    payload = task.get("payload") or {}
    return str(payload.get("target_member_id") or payload.get("target_profile") or "")


def _hook(obj: Any, name: str):
    """Optional callable attribute of a duck-typed peer client, or None."""
    value = getattr(obj, name, None)
    return value if callable(value) else None


def _authority(room: Mapping[str, Any]) -> tuple[str, int]:
    return str(room["authority_gateway_id"]), int(room["authority_epoch"])


def local_profiles_under(root: Path) -> tuple[str, ...]:
    """Live profile names under the Hermes root *root* (always including ``default``)."""
    from hermes_constants import named_profile_has_identity, named_profile_is_deleted

    profiles, profiles_dir = {"default"}, root / "profiles"
    if profiles_dir.is_dir():
        # ``profiles/.deleted/`` is the tombstone dir `hermes profile delete` leaves behind, not a
        # profile: feeding it to validate_roster failed plan_next_task on every cycle (#106847).
        # Marker-less dirs (cron/log side-effect shells) are not profiles either.
        profiles.update(
            path.name for path in profiles_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
            and named_profile_has_identity(path) and not named_profile_is_deleted(path))
    return tuple(sorted(profiles))

class HostedRoomService:
    """Own the hosted Discussion policy and its transport-free worker."""

    def __init__(
        self, server: ModuleType, *, db_path: Path | str | None = None,
        peer_routes: Mapping[tuple[str, str], PeerMemberRoute] | None = None,
        peer_clients: Mapping[Any, HostedRoomPeerClient] | None = None) -> None:
        self.server, self.db_path = server, Path(db_path or hosted_rooms.default_db_path())
        self.discussion_limits = _discussion_limits(self.root)
        hosted_rooms.prune_disbanded_rooms(self.db_path)
        self._policy_lock = threading.RLock()
        # Orders each room's Stop with its approval's live check and dispatch, which can wait on another
        # computer: an Allow either reaches the member before Stop marks its attempt stopping, or sees
        # the stop and is refused. Taken before ``_policy_lock``, never inside it. A room's lock lives
        # only while someone holds or waits on it.
        self._approval_stop_locks: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
        # A service-wide override of the per-room locks (``None`` here): set, it orders every room's
        # Stop and approvals under that one lock, as before rooms had their own.
        self._approval_stop_lock: Any = None
        try:
            self._pending_actions: dict[tuple[str, str], dict[str, Any]] = (
                hosted_room_actions.load_pending_actions(self.db_path))
        except Exception:
            logger.warning(
                "hosted room pending actions could not be restored",
                exc_info=True,
            )
            self._pending_actions = {}
        # Guards the two maps a status read projects (pending actions, peer-route status). A leaf
        # lock: nothing is called while holding it, so Desktop's room status never waits behind a
        # room's preparation, which holds ``_policy_lock`` across its log and checkpoint work.
        self._state_lock = threading.Lock()
        self.policy_checkpoint = HostedRoomPolicyCheckpoint(
            self.db_path,
            # Every Continue grants one more budget, so one discussion's projection holds up to that many.
            max_active_events=(1 + 2 * discussion.MAX_DISCUSSION_MEMBERS * self.discussion_limits.max_rounds + 2)
            * (router.MAX_DISCUSSION_CONTINUES + 1))
        self.rpc = self._make_rpc(server)
        self._link_load_error = None
        self._peer_route_status: dict[tuple[str, str], str] = {}
        self.peer_routes: dict[tuple[str, str], PeerMemberRoute] = {}
        self.peer_clients: dict[tuple[str, str], Any] = {}
        # Duck-typed transports of dropped routes, by grant digest, for this process's owed revokes;
        # an HTTP RoomLink revoke always goes through a fresh short-timeout client.
        self._revoke_clients: dict[str, Any] = {}
        # Lock order: peer_route_lock -> _policy_lock. Route sections only compare/persist/publish;
        # never call Stop, recovery or the network while holding either lock.
        self.peer_route_lock = threading.Lock()
        self._disbanding_rooms: set[str] = set()
        self._prepare_stalls = prepare_stall.PrepareStallTracker()
        self._router_calls = router_call.RouterCalls(self.db_path)
        try:
            self._load_stored_links()
        except Exception as exc:
            self._link_load_error = str(exc)
        # Set by anything that can record an owed revoke (a peer route, a grant handed in); a revoke
        # owed by an earlier process is found once here.
        self._may_owe_revokes = True
        supplied_clients = dict(peer_clients or {})
        for key, route in dict(peer_routes or {}).items():
            self.peer_routes[key] = route
            client = supplied_clients.get(key, supplied_clients.get(route.target_install_id))
            if client is not None:
                self.peer_clients[key] = client
        self._action_relay_stop = threading.Event()
        self._action_relay_thread: threading.Thread | None = None
        # Rooms this process served last cycle -> their local member profiles (see _served_bindings).
        self._served_rooms: dict[str, tuple[str, ...]] = {}
        self.runtime = HostedRoomRuntime(
            db_path=self.db_path, rooms=self._served_bindings, rpc=self.rpc,
            transport_resolver=self._resolve_member_transport, turn_lock=self._turn_lock,
            prepare_room=self._prepare_tracked, publish_terminal=self.publish_terminal,
            pending_action=self._set_pending_action, task_progress=record_task_progress,
            retire_task_progress=forget_task_progress,
            remote_member=self._member_is_peer, confirm_deferred_stop=self._stop_deferred_peer,
            maintain=self.retry_owed_revokes,
            poll_interval_seconds=_HOSTED_ROOM_IDLE_FALLBACK_SECONDS,
            active_poll_interval_seconds=_HOSTED_ROOM_ACTIVE_POLL_SECONDS,
            turn_timeout_seconds=_hosted_room_turn_timeout_seconds())

    def _make_rpc(self, server):
        return HostedRoomServerRPC(server, db_path=self.db_path)

    def _load_stored_links(self) -> None:
        """Rehydrate persisted peer routes; collect per-link errors into one string."""
        stored_links, load_errors = hosted_room_links.load_room_links_tolerant(self.db_path)
        errors = list(load_errors)
        for stored in stored_links:
            key, catalog = (stored.room_id, stored.member_id), stored.catalog
            if PROTOCOL_VERSION not in catalog.protocol_versions:
                errors.append(f"{stored.room_id}:{stored.member_id}:protocol-upgrade-required")
                continue
            self.peer_routes[key] = PeerMemberRoute(
                home_install_id=hosted_rooms.local_authority_gateway_id(),
                member_id=stored.member_id, target_install_id=catalog.installation_id,
                target_profile=stored.target_profile, capability_digest=catalog.catalog_digest,
                execution_policy_digest=catalog.execution_policy.policy_digest,
                cancellation_scope_id=stored.cancellation_scope_id, trace_id=stored.trace_id,
                grant=stored.grant)
            self.peer_clients[key] = PeerRunsHTTPClient(
                base_url=stored.target_url, api_key="", receipt_db_path=self.db_path)
            self._peer_route_status[key] = stored.status
        if errors:
            self._link_load_error = ",".join(errors)

    @property
    def root(self) -> Path:
        return self.db_path.parent

    def local_profiles(self) -> tuple[str, ...]:
        return local_profiles_under(self.root)

    def room_profiles(self, room: Mapping[str, Any]) -> tuple[str, ...]:
        """Profiles a SAVED room's roster is checked against. New groups use ``local_profiles``;
        a subclass may keep a seated member valid after it stops being eligible for new groups."""
        return self.local_profiles()

    def bindings(self) -> tuple[HostedRoomBinding, ...]:
        local_gateway_id = hosted_rooms.local_authority_gateway_id()
        return tuple(
            HostedRoomBinding(str(room["room_id"]), *_authority(room))
            for room in hosted_rooms.list_rooms(self.db_path)
            if str(room["authority_gateway_id"]) == local_gateway_id)

    def _served_bindings(self) -> tuple[HostedRoomBinding, ...]:
        """The runtime's per-cycle room list; a room that drops out because it was DISBANDED has its
        members' background processes reaped here. Every process serving rooms runs this, and each only
        reaps its own registry, so a disband from any entry point (dashboard, /room in a chat) reaches the
        process that actually spawned the job."""
        bindings = self.bindings()
        current = {binding.room_id for binding in bindings}
        for room_id in set(self._served_rooms) - current:
            profiles = self._served_rooms.pop(room_id)
            try:
                if hosted_rooms.room_state(
                        self.db_path, room_id=room_id, include_disbanded=True).get("disbanded_at") is not None:
                    self.reap_room_processes(room_id, profiles)
                    from gateway import room_mirror_db

                    room_mirror_db.remove_subs_for_room(self.db_path, room_id=room_id)
            except Exception as exc:  # one room's cleanup must not stall scheduling for the rest
                logger.warning("room %s: background process cleanup failed: %s", room_id, exc)
        for room_id in current - set(self._served_rooms):
            try:
                members = self._room(room_id).get("members") or []
            except hosted_rooms.HostedRoomError:
                continue
            self._served_rooms[room_id] = tuple(
                str(m.get("profile") or "") for m in members
                if isinstance(m, dict) and (m.get("target") or {}).get("kind", "local") == "local" and m.get("profile"))
        return bindings

    def reap_room_processes(self, room_id: str, profiles: Sequence[str]) -> int:
        """Kill background processes this process spawned for a disbanded room's members.

        A member's ``terminal(background=true)`` job is registered under its room session key and is not
        tied to any turn, so stopping the room's turns left dev servers / watchers running forever. Output
        is consumed so a completion notice cannot wake the dead room's session."""
        from tools.process_registry import process_registry

        keys: dict[str, str] = {}
        for profile in profiles:
            session = self.rpc.resolve_exact(
                profile=profile, title=room_session_title(room_id), source=ROOM_SESSION_SOURCE)
            if session and session.get("session_id"):
                keys[profile] = str(session["session_id"])
        killed = 0
        for profile, key in keys.items():
            for entry in process_registry.list_sessions(session_key=key):
                if entry.get("status") != "running":
                    continue
                result = process_registry.kill_process(
                    entry["session_id"], source="room.disband", consume_output=True)
                killed += result.get("status") in {"killed", "already_exited"}
            # The registry is per process and in memory: a job a previous run of this process started in a
            # persistent container is invisible to it, so also sweep the member's container by owner tag.
            try:
                killed += self._reap_container_jobs(profile, key)
            except Exception as exc:
                logger.warning("room %s: container job sweep for %s failed: %s", room_id, profile, exc)
        if killed:
            logger.info("room %s disbanded: stopped %d member background process(es)", room_id, killed)
        return killed

    def _reap_container_jobs(self, profile: str, session_key: str) -> int:
        """Kill owner-tagged background jobs of *session_key* in *profile*'s EXISTING Docker sandbox
        (never starts one). Returns how many tagged processes were found running."""
        import subprocess

        from tools.environments.docker import find_docker
        from tools.process_registry import sandbox_owner_kill_command
        from tools.terminal_scope import build_profile_terminal_scope

        home = self.root if profile == "default" else self.root / "profiles" / profile
        if build_profile_terminal_scope(home).get("TERMINAL_ENV") != "docker" or not (docker := find_docker()):
            return 0
        containers = subprocess.run(
            [docker, "ps", "-q", "--filter", "label=hermes-agent=1", "--filter", f"label=hermes-profile={profile}"],
            capture_output=True, text=True, timeout=15, check=True, stdin=subprocess.DEVNULL).stdout.split()
        found = 0
        for container in containers:
            out = subprocess.run(
                [docker, "exec", container, "sh", "-c", sandbox_owner_kill_command(session_key)],
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, check=False).stdout
            found += sum(line.strip().isdigit() for line in out.splitlines())
        return found

    def _room(self, room_id: str) -> dict[str, Any]:
        return hosted_rooms.room_state(self.db_path, room_id=room_id)

    def _owned_authority(self, room_id: str) -> tuple[str, int]:
        """(gateway_id, epoch) of a room this gateway owns; conflict error otherwise."""
        gateway_id, epoch = _authority(self._room(room_id))
        if gateway_id != hosted_rooms.local_authority_gateway_id():
            raise hosted_rooms.AuthorityConflictError(
                "This Group Chat is managed by another gateway.")
        return gateway_id, epoch

    def _turn_lock(self, profile: str) -> contextlib.AbstractContextManager[Path]:
        from tools.bot_relay import acquire_turn_lock
        return acquire_turn_lock(self.root, profile)

    def start(self) -> None:
        self.runtime.start()
        with self._policy_lock:
            if (
                self._action_relay_thread is not None
                and self._action_relay_thread.is_alive()
            ):
                return
            self._action_relay_stop.clear()
            self._action_relay_thread = threading.Thread(
                target=self._action_relay_loop,
                name="hosted-room-action-relay",
                daemon=True,
            )
            self._action_relay_thread.start()

    def stop(self, *, timeout: float = 5.0) -> bool:
        self._action_relay_stop.set()
        runtime_stopped = self.runtime.stop(timeout=timeout)
        with self._policy_lock:
            relay = self._action_relay_thread
        if relay is not None:
            relay.join(max(0.0, timeout))
        return runtime_stopped and (relay is None or not relay.is_alive())

    def _action_relay_loop(self) -> None:
        while not self._action_relay_stop.is_set():
            try:
                self._relay_action_decisions_once()
            except Exception as exc:
                logger.warning("Room action relay failed: %s", exc)
            self._action_relay_stop.wait(_HOSTED_ROOM_ACTIVE_POLL_SECONDS)

    def _relay_action_decisions_once(self) -> int:
        """Deliver exact durable decisions only in the process owning a session."""
        decisions = hosted_room_actions.load_undelivered_decisions(self.db_path)
        if not decisions:
            return 0
        self._refresh_pending_actions()
        delivered = 0
        for decision in decisions:
            room_id = str(decision.get("room_id") or "")
            member_id = str(decision.get("member_id") or "")
            action_id = str(decision.get("action_id") or "")
            with self._policy_lock, self._state_lock:
                action = self._pending_actions.get((room_id, member_id))
                action = dict(action) if action is not None else None
            if (
                action is None
                or str(
                    action.get("action_id") or action.get("request_id") or ""
                )
                != action_id
                or str(action.get("task_id") or "")
                != str(decision.get("task_id") or "")
                or int(action.get("execution_generation") or 0)
                != int(decision.get("execution_generation") or 0)
            ):
                # Never transfer a queued choice to a replacement action that
                # happens to reuse its external request id.
                hosted_room_actions.mark_decision_delivered(
                    self.db_path,
                    room_id=room_id,
                    member_id=member_id,
                    action_id=action_id,
                )
                delivered += 1
                continue
            session_id = str(action.get("session_id") or "")
            owns_session = getattr(self.rpc, "owns_session", None)
            if callable(owns_session) and not owns_session(session_id):
                continue
            try:
                self.approve_room_task(
                    room_id,
                    member_id=member_id,
                    task_id=str(action.get("task_id") or ""),
                    execution_generation=int(
                        action.get("execution_generation") or 0
                    ),
                    choice=str(decision.get("choice") or "deny"),
                    request_id=str(action.get("request_id") or action_id),
                )
            except Exception as exc:
                # A local session may have just settled. Exact task-generation
                # checks make the decision stale rather than transferable.
                logger.warning("Room action decision remains undelivered: %s", exc)
                continue
            hosted_room_actions.mark_decision_delivered(
                self.db_path,
                room_id=room_id,
                member_id=member_id,
                action_id=action_id,
            )
            delivered += 1
        return delivered

    def wakeup(self) -> None:
        self.runtime.wakeup()

    def _list_tasks(self, room_id: str, statuses) -> Iterator[Mapping[str, Any]]:
        for status in statuses:
            yield from driver.list_tasks(self.db_path, room_id=room_id, status=status)

    def _save_link(self, **link: Any) -> None:
        """Persist one stored link (``make_stored_link`` keyword fields)."""
        hosted_room_links.save_room_link(self.db_path, hosted_room_links.make_stored_link(**link))

    def register_peer_route(
        self, *, room_id: str, member_id: str, route: PeerMemberRoute,
        client: HostedRoomPeerClient, target_url: str | None = None,
        catalog: GatewayRoomCatalog | None = None) -> None:
        """Register one verified route and optionally persist its scoped grant."""
        bind_store = _hook(client, "bind_receipt_store")
        if bind_store is not None:
            bind_store(self.db_path)
        if catalog is not None:
            if not route.execution_policy_digest:
                route = replace(
                    route, execution_policy_digest=catalog.execution_policy.policy_digest)
            if (
                route.capability_digest != catalog.catalog_digest
                or route.execution_policy_digest != catalog.execution_policy.policy_digest):
                raise ValueError("peer route does not match its target catalog")
            if target_url is not None:
                self._save_link(
                    room_id=room_id, member_id=member_id, target_url=target_url,
                    target_profile=route.target_profile, grant=route.grant, catalog=catalog,
                    cancellation_scope_id=route.cancellation_scope_id, trace_id=route.trace_id)
        # Persistence is the publication boundary: a failed disk write must never
        # leave a process-local route that disappears after restart.
        self._publish_route((room_id, member_id), route, client)
        self.runtime.wakeup()

    def _publish_route(self, key: tuple[str, str], route: PeerMemberRoute, client=None) -> None:
        """Make a persisted route live as ``ready`` (and bind its client when given)."""
        with self._policy_lock:
            previous, current = self.peer_routes.get(key), self.peer_clients.get(key)
            if (previous is not None and replace(previous, grant=route.grant) == route
                    and isinstance(current, PeerRunsHTTPClient) and isinstance(client, PeerRunsHTTPClient)
                    and current.base_url == client.base_url):
                # The active worker still polls this client; keep its observed approval identities.
                client = current
            self.peer_routes[key] = route
            with self._state_lock:
                self._peer_route_status[key] = "ready"
            self._may_owe_revokes = True
            if client is not None:
                self.peer_clients[key] = client

    def revoke_room_routes(self, room_id: str) -> int:
        """Revoke and forget every scoped peer route for one room; an unreachable target
        leaves the room intact for retry rather than a false disband with a live grant.
        Retained grants are revoked individually: an expired route proves nothing about them."""
        revoked = self._revoke_retained_room_grants(room_id)
        with self._policy_lock:
            routes = [(key, route) for key, route in self.peer_routes.items() if key[0] == room_id]
        for key, route in routes:
            revoke = _hook(self.peer_clients.get(key), "revoke_grant")
            if revoke is None:
                raise RoomDisbandRefusedError(
                    "peer_revoke_unsupported", "The member cannot revoke its group chat grant; retry after upgrading it.")
            try:
                revoke(grant=route.grant)
            except PeerRunsHTTPError as exc:
                if not _grant_revoke_is_terminal(exc):
                    raise
        with self.peer_route_lock, self._policy_lock:
            if dict(routes) != {key: route for key, route in self.peer_routes.items() if key[0] == room_id}:
                raise hosted_rooms.RoomConflictError("Group chat peer routes changed during Disband; retry.")
            revoked.update(hosted_rooms.grant_digest(route.grant) for _key, route in routes)
            hosted_rooms.retire_room_link_records(self.db_path, room_id=room_id, revoked_digests=revoked)
            for key, _route in routes:
                for table in (self.peer_routes, self.peer_clients):
                    table.pop(key, None)
                with self._state_lock:
                    self._peer_route_status.pop(key, None)
        self.runtime.wakeup()
        return len(routes)

    def _revoke_retained_room_grants(self, room_id: str) -> set[str]:
        revoked = set()
        for held in hosted_rooms.held_room_grant_revokes(
                self.db_path, room_id=room_id, now=self.runtime.clock()):
            client = PeerRunsHTTPClient(base_url=held["target_url"], api_key="")
            try:
                client.revoke_grant(grant=held["grant"])
            except PeerRunsHTTPError as exc:
                if not _grant_revoke_is_terminal(exc):
                    raise
            revoked.add(hosted_rooms.grant_digest(held["grant"]))
        return revoked

    def hold_grant_revoke(self, *, room_id: str, member_id: str, target_url: str, grant: str) -> None:
        """Hold until the tombstone; a full live hold is refused, never revoked prematurely."""
        try:
            hosted_rooms.hold_room_grant_revoke(
                self.db_path, room_id=room_id, member_id=member_id, target_url=target_url, grant=grant,
                expires_at=room_grant_hard_expiry(grant))
        except hosted_rooms.OwedRevokesFullError:  # already tombstoned: no live hold to protect
            self._revoke_unowed(room_id=room_id, member_id=member_id, target_url=target_url,
                                grant=grant, client=None)
            return
        with self._policy_lock:
            self._may_owe_revokes = True
        self.runtime.wakeup()

    def owe_grant_revoke(
        self, *, room_id: str, member_id: str, target_url: str, grant: str, client: Any = None) -> None:
        """Record that this home owes a revoke for a grant it was handed (``retry_owed_revokes``).
        With no room left to record it, the grant is revoked at its member's gateway now, and an
        error is raised if that gateway does not confirm it: a live grant is never dropped quietly."""
        try:
            hosted_rooms.owe_room_grant_revoke(
                self.db_path, room_id=room_id, member_id=member_id, target_url=target_url, grant=grant,
                expires_at=room_grant_hard_expiry(grant))
        except hosted_rooms.OwedRevokesFullError:
            self._revoke_unowed(room_id=room_id, member_id=member_id, target_url=target_url,
                                grant=grant, client=client)
            return
        with self._policy_lock:
            self._may_owe_revokes = True
        self._keep_revoke_client(grant, client)
        self.runtime.wakeup()

    def _revoke_unowed(self, *, room_id: str, member_id: str, target_url: str, grant: str, client: Any) -> None:
        revoke = _hook(client, "revoke_grant") or PeerRunsHTTPClient(
            base_url=target_url, api_key="", timeout_seconds=_OWED_REVOKE_TIMEOUT_SECONDS,
            total_deadline=True).revoke_grant
        try:
            revoke(grant=grant)
        except PeerRunsHTTPError as exc:
            if not _grant_revoke_is_terminal(exc):
                message = (f"revoke for member {member_id} of room {room_id} could not be recorded "
                           f"(too many owed revokes) or sent: {exc}")
                self.runtime._record_error(message)
                raise hosted_rooms.OwedRevokesFullError(message) from exc

    def _keep_revoke_client(self, grant: str, client: Any) -> None:
        if client is not None and not isinstance(client, PeerRunsHTTPClient):
            self._revoke_clients[hosted_rooms.grant_digest(grant)] = client

    def ended_room(self, room_id: str) -> dict[str, Any] | None:
        """The kept tombstone of a room this gateway owned and disbanded, else None (live, moved,
        purged or unknown): a grant handed in for it is owed its revoke."""
        try:
            room = hosted_rooms.room_state(self.db_path, room_id=room_id, include_disbanded=True)
        except hosted_rooms.RoomNotFoundError:
            return None
        if room.get("disbanded_at") is None or str(
                room.get("authority_gateway_id")) != hosted_rooms.local_authority_gateway_id():
            return None
        return room

    def retry_owed_revokes(self) -> int:
        """Pay one due owed grant revoke per worker cycle, with backoff timed from when the attempt
        ended; returns how many settled. Only the member gateway's answer that the grant is unknown
        or already revoked settles it (a fault there, a 5xx or no answer keeps it owed); an owed
        revoke whose grant has expired is dropped. Nothing here names the grant. A gateway that
        never seated a peer member owes nothing, so its cycles do not touch the store."""
        if not self._may_owe_revokes:
            return 0
        settled = 0
        for owed in hosted_rooms.due_room_grant_revokes(
                self.db_path, limit=_OWED_REVOKES_PER_CYCLE, now=self.runtime.clock()):
            key = {k: str(owed[k]) for k in ("room_id", "member_id", "grant_digest")}
            try:
                client = self._revoke_clients.get(key["grant_digest"]) or PeerRunsHTTPClient(
                    base_url=str(owed["target_url"]), api_key="", timeout_seconds=_OWED_REVOKE_TIMEOUT_SECONDS,
                    total_deadline=True)
                client.revoke_grant(grant=str(owed["grant"]))
            except Exception as exc:  # health: allow BLE001 -- every transport fault retains debt with backoff and records an error
                if not (isinstance(exc, PeerRunsHTTPError) and _grant_revoke_is_terminal(exc)):
                    delay = min(_OWED_REVOKE_MAX_RETRY_SECONDS,
                                _OWED_REVOKE_FIRST_RETRY_SECONDS * 2 ** min(int(owed["attempts"]), 20))
                    hosted_rooms.settle_room_grant_revoke(
                        self.db_path, **key, retry_at=self.runtime.clock() + delay)
                    self.runtime._record_error(
                        f"revoke owed for member {key['member_id']} of room {key['room_id']} is pending: {exc}")
                    continue
            hosted_rooms.settle_room_grant_revoke(self.db_path, **key)
            self._revoke_clients.pop(key["grant_digest"], None)
            settled += 1
        with self._policy_lock:  # owe_grant_revoke sets the flag under it after its write
            self._may_owe_revokes = bool(self.peer_routes) or hosted_rooms.owes_room_grant_revokes(self.db_path)
        return settled

    def _resolve_member_transport(self, binding: HostedRoomBinding, task: Mapping[str, Any]):
        payload = task.get("payload", {})
        member_id = str(payload.get("target_member_id") or payload.get("target_profile") or "")
        key = (binding.room_id, member_id)
        route = self.peer_routes.get(key)
        if route is None:
            if self._member_is_peer(binding.room_id, member_id):
                raise OperatorSafeRoomError("peer room route is unavailable")
            return self.rpc
        client = self.peer_clients.get(key)
        if client is None:
            raise RuntimeError("peer room client is unavailable")
        identity = task.get("identity")
        execution_generation = int(task.get("execution_generation") or 0)
        bind_observation = _hook(client, "bind_observation")
        if (
            bind_observation is not None and isinstance(identity, driver.TaskIdentity)
            and execution_generation > 0):
            bind_observation(task_id=identity.task_id, execution_generation=execution_generation)

        def set_status(status: str):
            return lambda: self._set_route_status(*key, status)
        tracked_client = _RouteStatusPeerClient(
            client, live=lambda: key in self.peer_routes, on_ready=set_status("ready"),
            on_reauthorization=set_status("needs_reauthorization"),
            on_unavailable=set_status("unavailable"),
            on_refreshed=lambda grant, catalog=None, previous=None: self._rotate_route_grant(
                *key, grant, catalog, previous=previous))
        self._recover_peer_admission(binding, task, route, tracked_client)
        return PeerHostedRoomTransport(
            binding=binding, route=route, client=tracked_client,
            source_event_seq=int(payload.get("source_event_seq") or 0),
            task_id=getattr(identity, "task_id", None), execution_generation=execution_generation)

    def _recover_peer_admission(
        self, binding: HostedRoomBinding, task: Mapping[str, Any], route: PeerMemberRoute,
        client: Any) -> None:
        """Rediscover an admitted peer run without advancing its generation."""
        recover = _hook(client, "recover_dispatch")
        identity, payload = task.get("identity"), task.get("payload")
        execution_generation = int(task.get("execution_generation") or 0)
        if (
            recover is None or not isinstance(identity, driver.TaskIdentity)
            or not isinstance(payload, Mapping) or execution_generation < 1
            or task.get("status") not in {"indeterminate", "stopping", "deferred"}):
            return
        prompt = payload.get("prompt")
        source_event_seq = int(payload.get("source_event_seq") or 0)
        if not isinstance(prompt, str) or source_event_seq < 1 or not route.trace_id:
            raise RuntimeError("peer room admission identity is unavailable for recovery")
        dispatch = build_member_dispatch(
            binding=binding, route=route, room_id=identity.room_id, task_id=identity.task_id,
            target_profile=route.target_profile, execution_generation=execution_generation,
            source_event_seq=source_event_seq, prompt=prompt, trace_id=route.trace_id)
        refresh = _hook(client, "refresh_receipt")
        if task.get("status") == "deferred" and refresh is not None:
            refresh(dispatch=dispatch.as_mapping(), grant=route.grant)
        recover(dispatch=dispatch.as_mapping(), grant=route.grant)

    def close_peer_admission(self, binding: HostedRoomBinding, task: Mapping[str, Any]) -> bool:
        """Whether a deferred peer turn's admission key is closed on its member's gateway, which
        then never had the run and never will. False: that gateway has the run, and its receipt is
        now saved for Stop. Never sends the admission, so it never starts the turn. A missing route
        raises RuntimeError; an unreachable or too-old member gateway raises PeerRunsHTTPError."""
        key = (binding.room_id, _member_of(task))
        route, client = self.peer_routes.get(key), self.peer_clients.get(key)
        close = _hook(client, "close_admission")
        if route is None or close is None:
            raise OperatorSafeRoomError("peer room route is unavailable")
        identity, payload = task["identity"], task["payload"]
        if not route.trace_id:
            raise RuntimeError("peer room admission identity is unavailable for recovery")
        dispatch = build_member_dispatch(
            binding=binding, route=route, room_id=identity.room_id, task_id=identity.task_id,
            target_profile=route.target_profile, execution_generation=int(task["execution_generation"]),
            source_event_seq=int(payload["source_event_seq"]), prompt=str(payload["prompt"]),
            trace_id=route.trace_id)
        result = close(dispatch=dispatch.as_mapping(), grant=route.grant)
        return isinstance(result, Mapping) and result.get("closed") is True

    def _member_is_peer(self, room_id: str, member_id: str) -> bool:
        for m in self._room(room_id).get("members") or []:
            if isinstance(m, Mapping) and str(
                    m.get("member_id") or m.get("profile") or "") == member_id:
                target = m.get("target")
                return isinstance(target, Mapping) and target.get("kind") == "peer"
        return False

    def _set_route_status(self, room_id: str, member_id: str, status: str) -> None:
        with self._policy_lock, self._state_lock:
            if self._peer_route_status.get((room_id, member_id)) == status:
                return
            self._peer_route_status[(room_id, member_id)] = status
        hosted_room_links.mark_room_link_status(
            self.db_path, room_id=room_id, member_id=member_id, status=status)

    def _refresh_pending_actions(self) -> None:
        """Refresh durable actions so a separate Dashboard sees gateway writes."""
        restored = hosted_room_actions.load_pending_actions(self.db_path)
        with self._state_lock:
            self._pending_actions = restored

    def _drop_stale_approval_actions(self, room_id: str) -> None:
        """Remove approvals whose exact in-memory attempt cannot still answer."""
        tasks = {
            task["identity"].task_id: str(task.get("status") or "")
            for task in driver.list_tasks(self.db_path, room_id=room_id)
        }
        with self._policy_lock, self._state_lock:
            stale = [
                (key, dict(action))
                for key, action in self._pending_actions.items()
                if key[0] == room_id
                and str(action.get("kind") or "") == "approval"
                and tasks.get(str(action.get("task_id") or "")) not in _APPROVABLE_STATUSES
            ]
        for key, _action in stale:
            self._set_pending_action(key[0], key[1], None)

    @staticmethod
    def _retry_action_id(task: Mapping[str, Any]) -> str:
        identity = task["identity"]
        return (
            f"retry:{identity.task_id}:"
            f"{int(task.get('execution_generation') or 0)}:"
            f"{int(task.get('cancel_generation') or 0)}"
        )

    def _set_pending_action(
        self, room_id: str, member_id: str, action: Mapping[str, Any] | None) -> None:
        # Durable first (hosted_room_actions survives a gateway restart), then the in-memory mirror.
        if action is None:
            hosted_room_actions.clear_pending_action(self.db_path, room_id=room_id, member_id=member_id)
            with self._policy_lock, self._state_lock:
                self._pending_actions.pop((room_id, member_id), None)
            return
        normalized = {**action, "member_id": member_id, "created_at": float(action.get("created_at") or time.time())}
        hosted_room_actions.set_pending_action(self.db_path, room_id=room_id, member_id=member_id, action=normalized)
        with self._policy_lock, self._state_lock:
            self._pending_actions[(room_id, member_id)] = normalized

    @contextlib.contextmanager
    def disbanding(self, room_id: str):
        """Fence new routes while Stop runs outside the publication and policy locks."""
        with self.peer_route_lock:
            if room_id in self._disbanding_rooms:
                raise RoomDisbandRefusedError(
                    "room_disbanding_retry", "The group chat is already disbanding; retry deletion.")
            self._disbanding_rooms.add(room_id)
        try:
            yield
        finally:
            with self.peer_route_lock:
                self._disbanding_rooms.remove(room_id)

    def route_authority(self, room_id: str) -> tuple[str, int]:
        """Check publication authority under peer_route_lock, including an in-progress Disband."""
        if room_id in self._disbanding_rooms:
            from hermes_state_runtime import RuntimeStoreError
            # Stop can still fail: this temporary refusal must not owe a surviving route's revoke.
            raise RuntimeStoreError("room_disbanding_retry")
        return self._owned_authority(room_id)

    def _rotate_route_grant(
        self, room_id: str, member_id: str, grant: str, catalog: GatewayRoomCatalog | None = None,
        previous: str | None = None) -> None:
        """Persist a target-refreshed scoped grant before publishing it live. A renewal of
        ``previous`` is published only while the route still holds that grant: a re-seat that
        landed meanwhile stays the route's, and the renewal serves only the turn that asked."""
        with self.peer_route_lock:
            self.route_authority(room_id)
            self._rotate_live_route_grant(room_id, member_id, grant, catalog, previous)

    def _rotate_live_route_grant(
        self, room_id: str, member_id: str, grant: str, catalog: GatewayRoomCatalog | None,
        previous: str | None) -> None:
        """Compare, persist and publish while registration and Disband are excluded."""
        key = (room_id, member_id)
        route = self.peer_routes.get(key)
        if route is None:
            raise OperatorSafeRoomError("peer room route is unavailable")
        if previous is not None and route.grant != previous:
            return
        stored = next((
            l for l in hosted_room_links.load_room_links(self.db_path)
            if (l.room_id, l.member_id) == key), None)
        if stored is None:
            raise RuntimeError("peer room route cannot be renewed before persistence")
        digests = {}
        if catalog is not None:
            if (
                catalog.installation_id != route.target_install_id
                or catalog.execution_policy.target_profile != route.target_profile
                or PROTOCOL_VERSION not in catalog.protocol_versions
                or "direct" not in catalog.link_modes or not catalog.text
                or catalog.execution_policy.policy_digest != route.execution_policy_digest):
                self._set_route_status(room_id, member_id, "needs_reauthorization")
                raise RuntimeError(
                    "peer room execution policy changed; reauthorization is required")
            digests = {
                "capability_digest": catalog.catalog_digest,
                "execution_policy_digest": catalog.execution_policy.policy_digest}
        self._save_link(
            room_id=room_id, member_id=member_id, target_url=stored.target_url,
            target_profile=stored.target_profile, grant=grant, catalog=catalog or stored.catalog,
            cancellation_scope_id=stored.cancellation_scope_id, trace_id=stored.trace_id)
        self._publish_route(key, replace(route, grant=grant, **digests))

    def _route_statuses(self, room_id: str | None = None) -> list[dict[str, str]]:
        with self._state_lock:
            rows = sorted(self._peer_route_status.items())
        return [
            {"room_id": key[0], "member_id": key[1], "status": status}
            for key, status in rows if room_id is None or key[0] == room_id]

    def _events(self, room_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = hosted_rooms.read_events(
                self.db_path, room_id=room_id, since_seq=cursor, limit=hosted_rooms.MAX_LOG_LIMIT)
            rows = page.get("events")
            if isinstance(rows, list):
                events.extend(row for row in rows if isinstance(row, dict))
            next_cursor = int(page.get("cursor") or cursor)
            if not page.get("has_more"):
                return events
            if next_cursor <= cursor:
                raise RuntimeError("hosted room replay cursor did not advance")
            cursor = next_cursor

    def _policy_snapshot(self, room: Mapping[str, Any]) -> PolicySnapshot:
        return self.policy_checkpoint.snapshot(
            room_id=str(room["room_id"]), latest_seq=int(room["latest_seq"]))

    def _publish_terminal_tasks(self, room: Mapping[str, Any]) -> bool:
        changed, room_id, local_profiles = False, str(room["room_id"]), self.room_profiles(room)
        cursor = int(room["latest_seq"])
        for task in self._list_tasks(room_id, _TERMINAL_STATUSES):
            status, execution_generation = task["status"], int(task["execution_generation"])
            if self.policy_checkpoint.publication_exists(
                room_id=room_id, task_id=task["identity"].task_id, status=status,
                execution_generation=execution_generation):
                continue
            task_events = self.policy_checkpoint.events_for_task(
                room_id=room_id, source_event_seq=int(task["payload"]["source_event_seq"]),
                input_context=task["payload"].get("input_context"), task_id=task["identity"].task_id)
            plan = discussion.reconstruct_task_plan(
                room, task_events, task, local_profiles=local_profiles)
            message_id = f"dmessage:{task['identity'].task_id.removeprefix('dtask:')}"
            if any(event.get("event_id") == message_id and event["kind"] == "message.member" for event in task_events):
                # Finish an already visible immutable reply even if a later request arrived.
                task_events = [event for event in task_events if event["kind"] != "message.user"
                               or int(event["seq"]) <= int(task["payload"]["source_event_seq"])]
            publication = discussion.plan_publication(
                room, task_events, plan, status=status, result=task.get("result"),
                execution_generation=execution_generation if status == "deferred" else None,
                local_profiles=local_profiles)
            for event in publication.events:
                appended = hosted_rooms.append_event(
                    self.db_path, **event.append_kwargs(room_id), expected_latest_seq=cursor)
                cursor = max(cursor, int(appended["seq"]))
            changed = True
        return changed

    def _append_room_status(
        self, room: Mapping[str, Any], decision: discussion.DiscussionDecision) -> None:
        if decision.discussion_event_id is None:
            return
        gateway_id, epoch = _authority(room)
        # A continued discussion completes again under a new id; the first completion keeps the legacy one.
        suffix = f":c{decision.continues}" if decision.continues else ""
        hosted_rooms.append_event(
            self.db_path, room_id=str(room["room_id"]),
            event_id=f"dactivity:{decision.discussion_event_id}:{decision.reason}{suffix}",
            kind="room.activity", actor={"kind": "gateway", "id": gateway_id},
            payload={
                "status": decision.status, "reason_code": decision.reason,
                "thread_id": decision.thread_id,
                "discussion_event_id": decision.discussion_event_id},
            authority_gateway_id=gateway_id, authority_epoch=epoch)

    def prepare_room(self, binding: HostedRoomBinding) -> None:
        with self._policy_lock:
            self._drop_stale_approval_actions(binding.room_id)
            room = self._room(binding.room_id)
            snapshot = self._policy_snapshot(room)  # sync() side effect feeds the publish below
            try:
                changed = self._publish_terminal_tasks(room)
            except hosted_rooms.EventCursorConflictError:
                # Rebuild publication next poll; the settled task is never readmitted.
                return
            if changed:
                room = self._room(binding.room_id)
                snapshot = self._policy_snapshot(room)
            self.policy_checkpoint.compact_completed(room_id=binding.room_id)
            driver.prune_published_terminal_tasks(
                self.db_path, room_id=binding.room_id, clock=self.runtime.clock)
            # Reconcile the manual task DAG against the log first: a worker turn dispatched from the DAG that has
            # now settled must complete its task (unblocking dependents) before we decide what to dispatch next.
            hosted_room_dag.sweep_completed(self, room)
            if next(iter(self._list_tasks(binding.room_id, _LIVE_STATUSES)), None) is not None:
                return
            # The snapshot holds one active discussion; its latest user message opened it.
            source_seq = max(
                (int(event["seq"]) for event in snapshot.events if event.get("kind") == "message.user"), default=None)
            decision = discussion.plan_next_task(
                room, list(snapshot.events), local_profiles=self.room_profiles(room),
                initial_watermarks=snapshot.watermarks, freeze_input_context=True, limits=self.discussion_limits,
                room_context=None if source_seq is None else room_context(
                    self.db_path, room_id=binding.room_id, before_seq=source_seq),
                task_dag_owners=hosted_room_dag.task_owners(self, binding.room_id))
            if decision.status == "task" and decision.task is not None:
                existing = driver.get_task_for_turn(self.db_path, decision.task.identity)
                legacy_payload = dict(decision.task.payload)
                legacy_payload.pop("input_context", None)
                if existing is not None and "input_context" in existing["payload"]:
                    # A rebuilt cache may add older committed context. The slot
                    # already belongs to its original, frozen admission.
                    prior_events = self.policy_checkpoint.events_for_task(
                        room_id=binding.room_id, source_event_seq=existing["payload"]["source_event_seq"],
                        input_context=existing["payload"]["input_context"], task_id=existing["identity"].task_id)
                    discussion.reconstruct_task_plan(room, prior_events, existing, local_profiles=self.room_profiles(room))
                    admitted = existing
                elif existing is not None and existing["payload"] == legacy_payload:
                    discussion.reconstruct_task_plan(room, list(snapshot.events), existing,
                                                     local_profiles=self.room_profiles(room))
                    admitted = existing
                else:
                    admitted = driver.admit_task(
                        self.db_path, decision.task.identity, payload=decision.task.payload, clock=time.time)
                # A stop can race the policy read from another process: re-read after admission
                # and cancel a task whose source event is now behind the room stop fence.
                fence = self._policy_snapshot(self._room(binding.room_id)).stopped_through_seq
                if decision.source_event_seq is not None and decision.source_event_seq < fence:
                    self.runtime.cancel(
                        admitted["identity"], cancel_id=f"stop-fence:{fence}", acknowledge=False)
            elif decision.status in {"settled", "bounded"}:
                self._append_room_status(room, decision)
            if decision.status == "idle":
                # The mention-driven scheduler has nothing to do. Only here —
                # never competing with a real turn — do we consult the manual
                # task DAG and auto-dispatch its next claimable task by appending
                # a targeted @mention anchor the existing scheduler executes and
                # publishes. This reuses 100% of the turn-coordinate/
                # reconstruction machinery instead of a parallel dispatcher.
                hosted_room_dag.autodispatch_next(self, room)
            elif decision.status == "route":
                self._start_router(binding, room, snapshot, decision)

    def profile_homes(self) -> Mapping[str, Path]:
        """Profile name -> home, for the router's member roles (``profile.yaml`` descriptions)."""
        return {name: self.root if name == "default" else self.root / "profiles" / name
                for name in self.local_profiles()}

    def _start_router(self, binding: HostedRoomBinding, room: Mapping[str, Any], snapshot: PolicySnapshot,
                      decision: discussion.DiscussionDecision) -> None:
        """Ask the router on its own thread; the Send RPC and the policy lock never wait on a model."""
        source = next(event for event in snapshot.events if event.get("event_id") == decision.discussion_event_id)
        context = room_context(self.db_path, room_id=binding.room_id, before_seq=int(source["seq"]))
        key = (binding.room_id, str(decision.discussion_event_id))
        self._router_calls.start(
            key, lambda: router_call.ask_router(root=self.root, db_path=self.db_path, room=room, discussion=source,
                                                context=context, homes=self.profile_homes()),
            lambda picked: self._record_route(binding, decision, picked))

    def _record_route(self, binding: HostedRoomBinding, asked: discussion.DiscussionDecision,
                      picked: tuple[str, ...] | None) -> None:
        """Write the router's answer only when the room still waits for exactly this message's route: a
        Stop or a newer message in the thread while the call ran makes the answer moot. Stop and Send append
        without the policy lock, so the write carries the log position the plan was read at and a changed
        log re-plans instead of recording a route over a Stop."""
        for _attempt in range(_FENCED_APPEND_ATTEMPTS):
            with self._policy_lock:
                room = self._room(binding.room_id)
                snapshot = self._policy_snapshot(room)
                current = discussion.plan_next_task(
                    room, list(snapshot.events), local_profiles=self.room_profiles(room),
                    initial_watermarks=snapshot.watermarks, freeze_input_context=True, limits=self.discussion_limits,
                    room_context=room_context(self.db_path, room_id=binding.room_id,
                                              before_seq=asked.source_event_seq),
                    task_dag_owners=hosted_room_dag.task_owners(self, binding.room_id))
                if (current.status, current.discussion_event_id) != ("route", asked.discussion_event_id):
                    return
                gateway_id, epoch = _authority(room)
                try:
                    hosted_rooms.append_event(
                        self.db_path, room_id=binding.room_id,
                        event_id=router.routed_event_id(str(asked.discussion_event_id)),
                        kind=router.ROUTED_EVENT_KIND, actor={"kind": "gateway", "id": gateway_id},
                        payload=router.routed_payload(discussion_event_id=str(asked.discussion_event_id),
                                                      thread_id=str(asked.thread_id), member_ids=picked),
                        authority_gateway_id=gateway_id, authority_epoch=epoch,
                        expected_latest_seq=int(room["latest_seq"]))
                except hosted_rooms.EventCursorConflictError:
                    continue
            break
        self.runtime.wakeup()

    def continue_room(self, room_id: str, *, discussion_event_id: str) -> dict[str, Any]:
        """The user's Continue on a discussion paused at its cap: one more budget of rounds and messages."""
        gateway_id, epoch = self._owned_authority(room_id)
        with self._policy_lock:
            event = self._append_continue(room_id, discussion_event_id, gateway_id, epoch)
        self._prepare_stalls.clear(room_id)
        binding = next((b for b in self.bindings() if b.room_id == room_id), None)
        if binding is not None:
            with contextlib.suppress(Exception):
                self._prepare_tracked(binding)
        self.runtime.wakeup()
        return event

    def _append_continue(self, room_id: str, discussion_event_id: str, gateway_id: str, epoch: int) -> dict[str, Any]:
        """Append ``room.continued`` only onto the exact log the pause was read from. Stop and Send append
        without the policy lock, so a Stop or a newer message landing between the read and the write must
        refuse the Continue (re-read once more), never reopen the discussion over them."""
        for _attempt in range(_FENCED_APPEND_ATTEMPTS):
            events = self._events(room_id)
            paused = router.paused_discussion(events, discussion_event_id)
            if paused is None:
                raise hosted_rooms.HostedRoomError("This discussion is not paused.")
            if paused["grant"] > router.MAX_DISCUSSION_CONTINUES:
                raise hosted_rooms.HostedRoomError("This discussion already used every Continue.")
            try:
                return hosted_rooms.append_event(
                    self.db_path, room_id=room_id, event_id=f"dcontinue:{discussion_event_id}:{paused['grant']}",
                    kind=router.CONTINUED_EVENT_KIND, actor={"kind": "gateway", "id": gateway_id},
                    payload={"discussion_event_id": discussion_event_id, "thread_id": paused["thread_id"],
                             "grant": paused["grant"]},
                    authority_gateway_id=gateway_id, authority_epoch=epoch,
                    expected_latest_seq=max((int(event["seq"]) for event in events), default=0))
            except hosted_rooms.EventCursorConflictError:
                continue
        raise hosted_rooms.HostedRoomError("The room kept changing; try Continue again.")

    def _prepare_tracked(self, binding: HostedRoomBinding, *, retry_stalled: bool = False) -> None:
        """``prepare_room`` with its failures counted per room: a stalled room is skipped (not retried)
        until the next Send or Retry, and is reported once in the room (hosted_room_prepare_stall)."""
        stalls = self._prepare_stalls
        if stalls.stalled(binding.room_id) and not retry_stalled:
            return
        try:
            self.prepare_room(binding)
        except Exception as exc:
            if stalls.failed(binding.room_id, exc):
                prepare_stall.report_stall(self.db_path, binding.room_id, exc)
            raise
        stalls.clear(binding.room_id)

    def publish_terminal(self, binding: HostedRoomBinding, task: Mapping[str, Any]) -> None:
        # A deferred peer still owns this generation; Retry resumes its existing run.
        if task["status"] != "deferred" or not self._member_is_peer(binding.room_id, task_member_id(task)):
            forget_task_progress(binding.room_id, task["identity"].task_id, int(task["execution_generation"]))
        self._prepare_tracked(binding)
        self.runtime.wakeup()

    def create_room(self, *, room_id: str, name: str, members: Any, admit: Any = None) -> dict[str, Any]:
        normalized = discussion.validate_roster(members, local_profiles=self.local_profiles())
        room = hosted_rooms.create_room(
            self.db_path, room_id=room_id, name=name,
            members=[
                {
                    "member_id": member.member_id, "profile": member.profile,
                    "handle": member.handle, "target": dict(member.target or {}),
                    **({"display_name": member.display_name} if member.display_name else {}),
                    # Persist the orchestration role so both the scheduler (plan_next_task reads it back off the
                    # stored roster) and the decider tool-whitelist (F1) see the decider. Omitted for plain
                    # workers so mesh rooms stay byte-identical.
                    **({"role": member.role} if member.role != discussion.WORKER_ROLE else {})}
                for member in normalized],
            authority_gateway_id=hosted_rooms.local_authority_gateway_id(), admit=admit)
        self.runtime.wakeup()
        return room

    def update_members(
        self, *, room_id: str, event_id: str, members: Any
    ) -> dict[str, Any]:
        """Replace a locally-owned room's roster after full discussion validation.

        Authority-fenced (``_owned_authority``) then re-validated through the same
        ``discussion.validate_roster`` gate ``create_room`` uses, so the 2-6
        bound, local-profile requirement, and unique handles/targets all hold
        for the *new* roster — not just the storage-layer shape check.
        """
        self._owned_authority(room_id)
        normalized = discussion.validate_roster(
            members,
            local_profiles=self.local_profiles(),
        )
        room = hosted_rooms.change_members(
            self.db_path,
            room_id=room_id,
            event_id=event_id,
            members=[
                {
                    "member_id": member.member_id,
                    "profile": member.profile,
                    "handle": member.handle,
                    "target": dict(member.target or {}),
                    **(
                        {"display_name": member.display_name}
                        if member.display_name
                        else {}
                    ),
                    **(
                        {"role": member.role}
                        if member.role != discussion.WORKER_ROLE
                        else {}
                    ),
                }
                for member in normalized
            ],
        )
        binding = next(
            (
                candidate
                for candidate in self.bindings()
                if candidate.room_id == room_id
            ),
            None,
        )
        if binding is not None:
            self.prepare_room(binding)
        self.runtime.wakeup()
        return room

    def send(
        self, *, room_id: str, event_id: str, payload: Any, actor_id: str = "desktop", recipient: str | None = None,
    ) -> dict[str, Any]:
        from gateway.session_hosted_attachments import append_user_event
        try:
            normalized = discussion.validate_user_payload(payload)
            gateway_id, epoch = self._owned_authority(room_id)
            room = self._room(room_id)
            # A roster that no longer resolves (a member's profile was deleted) is NOT refused here: upstream #143239
            # accepts the Send, never raises after the commit, and the room driver says so once (member.unavailable)
            # with a retryable prepare action. This supersedes the fork's earlier refuse-before-append check.
            recipient_member = None
            if recipient is not None:
                # Dashboard work requests name an explicit recipient: it must be on this room's frozen roster.
                requested = str(recipient).strip().lstrip("@").casefold()
                recipient_member = next((member for member in room.get("members") or []
                                         if str(member.get("handle") or "").casefold() == requested), None)
                if not requested or recipient_member is None:
                    raise hosted_rooms.HostedRoomError("recipient is not a member of this room")
            if recipient_member is not None and recipient_member.get("role") != discussion.DECIDER_ROLE:
                event = hosted_room_dag.append_direct_assignment(
                    self, room_id=room_id, event_id=event_id, payload=normalized,
                    owner=str(recipient_member["handle"]), gateway_id=gateway_id, epoch=epoch)
            else:
                event = append_user_event(
                    self, room_id=room_id, event_id=event_id, payload=normalized,
                    gateway_id=gateway_id, epoch=epoch, actor_id=actor_id or "desktop")
        except hosted_rooms.EventConflictError:
            raise  # this event id is already in the room
        except ValueError as exc:
            # Refused before the user event committed: nothing of this request is in the room.
            # Both groups.send dispatchers say so (``admitted: false``), so a client may hand the
            # words back; any later failure leaves the message posted and stays unmarked.
            exc.admitted = False
            raise
        # The committed event is the Send's result: a failure from here on is the room driver's,
        # which runs the same preparation on its next passes and, if it keeps failing, says so in
        # the room once. Raising would tell the client a message it can already see in the room was
        # not sent (#143239). A new message starts a fresh attempt streak for a stalled room.
        self._prepare_stalls.clear(room_id)
        try:
            binding = next((b for b in self.bindings() if b.room_id == room_id), None)
            if binding is not None:
                self._prepare_tracked(binding)
        except Exception:
            logger.warning("hosted room %s: preparing the room after a stored Send failed; "
                           "the room driver retries it", room_id, exc_info=True)
        self.runtime.wakeup()
        return event

    def _room_approval_stop_lock(self, room_id: str):
        if self._approval_stop_lock is not None:
            return self._approval_stop_lock
        with self._policy_lock:
            lock = self._approval_stop_locks.get(room_id)
            if lock is None:
                lock = self._approval_stop_locks[room_id] = threading.RLock()
            return lock

    def stop_room(
        self, room_id: str, *, cancel_id: str, require_acknowledged: bool = False) -> int:
        """Stop the room's work; the count leaves out a deferred turn on another gateway that its
        gateway has not confirmed stopped (``_stop_deferred_peer``): it stays deferred."""
        # Every accepted spelling of a room shares its lock and its cached approvals.
        room_id = hosted_rooms._room_id(room_id)
        with self._room_approval_stop_lock(room_id):
            return self._stop_room(room_id, cancel_id=cancel_id, require_acknowledged=require_acknowledged)

    def _stop_room(self, room_id: str, *, cancel_id: str, require_acknowledged: bool) -> int:
        gateway_id, epoch = self._owned_authority(room_id)
        fence = hosted_rooms.request_room_stop(
            self.db_path, room_id=room_id, cancel_id=cancel_id, expected_gateway_id=gateway_id,
            expected_epoch=epoch)
        pending, unconfirmed = 0, []
        with self._policy_lock:
            tasks = {
                (task["identity"].room_id, task["identity"].task_id): task
                for task in self._list_tasks(room_id, _STOPPABLE_STATUSES)
                # A replay retains its original fence; work admitted afterward belongs to the
                # next message. Disband must drain all work before deleting the room.
                if require_acknowledged or int(task["payload"]["source_event_seq"]) < int(fence["seq"])}
            # A stopped attempt's approval is spent: a concurrent Allow waiting on the lock finds nothing.
            stopped = {(task["identity"].task_id, task.get("execution_generation")) for task in tasks.values()}
            with self._state_lock:
                stopped_actions = [key for key, action in self._pending_actions.items() if key[0] == room_id
                                   and (action.get("task_id"), action.get("execution_generation")) in stopped]
            for _room_id, member_id in stopped_actions:
                hosted_room_actions.clear_pending_action(
                    self.db_path, room_id=room_id, member_id=member_id)
            with self._state_lock:
                for key in stopped_actions:
                    self._pending_actions.pop(key, None)
        # Stopping a member on another gateway waits on the network: never under the policy lock.
        # The snapshot only picks the tasks: the worker can defer a stopping turn at any point, so
        # cancel decides on its own read (``confirm_deferred_stop``), the count on the state it left.
        for task in tasks.values():
            own_cancel_id = (
                task.get("status") == "stopping" and str(task.get("cancel_id") or ""))
            result = self.runtime.cancel(task["identity"], cancel_id=own_cancel_id or cancel_id)
            if result["status"] == "stopping":
                pending += 1
            elif result["status"] == "deferred":
                unconfirmed.append(result)
        # Disband waits for every member's Stop, a member on another gateway included: a deferred
        # turn its gateway has not confirmed stopped holds it until confirmed or Discarded.
        if require_acknowledged and (pending or unconfirmed):
            raise self._stop_pending_refusal(room_id, unconfirmed) or hosted_rooms.RoomStopPendingError(
                "Group chat work is still stopping; retry deletion after Stop completes.")
        self.runtime.wakeup()
        return len(tasks) - len(unconfirmed)

    def _stop_pending_refusal(
            self, room_id: str, deferred: list[dict[str, Any]]) -> hosted_rooms.RoomStopPendingError | None:
        """A more specific refusal for Disband held by *deferred* turns (None: the generic one)."""
        return None

    def peer_admission_receipt(self, room_id: str, task: Mapping[str, Any]) -> dict[str, Any] | None:
        """The durable run handle this home holds for a peer attempt, or None when its admission
        was never confirmed (the member's gateway may or may not have the run)."""
        route = self.peer_routes.get((room_id, _member_of(task)))
        if route is None:
            return None
        room = self._room(room_id)
        return hosted_rooms.remote_run_receipt(self.db_path, record={
            "room_id": room_id, "home_install_id": route.home_install_id,
            "authority_gateway_id": str(room["authority_gateway_id"]),
            "authority_epoch": int(room["authority_epoch"]), "member_id": route.member_id,
            "target_install_id": route.target_install_id, "target_profile": route.target_profile,
            "task_id": task["identity"].task_id, "execution_generation": int(task["execution_generation"])})

    def _stop_deferred_peer(self, room_id: str, task: Mapping[str, Any]) -> bool:
        """Whether a deferred task may be cancelled here: for a member on this gateway, once the
        process serving it confirms (``deferred_stop_confirmed``). For a member on another
        gateway, only once that gateway answers that the exact run this home holds a receipt for
        has ended. Without a receipt nothing is sent: the admission is uncertain, and replaying it
        to find the run could start it. Deferral freed the room, so it is not held."""
        member = _member_of(task)
        if not self._member_is_peer(room_id, member):
            return self.runtime.deferred_stop_confirmed(room_id, task)
        route, client = self.peer_routes.get((room_id, member)), self.peer_clients.get((room_id, member))
        stop, bind_scope = _hook(client, "stop_receipt"), _hook(client, "bind_room_scope")
        if route is None or stop is None:
            return False
        room = self._room(room_id)
        try:
            if bind_scope is not None:
                bind_scope(
                    room_id=room_id, home_install_id=route.home_install_id,
                    authority_gateway_id=str(room["authority_gateway_id"]),
                    authority_epoch=int(room["authority_epoch"]), member_id=route.member_id,
                    target_install_id=route.target_install_id, target_profile=route.target_profile)
            result = stop(task_id=task["identity"].task_id,
                          execution_generation=int(task["execution_generation"]), grant=route.grant)
        except PeerRunsHTTPError as exc:
            if exc.error_code == "run_not_found" and self._close_missing_peer_run(room_id, task, client):
                return True
            self.runtime._record_error(f"stop of deferred task {task['identity'].task_id} is unconfirmed: {exc}")
            return False
        return result is not None and str(result.get("status") or "") in _PEER_RUN_ENDED

    def _close_missing_peer_run(self, room_id: str, task: Mapping[str, Any], client: Any) -> bool:
        """The member gateway holds no run under this home's receipt: its admission was refused
        after Discard found the reservation. Drop that receipt and ask by key again, which closes
        the key when nothing ran under it (``close_peer_admission``), never replaying the turn."""
        receipt, forget = self.peer_admission_receipt(room_id, task), _hook(client, "forget_receipt")
        if receipt is None or forget is None:
            return False
        forget(task_id=task["identity"].task_id, execution_generation=int(task["execution_generation"]),
               run_id=str(receipt["run_id"]))
        try:
            return self.close_peer_admission(HostedRoomBinding(room_id, *self._owned_authority(room_id)), task)
        except (PeerRunsHTTPError, RuntimeError) as exc:
            self.runtime._record_error(f"close of deferred task {task['identity'].task_id} is unconfirmed: {exc}")
            return False

    def retry_room_task(
        self, room_id: str, *, task_id: str, member_id: str | None = None,
        execution_generation: int | None = None) -> dict[str, Any]:
        """Retry one uncertain or deferred task only after explicit user action."""
        if task_id.startswith(prepare_stall.RETRY_TASK_PREFIX):
            return self._retry_preparation(room_id, task_id)
        candidates = self._list_tasks(room_id, _RETRYABLE_STATUSES)
        task = next((c for c in candidates if c["identity"].task_id == task_id
                     and member_id in (None, task_member_id(c))
                     and execution_generation in (None, c["execution_generation"])), None)
        if task is None:
            raise driver.InvalidTaskTransitionError("no retryable room task matches task_id")
        return self.runtime.retry_indeterminate(
            task["identity"], execution_generation=task["execution_generation"])

    def _retry_preparation(self, room_id: str, task_id: str) -> dict[str, Any]:
        """The stalled room's Retry: prepare once now; a failure stays stalled and is the RPC's error."""
        binding = next((b for b in self.bindings() if b.room_id == room_id), None)
        if (binding is None or task_id != f"{prepare_stall.RETRY_TASK_PREFIX}{room_id}"
                or not self._prepare_stalls.stalled(room_id)):
            raise driver.InvalidTaskTransitionError("no retryable room task matches task_id")
        try:
            self._prepare_tracked(binding, retry_stalled=True)
        except Exception as exc:
            self._prepare_stalls.stall(room_id, exc)
            raise
        self.runtime.wakeup()
        return {"identity": driver.TaskIdentity(room_id=room_id, task_id=task_id, thread_id=task_id, turn_id=task_id),
                "status": "prepared", "execution_generation": 1, "cancel_generation": 0}

    def approve_room_task(
        self, room_id: str, *, member_id: str, task_id: str, execution_generation: int,
        choice: str, request_id: str | None = None) -> Mapping[str, Any]:
        """Resolve one exact local or peer approval and wake room observation.

        Both the gateway and the dashboard run room turns; the approval may belong to a turn the OTHER
        process holds. Re-read the durable actions and, when this process does not own the member's
        session, queue the decision for the owner (as ``handle_action`` does) instead of reporting
        "no longer pending"."""
        room_id = hosted_rooms._room_id(room_id)
        key = (room_id, member_id)
        route, client = self.peer_routes.get(key), self.peer_clients.get(key)
        self._refresh_pending_actions()
        requested_approval_id = str(request_id or "")

        def matches(pending: Mapping[str, Any] | None) -> bool:
            return pending is not None and (
                str(pending.get("request_id") or ""), pending.get("task_id"),
                int(pending.get("execution_generation") or 0),
            ) == (requested_approval_id, task_id, execution_generation)
        # The live check and the dispatch hold the Stop lock, so a Stop either lands first (and this
        # Allow is refused) or after the member already has its answer.
        with self._room_approval_stop_lock(room_id):
            live = _approvable_attempts(driver.list_tasks(self.db_path, room_id=room_id))
            with self._policy_lock:
                with self._state_lock:
                    action = self._pending_actions.get(key)
                if not requested_approval_id or not matches(action):
                    raise RuntimeError("room approval is no longer pending")
                if (member_id, task_id, execution_generation) not in live:
                    hosted_room_actions.clear_pending_action(
                        self.db_path, room_id=room_id, member_id=member_id)
                    with self._state_lock:
                        self._pending_actions.pop(key, None)
                    raise RuntimeError("room approval is no longer pending")
            if choice not in {"once", "deny"}:
                raise RuntimeError("room approval choice must be once or deny")
            owns_session = getattr(self.rpc, "owns_session", None)
            if route is None and callable(owns_session) and not owns_session(str(action.get("session_id") or "")):
                outcome = hosted_room_actions.request_action_decision(
                    self.db_path, room_id=room_id, member_id=member_id,
                    action_id=str(action.get("action_id") or requested_approval_id),
                    decision={"choice": choice, "task_id": task_id, "execution_generation": execution_generation,
                              "request_id": requested_approval_id})
                return {"queued": True, "outcome": outcome}
            result = self._dispatch_approval(
                route, client, action, task_id=task_id, execution_generation=execution_generation,
                request_id=requested_approval_id, choice=choice)
            with self._policy_lock:
                if matches(self._pending_actions.get(key)):
                    hosted_room_actions.clear_pending_action(
                        self.db_path, room_id=room_id, member_id=member_id)
                    with self._state_lock:
                        self._pending_actions.pop(key, None)
        self.runtime.wakeup()
        return result

    def _dispatch_approval(
        self, route, client, action: Mapping[str, Any], *, task_id: str, execution_generation: int,
        request_id: str, choice: str) -> Mapping[str, Any]:
        approve = _hook(client, "approve_receipt")
        if route is not None and approve is not None:
            result = approve(
                task_id=task_id, execution_generation=execution_generation,
                request_id=request_id, choice=choice, grant=route.grant)
        else:
            session_id = str(action.get("session_id") or "")
            if not session_id:
                raise RuntimeError("local room approval identity is unavailable")
            result = self.rpc.approve(
                session_id=session_id, request_id=request_id, choice=choice)
        if result is None:
            raise RuntimeError("room approval target is unavailable")
        return result

    def status(self, room_id: str | None = None) -> dict[str, Any]:
        runtime = {**self.runtime.status(), "peer_routes": self._route_statuses(room_id)}
        if self._link_load_error:
            runtime["link_load_error"] = self._link_load_error
        if room_id is None:
            return runtime
        self._refresh_pending_actions()
        tasks = driver.list_tasks(self.db_path, room_id=room_id)
        counts = Counter(str(task["status"]) for task in tasks)
        pending_actions = [
            attempt_action("retry", task) for task in tasks if task["status"] in _RETRYABLE_STATUSES]
        if self._prepare_stalls.stalled(room_id):
            pending_actions.append(prepare_stall.retry_action(room_id, self._room(room_id)))
        # Stop can end the attempt without another info poll clearing its cached approval.
        # A late poll from an older generation must not offer that approval on the next attempt.
        approvable = _approvable_attempts(tasks)
        with self._state_lock:
            pending_actions.extend(
                dict(action) for (action_room_id, member_id), action in self._pending_actions.items()
                if action_room_id == room_id
                and (member_id, action.get("task_id"), action.get("execution_generation")) in approvable)
        # Who is answering right now, since when, and (when this process runs the turn) in which tool.
        member_activity = []
        for task in tasks:
            # Recovery keeps its durable uncertainty until a receipt arrives, even while the
            # peer confirms the same attempt is running. Project that observation as live work.
            recovered_active = (task["status"] == "indeterminate"
                and self._member_is_peer(room_id, task_member_id(task))
                and task_is_active(room_id, task["identity"].task_id, int(task["execution_generation"])))
            if task["status"] in _LIVE_STATUSES or recovered_active:
                member_activity.append(_member_activity({**task, "status": "running"} if recovered_active else task))
        return {
            "running": runtime["running"], "working": bool(member_activity),
            "blocked": room_id in runtime["blocked_rooms"]
            or bool(counts.get("indeterminate") or counts.get("stopping")),
            "counts": dict(counts), "pending_actions": pending_actions,
            "member_activity": member_activity,
            "last_error": self._prepare_stalls.last_error(room_id),
            "max_rounds": self.discussion_limits.max_rounds,
            "max_continues": router.MAX_DISCUSSION_CONTINUES,
            "routing_modes": list(ROUTING_MODES),
            "peer_routes": self._route_statuses(room_id)}

    # ── Agent team UI extensions ──────────────────────────────────────

    def topology(self, room_id: str) -> dict[str, Any] | None:
        """Return team topology: members with roles, states, and current tasks."""
        room = hosted_rooms.room_state(self.db_path, room_id=room_id)
        if room is None:
            return None
        members = room.get("members") or []
        if not members:
            return None

        # Build member role list from stored role data
        member_roles: list[dict[str, Any]] = []
        coordinator_id = None
        team_lead_id = None

        for m in members:
            handle = str(m.get("handle") or "")
            member_id = str(m.get("member_id") or "")
            profile = str(m.get("profile") or "")
            stored_role = str(m.get("role") or "worker")
            role = (
                "team_lead"
                if stored_role == discussion.DECIDER_ROLE
                else "teammate"
                if stored_role == discussion.WORKER_ROLE
                else stored_role
            )

            entry: dict[str, Any] = {
                "member_id": member_id,
                "handle": handle,
                "profile": profile,
                "role": role,
                "observer_state": None,
                "activity_level": None,
                "current_task": None,
                "current_task_id": None,
            }

            if role == "coordinator":
                coordinator_id = member_id
            elif role == "team_lead":
                team_lead_id = member_id
            elif role == "observer":
                # Derive observer state from driver
                entry["observer_state"] = self._derive_observer_state(room_id)
                entry["activity_level"] = None
            elif role == "teammate":
                entry["activity_level"] = self._derive_activity_level(room_id, member_id)
                # Fetch current task from C3 DAG
                task = self._current_task_for_member(room_id, handle)
                if task:
                    entry["current_task"] = task.get("subject")
                    entry["current_task_id"] = task.get("task_id")

            member_roles.append(entry)

        # Sort: coordinator → team_lead → teammate → observer
        _order = {"coordinator": 0, "team_lead": 1, "teammate": 2, "observer": 3}
        member_roles.sort(key=lambda x: _order.get(str(x["role"]), 99))

        # Derive current turn/round from driver tasks
        current_turn = None
        current_round = None
        max_rounds = None
        try:
            tasks = driver.list_tasks(self.db_path, room_id=room_id)
            for task in tasks:
                if task.get("status") in {"running", "queued"}:
                    tid = task.get("identity")
                    if tid and hasattr(tid, "turn_id"):
                        parsed = discussion._TURN_ID_RE.match(tid.turn_id)
                        if parsed:
                            current_turn = int(parsed.group("turn"))
                            current_round = int(parsed.group("round"))
                    break
            max_rounds = getattr(discussion, "MAX_DISCUSSION_ROUNDS", 5)
        except Exception:
            pass

        return {
            "room_id": room_id,
            "members": member_roles,
            "coordinator_id": coordinator_id,
            "team_lead_id": team_lead_id,
            "current_turn": current_turn,
            "current_round": current_round,
            "max_rounds": max_rounds,
        }

    def _derive_observer_state(self, room_id: str) -> str | None:
        """Derive observer state-machine state from driver / policy."""
        # v1: derive from pending_actions and driver status
        with self._policy_lock, self._state_lock:
            for (action_room_id, _member_id), action in self._pending_actions.items():
                if action_room_id == room_id and action.get("kind") == "observer":
                    return "armed"
        # Check if room has an observer member with activity
        try:
            room = hosted_rooms.room_state(self.db_path, room_id=room_id)
            if room:
                has_observer = any(
                    str(m.get("role") or "") == "observer"
                    for m in (room.get("members") or [])
                )
                if has_observer:
                    return "armed"
        except Exception:
            pass
        return None

    def _derive_activity_level(self, room_id: str, member_id: str) -> int | None:
        """Derive a 0-100 activity level for a teammate."""
        try:
            tasks = driver.list_tasks(self.db_path, room_id=room_id)
            member_tasks = [
                t for t in tasks
                if t.get("identity") and getattr(t["identity"], "member_id", None) == member_id
            ]
            total = len(tasks)
            if total == 0:
                return None
            completed = sum(1 for t in member_tasks if t.get("status") == "settled")
            return int((completed / max(total, 1)) * 100)
        except Exception:
            return None

    def _current_task_for_member(
        self, room_id: str, handle: str
    ) -> dict[str, Any] | None:
        """Return the current in-progress task for a member from the C3 DAG."""
        try:
            from gateway.room_task_dag import current_task_for_owner

            return current_task_for_owner(
                self.db_path, room_id=room_id, owner=handle
            )
        except Exception:
            return None

    def pending_actions(self, room_id: str) -> dict[str, Any]:
        """Return approvals and explicit retries awaiting operator action."""
        self._refresh_pending_actions()
        self._drop_stale_approval_actions(room_id)
        actions: list[dict[str, Any]] = []
        with self._policy_lock, self._state_lock:
            for (action_room_id, _member_id), action in self._pending_actions.items():
                if action_room_id == room_id:
                    raw_kind = str(action.get("kind") or "permission")
                    approval = action.get("approval")
                    approval_detail = (
                        dict(approval) if isinstance(approval, Mapping) else {}
                    )
                    detail = dict(action.get("detail") or {})
                    if raw_kind == "approval":
                        # The Dashboard only needs decision metadata. Never copy
                        # arbitrary approval fields such as command, args, cwd,
                        # or environment into this operator-facing read model.
                        detail = {
                            "tool_name": str(
                                detail.get("tool_name")
                                or approval_detail.get("tool_name")
                                or "terminal"
                            ),
                            "scope": str(
                                detail.get("scope")
                                or approval_detail.get("pattern_key")
                                or "once"
                            ),
                        }
                    actions.append({
                        "room_id": str(action.get("room_id") or room_id),
                        "action_id": str(action.get("action_id") or action.get("request_id") or ""),
                        "kind": "permission" if raw_kind == "approval" else raw_kind,
                        "description": str(
                            action.get("description")
                            or approval_detail.get("description")
                            or ""
                        ),
                        "from_handle": str(action.get("from_handle") or action.get("member_id") or ""),
                        "detail": detail,
                        "created_at": float(action.get("created_at") or 0),
                    })
        for task in driver.list_tasks(self.db_path, room_id=room_id):
            if str(task.get("status") or "") not in {"indeterminate", "deferred"}:
                continue
            identity = task["identity"]
            payload = task.get("payload") or {}
            actions.append({
                "room_id": room_id,
                "action_id": self._retry_action_id(task),
                "kind": "retry",
                "description": "Retry uncertain Room turn after restart",
                "from_handle": str(
                    payload.get("target_member_id")
                    or payload.get("target_profile")
                    or ""
                ),
                "detail": {
                    "task_id": identity.task_id,
                    "thread_id": identity.thread_id,
                    "status": str(task.get("status") or ""),
                    "execution_generation": int(
                        task.get("execution_generation") or 0
                    ),
                },
                "created_at": float(
                    task.get("indeterminate_at")
                    or task.get("updated_at")
                    or 0
                ),
            })
        return {"room_id": room_id, "actions": actions}

    def handle_action(
        self, room_id: str, action_id: str, decision: str
    ) -> dict[str, Any]:
        """Approve or deny one exact pending Room action."""
        if decision not in {"approve", "deny"}:
            return {"ok": False, "message": "Decision must be approve or deny"}
        self._refresh_pending_actions()
        self._drop_stale_approval_actions(room_id)
        if action_id.startswith("retry:"):
            if decision != "approve":
                return {"ok": False, "message": "Retry can only be approved"}
            retry_task = next(
                (
                    task
                    for task in driver.list_tasks(self.db_path, room_id=room_id)
                    if str(task.get("status") or "")
                    in {"indeterminate", "deferred"}
                    and self._retry_action_id(task) == action_id
                ),
                None,
            )
            if retry_task is None:
                return {"ok": False, "message": "Retry action is stale"}
            try:
                self.retry_room_task(
                    room_id, task_id=retry_task["identity"].task_id
                )
            except Exception as exc:
                return {"ok": False, "message": str(exc)}
            return {"ok": True, "message": "Room turn retry queued"}
        matched: tuple[tuple[str, str], dict[str, Any]] | None = None
        with self._policy_lock, self._state_lock:
            for key, action in self._pending_actions.items():
                if (
                    key[0] == room_id
                    and str(action.get("action_id") or action.get("request_id") or "")
                    == action_id
                ):
                    matched = (key, dict(action))
                    break
        if matched is None:
            return {"ok": False, "message": "Action not found"}
        key, action = matched
        kind = str(action.get("kind") or "")
        if kind in {"approval", "permission"}:
            session_id = str(action.get("session_id") or "")
            owns_session = getattr(self.rpc, "owns_session", None)
            if callable(owns_session) and not owns_session(session_id):
                try:
                    outcome = hosted_room_actions.request_action_decision(
                        self.db_path,
                        room_id=room_id,
                        member_id=key[1],
                        action_id=action_id,
                        decision={
                            "choice": "once" if decision == "approve" else "deny",
                            "task_id": str(action.get("task_id") or ""),
                            "execution_generation": int(
                                action.get("execution_generation") or 0
                            ),
                            "request_id": str(
                                action.get("request_id") or action_id
                            ),
                        },
                    )
                except hosted_room_actions.ActionDecisionConflictError as exc:
                    return {"ok": False, "message": str(exc)}
                return {
                    "ok": True,
                    "message": (
                        "Action decision queued"
                        if outcome == "queued"
                        else "Action decision already recorded"
                    ),
                }
            try:
                result = self.approve_room_task(
                    room_id,
                    member_id=key[1],
                    task_id=str(action.get("task_id") or ""),
                    execution_generation=int(
                        action.get("execution_generation") or 0
                    ),
                    choice="once" if decision == "approve" else "deny",
                    request_id=str(action.get("request_id") or action_id),
                )
            except Exception as exc:
                return {"ok": False, "message": str(exc)}
            return {
                "ok": True,
                "message": f"Action {decision}d",
                "result": dict(result),
            }
        self._set_pending_action(room_id, key[1], None)
        self.runtime.wakeup()
        return {"ok": True, "message": f"Action {decision}d"}

    def peer_grants(self, room_id: str) -> dict[str, Any]:
        """Return peer route grants with status, catalog, and linkage info."""
        routes = self._route_statuses(room_id)
        # Enrich with catalog info from stored links
        enriched: list[dict[str, Any]] = []
        with self._policy_lock:
            for r in routes:
                entry = dict(r)
                key = (r["room_id"], r["member_id"])
                route = self.peer_routes.get(key)
                if route is not None:
                    entry["target_profile"] = getattr(route, "target_profile", None)
                    entry["target_install_id"] = getattr(route, "target_install_id", None)
                    entry["capability_digest"] = getattr(route, "capability_digest", None)
                    entry["execution_policy_digest"] = getattr(route, "execution_policy_digest", None)
                    if hasattr(route, "catalog"):
                        cat = getattr(route, "catalog", None)
                        if cat is not None:
                            entry["execution_policy"] = (
                                cat.execution_policy.as_mapping()
                                if hasattr(cat, "execution_policy")
                                else None
                            )
                enriched.append(entry)
        return {"room_id": room_id, "peer_grants": enriched}

    def replication_health(self, room_id: str) -> dict[str, Any]:
        """Derive replication health from peer route statuses."""
        routes = self._route_statuses(room_id)
        total = len(routes)
        ready = sum(1 for r in routes if r["status"] == "ready")
        unavailable = sum(1 for r in routes if r["status"] == "unavailable")
        reauth = sum(1 for r in routes if r["status"] == "needs_reauthorization")
        healthy = total == 0 or (ready == total)
        return {
            "room_id": room_id,
            "healthy": healthy,
            "total_peers": total,
            "ready": ready,
            "unavailable": unavailable,
            "needs_reauthorization": reauth,
            "peers": routes,
        }

    def policy_trace(self, room_id: str) -> dict[str, Any]:
        """Return policy checkpoint snapshot for a room."""
        try:
            room = hosted_rooms.room_state(self.db_path, room_id=room_id)
            latest_seq = int(room.get("latest_seq") or 0) if room else 0
            snapshot = self.policy_checkpoint.snapshot(
                room_id=room_id, latest_seq=latest_seq
            )
            return {
                "room_id": room_id,
                "through_seq": snapshot.through_seq,
                "stopped_through_seq": snapshot.stopped_through_seq,
                "event_count": len(snapshot.events),
                "events": [
                    {
                        "seq": ev.get("seq"),
                        "kind": ev.get("kind"),
                        "actor": ev.get("actor"),
                        "created_at": ev.get("created_at"),
                    }
                    for ev in snapshot.events
                ],
                "watermarks": {
                    f"{k[0]}::{k[1]}": v
                    for k, v in (snapshot.watermarks or {}).items()
                },
            }
        except Exception:
            return {"room_id": room_id, "error": "policy trace unavailable"}


class _RouteStatusPeerClient:
    """Classify scoped-auth failures without exposing route credentials. Once its route is dropped
    (Disband), a transport still holding it sends nothing more under it."""

    def __init__(
        self, client, *, on_ready, on_reauthorization, on_unavailable, on_refreshed,
        live: Callable[[], bool] = lambda: True) -> None:
        self._client, self._live, self._on_ready, self._on_refreshed = client, live, on_ready, on_refreshed
        self._on_reauthorization, self._on_unavailable = on_reauthorization, on_unavailable

    def _refresh_grant(self, kwargs: dict) -> dict:
        """Rotate an expiring grant before dispatch; return the kwargs to send. Refresh
        failures escalate to reauthorization only when the peer says so or the grant is
        past its hard expiry; otherwise the original grant is tried as-is. A refreshed
        catalog whose digests drift from the dispatch is a policy change: refused."""
        grant = kwargs["grant"]
        if not room_grant_needs_dispatch_refresh(grant):
            return kwargs
        checked = HostedMemberDispatch.from_mapping(kwargs["dispatch"])
        refresh = _hook(self._client, "refresh_grant")
        if refresh is None:
            return kwargs
        try:
            refreshed = refresh(
                grant=grant, capability_digest=checked.capability_digest,
                execution_policy_digest=checked.execution_policy_digest)
        except Exception as exc:
            if getattr(exc, "needs_reauthorization", False) or (
                room_grant_needs_dispatch_refresh(grant, leeway_seconds=0)):
                self._on_reauthorization()
                raise
            return kwargs
        replacement = str(refreshed.get("grant") or "")
        if not replacement:
            raise RuntimeError("peer returned no refreshed room grant")
        refreshed_catalog = None
        if refreshed.get("catalog") is not None:
            refreshed_catalog = GatewayRoomCatalog.from_mapping(refreshed.get("catalog"))
            drift = digest_reauthorization_error(
                refreshed_catalog, capability_digest=checked.capability_digest,
                execution_policy_digest=checked.execution_policy_digest)
            if drift is not None:
                self._on_reauthorization()
                raise drift
        self._on_refreshed(replacement, refreshed_catalog, previous=grant)
        return {**kwargs, "grant": replacement}

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def tracked(*args, **kwargs):
            if not self._live():
                raise PeerRunsHTTPError("peer room route was dropped", not_admitted=True)
            if name in {"dispatch", "recover_dispatch"} and "grant" in kwargs:
                kwargs = self._refresh_grant(kwargs)
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                if getattr(exc, "needs_reauthorization", False):
                    self._on_reauthorization()
                elif getattr(exc, "not_admitted", False):
                    self._on_unavailable()
                raise
            if name != "prepare":
                self._on_ready()
            return result
        return tracked
