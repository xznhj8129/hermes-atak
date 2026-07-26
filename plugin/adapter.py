"""Passive ATAK/CoT messaging adapter for the Hermes gateway.

The adapter contains transport and translation logic only. It does not call a
chat model, generate responses, inspect phrases, or implement agent behavior.
Inbound GeoChat is handed to ``BasePlatformAdapter.handle_message`` and the
normal gateway later calls ``send`` with the final response.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

try:
    from frogcot import (
        ATAKClient,
        CoTParseError,
        GeoPoint,
        GeoChat,
        Marker,
        PersistentCoTClient,
        SituationalAwareness,
    )
except ImportError:  # discovery can occur before optional dependencies install
    ATAKClient = None
    CoTParseError = ValueError
    GeoPoint = None
    GeoChat = None
    Marker = None
    PersistentCoTClient = None
    SituationalAwareness = None

try:
    from froggeolib import GPSposition as FrogGPSPosition
    from froggeolib import gps_to_vector as froggeo_gps_to_vector
except ImportError:
    FrogGPSPosition = None
    froggeo_gps_to_vector = None


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Peer:
    uid: str
    callsign: str


@dataclass(frozen=True)
class _Room:
    id: str
    name: str


@dataclass(frozen=True)
class _ChatEnvelope:
    sender_uid: str
    sender_callsign: str
    message_id: str
    room: Optional[str]
    direct: bool


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(parent: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if parent is None:
        return None
    return next((item for item in parent if _local_name(item.tag) == name), None)


def _configured(extra: dict, key: str) -> str:
    value = extra.get(key)
    return str(value).strip() if value is not None else ""


class ATAKAdapter(BasePlatformAdapter):
    """Persistent CoT transport and GeoChat-to-Hermes translation adapter."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("atak"))
        extra = getattr(config, "extra", {}) or {}

        self.host = _configured(extra, "host")
        self.port = int(extra.get("port", 8089))
        self.ca = _configured(extra, "ca")
        self.client_certificate = _configured(extra, "client_certificate")
        self.client_key = _configured(extra, "client_key")
        self.server_hostname = _configured(extra, "server_hostname") or self.host
        self.callsign = _configured(extra, "callsign")
        self.uid = _configured(extra, "uid")
        self.cot_type = _configured(extra, "cot_type") or "a-f-G-U-C"
        self.receive_timeout = max(0.1, float(extra.get("receive_timeout", 1.0)))
        self.reconnect_initial = max(0.1, float(extra.get("reconnect_initial", 1.0)))
        self.reconnect_max = max(
            self.reconnect_initial, float(extra.get("reconnect_max", 30.0))
        )
        self.position = self._parse_position(extra.get("position"))
        self.ots_python = _configured(extra, "ots_python")
        self.ots_config = _configured(extra, "ots_config")
        self.ots_snapshot_ttl = max(0.0, float(extra.get("ots_snapshot_ttl", 2.0)))

        self.situational_awareness = SituationalAwareness() if SituationalAwareness else None
        self._atak = ATAKClient(self.callsign, cottype=self.cot_type, is_self=True) if ATAKClient else None
        if self._atak is not None and self.uid:
            # frogcot creates a random UID by default; a configured stable UID
            # is required so peers and direct-message routing survive reconnects.
            self._atak.uid = self.uid

        self._client_factory: Callable[..., Any] = PersistentCoTClient
        self._client = None
        self._receive_task: Optional[asyncio.Task] = None
        self._presence_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._peers: Dict[str, _Peer] = {}
        self._rooms: Dict[str, _Room] = {}
        self._receipts: Dict[str, Dict[str, str]] = {}
        self._server_markers: Dict[str, Any] = {}
        self._ots_snapshot_at = 0.0
        self._ots_snapshot_error: Optional[str] = None

        # A configured home channel makes proactive cross-platform sends work
        # before the peer has sent a message during this process lifetime.
        home = getattr(config, "home_channel", None)
        home_uid = str(getattr(home, "chat_id", "") or "").strip()
        if home_uid:
            home_name = str(getattr(home, "name", "") or home_uid).strip()
            self._peers[home_uid] = _Peer(home_uid, home_name)

    @property
    def name(self) -> str:
        return "ATAK / CoT"

    @staticmethod
    def _parse_position(value: Any) -> Optional[dict]:
        if not isinstance(value, dict):
            return None
        try:
            return {
                "lat": float(value["lat"]),
                "lon": float(value["lon"]),
                "alt": float(value.get("alt", 0.0)),
                "ce": float(value.get("ce", 9999999.0)),
                "le": float(value.get("le", 9999999.0)),
            }
        except (KeyError, TypeError, ValueError):
            raise ValueError("ATAK position requires numeric lat/lon and optional alt/ce/le")

    def _missing_config(self) -> list[str]:
        required = {
            "host": self.host,
            "ca": self.ca,
            "client_certificate": self.client_certificate,
            "client_key": self.client_key,
            "callsign": self.callsign,
            "uid": self.uid,
            "position": self.position,
        }
        return [name for name, value in required.items() if not value]

    def _new_client(self):
        if self._client_factory is None:
            raise RuntimeError("frogcot PersistentCoTClient is unavailable")
        return self._client_factory(
            host=self.host,
            port=self.port,
            ca=self.ca,
            client_certificate=self.client_certificate,
            client_key=self.client_key,
            server_hostname=self.server_hostname,
        )

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if PersistentCoTClient is None or self.situational_awareness is None or self._atak is None:
            self._set_fatal_error(
                "dependency_missing", "frogcot is not importable", retryable=False
            )
            return False
        missing = self._missing_config()
        if missing:
            message = "missing ATAK platform extra: " + ", ".join(missing)
            logger.error(message)
            self._set_fatal_error("config_missing", message, retryable=False)
            return False
        if self._receive_task and not self._receive_task.done():
            return True

        self._stopping.clear()
        try:
            await self._open_and_identify()
        except Exception as exc:
            logger.warning("ATAK: initial mutual-TLS connection failed: %s", exc)
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False

        self._mark_connected()
        self._receive_task = asyncio.create_task(
            self._receive_loop(), name="atak-cot-receive"
        )
        self._presence_task = asyncio.create_task(
            self._presence_loop(), name="atak-cot-presence"
        )
        logger.info("ATAK: connected to %s:%s as %s", self.host, self.port, self.callsign)
        return True

    async def _open_client(self) -> None:
        client = self._new_client()
        try:
            await asyncio.to_thread(client.connect)
        except BaseException:
            await asyncio.to_thread(client.close)
            raise
        self._client = client

    async def _open_and_identify(self) -> None:
        await self._open_client()
        try:
            await self._send_presence()
        except BaseException:
            client, self._client = self._client, None
            if client is not None:
                await asyncio.to_thread(client.close)
            raise

    async def _send_presence(self) -> None:
        client = self._client
        if client is not None and self.position is not None:
            await asyncio.to_thread(client.send, self._atak.pli(self.position))

    async def _presence_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    await self._send_presence()
                except Exception as exc:
                    logger.warning("ATAK: presence heartbeat failed: %s", exc)

    async def disconnect(self) -> None:
        self._stopping.set()
        self._mark_disconnected()
        presence_task, self._presence_task = self._presence_task, None
        if presence_task is not None and presence_task is not asyncio.current_task():
            presence_task.cancel()
        client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.close)
        task, self._receive_task = self._receive_task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _receive_loop(self) -> None:
        backoff = self.reconnect_initial
        while not self._stopping.is_set():
            client = self._client
            if client is None:
                try:
                    await self._open_and_identify()
                    self._mark_connected()
                    backoff = self.reconnect_initial
                    logger.info("ATAK: mutual-TLS CoT stream reconnected")
                    client = self._client
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._mark_disconnected()
                    delay = min(self.reconnect_max, backoff)
                    delay *= random.uniform(0.8, 1.2)
                    logger.warning("ATAK: reconnect failed (%s); retrying in %.1fs", exc, delay)
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(self.reconnect_max, backoff * 2.0)
                    continue

            try:
                xml = await asyncio.to_thread(client.receive, self.receive_timeout)
                if xml is not None:
                    await self._handle_cot(xml)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping.is_set():
                    break
                logger.warning("ATAK: CoT stream dropped: %s", exc)
                self._mark_disconnected()
                if self._client is client:
                    self._client = None
                await asyncio.to_thread(client.close)

    async def _handle_cot(self, xml: bytes) -> None:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            logger.debug("ATAK: ignored invalid CoT XML: %s", exc)
            return

        cot_type = root.get("type", "")
        if cot_type in {"b-t-f-d", "b-t-f-r"}:
            message_id = root.get("uid", "").strip()
            if message_id:
                self._receipts[message_id] = {
                    "status": "read" if cot_type == "b-t-f-r" else "delivered",
                    "time": root.get("time", ""),
                }
            return

        try:
            state_event = self.situational_awareness.ingest(xml)
        except CoTParseError as exc:
            logger.debug("ATAK: ignored invalid CoT event: %s", exc)
            return

        # SituationalAwareness retains every point-bearing contact/marker. Only
        # b-t-f GeoChat crosses the messaging boundary into Hermes.
        if GeoChat is None or not isinstance(state_event, GeoChat):
            return
        if not state_event.text.strip():
            return
        try:
            envelope = self._parse_chat_envelope(xml)
        except (ET.ParseError, ValueError) as exc:
            logger.debug("ATAK: ignored GeoChat without sender identity: %s", exc)
            return
        if envelope.sender_uid == self.uid:
            return

        peer = _Peer(envelope.sender_uid, envelope.sender_callsign)
        self._peers[peer.uid] = peer
        chat_id = peer.uid
        chat_name = peer.callsign
        chat_type = "dm"
        if not envelope.direct:
            room_id = (envelope.room or "").strip()
            if not room_id:
                return
            room = _Room(room_id, room_id)
            self._rooms[room.id] = room
            chat_id = room.id
            chat_name = room.name
            chat_type = "group"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=peer.uid,
            user_name=peer.callsign,
            message_id=envelope.message_id,
        )
        event = MessageEvent(
            text=state_event.text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=xml,
            message_id=envelope.message_id,
            timestamp=state_event.time,
            metadata={
                "atak_sender_uid": peer.uid,
                "atak_sender_callsign": peer.callsign,
                "atak_room": envelope.room,
                "atak_direct": envelope.direct,
            },
        )
        await self.handle_message(event)

    def _parse_chat_envelope(self, xml: bytes) -> _ChatEnvelope:
        root = ET.fromstring(xml)
        if root.get("type") != "b-t-f":
            raise ValueError("not GeoChat")
        detail = _child(root, "detail")
        chat = _child(detail, "__chat")
        chatgrp = _child(chat, "chatgrp")
        sender_uid = chatgrp.get("uid0", "").strip() if chatgrp is not None else ""
        callsign = chat.get("senderCallsign", "").strip() if chat is not None else ""
        if not sender_uid:
            raise ValueError("GeoChat has no chatgrp uid0")
        return _ChatEnvelope(
            sender_uid=sender_uid,
            sender_callsign=callsign or sender_uid,
            message_id=(chat.get("messageId", "").strip() if chat is not None else "")
            or root.get("uid", ""),
            room=chat.get("chatroom") if chat is not None else None,
            direct=(chat.get("parent") == "RootContactGroup" if chat is not None else False),
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        target_id = str(chat_id)
        peer = self._peers.get(target_id)
        room = self._rooms.get(target_id)
        if peer is None and room is None and self.situational_awareness is not None:
            contact = self.situational_awareness.get_contact(target_id)
            if contact is not None:
                peer = _Peer(contact.uid, contact.callsign or contact.uid)
                self._peers[peer.uid] = peer
        if peer is None and room is None:
            return SendResult(success=False, error="Unknown ATAK peer or room")
        if not content or not content.strip():
            return SendResult(success=False, error="Cannot send empty GeoChat")
        client = self._client
        if client is None or not getattr(client, "connected", False):
            return SendResult(success=False, error="ATAK CoT stream is not connected", retryable=True)

        if peer is not None:
            payload = self._atak.geochat(content, dest=peer, pos=self.position)
        else:
            payload = self._atak.geochat(content, to_team=room.name, pos=self.position)
        if not payload:
            return SendResult(success=False, error="frogcot could not serialize GeoChat")
        try:
            async with self._send_lock:
                if client is not self._client:
                    return SendResult(success=False, error="ATAK CoT stream changed", retryable=True)
                await asyncio.to_thread(client.send, payload)
        except Exception as exc:
            logger.warning("ATAK: direct GeoChat send failed: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

        root = ET.fromstring(payload)
        chat = _child(_child(root, "detail"), "__chat")
        message_id = chat.get("messageId") if chat is not None else root.get("uid")
        return SendResult(success=True, message_id=message_id, raw_response=payload)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """CoT GeoChat has no portable typing indicator."""

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        peer = self._peers.get(str(chat_id))
        room = self._rooms.get(str(chat_id))
        return {
            "name": peer.callsign if peer else room.name if room else str(chat_id),
            "type": "dm" if peer else "group" if room else "dm",
            "chat_id": str(chat_id),
        }

    def refresh_server_markers(self) -> None:
        """Refresh OTS-retained markers on demand without polling."""
        if not self.ots_python or not self.ots_config or Marker is None or GeoPoint is None:
            self._ots_snapshot_error = "OpenTAKServer snapshot is not configured"
            return
        now = time.monotonic()
        if now - self._ots_snapshot_at < self.ots_snapshot_ttl:
            return

        helper = Path(__file__).with_name("ots_snapshot.py")
        try:
            completed = subprocess.run(
                [self.ots_python, str(helper), self.ots_config],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            payload = json.loads(completed.stdout)
            if completed.returncode != 0 or "markers" not in payload:
                raise RuntimeError(payload.get("error", "snapshot helper failed"))

            refreshed = {}
            for item in payload["markers"]:
                event_time = datetime.datetime.fromisoformat(item["time"])
                if event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=datetime.timezone.utc)
                point = GeoPoint(
                    latitude=float(item["latitude"]),
                    longitude=float(item["longitude"]),
                    hae=float(item["hae"]) if item["hae"] is not None else None,
                    ce=float(item["ce"]) if item["ce"] is not None else None,
                    le=float(item["le"]) if item["le"] is not None else None,
                )
                marker = Marker(
                    uid=str(item["uid"]),
                    cot_type=str(item["cot_type"]),
                    callsign=item.get("callsign"),
                    point=point,
                    time=event_time,
                )
                refreshed[marker.uid] = marker
            self._server_markers = refreshed
            self._ots_snapshot_error = None
            self._ots_snapshot_at = now
        except Exception as exc:
            self._ots_snapshot_error = type(exc).__name__
            logger.warning("ATAK: OpenTAKServer marker snapshot failed: %s", type(exc).__name__)


def _live_adapter() -> Optional[ATAKAdapter]:
    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        if runner is None:
            return None
        return runner.adapters.get(Platform("atak"))
    except Exception:
        return None


def _point_dict(point, include_coordinates: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if include_coordinates:
        result.update(
            {
                "latitude": point.latitude,
                "longitude": point.longitude,
                "hae": point.hae,
            }
        )
    return result


def _all_markers(adapter: ATAKAdapter) -> list:
    markers = dict(adapter._server_markers)
    for marker in adapter.situational_awareness.list_markers():
        current = markers.get(marker.uid)
        if current is None or marker.time >= current.time:
            markers[marker.uid] = marker
    return list(markers.values())


def _state_record(adapter: ATAKAdapter, value):
    if hasattr(value, "point"):
        return value
    state = adapter.situational_awareness
    record = state.get_contact(str(value))
    if record is None:
        record = state.get_marker(str(value))
    if record is None:
        key = str(value)
        record = adapter._server_markers.get(key)
        if record is None:
            folded = key.casefold()
            record = next(
                (
                    marker
                    for marker in adapter._server_markers.values()
                    if marker.callsign and marker.callsign.casefold() == folded
                ),
                None,
            )
    if record is None:
        raise KeyError("unknown contact or marker: {!r}".format(value))
    return record


def _froggeo_vector(adapter: ATAKAdapter, origin, target):
    origin_record = _state_record(adapter, origin)
    target_record = _state_record(adapter, target)
    origin_point = origin_record.point
    target_point = target_record.point
    origin_position = FrogGPSPosition(
        origin_point.latitude,
        origin_point.longitude,
        origin_point.hae or 0.0,
        origin_point.ce or 0.0,
        origin_point.le or 0.0,
    )
    target_position = FrogGPSPosition(
        target_point.latitude,
        target_point.longitude,
        target_point.hae or 0.0,
        target_point.ce or 0.0,
        target_point.le or 0.0,
    )
    return froggeo_gps_to_vector(origin_position, target_position)


def atak_state_tool(args, **_kwargs) -> str:
    """Read live CoT state retained by the connected ATAK adapter."""
    adapter = _live_adapter()
    if adapter is None:
        return json.dumps({"success": False, "error": "ATAK adapter is not running"})

    state = adapter.situational_awareness
    action = str(args.get("action", "status")).strip().lower()
    include_coordinates = bool(args.get("include_coordinates", False))
    marker_actions = {
        "markers",
        "relative_markers",
        "nearest_marker",
        "range_bearing",
    }
    if action in marker_actions:
        adapter.refresh_server_markers()

    if action == "status":
        client = adapter._client
        markers = _all_markers(adapter)
        return json.dumps(
            {
                "success": True,
                "connected": bool(client is not None and getattr(client, "connected", False)),
                "uid": adapter.uid,
                "callsign": adapter.callsign,
                "contacts": len(state.contacts),
                "markers": len(markers),
                "live_stream_markers": len(state.list_markers()),
                "opentakserver_markers": len(adapter._server_markers),
                "opentakserver_snapshot_error": adapter._ots_snapshot_error,
                "chats": len(state.chats),
                "receipts": len(adapter._receipts),
                "geospatial_provider": "froggeolib",
            }
        )

    if action == "receipts":
        return json.dumps(
            {
                "success": True,
                "receipts": [
                    {"message_id": message_id, **receipt}
                    for message_id, receipt in adapter._receipts.items()
                ],
            }
        )

    if action in {"contacts", "markers"}:
        records = state.contacts if action == "contacts" else _all_markers(adapter)
        items = []
        for record in records:
            item = {
                "uid": record.uid,
                "callsign": record.callsign,
                "cot_type": record.cot_type,
                "time": record.time.isoformat(),
            }
            if action == "markers":
                item["source"] = (
                    "opentakserver"
                    if record.uid in adapter._server_markers
                    else "live_stream"
                )
            item.update(_point_dict(record.point, include_coordinates))
            items.append(item)
        response = {"success": True, action: items}
        if action == "markers":
            response["opentakserver_snapshot_error"] = adapter._ots_snapshot_error
        return json.dumps(response)

    origin = str(args.get("origin", "")).strip()
    if not origin:
        return json.dumps({"success": False, "error": "origin is required"})
    try:
        if action == "relative_markers":
            markers = []
            for marker in _all_markers(adapter):
                vector = _froggeo_vector(adapter, origin, marker)
                markers.append(
                    {
                        "uid": marker.uid,
                        "callsign": marker.callsign,
                        "cot_type": marker.cot_type,
                        "time": marker.time.isoformat(),
                        "source": (
                            "opentakserver"
                            if marker.uid in adapter._server_markers
                            else "live_stream"
                        ),
                        "range_m": vector.dist,
                        "bearing_deg": vector.az,
                        "elevation_deg": vector.elev,
                    }
                )
            markers.sort(key=lambda item: item["range_m"])
            return json.dumps({"success": True, "origin": origin, "markers": markers})
        if action == "nearest_marker":
            markers = [
                (marker, _froggeo_vector(adapter, origin, marker))
                for marker in _all_markers(adapter)
            ]
            if not markers:
                return json.dumps({"success": False, "error": "No markers are available"})
            marker, vector = min(markers, key=lambda item: item[1].dist)
            return json.dumps(
                {
                    "success": True,
                    "marker": {
                        "uid": marker.uid,
                        "callsign": marker.callsign,
                        "cot_type": marker.cot_type,
                    },
                    "range_m": vector.dist,
                    "bearing_deg": vector.az,
                    "elevation_deg": vector.elev,
                }
            )
        if action == "range_bearing":
            target = str(args.get("target", "")).strip()
            if not target:
                return json.dumps({"success": False, "error": "target is required"})
            vector = _froggeo_vector(adapter, origin, target)
            return json.dumps(
                {
                    "success": True,
                    "origin": origin,
                    "target": target,
                    "range_m": vector.dist,
                    "bearing_deg": vector.az,
                    "elevation_deg": vector.elev,
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        return json.dumps({"success": False, "error": str(exc)})

    return json.dumps({"success": False, "error": "Unknown ATAK state action"})


def check_requirements() -> bool:
    """Return whether frogcot is importable; credentials live only in config."""
    return all(
        dependency is not None
        for dependency in (
            ATAKClient,
            PersistentCoTClient,
            SituationalAwareness,
            FrogGPSPosition,
            froggeo_gps_to_vector,
        )
    )


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    required = ("host", "ca", "client_certificate", "client_key", "callsign", "uid")
    return all(_configured(extra, key) for key in required)


def is_connected(adapter) -> bool:
    client = getattr(adapter, "_client", None)
    return bool(client is not None and getattr(client, "connected", False))


def register(ctx) -> None:
    """Hermes plugin entry point."""
    ctx.register_platform(
        name="atak",
        label="ATAK / CoT",
        adapter_factory=lambda cfg: ATAKAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint=(
            "Install frogcot 1.2+ and froggeolib 1.1+ into the Hermes environment"
        ),
        allowed_users_env="ATAK_ALLOWED_USERS",
        allow_all_env="ATAK_ALLOW_ALL_USERS",
        emoji="🛰️",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=(
            "You are replying through ATAK direct GeoChat. Use concise plain text; "
            "the adapter returns your final response to the originating ATAK peer."
        ),
    )
    ctx.register_tool(
        name="atak_state",
        toolset="atak",
        schema={
            "name": "atak_state",
            "description": (
                "Inspect live ATAK contacts and markers, list markers with range and "
                "bearing relative to an origin, or calculate froggeolib WGS84 vectors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "status",
                            "receipts",
                            "contacts",
                            "markers",
                            "relative_markers",
                            "nearest_marker",
                            "range_bearing",
                        ],
                    },
                    "origin": {
                        "type": "string",
                        "description": (
                            "Exact contact or marker UID/callsign for nearest, "
                            "relative-marker, or range/bearing calculations."
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": "Exact target UID/callsign for range_bearing.",
                    },
                    "include_coordinates": {
                        "type": "boolean",
                        "description": "Include raw coordinates in list results.",
                        "default": False,
                    },
                },
                "required": ["action"],
            },
        },
        handler=atak_state_tool,
        check_fn=check_requirements,
        description="Live ATAK/CoT situational state",
        emoji="🛰️",
    )
