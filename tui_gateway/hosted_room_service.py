"""Production coordinator for same-gateway hosted Discussion rooms."""

from __future__ import annotations

import contextlib
import hashlib
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
from gateway.hosted_room_policy_checkpoint import (
    HostedRoomPolicyCheckpoint,
    PolicySnapshot,
)
from gateway.hosted_room_peer import (
    GatewayRoomCatalog,
    HostedMemberDispatch,
    PROTOCOL_VERSION,
)
from tui_gateway.hosted_room_driver import HostedRoomBinding, HostedRoomRuntime
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient, PeerRunsHTTPError
from tui_gateway.hosted_room_peer_transport import (
    HostedRoomPeerClient,
    PeerHostedRoomTransport,
    PeerMemberRoute,
)


logger = logging.getLogger("gateway.run")

_HOSTED_ROOM_IDLE_FALLBACK_SECONDS = 5.0
_HOSTED_ROOM_ACTIVE_POLL_SECONDS = 0.25
_HOSTED_ROOM_TERMINAL_GRACE_SECONDS = 30.0


def _hosted_room_turn_timeout_seconds() -> float:
    try:
        agent_timeout = float(os.getenv("HERMES_AGENT_TIMEOUT", "1800"))
    except (TypeError, ValueError):
        agent_timeout = 1800.0
    if agent_timeout <= 0:
        agent_timeout = 1800.0
    return agent_timeout + _HOSTED_ROOM_TERMINAL_GRACE_SECONDS


def _grant_revoke_is_terminal(exc: PeerRunsHTTPError) -> bool:
    """Return whether the peer proves the scoped grant is already unusable."""

    return exc.status_code in {401, 403} and exc.error_code in {
        "invalid_room_grant",
        "room_reauthorization_required",
    }


class HostedRoomService:
    """Own the hosted Discussion policy and its transport-free worker."""

    def __init__(
        self,
        server: ModuleType,
        *,
        db_path: Path | str | None = None,
        peer_routes: Mapping[tuple[str, str], PeerMemberRoute] | None = None,
        peer_clients: Mapping[Any, HostedRoomPeerClient] | None = None,
    ) -> None:
        self.server = server
        self.db_path = Path(db_path or hosted_rooms.default_db_path())
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
        self.peer_routes = {}
        self.peer_clients = {}
        try:
            stored_links, load_errors = hosted_room_links.load_room_links_tolerant(
                self.db_path
            )
            errors = list(load_errors)
            for stored in stored_links:
                if PROTOCOL_VERSION not in stored.catalog.protocol_versions:
                    errors.append(
                        f"{stored.room_id}:{stored.member_id}:protocol-upgrade-required"
                    )
                    continue
                client = PeerRunsHTTPClient(
                    base_url=stored.target_url,
                    api_key="",
                    receipt_db_path=self.db_path,
                )
                route = PeerMemberRoute(
                    home_install_id=hosted_rooms.local_authority_gateway_id(),
                    member_id=stored.member_id,
                    target_install_id=stored.catalog.installation_id,
                    target_profile=stored.target_profile,
                    capability_digest=stored.catalog.catalog_digest,
                    execution_policy_digest=(
                        stored.catalog.execution_policy.policy_digest
                    ),
                    cancellation_scope_id=stored.cancellation_scope_id,
                    trace_id=stored.trace_id,
                    grant=stored.grant,
                )
                self.peer_routes[(stored.room_id, stored.member_id)] = route
                self.peer_clients[(stored.room_id, stored.member_id)] = client
                self._peer_route_status[(stored.room_id, stored.member_id)] = (
                    stored.status
                )
            if errors:
                self._link_load_error = ",".join(errors)
        except Exception as exc:
            self._link_load_error = str(exc)
        supplied_routes = dict(peer_routes or {})
        supplied_clients = dict(peer_clients or {})
        self.peer_routes.update(supplied_routes)
        for key, route in supplied_routes.items():
            client = supplied_clients.get(key)
            if client is None:
                client = supplied_clients.get(route.target_install_id)
            if client is not None:
                self.peer_clients[key] = client
        self._action_relay_stop = threading.Event()
        self._action_relay_thread: threading.Thread | None = None
        self.runtime = HostedRoomRuntime(
            db_path=self.db_path,
            rooms=self.bindings,
            rpc=self.rpc,
            transport_resolver=self._resolve_member_transport,
            turn_lock=self._turn_lock,
            prepare_room=self.prepare_room,
            publish_terminal=self.publish_terminal,
            pending_action=self._set_pending_action,
            poll_interval_seconds=_HOSTED_ROOM_IDLE_FALLBACK_SECONDS,
            active_poll_interval_seconds=_HOSTED_ROOM_ACTIVE_POLL_SECONDS,
            turn_timeout_seconds=_hosted_room_turn_timeout_seconds(),
        )

    @property
    def root(self) -> Path:
        return self.db_path.parent

    def local_profiles(self) -> tuple[str, ...]:
        profiles = {"default"}
        profiles_dir = self.root / "profiles"
        if profiles_dir.is_dir():
            profiles.update(
                path.name for path in profiles_dir.iterdir() if path.is_dir()
            )
        return tuple(sorted(profiles))

    def bindings(self) -> tuple[HostedRoomBinding, ...]:
        local_gateway_id = hosted_rooms.local_authority_gateway_id()
        return tuple(
            HostedRoomBinding(
                room_id=str(room["room_id"]),
                gateway_id=str(room["authority_gateway_id"]),
                authority_epoch=int(room["authority_epoch"]),
            )
            for room in hosted_rooms.list_rooms(self.db_path)
            if str(room["authority_gateway_id"]) == local_gateway_id
        )

    def _owned_room(self, room_id: str) -> dict[str, Any]:
        room = hosted_rooms.room_state(self.db_path, room_id=room_id)
        if str(room["authority_gateway_id"]) != (
            hosted_rooms.local_authority_gateway_id()
        ):
            raise hosted_rooms.AuthorityConflictError(
                "This Group Chat is managed by another gateway."
            )
        return room

    @contextlib.contextmanager
    def _turn_lock(self, profile: str) -> Iterator[None]:
        from tools.bot_relay import acquire_turn_lock

        with acquire_turn_lock(self.root, profile):
            yield

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

    def register_peer_route(
        self,
        *,
        room_id: str,
        member_id: str,
        route: PeerMemberRoute,
        client: HostedRoomPeerClient,
        target_url: str | None = None,
        catalog: GatewayRoomCatalog | None = None,
    ) -> None:
        """Register one verified route and optionally persist its scoped grant."""
        bind_store = getattr(client, "bind_receipt_store", None)
        if callable(bind_store):
            bind_store(self.db_path)
        if catalog is not None:
            if not route.execution_policy_digest:
                route = replace(
                    route,
                    execution_policy_digest=(
                        catalog.execution_policy.policy_digest
                    ),
                )
            if (
                route.capability_digest != catalog.catalog_digest
                or route.execution_policy_digest
                != catalog.execution_policy.policy_digest
            ):
                raise ValueError("peer route does not match its target catalog")
        if target_url is not None and catalog is not None:
            hosted_room_links.save_room_link(
                self.db_path,
                hosted_room_links.make_stored_link(
                    room_id=room_id,
                    member_id=member_id,
                    target_url=target_url,
                    target_profile=route.target_profile,
                    grant=route.grant,
                    catalog=catalog,
                    cancellation_scope_id=route.cancellation_scope_id,
                    trace_id=route.trace_id,
                ),
            )
        # Persistence is the publication boundary. A failed disk write must
        # never leave a process-local route that disappears after restart.
        with self._policy_lock:
            self.peer_routes[(room_id, member_id)] = route
            self.peer_clients[(room_id, member_id)] = client
            self._peer_route_status[(room_id, member_id)] = "ready"
        self.runtime.wakeup()

    def revoke_room_routes(self, room_id: str) -> int:
        """Revoke and forget every scoped peer route for one room.

        The remote revocation is the boundary: if a target is unreachable the
        room remains intact and the user may retry rather than receiving a
        false successful disband while a grant is still live.
        """
        with self._policy_lock:
            routes = [
                (key, route)
                for key, route in self.peer_routes.items()
                if key[0] == room_id
            ]
        for key, route in routes:
            client = self.peer_clients.get(key)
            revoke = getattr(client, "revoke_grant", None)
            if not callable(revoke):
                raise RuntimeError("peer room grant cannot be revoked safely")
            try:
                revoke(grant=route.grant)
            except PeerRunsHTTPError as exc:
                if not _grant_revoke_is_terminal(exc):
                    raise

        hosted_rooms.delete_room_link_records(self.db_path, room_id=room_id)
        with self._policy_lock:
            for key, route in routes:
                self.peer_routes.pop(key, None)
                self._peer_route_status.pop(key, None)
                self.peer_clients.pop(key, None)
        return len(routes)

    def _resolve_member_transport(
        self,
        binding: HostedRoomBinding,
        task: Mapping[str, Any],
    ):
        payload = task.get("payload", {})
        member_id = str(
            payload.get("target_member_id") or payload.get("target_profile") or ""
        )
        route = self.peer_routes.get((binding.room_id, member_id))
        if route is None:
            if self._member_is_peer(binding.room_id, member_id):
                raise RuntimeError("peer room route is unavailable")
            return self.rpc
        client = self.peer_clients.get((binding.room_id, member_id))
        if client is None:
            raise RuntimeError("peer room client is unavailable")
        identity = task.get("identity")
        execution_generation = int(task.get("execution_generation") or 0)
        bind_observation = getattr(client, "bind_observation", None)
        if (
            callable(bind_observation)
            and isinstance(identity, driver.TaskIdentity)
            and execution_generation > 0
        ):
            bind_observation(
                task_id=identity.task_id,
                execution_generation=execution_generation,
            )
        tracked_client = _RouteStatusPeerClient(
            client,
            on_ready=lambda: self._set_route_status(
                binding.room_id, member_id, "ready"
            ),
            on_reauthorization=lambda: self._set_route_status(
                binding.room_id, member_id, "needs_reauthorization"
            ),
            on_unavailable=lambda: self._set_route_status(
                binding.room_id, member_id, "unavailable"
            ),
            on_refreshed=lambda grant, catalog=None: self._rotate_route_grant(
                binding.room_id, member_id, grant, catalog
            ),
        )
        self._recover_peer_admission(binding, task, route, tracked_client)
        return PeerHostedRoomTransport(
            binding=binding,
            route=route,
            client=tracked_client,
            source_event_seq=int(payload.get("source_event_seq") or 0),
            task_id=getattr(task.get("identity"), "task_id", None),
            execution_generation=int(task.get("execution_generation") or 0),
        )

    def _recover_peer_admission(
        self,
        binding: HostedRoomBinding,
        task: Mapping[str, Any],
        route: PeerMemberRoute,
        client: Any,
    ) -> None:
        """Rediscover an admitted peer run without advancing its generation."""
        recover = getattr(client, "recover_dispatch", None)
        identity = task.get("identity")
        payload = task.get("payload")
        execution_generation = int(task.get("execution_generation") or 0)
        if (
            not callable(recover)
            or not isinstance(identity, driver.TaskIdentity)
            or not isinstance(payload, Mapping)
            or execution_generation < 1
            or task.get("status") not in {"running", "indeterminate", "stopping"}
        ):
            return
        prompt = payload.get("prompt")
        source_event_seq = int(payload.get("source_event_seq") or 0)
        if not isinstance(prompt, str) or source_event_seq < 1 or not route.trace_id:
            raise RuntimeError("peer room admission identity is unavailable for recovery")
        dispatch = HostedMemberDispatch.from_mapping({
            "protocol_version": PROTOCOL_VERSION,
            "room_id": identity.room_id,
            "home_install_id": route.home_install_id,
            "authority_gateway_id": binding.gateway_id,
            "authority_epoch": binding.authority_epoch,
            "member_id": route.member_id,
            "target_install_id": route.target_install_id,
            "target_profile": route.target_profile,
            "task_id": identity.task_id,
            "execution_generation": execution_generation,
            "source_event_seq": source_event_seq,
            "cancellation_scope_id": route.cancellation_scope_id,
            "prompt": prompt,
            "prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "capability_digest": route.capability_digest,
            "execution_policy_digest": route.execution_policy_digest,
            "trace_id": route.trace_id,
        })
        recover(dispatch=dispatch.as_mapping(), grant=route.grant)

    def _member_is_peer(self, room_id: str, member_id: str) -> bool:
        room = hosted_rooms.room_state(self.db_path, room_id=room_id)
        for member in room.get("members") or []:
            if not isinstance(member, Mapping):
                continue
            if str(member.get("member_id") or member.get("profile") or "") != member_id:
                continue
            target = member.get("target")
            return isinstance(target, Mapping) and target.get("kind") == "peer"
        return False

    def _set_route_status(self, room_id: str, member_id: str, status: str) -> None:
        key = (room_id, member_id)
        with self._policy_lock:
            if self._peer_route_status.get(key) == status:
                return
            self._peer_route_status[key] = status
        hosted_room_links.mark_room_link_status(
            self.db_path,
            room_id=room_id,
            member_id=member_id,
            status=status,
        )

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
        self,
        room_id: str,
        member_id: str,
        action: Mapping[str, Any] | None,
    ) -> None:
        key = (room_id, member_id)
        if action is None:
            hosted_room_actions.clear_pending_action(
                self.db_path, room_id=room_id, member_id=member_id
            )
            with self._policy_lock:
                self._pending_actions.pop(key, None)
            return
        normalized = {
            **action,
            "member_id": member_id,
            "created_at": float(action.get("created_at") or time.time()),
        }
        hosted_room_actions.set_pending_action(
            self.db_path,
            room_id=room_id,
            member_id=member_id,
            action=normalized,
        )
        with self._policy_lock:
            self._pending_actions[key] = normalized

    def _rotate_route_grant(
        self,
        room_id: str,
        member_id: str,
        grant: str,
        catalog: GatewayRoomCatalog | None = None,
    ) -> None:
        """Persist a target-refreshed scoped grant before publishing it live."""
        key = (room_id, member_id)
        route = self.peer_routes.get(key)
        if route is None:
            raise RuntimeError("peer room route is unavailable")
        stored = next(
            (
                link
                for link in hosted_room_links.load_room_links(self.db_path)
                if (link.room_id, link.member_id) == key
            ),
            None,
        )
        if stored is None:
            raise RuntimeError("peer room route cannot be renewed before persistence")
        effective_catalog = catalog or stored.catalog
        if catalog is not None and (
            catalog.installation_id != route.target_install_id
            or catalog.execution_policy.target_profile != route.target_profile
            or PROTOCOL_VERSION not in catalog.protocol_versions
            or "direct" not in catalog.link_modes
            or not catalog.text
            or catalog.execution_policy.policy_digest
            != route.execution_policy_digest
        ):
            self._set_route_status(room_id, member_id, "needs_reauthorization")
            raise RuntimeError(
                "peer room execution policy changed; reauthorization is required"
            )
        rotated_route = replace(
            route,
            grant=grant,
            capability_digest=(
                catalog.catalog_digest
                if catalog is not None
                else route.capability_digest
            ),
            execution_policy_digest=(
                catalog.execution_policy.policy_digest
                if catalog is not None
                else route.execution_policy_digest
            ),
        )
        hosted_room_links.save_room_link(
            self.db_path,
            hosted_room_links.make_stored_link(
                room_id=room_id,
                member_id=member_id,
                target_url=stored.target_url,
                target_profile=stored.target_profile,
                grant=grant,
                catalog=effective_catalog,
                cancellation_scope_id=stored.cancellation_scope_id,
                trace_id=stored.trace_id,
            ),
        )
        with self._policy_lock:
            self.peer_routes[key] = rotated_route
            self._peer_route_status[key] = "ready"

    def _route_statuses(self, room_id: str | None = None) -> list[dict[str, str]]:
        with self._policy_lock:
            rows = [
                {
                    "room_id": key[0],
                    "member_id": key[1],
                    "status": status,
                }
                for key, status in self._peer_route_status.items()
                if room_id is None or key[0] == room_id
            ]
        return sorted(rows, key=lambda row: (row["room_id"], row["member_id"]))

    def _events(self, room_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = hosted_rooms.read_events(
                self.db_path,
                room_id=room_id,
                since_seq=cursor,
                limit=hosted_rooms.MAX_LOG_LIMIT,
            )
            rows = page.get("events")
            if isinstance(rows, list):
                events.extend(row for row in rows if isinstance(row, dict))
            next_cursor = int(page.get("cursor") or cursor)
            if not page.get("has_more"):
                return events
            if next_cursor <= cursor:
                raise RuntimeError("hosted room replay cursor did not advance")
            cursor = next_cursor

    def _append_plan(self, room_id: str, plan: discussion.PublicationPlan) -> None:
        for event in plan.events:
            hosted_rooms.append_event(
                self.db_path,
                **event.append_kwargs(room_id),
            )

    def _policy_snapshot(self, room: Mapping[str, Any]) -> PolicySnapshot:
        return self.policy_checkpoint.snapshot(
            room_id=str(room["room_id"]),
            latest_seq=int(room["latest_seq"]),
        )

    def _publish_terminal_tasks(
        self,
        room: Mapping[str, Any],
    ) -> bool:
        changed = False
        local_profiles = self.local_profiles()
        for status in ("deferred", "settled", "failed", "cancelled"):
            for task in driver.list_tasks(
                self.db_path,
                room_id=str(room["room_id"]),
                status=status,
            ):
                identity = task["identity"]
                if self.policy_checkpoint.publication_exists(
                    room_id=str(room["room_id"]),
                    task_id=identity.task_id,
                    status=status,
                    execution_generation=int(task["execution_generation"]),
                ):
                    continue
                task_events = self.policy_checkpoint.events_for_task(
                    room_id=str(room["room_id"]),
                    source_event_seq=int(task["payload"]["source_event_seq"]),
                )
                plan = discussion.reconstruct_task_plan(
                    room,
                    task_events,
                    task,
                    local_profiles=local_profiles,
                )
                publication = discussion.plan_publication(
                    room,
                    task_events,
                    plan,
                    status=status,
                    result=task.get("result"),
                    execution_generation=(
                        int(task["execution_generation"])
                        if status == "deferred"
                        else None
                    ),
                    local_profiles=local_profiles,
                )
                self._append_plan(str(room["room_id"]), publication)
                changed = True
        return changed

    def _append_room_status(
        self,
        room: Mapping[str, Any],
        decision: discussion.DiscussionDecision,
    ) -> None:
        if decision.discussion_event_id is None:
            return
        hosted_rooms.append_event(
            self.db_path,
            room_id=str(room["room_id"]),
            event_id=f"dactivity:{decision.discussion_event_id}:{decision.reason}",
            kind="room.activity",
            actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
            payload={
                "status": decision.status,
                "reason_code": decision.reason,
                "thread_id": decision.thread_id,
                "discussion_event_id": decision.discussion_event_id,
            },
            authority_gateway_id=str(room["authority_gateway_id"]),
            authority_epoch=int(room["authority_epoch"]),
        )

    def prepare_room(self, binding: HostedRoomBinding) -> None:
        with self._policy_lock:
            room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
            snapshot = self._policy_snapshot(room)
            events = list(snapshot.events)
            if self._publish_terminal_tasks(room):
                room = hosted_rooms.room_state(
                    self.db_path,
                    room_id=binding.room_id,
                )
                snapshot = self._policy_snapshot(room)
                events = list(snapshot.events)
            self.policy_checkpoint.compact_completed(room_id=binding.room_id)
            driver.prune_published_terminal_tasks(
                self.db_path,
                room_id=binding.room_id,
                clock=self.runtime.clock,
            )
            # Reconcile the manual task DAG against the log first: a worker turn
            # dispatched from the DAG that has now settled must complete its task
            # (unblocking dependents) before we decide what to dispatch next.
            self._sweep_completed_dag_dispatches(room)
            if any(
                driver.list_tasks(
                    self.db_path,
                    room_id=binding.room_id,
                    status=status,
                )
                for status in ("queued", "running", "stopping")
            ):
                return
            decision = discussion.plan_next_task(
                room,
                events,
                local_profiles=self.local_profiles(),
                initial_watermarks=snapshot.watermarks,
            )
            if decision.status == "task" and decision.task is not None:
                driver.admit_task(
                    self.db_path,
                    decision.task.identity,
                    payload=decision.task.payload,
                    clock=time.time,
                )
                # A stop can race the policy read from another process. Re-read
                # after admission and cancel before the runtime can execute a
                # task whose source event is now behind the room stop fence.
                fresh_room = hosted_rooms.room_state(
                    self.db_path,
                    room_id=binding.room_id,
                )
                stopped_through_seq = self._policy_snapshot(
                    fresh_room
                ).stopped_through_seq
                if (
                    decision.source_event_seq is not None
                    and decision.source_event_seq < stopped_through_seq
                ):
                    self.runtime.cancel(
                        decision.task.identity,
                        cancel_id=f"stop-fence:{stopped_through_seq}",
                    )
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

        Scans the room log for a settled member reply on a ``dagtask:`` anchor
        thread and marks the matching in_progress DAG task completed (which
        auto-unblocks its dependents on the next claim). Best-effort and
        idempotent; failures never break scheduling.
        """

        try:
            from gateway import room_task_dag as _dag
        except Exception:
            return
        room_id = str(room["room_id"])
        try:
            pending_dispatches = _dag.list_in_progress_dispatches(
                self.db_path, room_id=room_id
            )
        except Exception:
            return
        # The common case is no manual DAG dispatch awaiting completion. Do not
        # replay the entire room log merely to rediscover that fact: policy
        # checkpoint compaction relies on completed history staying off this hot
        # path as rooms grow.
        pending_threads = {
            str(task.get("dispatch_thread_id") or "")
            for task in pending_dispatches
            if task.get("dispatch_thread_id")
        }
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

    def publish_terminal(
        self,
        binding: HostedRoomBinding,
        _task: Mapping[str, Any],
    ) -> None:
        self.prepare_room(binding)
        self.runtime.wakeup()

    def create_room(self, *, room_id: str, name: str, members: Any) -> dict[str, Any]:
        normalized = discussion.validate_roster(
            members,
            local_profiles=self.local_profiles(),
        )
        room = hosted_rooms.create_room(
            self.db_path,
            room_id=room_id,
            name=name,
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
                    # Persist the orchestration role so both the scheduler
                    # (plan_next_task reads it back off the stored roster) and
                    # the decider tool-whitelist (F1) see the decider. Omitted
                    # for plain workers so mesh rooms stay byte-identical.
                    **(
                        {"role": member.role}
                        if member.role != discussion.WORKER_ROLE
                        else {}
                    ),
                }
                for member in normalized
            ],
            authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        )
        self.runtime.wakeup()
        return room

    def update_members(
        self, *, room_id: str, event_id: str, members: Any
    ) -> dict[str, Any]:
        """Replace a locally-owned room's roster after full discussion validation.

        Authority-fenced (``_owned_room``) then re-validated through the same
        ``discussion.validate_roster`` gate ``create_room`` uses, so the 2-6
        bound, local-profile requirement, and unique handles/targets all hold
        for the *new* roster — not just the storage-layer shape check.
        """
        self._owned_room(room_id)
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
        self,
        *,
        room_id: str,
        event_id: str,
        payload: Any,
        actor_id: str = "desktop",
    ) -> dict[str, Any]:
        normalized = discussion.validate_user_payload(payload)
        room = self._owned_room(room_id)
        event = hosted_rooms.append_event(
            self.db_path,
            room_id=room_id,
            event_id=event_id,
            kind="message.user",
            actor={"kind": "user", "id": actor_id or "desktop"},
            payload=normalized,
            authority_gateway_id=str(room["authority_gateway_id"]),
            authority_epoch=int(room["authority_epoch"]),
        )
        binding = next(
            (
                candidate
                for candidate in self.bindings()
                if candidate.room_id == room_id
            ),
            None,
        )
        if binding is None:
            raise hosted_rooms.RoomNotFoundError("hosted room not found")
        self.prepare_room(binding)
        self.runtime.wakeup()
        return event

    def stop_room(
        self,
        room_id: str,
        *,
        cancel_id: str,
        require_acknowledged: bool = False,
    ) -> int:
        room = self._owned_room(room_id)
        hosted_rooms.request_room_stop(
            self.db_path,
            room_id=room_id,
            cancel_id=cancel_id,
            expected_gateway_id=str(room["authority_gateway_id"]),
            expected_epoch=int(room["authority_epoch"]),
        )
        cancelled = 0
        pending = 0
        with self._policy_lock:
            tasks = {}
            for status in (
                "queued",
                "running",
                "indeterminate",
                "deferred",
                "stopping",
            ):
                for task in driver.list_tasks(
                    self.db_path,
                    room_id=room_id,
                    status=status,
                ):
                    identity = task["identity"]
                    tasks[(identity.room_id, identity.task_id)] = task
            for task in tasks.values():
                task_cancel_id = (
                    str(task.get("cancel_id") or "")
                    if task.get("status") == "stopping"
                    else ""
                )
                result = self.runtime.cancel(
                    task["identity"],
                    cancel_id=task_cancel_id or cancel_id,
                )
                cancelled += 1
                if result["status"] == "stopping":
                    pending += 1
        if require_acknowledged and pending:
            raise RuntimeError(
                "room work is still stopping; retry deletion after Stop completes"
            )
        self.runtime.wakeup()
        return cancelled

    def retry_room_task(self, room_id: str, *, task_id: str) -> dict[str, Any]:
        """Retry one uncertain or deferred task only after explicit user action."""

        task = next(
            (
                candidate
                for status in ("indeterminate", "deferred")
                for candidate in driver.list_tasks(
                    self.db_path, room_id=room_id, status=status
                )
                if candidate["identity"].task_id == task_id
            ),
            None,
        )
        if task is None:
            raise driver.InvalidTaskTransitionError(
                "no retryable room task matches task_id"
            )
        return self.runtime.retry_indeterminate(task["identity"])

    def approve_room_task(
        self,
        room_id: str,
        *,
        member_id: str,
        task_id: str,
        execution_generation: int,
        choice: str,
        request_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Resolve one exact local or peer approval and wake room observation."""
        key = (room_id, member_id)
        route = self.peer_routes.get(key)
        client = self.peer_clients.get(key)
        with self._policy_lock:
            action = self._pending_actions.get(key)
        requested_approval_id = str(request_id or "")
        pending_approval_id = str((action or {}).get("request_id") or "")
        if (
            action is None
            or action.get("task_id") != task_id
            or int(action.get("execution_generation") or 0)
            != execution_generation
            or not requested_approval_id
            or not pending_approval_id
            or requested_approval_id != pending_approval_id
        ):
            raise RuntimeError("room approval is no longer pending")
        if choice not in {"once", "deny"}:
            raise RuntimeError("room approval choice must be once or deny")
        approve = getattr(client, "approve_receipt", None)
        if route is not None and callable(approve):
            result = approve(
                task_id=task_id,
                execution_generation=execution_generation,
                request_id=requested_approval_id,
                choice=choice,
                grant=route.grant,
            )
        else:
            session_id = str(action.get("session_id") or "")
            if not session_id:
                raise RuntimeError("local room approval identity is unavailable")
            result = self.rpc.approve(
                session_id=session_id,
                request_id=requested_approval_id,
                choice=choice,
            )
        if result is None:
            raise RuntimeError("room approval target is unavailable")
        with self._policy_lock:
            current = self._pending_actions.get(key)
            still_current = (
                current is not None
                and str(current.get("request_id") or "") == requested_approval_id
                and current.get("task_id") == task_id
                and int(current.get("execution_generation") or 0)
                == execution_generation
            )
        if still_current:
            self._set_pending_action(room_id, member_id, None)
        self.runtime.wakeup()
        return result

    def status(self, room_id: str | None = None) -> dict[str, Any]:
        runtime = self.runtime.status()
        runtime = {**runtime, "peer_routes": self._route_statuses(room_id)}
        if self._link_load_error:
            runtime = {**runtime, "link_load_error": self._link_load_error}
        if room_id is None:
            return runtime
        self._refresh_pending_actions()
        self._drop_stale_approval_actions(room_id)
        tasks = driver.list_tasks(self.db_path, room_id=room_id)
        counts = Counter(str(task["status"]) for task in tasks)
        pending_actions = [
            {
                "kind": "retry",
                "task_id": task["identity"].task_id,
            }
            for task in tasks
            if task["status"] in {"indeterminate", "deferred"}
        ]
        with self._policy_lock:
            pending_actions.extend(
                dict(action)
                for (
                    action_room_id,
                    _member_id,
                ), action in self._pending_actions.items()
                if action_room_id == room_id
            )
        return {
            "running": runtime["running"],
            "working": bool(
                counts.get("running") or counts.get("queued") or counts.get("stopping")
            ),
            "blocked": room_id in runtime["blocked_rooms"]
            or bool(counts.get("indeterminate") or counts.get("stopping")),
            "counts": dict(counts),
            "pending_actions": pending_actions,
            "peer_routes": self._route_statuses(room_id),
        }

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
            role = str(m.get("role") or "teammate")

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
            from gateway import hosted_room_discussion as discussion
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
        self,
        client,
        *,
        on_ready,
        on_reauthorization,
        on_unavailable,
        on_refreshed,
    ) -> None:
        self._client = client
        self._on_ready = on_ready
        self._on_reauthorization = on_reauthorization
        self._on_unavailable = on_unavailable
        self._on_refreshed = on_refreshed

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def tracked(*args, **kwargs):
            if name in {"dispatch", "recover_dispatch"} and "grant" in kwargs:
                from gateway.hosted_room_peer import (
                    room_grant_needs_dispatch_refresh,
                )

                grant = kwargs["grant"]
                if room_grant_needs_dispatch_refresh(grant):
                    checked = HostedMemberDispatch.from_mapping(
                        kwargs["dispatch"]
                    )
                    refresh = getattr(self._client, "refresh_grant", None)
                    if callable(refresh):
                        try:
                            refreshed = refresh(
                                grant=grant,
                                capability_digest=checked.capability_digest,
                                execution_policy_digest=(
                                    checked.execution_policy_digest
                                ),
                            )
                        except Exception as exc:
                            if bool(
                                getattr(exc, "needs_reauthorization", False)
                            ):
                                self._on_reauthorization()
                                raise
                            if room_grant_needs_dispatch_refresh(
                                grant, leeway_seconds=0
                            ):
                                self._on_reauthorization()
                                raise
                        else:
                            replacement = str(refreshed.get("grant") or "")
                            if not replacement:
                                raise RuntimeError(
                                    "peer returned no refreshed room grant"
                                )
                            refreshed_catalog = None
                            if refreshed.get("catalog") is not None:
                                from gateway.hosted_room_peer import (
                                    GatewayRoomCatalog,
                                )

                                refreshed_catalog = GatewayRoomCatalog.from_mapping(
                                    refreshed.get("catalog")
                                )
                                if (
                                    refreshed_catalog.execution_policy.policy_digest
                                    != checked.execution_policy_digest
                                ):
                                    self._on_reauthorization()
                                    raise PeerRunsHTTPError(
                                        "peer room execution policy needs reauthorization",
                                        status_code=403,
                                        error_code="room_execution_policy_changed",
                                        not_admitted=True,
                                    )
                                if (
                                    refreshed_catalog.catalog_digest
                                    != checked.capability_digest
                                ):
                                    self._on_reauthorization()
                                    raise PeerRunsHTTPError(
                                        "peer room capabilities need reauthorization",
                                        status_code=403,
                                        error_code="room_capability_catalog_changed",
                                        not_admitted=True,
                                    )
                            self._on_refreshed(replacement, refreshed_catalog)
                            kwargs = {**kwargs, "grant": replacement}
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                if bool(getattr(exc, "needs_reauthorization", False)):
                    self._on_reauthorization()
                    raise
                elif bool(getattr(exc, "not_admitted", False)):
                    self._on_unavailable()
                    raise
                else:
                    raise
            if name != "prepare":
                self._on_ready()
            return result

        return tracked
