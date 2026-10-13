"""Production coordinator for same-gateway hosted Discussion rooms."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

from gateway import hosted_room_actions
from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_room_links
from gateway import hosted_rooms
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint, PolicySnapshot
from gateway.hosted_room_peer import (
    GatewayRoomCatalog, HostedMemberDispatch, PROTOCOL_VERSION, room_grant_needs_dispatch_refresh)
from tui_gateway.hosted_room_driver import HostedRoomBinding, HostedRoomRuntime
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
        hosted_rooms.prune_disbanded_rooms(self.db_path)
        self._policy_lock = threading.RLock()
        try:
            self._pending_actions = hosted_room_actions.load_pending_actions(
                self.db_path
            )
        except Exception:
            logger.warning(
                "hosted room pending actions could not be restored",
                exc_info=True,
            )
            self._pending_actions = {}
        self.policy_checkpoint = HostedRoomPolicyCheckpoint(self.db_path)
        self.rpc = HostedRoomServerRPC(server)
        self._link_load_error = None
        self._peer_route_status: dict[tuple[str, str], str] = {}
        self.peer_routes: dict[tuple[str, str], PeerMemberRoute] = {}
        self.peer_clients: dict[tuple[str, str], Any] = {}
        try:
            self._load_stored_links()
        except Exception as exc:
            self._link_load_error = str(exc)
        supplied_clients = dict(peer_clients or {})
        for key, route in dict(peer_routes or {}).items():
            self.peer_routes[key] = route
            client = supplied_clients.get(key, supplied_clients.get(route.target_install_id))
            if client is not None:
                self.peer_clients[key] = client
        self._action_relay_stop = threading.Event()
        self._action_relay_thread: threading.Thread | None = None
        self.runtime = HostedRoomRuntime(
            db_path=self.db_path, rooms=self.bindings, rpc=self.rpc,
            transport_resolver=self._resolve_member_transport, turn_lock=self._turn_lock,
            prepare_room=self.prepare_room, publish_terminal=self.publish_terminal,
            pending_action=self._set_pending_action,
            poll_interval_seconds=_HOSTED_ROOM_IDLE_FALLBACK_SECONDS,
            active_poll_interval_seconds=_HOSTED_ROOM_ACTIVE_POLL_SECONDS,
            turn_timeout_seconds=_hosted_room_turn_timeout_seconds())

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

    def bindings(self) -> tuple[HostedRoomBinding, ...]:
        local_gateway_id = hosted_rooms.local_authority_gateway_id()
        return tuple(
            HostedRoomBinding(str(room["room_id"]), *_authority(room))
            for room in hosted_rooms.list_rooms(self.db_path)
            if str(room["authority_gateway_id"]) == local_gateway_id)

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
            with self._policy_lock:
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
            self.peer_routes[key], self._peer_route_status[key] = route, "ready"
            if client is not None:
                self.peer_clients[key] = client

    def revoke_room_routes(self, room_id: str) -> int:
        """Revoke and forget every scoped peer route for one room; an unreachable target
        leaves the room intact for retry rather than a false disband with a live grant."""
        with self._policy_lock:
            routes = [(key, route) for key, route in self.peer_routes.items() if key[0] == room_id]
        for key, route in routes:
            revoke = _hook(self.peer_clients.get(key), "revoke_grant")
            if revoke is None:
                raise RuntimeError("peer room grant cannot be revoked safely")
            try:
                revoke(grant=route.grant)
            except PeerRunsHTTPError as exc:
                if not _grant_revoke_is_terminal(exc):
                    raise
        hosted_rooms.delete_room_link_records(self.db_path, room_id=room_id)
        with self._policy_lock:
            for key, _route in routes:
                for table in (self.peer_routes, self._peer_route_status, self.peer_clients):
                    table.pop(key, None)
        return len(routes)

    def _resolve_member_transport(self, binding: HostedRoomBinding, task: Mapping[str, Any]):
        payload = task.get("payload", {})
        member_id = str(payload.get("target_member_id") or payload.get("target_profile") or "")
        key = (binding.room_id, member_id)
        route = self.peer_routes.get(key)
        if route is None:
            if self._member_is_peer(binding.room_id, member_id):
                raise RuntimeError("peer room route is unavailable")
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
            client, on_ready=set_status("ready"),
            on_reauthorization=set_status("needs_reauthorization"),
            on_unavailable=set_status("unavailable"),
            on_refreshed=lambda grant, catalog=None: self._rotate_route_grant(
                *key, grant, catalog))
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
            or task.get("status") not in {"running", "indeterminate", "stopping"}):
            return
        prompt = payload.get("prompt")
        source_event_seq = int(payload.get("source_event_seq") or 0)
        if not isinstance(prompt, str) or source_event_seq < 1 or not route.trace_id:
            raise RuntimeError("peer room admission identity is unavailable for recovery")
        dispatch = build_member_dispatch(
            binding=binding, route=route, room_id=identity.room_id, task_id=identity.task_id,
            target_profile=route.target_profile, execution_generation=execution_generation,
            source_event_seq=source_event_seq, prompt=prompt, trace_id=route.trace_id)
        recover(dispatch=dispatch.as_mapping(), grant=route.grant)

    def _member_is_peer(self, room_id: str, member_id: str) -> bool:
        for m in self._room(room_id).get("members") or []:
            if isinstance(m, Mapping) and str(
                    m.get("member_id") or m.get("profile") or "") == member_id:
                target = m.get("target")
                return isinstance(target, Mapping) and target.get("kind") == "peer"
        return False

    def _set_route_status(self, room_id: str, member_id: str, status: str) -> None:
        with self._policy_lock:
            if self._peer_route_status.get((room_id, member_id)) == status:
                return
            self._peer_route_status[(room_id, member_id)] = status
        hosted_room_links.mark_room_link_status(
            self.db_path, room_id=room_id, member_id=member_id, status=status)

    def _refresh_pending_actions(self) -> None:
        """Refresh durable actions so a separate Dashboard sees gateway writes."""
        restored = hosted_room_actions.load_pending_actions(self.db_path)
        with self._policy_lock:
            self._pending_actions = restored

    def _drop_stale_approval_actions(self, room_id: str) -> None:
        """Remove approvals whose exact in-memory attempt cannot still answer."""
        tasks = {
            task["identity"].task_id: str(task.get("status") or "")
            for task in driver.list_tasks(self.db_path, room_id=room_id)
        }
        with self._policy_lock:
            stale = [
                (key, dict(action))
                for key, action in self._pending_actions.items()
                if key[0] == room_id
                and str(action.get("kind") or "") == "approval"
                and tasks.get(str(action.get("task_id") or ""))
                in {"indeterminate", "deferred", "settled", "failed", "cancelled"}
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
            with self._policy_lock:
                self._pending_actions.pop((room_id, member_id), None)
            return
        normalized = {**action, "member_id": member_id, "created_at": float(action.get("created_at") or time.time())}
        hosted_room_actions.set_pending_action(self.db_path, room_id=room_id, member_id=member_id, action=normalized)
        with self._policy_lock:
            self._pending_actions[(room_id, member_id)] = normalized

    def _rotate_route_grant(
        self, room_id: str, member_id: str, grant: str, catalog: GatewayRoomCatalog | None = None
    ) -> None:
        """Persist a target-refreshed scoped grant before publishing it live."""
        key = (room_id, member_id)
        route = self.peer_routes.get(key)
        if route is None:
            raise RuntimeError("peer room route is unavailable")
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
        with self._policy_lock:
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
        changed, room_id, local_profiles = False, str(room["room_id"]), self.local_profiles()
        for task in self._list_tasks(room_id, _TERMINAL_STATUSES):
            status, execution_generation = task["status"], int(task["execution_generation"])
            if self.policy_checkpoint.publication_exists(
                room_id=room_id, task_id=task["identity"].task_id, status=status,
                execution_generation=execution_generation):
                continue
            task_events = self.policy_checkpoint.events_for_task(
                room_id=room_id, source_event_seq=int(task["payload"]["source_event_seq"]))
            plan = discussion.reconstruct_task_plan(
                room, task_events, task, local_profiles=local_profiles)
            publication = discussion.plan_publication(
                room, task_events, plan, status=status, result=task.get("result"),
                execution_generation=execution_generation if status == "deferred" else None,
                local_profiles=local_profiles)
            for event in publication.events:
                hosted_rooms.append_event(self.db_path, **event.append_kwargs(room_id))
            changed = True
        return changed

    def _append_room_status(
        self, room: Mapping[str, Any], decision: discussion.DiscussionDecision) -> None:
        if decision.discussion_event_id is None:
            return
        gateway_id, epoch = _authority(room)
        hosted_rooms.append_event(
            self.db_path, room_id=str(room["room_id"]),
            event_id=f"dactivity:{decision.discussion_event_id}:{decision.reason}",
            kind="room.activity", actor={"kind": "gateway", "id": gateway_id},
            payload={
                "status": decision.status, "reason_code": decision.reason,
                "thread_id": decision.thread_id,
                "discussion_event_id": decision.discussion_event_id},
            authority_gateway_id=gateway_id, authority_epoch=epoch)

    def prepare_room(self, binding: HostedRoomBinding) -> None:
        with self._policy_lock:
            room = self._room(binding.room_id)
            snapshot = self._policy_snapshot(room)  # sync() side effect feeds the publish below
            if self._publish_terminal_tasks(room):
                room = self._room(binding.room_id)
                snapshot = self._policy_snapshot(room)
            self.policy_checkpoint.compact_completed(room_id=binding.room_id)
            driver.prune_published_terminal_tasks(
                self.db_path, room_id=binding.room_id, clock=self.runtime.clock)
            # Reconcile the manual task DAG against the log first: a worker turn dispatched from the DAG that has
            # now settled must complete its task (unblocking dependents) before we decide what to dispatch next.
            self._sweep_completed_dag_dispatches(room)
            if next(iter(self._list_tasks(binding.room_id, _LIVE_STATUSES)), None) is not None:
                return
            decision = discussion.plan_next_task(
                room, list(snapshot.events), local_profiles=self.local_profiles(),
                initial_watermarks=snapshot.watermarks)
            if decision.status == "task" and decision.task is not None:
                driver.admit_task(
                    self.db_path, decision.task.identity, payload=decision.task.payload,
                    clock=time.time)
                # A stop can race the policy read from another process: re-read after admission
                # and cancel a task whose source event is now behind the room stop fence.
                fence = self._policy_snapshot(self._room(binding.room_id)).stopped_through_seq
                if decision.source_event_seq is not None and decision.source_event_seq < fence:
                    self.runtime.cancel(decision.task.identity, cancel_id=f"stop-fence:{fence}")
            elif decision.status in {"settled", "bounded"}:
                self._append_room_status(room, decision)
            if decision.status == "idle":
                # The mention-driven scheduler has nothing to do. Only here —
                # never competing with a real turn — do we consult the manual
                # task DAG and auto-dispatch its next claimable task by appending
                # a targeted @mention anchor the existing scheduler executes and
                # publishes. This reuses 100% of the turn-coordinate/
                # reconstruction machinery instead of a parallel dispatcher.
                self._maybe_autodispatch_dag_task(binding, room)

    def _maybe_autodispatch_dag_task(
        self,
        binding: HostedRoomBinding,
        room: Mapping[str, Any],
    ) -> None:
        """Auto-dispatch the next claimable manual DAG task, if any.

        Must be called from ``prepare_room`` under ``self._policy_lock`` while the
        room is idle (no queued/running task, no pending user turn). Picks the
        lowest-seq available task whose subject names exactly one worker via
        ``@handle`` (the same routing the scheduler uses), atomically claims it,
        and appends a ``message.user`` anchor on a deterministic per-task thread
        so the worker's settled reply can be matched back and complete the task.
        Any failure is swallowed — auto-dispatch is best-effort and must never
        break the tested scheduling path.
        """

        try:
            from gateway import room_task_dag as _dag
        except Exception:
            return
        room_id = str(room["room_id"])
        try:
            tasks = _dag.list_claimable(self.db_path, room_id=room_id)
            members = discussion.validate_roster(
                room.get("members") or [],
                local_profiles=self.local_profiles(),
            )
        except Exception:
            return
        # Skip tasks that cannot name one unambiguous worker instead of letting
        # the first malformed subject permanently head-of-line block every later
        # valid task. The task stays pending for an operator to correct or remove.
        for task in tasks:
            targets = discussion.resolve_mentions(
                (str(task.get("subject") or ""),),
                members,
                default_all=False,
            )
            if len(targets) != 1:
                continue
            target = targets[0]
            anchor_thread = f"dagtask:{task['task_id']}"
            claimed = _dag.claim_task_for_dispatch(
                self.db_path,
                room_id=room_id,
                task_id=str(task["task_id"]),
                owner=target.handle,
                dispatch_thread_id=anchor_thread,
            )
            if claimed is None:
                continue
            # The subject was just resolved to exactly one target, so it already
            # contains the routing mention. Preserve it verbatim rather than
            # prepending the handle a second time in the durable event log/UI.
            anchor_text = str(task.get("subject") or "").strip()
            try:
                hosted_rooms.append_event(
                    self.db_path,
                    room_id=room_id,
                    event_id=f"dagdispatch:{task['task_id']}",
                    kind="message.user",
                    actor={"kind": "user", "id": "task-dag"},
                    payload={"text": anchor_text, "thread_id": anchor_thread},
                    authority_gateway_id=str(room["authority_gateway_id"]),
                    authority_epoch=int(room["authority_epoch"]),
                )
                # Wake the worker loop so the anchor is admitted this instant
                # rather than after a full poll interval.
                self.runtime.wakeup()
            except Exception:
                # Anchor append failed — release the claim so it retries next tick.
                try:
                    _dag.release_task(
                        self.db_path,
                        room_id=room_id,
                        task_id=str(task["task_id"]),
                    )
                except Exception:
                    pass
            return

    def _sweep_completed_dag_dispatches(self, room: Mapping[str, Any]) -> None:
        """Complete any auto-dispatched DAG task whose worker turn has settled.

        Only when the DAG has in_progress dispatches does it read the room log, and then only to find a settled
        member reply on one of those ``dagtask:`` anchor threads; the matching task is marked completed (which
        auto-unblocks its dependents on the next claim). Reading the durable log rather than the policy snapshot
        keeps completion robust across restarts and missed passes. Best-effort and idempotent.
        """
        try:
            from gateway import room_task_dag as _dag
        except Exception:
            return
        room_id = str(room["room_id"])
        try:
            pending_dispatches = _dag.list_in_progress_dispatches(self.db_path, room_id=room_id)
        except Exception:
            return
        # The common case is no manual DAG dispatch awaiting completion: return before touching the room log, so
        # prepare_room keeps the policy checkpoint's bounded-replay guarantee as rooms grow.
        pending_threads = {
            str(task.get("dispatch_thread_id") or "") for task in pending_dispatches if task.get("dispatch_thread_id")}
        if not pending_threads:
            return
        try:
            events = self._events(room_id)
        except Exception:
            return
        # A dispatch thread contains the decider's @worker instruction before
        # the worker answers.  Completing on any member message therefore marks
        # the DAG row done too early and can unlock dependants while the worker
        # turn is still running.  Match the claimed owner to the frozen roster
        # member, and require that exact worker message to be committed by its
        # corresponding turn.settled event.
        member_id_by_handle = {
            str(member.get("handle") or "").casefold(): str(
                member.get("member_id") or ""
            )
            for member in (room.get("members") or [])
            if isinstance(member, Mapping)
        }
        expected_member_by_thread = {
            str(task.get("dispatch_thread_id") or ""): member_id_by_handle.get(
                str(task.get("owner") or "").casefold(), ""
            )
            for task in pending_dispatches
            if task.get("dispatch_thread_id")
        }
        committed_message_ids = {
            str((event.get("payload") or {}).get("message_event_id") or "")
            for event in events
            if event.get("kind") == "turn.settled"
        }
        settled_threads: set[str] = set()
        for event in events:
            if event.get("kind") != "message.member":
                continue
            payload = event.get("payload") or {}
            thread_id = str(payload.get("thread_id") or "")
            if thread_id not in pending_threads:
                continue
            if str(payload.get("member_id") or "") != expected_member_by_thread.get(
                thread_id
            ):
                continue
            if str(event.get("event_id") or "") not in committed_message_ids:
                continue
            if discussion.is_pass_text(payload.get("text")):
                continue
            settled_threads.add(thread_id)
        for thread_id in settled_threads:
            try:
                completed = _dag.complete_dispatched(
                    self.db_path, room_id=room_id, dispatch_thread_id=thread_id
                )
            except Exception:
                continue
            if completed is not None:
                # A dependent may have just unblocked; re-run the scheduler
                # promptly instead of waiting out the poll interval.
                self.runtime.wakeup()

    def publish_terminal(self, binding: HostedRoomBinding, _task: Mapping[str, Any]) -> None:
        self.prepare_room(binding)
        self.runtime.wakeup()

    def create_room(self, *, room_id: str, name: str, members: Any) -> dict[str, Any]:
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
            authority_gateway_id=hosted_rooms.local_authority_gateway_id())
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
        normalized = discussion.validate_user_payload(payload)
        gateway_id, epoch = self._owned_authority(room_id)
        if recipient is not None:
            # Dashboard work requests name an explicit recipient: it must be on this room's frozen roster.
            requested = str(recipient).strip().lstrip("@").casefold()
            handles = {str(member.get("handle") or "").casefold() for member in self._room(room_id).get("members") or []}
            if not requested or requested not in handles:
                raise hosted_rooms.HostedRoomError("recipient is not a member of this room")
        event = hosted_rooms.append_event(
            self.db_path, room_id=room_id, event_id=event_id, kind="message.user",
            actor={"kind": "user", "id": actor_id or "desktop"}, payload=normalized,
            authority_gateway_id=gateway_id, authority_epoch=epoch)
        binding = next((b for b in self.bindings() if b.room_id == room_id), None)
        if binding is None:
            raise hosted_rooms.RoomNotFoundError("hosted room not found")
        self.prepare_room(binding)
        self.runtime.wakeup()
        return event

    def stop_room(
        self, room_id: str, *, cancel_id: str, require_acknowledged: bool = False) -> int:
        gateway_id, epoch = self._owned_authority(room_id)
        hosted_rooms.request_room_stop(
            self.db_path, room_id=room_id, cancel_id=cancel_id, expected_gateway_id=gateway_id,
            expected_epoch=epoch)
        pending = 0
        with self._policy_lock:
            tasks = {
                (task["identity"].room_id, task["identity"].task_id): task
                for task in self._list_tasks(room_id, _STOPPABLE_STATUSES)}
            for task in tasks.values():
                own_cancel_id = (
                    task.get("status") == "stopping" and str(task.get("cancel_id") or ""))
                result = self.runtime.cancel(task["identity"], cancel_id=own_cancel_id or cancel_id)
                if result["status"] == "stopping":
                    pending += 1
        if require_acknowledged and pending:
            raise RuntimeError("room work is still stopping; retry deletion after Stop completes")
        self.runtime.wakeup()
        return len(tasks)

    def retry_room_task(self, room_id: str, *, task_id: str) -> dict[str, Any]:
        """Retry one uncertain or deferred task only after explicit user action."""
        candidates = self._list_tasks(room_id, _RETRYABLE_STATUSES)
        task = next((c for c in candidates if c["identity"].task_id == task_id), None)
        if task is None:
            raise driver.InvalidTaskTransitionError("no retryable room task matches task_id")
        return self.runtime.retry_indeterminate(task["identity"])

    def approve_room_task(
        self, room_id: str, *, member_id: str, task_id: str, execution_generation: int,
        choice: str, request_id: str | None = None) -> Mapping[str, Any]:
        """Resolve one exact local or peer approval and wake room observation."""
        key = (room_id, member_id)
        route, client = self.peer_routes.get(key), self.peer_clients.get(key)
        with self._policy_lock:
            action = self._pending_actions.get(key)
        requested_approval_id = str(request_id or "")

        def matches(pending: Mapping[str, Any] | None) -> bool:
            return pending is not None and (
                str(pending.get("request_id") or ""), pending.get("task_id"),
                int(pending.get("execution_generation") or 0),
            ) == (requested_approval_id, task_id, execution_generation)
        if not requested_approval_id or not matches(action):
            raise RuntimeError("room approval is no longer pending")
        if choice not in {"once", "deny"}:
            raise RuntimeError("room approval choice must be once or deny")
        approve = _hook(client, "approve_receipt")
        if route is not None and approve is not None:
            result = approve(
                task_id=task_id, execution_generation=execution_generation,
                request_id=requested_approval_id, choice=choice, grant=route.grant)
        else:
            session_id = str(action.get("session_id") or "")
            if not session_id:
                raise RuntimeError("local room approval identity is unavailable")
            result = self.rpc.approve(
                session_id=session_id, request_id=requested_approval_id, choice=choice)
        if result is None:
            raise RuntimeError("room approval target is unavailable")
        with self._policy_lock:
            still_current = matches(self._pending_actions.get(key))
        if still_current:
            self._set_pending_action(room_id, member_id, None)  # clears the durable row too
        self.runtime.wakeup()
        return result

    def status(self, room_id: str | None = None) -> dict[str, Any]:
        runtime = {**self.runtime.status(), "peer_routes": self._route_statuses(room_id)}
        if self._link_load_error:
            runtime["link_load_error"] = self._link_load_error
        if room_id is None:
            return runtime
        self._refresh_pending_actions()
        self._drop_stale_approval_actions(room_id)
        tasks = driver.list_tasks(self.db_path, room_id=room_id)
        counts = Counter(str(task["status"]) for task in tasks)
        pending_actions = [
            {"kind": "retry", "task_id": task["identity"].task_id}
            for task in tasks if task["status"] in _RETRYABLE_STATUSES]
        with self._policy_lock:
            pending_actions.extend(
                dict(action) for (action_room_id, _member_id), action
                in self._pending_actions.items() if action_room_id == room_id)
        return {
            "running": runtime["running"], "working": any(counts.get(s) for s in _LIVE_STATUSES),
            "blocked": room_id in runtime["blocked_rooms"]
            or bool(counts.get("indeterminate") or counts.get("stopping")),
            "counts": dict(counts), "pending_actions": pending_actions,
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
        with self._policy_lock:
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
        with self._policy_lock:
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
        with self._policy_lock:
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

    def mailbox(self, room_id: str, member_id: str) -> dict[str, Any]:
        """Return mailbox messages for a member (replayed from event log)."""
        messages: list[dict[str, Any]] = []
        try:
            events = hosted_rooms.read_events(
                self.db_path, room_id=room_id, since_seq=0, limit=500
            )
            for ev in events.get("events", []):
                kind = str(ev.get("kind") or "")
                payload = ev.get("payload") or {}
                # Protocol messages addressed to this member
                if not kind.startswith(("coordinator.", "teammate.", "observer.", "heartbeat.", "shutdown.", "permission.", "plan.")):
                    continue
                target = str(payload.get("target_handle") or payload.get("to") or "")
                if target and target != member_id and target != str(ev.get("actor", {}).get("id", "")):
                    continue
                text = str(payload.get("text") or payload.get("summary") or "")
                messages.append({
                    "message_id": str(ev.get("event_id") or ""),
                    "member_id": member_id,
                    "kind": kind,
                    "summary": text[:200] if text else "",
                    "from_handle": str(payload.get("from_handle") or ev.get("actor", {}).get("id", "")),
                    "payload": payload,
                    "read": False,
                    "created_at": ev.get("created_at", 0),
                })
        except Exception:
            pass
        return {
            "room_id": room_id,
            "member_id": member_id,
            "messages": messages,
            "unread_count": len(messages),
        }

    def mark_mailbox_read(self, room_id: str, member_id: str) -> dict[str, Any]:
        """Mark all messages in a member's mailbox as read (no-op in v1)."""
        return {"ok": True, "message": "Mailbox marked as read"}

    def observer_status(self, room_id: str) -> dict[str, Any]:
        """Return observer state machine status for a room."""
        state = self._derive_observer_state(room_id)
        return {
            "room_id": room_id,
            "state": state or "armed",
            "current_turn": 0,
            "current_round": 0,
            "rules_checked": 0,
            "violations": 0,
            "last_heartbeat_at": None,
            "last_digest": None,
        }

    def pause_observer(self, room_id: str) -> dict[str, Any]:
        """Pause the observer for a room."""
        return {"ok": True, "message": "Observer paused"}

    def resume_observer(self, room_id: str) -> dict[str, Any]:
        """Resume the observer for a room."""
        return {"ok": True, "message": "Observer resumed"}

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
    """Classify scoped-auth failures without exposing route credentials."""

    def __init__(
        self, client, *, on_ready, on_reauthorization, on_unavailable, on_refreshed) -> None:
        self._client, self._on_ready, self._on_refreshed = client, on_ready, on_refreshed
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
        self._on_refreshed(replacement, refreshed_catalog)
        return {**kwargs, "grant": replacement}

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def tracked(*args, **kwargs):
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
