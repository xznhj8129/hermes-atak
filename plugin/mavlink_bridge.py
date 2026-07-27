"""Passive, multi-system MAVLink telemetry tracking for ATAK markers.

This module deliberately uses pymavlink for observation of every source
system on one shared mavlink-router TCP stream. MAVSDK remains the platform's
control/service boundary; MAVSDK-Python cannot reliably select multiple target
systems by sysid on a single shared connection.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional

try:
    from pymavlink import mavutil
except ImportError:  # plugin discovery can precede optional dependency install
    mavutil = None


logger = logging.getLogger(__name__)

# Stable MAV_TYPE values from common.xml. Only airborne vehicle heartbeats make
# a source system publishable; GCS and peripheral/component heartbeats do not.
MAV_TYPE_GENERIC = 0
MAV_TYPE_FIXED_WING = 1
MAV_TYPE_QUADROTOR = 2
MAV_TYPE_COAXIAL = 3
MAV_TYPE_HELICOPTER = 4
MAV_TYPE_GCS = 6
MAV_TYPE_HEXAROTOR = 13
MAV_TYPE_OCTOROTOR = 14
MAV_TYPE_TRICOPTER = 15
MAV_TYPE_FLAPPING_WING = 16
MAV_TYPE_KITE = 17
MAV_TYPE_VTOL_DUOROTOR = 19
MAV_TYPE_VTOL_QUADROTOR = 20
MAV_TYPE_VTOL_TILTROTOR = 21
MAV_TYPE_VTOL_RESERVED2 = 22
MAV_TYPE_VTOL_RESERVED3 = 23
MAV_TYPE_VTOL_RESERVED4 = 24
MAV_TYPE_VTOL_RESERVED5 = 25
MAV_TYPE_GIMBAL = 26
MAV_TYPE_PARAFOIL = 28
MAV_TYPE_DODECAROTOR = 29

AIRBORNE_MAV_TYPES = frozenset(
    {
        MAV_TYPE_FIXED_WING,
        MAV_TYPE_QUADROTOR,
        MAV_TYPE_COAXIAL,
        MAV_TYPE_HELICOPTER,
        MAV_TYPE_HEXAROTOR,
        MAV_TYPE_OCTOROTOR,
        MAV_TYPE_TRICOPTER,
        MAV_TYPE_FLAPPING_WING,
        MAV_TYPE_KITE,
        MAV_TYPE_VTOL_DUOROTOR,
        MAV_TYPE_VTOL_QUADROTOR,
        MAV_TYPE_VTOL_TILTROTOR,
        MAV_TYPE_VTOL_RESERVED2,
        MAV_TYPE_VTOL_RESERVED3,
        MAV_TYPE_VTOL_RESERVED4,
        MAV_TYPE_VTOL_RESERVED5,
        MAV_TYPE_PARAFOIL,
        MAV_TYPE_DODECAROTOR,
    }
)


@dataclass
class _VehicleState:
    sysid: int
    component_id: int = 1
    airborne: bool = False
    mav_type: Optional[int] = None
    autopilot: Optional[int] = None
    base_mode: Optional[int] = None
    custom_mode: Optional[int] = None
    system_status: Optional[int] = None
    last_heartbeat: Optional[float] = None
    last_fix: Optional[float] = None
    last_position: Optional[float] = None
    last_published: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    relative_altitude: Optional[float] = None
    ground_speed: Optional[float] = None
    course: Optional[float] = None
    satellites: Optional[int] = None
    battery_remaining: Optional[int] = None
    command_acks: Dict[int, tuple[int, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class VehicleTelemetry:
    """A publishable snapshot for one actual MAVLink source system."""

    sysid: int
    latitude: float
    longitude: float
    altitude: float
    mav_type: Optional[int]
    autopilot: Optional[int]
    base_mode: Optional[int]
    system_status: Optional[int]
    ground_speed: Optional[float]
    course: Optional[float]
    satellites: Optional[int]
    battery_remaining: Optional[int]


def pymavlink_available() -> bool:
    return mavutil is not None


class MavlinkTelemetryTracker:
    """Retain independent heartbeat, fix, position, and cadence per sysid."""

    def __init__(self, *, freshness: float = 5.0, cadence: float = 1.0):
        self.freshness = max(0.1, float(freshness))
        self.cadence = max(0.1, float(cadence))
        self.vehicles: Dict[int, _VehicleState] = {}

    def ingest(self, message, *, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else float(now)
        sysid = int(message.get_srcSystem())
        if not 1 <= sysid <= 255:
            return
        message_type = str(message.get_type()).upper()
        state = self.vehicles.setdefault(sysid, _VehicleState(sysid=sysid))

        if message_type == "HEARTBEAT":
            mav_type = int(message.type)
            # Ignore component/peripheral heartbeats. Once an airborne
            # autopilot has identified this sysid, a camera heartbeat sharing
            # the sysid must not make the vehicle disappear.
            if mav_type in AIRBORNE_MAV_TYPES:
                state.airborne = True
                state.component_id = int(
                    getattr(message, "get_srcComponent", lambda: 1)()
                )
                state.mav_type = mav_type
                state.autopilot = int(message.autopilot)
                state.base_mode = int(message.base_mode)
                custom_mode = getattr(message, "custom_mode", None)
                state.custom_mode = (
                    int(custom_mode) if custom_mode is not None else None
                )
                state.system_status = int(message.system_status)
                state.last_heartbeat = now
            return

        if message_type == "GPS_RAW_INT":
            fix_type = int(message.fix_type)
            latitude = float(message.lat) / 1e7
            longitude = float(message.lon) / 1e7
            if fix_type < 3 or not self._valid_location(latitude, longitude):
                state.last_fix = None
                return
            state.last_fix = now
            state.last_position = now
            state.latitude = latitude
            state.longitude = longitude
            state.altitude = float(message.alt) / 1000.0
            velocity = getattr(message, "vel", None)
            course = getattr(message, "cog", None)
            satellites = getattr(message, "satellites_visible", None)
            state.ground_speed = (
                float(velocity) / 100.0 if velocity is not None and velocity != 65535 else None
            )
            state.course = (
                float(course) / 100.0 if course is not None and course != 65535 else None
            )
            state.satellites = (
                int(satellites) if satellites is not None and satellites != 255 else None
            )
            return

        if message_type == "GLOBAL_POSITION_INT":
            latitude = float(message.lat) / 1e7
            longitude = float(message.lon) / 1e7
            if not self._valid_location(latitude, longitude):
                return
            state.last_position = now
            state.latitude = latitude
            state.longitude = longitude
            state.altitude = float(message.alt) / 1000.0
            relative_altitude = getattr(message, "relative_alt", None)
            if relative_altitude is not None:
                state.relative_altitude = float(relative_altitude) / 1000.0
            vx = getattr(message, "vx", None)
            vy = getattr(message, "vy", None)
            if vx is not None and vy is not None:
                state.ground_speed = math.hypot(float(vx), float(vy)) / 100.0
            heading = getattr(message, "hdg", None)
            if heading is not None and heading != 65535:
                state.course = float(heading) / 100.0
            return

        if message_type == "SYS_STATUS":
            battery = getattr(message, "battery_remaining", None)
            if battery is not None and int(battery) >= 0:
                state.battery_remaining = int(battery)
            return

        if message_type == "COMMAND_ACK":
            state.command_acks[int(message.command)] = (int(message.result), now)

    @staticmethod
    def _valid_location(latitude: float, longitude: float) -> bool:
        return (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90.0 <= latitude <= 90.0
            and -180.0 <= longitude <= 180.0
            and not (latitude == 0.0 and longitude == 0.0)
        )

    def due(self, *, now: Optional[float] = None) -> list[VehicleTelemetry]:
        now = time.monotonic() if now is None else float(now)
        due = []
        for sysid in sorted(self.vehicles):
            state = self.vehicles[sysid]
            if not self._fresh(state.last_heartbeat, now):
                continue
            if not self._fresh(state.last_fix, now):
                continue
            if not self._fresh(state.last_position, now):
                continue
            if not state.airborne:
                continue
            if (
                state.last_published is not None
                and now - state.last_published < self.cadence
            ):
                continue
            if (
                state.latitude is None
                or state.longitude is None
                or state.altitude is None
            ):
                continue
            due.append(
                VehicleTelemetry(
                    sysid=sysid,
                    latitude=state.latitude,
                    longitude=state.longitude,
                    altitude=state.altitude,
                    mav_type=state.mav_type,
                    autopilot=state.autopilot,
                    base_mode=state.base_mode,
                    system_status=state.system_status,
                    ground_speed=state.ground_speed,
                    course=state.course,
                    satellites=state.satellites,
                    battery_remaining=state.battery_remaining,
                )
            )
        return due

    def mark_published(self, sysid: int, *, now: Optional[float] = None) -> None:
        state = self.vehicles.get(int(sysid))
        if state is not None:
            state.last_published = time.monotonic() if now is None else float(now)

    def _fresh(self, timestamp: Optional[float], now: float) -> bool:
        return timestamp is not None and 0.0 <= now - timestamp <= self.freshness


ConnectionFactory = Callable[[str], object]
Publisher = Callable[[VehicleTelemetry], Awaitable[bool]]


class MavlinkTelemetryBridge:
    """Reconnect a passive pymavlink TCP client and publish fresh snapshots."""

    def __init__(
        self,
        *,
        endpoint: str,
        publish: Publisher,
        freshness: float = 5.0,
        cadence: float = 1.0,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
        receive_timeout: float = 0.25,
        connection_factory: Optional[ConnectionFactory] = None,
    ):
        if not str(endpoint).startswith(("tcp:", "tcpin:")):
            raise ValueError("MAVLink telemetry endpoint must be a TCP endpoint")
        if str(endpoint).startswith("tcpin:"):
            raise ValueError("MAVLink telemetry bridge must connect, not listen")
        self.endpoint = str(endpoint)
        self.publish = publish
        self.tracker = MavlinkTelemetryTracker(
            freshness=freshness,
            cadence=cadence,
        )
        self.reconnect_initial = max(0.1, float(reconnect_initial))
        self.reconnect_max = max(
            self.reconnect_initial, float(reconnect_max)
        )
        self.receive_timeout = max(0.05, float(receive_timeout))
        self.connection_factory = connection_factory or self._open_connection
        self.task: Optional[asyncio.Task] = None
        self._connection = None
        self._stopping = asyncio.Event()

    @staticmethod
    def _open_connection(endpoint: str):
        if mavutil is None:
            raise RuntimeError("pymavlink is not importable")
        # source_system identifies this passive client if a library-level
        # packet is ever emitted. Vehicle identity always comes from each
        # received message's get_srcSystem(), never this GCS value.
        connection = mavutil.mavlink_connection(
            endpoint,
            source_system=255,
            source_component=0,
            autoreconnect=False,
            robust_parsing=True,
            input=True,
        )
        # pymavlink's mavtcp EOF hook only reconnects when its own
        # autoreconnect mode is enabled. With autoreconnect disabled it returns
        # to recv_match on the closed socket, which can spin until timeout.
        # Surface EOF to this bridge so its cancellable bounded-backoff loop
        # remains the single reconnect owner.
        def raise_tcp_eof() -> None:
            raise EOFError("MAVLink router closed the TCP stream")

        connection.handle_eof = raise_tcp_eof
        return connection

    def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self._stopping.clear()
        self.task = asyncio.create_task(self._run(), name="atak-mavlink-telemetry")

    async def stop(self) -> None:
        self._stopping.set()
        connection, self._connection = self._connection, None
        if connection is not None:
            await asyncio.to_thread(connection.close)
        task, self.task = self.task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        backoff = self.reconnect_initial
        while not self._stopping.is_set():
            connection = None
            try:
                connection = await asyncio.to_thread(
                    self.connection_factory, self.endpoint
                )
                self._connection = connection
                backoff = self.reconnect_initial
                logger.info("ATAK: MAVLink telemetry connected to %s", self.endpoint)
                while not self._stopping.is_set():
                    message = await asyncio.to_thread(
                        connection.recv_match,
                        blocking=True,
                        timeout=self.receive_timeout,
                    )
                    now = time.monotonic()
                    if message is not None:
                        try:
                            self.tracker.ingest(message, now=now)
                        except (AttributeError, TypeError, ValueError) as exc:
                            logger.debug("ATAK: ignored malformed MAVLink message: %s", exc)
                    for vehicle in self.tracker.due(now=now):
                        if await self.publish(vehicle):
                            self.tracker.mark_published(vehicle.sysid, now=now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopping.is_set():
                    logger.warning(
                        "ATAK: MAVLink telemetry connection failed (%s); "
                        "reconnecting in %.1fs",
                        exc,
                        backoff,
                    )
            finally:
                if self._connection is connection:
                    self._connection = None
                if connection is not None:
                    await asyncio.to_thread(connection.close)

            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(self.reconnect_max, backoff * 2.0)
