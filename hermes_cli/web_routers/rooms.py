"""Hosted-room dashboard routes (fork: Rooms / agent team).

Read-only inspector over the hosted-room store and the process-owned
HostedRoomService: roster, authority/driver status and event replay. The whole
``/api/`` tree is gated by the dashboard auth middleware; every payload here is
non-secret (members carry no tokens, peer-route status is a classification).
"""

import asyncio

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/rooms")
async def list_hosted_rooms(all: bool = False):
    """Return one bounded page of hosted rooms for the inspector list."""

    def _run():
        from gateway.hosted_rooms import default_db_path, list_rooms

        rooms = list_rooms(default_db_path(), include_disbanded=bool(all))
        return {
            "rooms": [
                {
                    "room_id": r.get("room_id"),
                    "name": r.get("name"),
                    "members": r.get("members") or [],
                    "revision": r.get("revision", 0),
                    "latest_seq": r.get("latest_seq", 0),
                    "authority_epoch": r.get("authority_epoch", 0),
                    "authority_gateway_id": r.get("authority_gateway_id"),
                    "created_at": r.get("created_at"),
                    "updated_at": r.get("updated_at"),
                    "disbanded_at": r.get("disbanded_at"),
                }
                for r in rooms
            ]
        }

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}")
async def get_hosted_room(room_id: str):
    """Return one room's replay/authority state plus live driver status."""

    def _run():
        from gateway.hosted_rooms import (
            HostedRoomError,
            RoomNotFoundError,
            default_db_path,
            room_state,
        )
        from tui_gateway.methods_groups import get_hosted_room_service

        try:
            room = room_state(
                default_db_path(), room_id=room_id, include_disbanded=True
            )
        except RoomNotFoundError:
            raise HTTPException(status_code=404, detail="room not found")
        except HostedRoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        driver_status = None
        service = get_hosted_room_service()
        if service is not None and room.get("disbanded_at") is None:
            try:
                driver_status = service.status(str(room.get("room_id")))
            except Exception:
                driver_status = None
        return {"room": room, "driver_status": driver_status}

    return await asyncio.to_thread(_run)


@router.get("/api/rooms/{room_id}/log")
async def get_hosted_room_log(room_id: str, since_seq: int = 0, limit: int = 100):
    """Return a bounded, monotonic slice of the append-only room event log."""

    def _run():
        from gateway.hosted_rooms import (
            HostedRoomError,
            RoomNotFoundError,
            default_db_path,
            read_events,
        )

        try:
            return read_events(
                default_db_path(),
                room_id=room_id,
                since_seq=since_seq,
                limit=limit,
                include_disbanded=True,
            )
        except RoomNotFoundError:
            raise HTTPException(status_code=404, detail="room not found")
        except HostedRoomError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return await asyncio.to_thread(_run)
