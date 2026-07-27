"""Persistent, targeted MAVLink command API for the Hermes ATAK plugin.

The gateway owns one long-lived controller. Tool calls enqueue small jobs and
return immediately; the jobs use telemetry retained by ``mavlink_bridge`` for
preflight checks and post-command verification. No per-command Python scripts
or per-command MAVLink connections are created.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

try:
    from froggeolib import (
        GPSposition,
        gps_to_vector,
        vector_to_gps,
    )
except ImportError:
    GPSposition = None
    gps_to_vector = None
    vector_to_gps = None

try:
    from pymavlink import mavutil
except ImportError:
    mavutil = None


logger = logging.getLogger(__name__)
Notify = Callable[[str, str], Awaitable[None]]
ResolvePoint = Callable[[str], Any]

MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
MAV_AUTOPILOT_ARDUPILOTMEGA = 3
TERMINAL_PHASES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass
class ControlJob:
    id: str
    action: str
    sysid: int
    phase: str = "queued"
    message: str = ""
    created_unix: float = field(default_factory=time.time)
    updated_unix: float = field(default_factory=time.time)
    notify_chat_id: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "sysid": self.sysid,
            "phase": self.phase,
            "message": self.message,
            "created_unix": self.created_unix,
            "updated_unix": self.updated_unix,
            "arguments": {
                key: value
                for key, value in self.arguments.items()
                if key not in {"notify_chat_id"}
            },
        }


class MavlinkControlService:
    """Queue commands against the adapter's persistent routed MAVLink link."""

    def __init__(
        self,
        *,
        bridge,
        callsign: Callable[[int], str],
        resolve_point: ResolvePoint,
        notify: Notify,
        command_timeout: float = 60.0,
        arrival_radius: float = 10.0,
    ):
        self.bridge = bridge
        self.callsign = callsign
        self.resolve_point = resolve_point
        self.notify = notify
        self.command_timeout = max(5.0, float(command_timeout))
        self.arrival_radius = max(1.0, float(arrival_radius))
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.jobs: Dict[str, ControlJob] = {}
        self._jobs_lock = threading.Lock()
        self._vehicle_locks: Dict[int, asyncio.Lock] = {}
        self._send_lock = asyncio.Lock()
        self._stopping = False

    def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stopping = False

    async def stop(self) -> None:
        self._stopping = True
        with self._jobs_lock:
            tasks = [
                job.task
                for job in self.jobs.values()
                if job.task is not None and not job.task.done()
            ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.loop = None

    def status(self, sysid: Optional[int] = None) -> dict:
        states = getattr(getattr(self.bridge, "tracker", None), "vehicles", {})
        now = time.monotonic()
        selected = (
            [states[int(sysid)]]
            if sysid is not None and int(sysid) in states
            else [states[key] for key in sorted(states)]
            if sysid is None
            else []
        )
        vehicles = [self._snapshot(state, now=now) for state in selected]
        return {
            "success": True,
            "connected": getattr(self.bridge, "_connection", None) is not None,
            "vehicles": vehicles,
        }

    def jobs_status(self, job_id: str = "") -> dict:
        with self._jobs_lock:
            if job_id:
                job = self.jobs.get(job_id)
                if job is None:
                    return {"success": False, "error": f"unknown job: {job_id}"}
                return {"success": True, "job": job.public()}
            jobs = sorted(
                self.jobs.values(), key=lambda item: item.created_unix, reverse=True
            )
            return {"success": True, "jobs": [job.public() for job in jobs[:20]]}

    def submit(self, action: str, args: dict, *, notify_chat_id: str = "") -> dict:
        if self._stopping or self.loop is None or not self.loop.is_running():
            return {"success": False, "error": "MAVLink control service is not running"}
        if action not in {
            "arm",
            "disarm",
            "set_mode",
            "takeoff",
            "land",
            "rtl",
            "hold",
            "goto",
        }:
            return {"success": False, "error": f"unsupported action: {action}"}
        try:
            sysid = int(args.get("sysid"))
        except (TypeError, ValueError):
            return {"success": False, "error": "an explicit integer sysid is required"}
        if not 1 <= sysid <= 254:
            return {"success": False, "error": "sysid must be between 1 and 254"}
        state = self._state(sysid)
        if state is None:
            return {
                "success": False,
                "error": f"sysid {sysid} has not been discovered by fresh telemetry",
            }
        if not self._fresh(state):
            return {"success": False, "error": f"sysid {sysid} heartbeat is stale"}

        job = ControlJob(
            id=uuid.uuid4().hex[:12],
            action=action,
            sysid=sysid,
            message="accepted; waiting for controller",
            notify_chat_id=notify_chat_id,
            arguments=dict(args),
        )
        with self._jobs_lock:
            self.jobs[job.id] = job
        self.loop.call_soon_threadsafe(self._launch, job)
        return {
            "success": True,
            "accepted": True,
            "job_id": job.id,
            "message": (
                f"{self.callsign(sysid)} {action} accepted; "
                f"background verification started"
            ),
        }

    def cancel(self, job_id: str) -> dict:
        with self._jobs_lock:
            job = self.jobs.get(str(job_id))
        if job is None:
            return {"success": False, "error": f"unknown job: {job_id}"}
        if job.phase in TERMINAL_PHASES:
            return {"success": False, "error": f"job is already {job.phase}"}
        if self.loop is None:
            return {"success": False, "error": "control service is not running"}
        self.loop.call_soon_threadsafe(job.task.cancel if job.task else lambda: None)
        return {"success": True, "job_id": job.id, "message": "cancellation requested"}

    def _launch(self, job: ControlJob) -> None:
        job.task = asyncio.create_task(
            self._run_job(job), name=f"mavlink-control-{job.id}"
        )

    async def _run_job(self, job: ControlJob) -> None:
        lock = self._vehicle_locks.setdefault(job.sysid, asyncio.Lock())
        try:
            await self._update(job, "preflight", "checking fresh telemetry and target")
            async with lock:
                state = self._require_fresh(job.sysid)
                await self._update(job, "commanding", "preflight passed; transmitting command")
                await self._execute(job, state)
        except asyncio.CancelledError:
            await self._update(job, "cancelled", "monitoring job cancelled; no retry sent")
        except Exception as exc:
            logger.warning("MAVLink control job %s failed: %s", job.id, exc)
            await self._update(job, "failed", f"{type(exc).__name__}: {exc}")
            await self._notify_unexpected_failure(job)

    async def _execute(self, job: ControlJob, state) -> None:
        action = job.action
        if action == "arm":
            await self._command_long(job.sysid, "MAV_CMD_COMPONENT_ARM_DISARM", 1.0)
            await self._verify(job, lambda item: self._armed(item), "armed")
        elif action == "disarm":
            await self._command_long(job.sysid, "MAV_CMD_COMPONENT_ARM_DISARM", 0.0)
            await self._verify(job, lambda item: not self._armed(item), "disarmed")
        elif action == "set_mode":
            if "custom_mode" not in job.arguments:
                raise ValueError("set_mode requires custom_mode")
            base_mode = int(job.arguments.get("base_mode", 1))
            custom_mode = int(job.arguments["custom_mode"])
            await self._command_long(
                job.sysid,
                "MAV_CMD_DO_SET_MODE",
                float(base_mode),
                float(custom_mode),
            )
            await self._verify(
                job,
                lambda item: item.custom_mode == custom_mode,
                f"custom mode {custom_mode} verified",
            )
        elif action == "takeoff":
            altitude = float(job.arguments.get("altitude_m", 10.0))
            if not 1.0 <= altitude <= 120.0:
                raise ValueError("takeoff altitude_m must be between 1 and 120")
            guided_mode = self._guided_mode(state)
            if not self._armed(state):
                await self._update(
                    job,
                    "commanding",
                    "vehicle disarmed; sending arm command",
                )
                await self._command_long(
                    job.sysid, "MAV_CMD_COMPONENT_ARM_DISARM", 1.0
                )
                await self._update(
                    job,
                    "monitoring",
                    "arm command accepted; verifying armed telemetry",
                )
                state = await self._wait(
                    job.sysid, lambda item: self._armed(item)
                )
                await self._update(
                    job,
                    "commanding",
                    "armed state verified; preparing autonomous takeoff",
                )

            if guided_mode is not None and state.custom_mode != guided_mode:
                await self._update(
                    job,
                    "commanding",
                    f"selecting guided mode {guided_mode} for autonomous takeoff",
                )
                await self._command_long(
                    job.sysid,
                    "MAV_CMD_DO_SET_MODE",
                    float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
                    float(guided_mode),
                )
                await self._update(
                    job,
                    "monitoring",
                    f"mode command accepted; verifying guided mode {guided_mode}",
                )
                state = await self._wait(
                    job.sysid,
                    lambda item: item.custom_mode == guided_mode,
                )

            await self._update(
                job,
                "commanding",
                f"takeoff mode ready; requesting {altitude:.1f} m",
            )
            await self._command_long(
                job.sysid, "MAV_CMD_NAV_TAKEOFF", 0, 0, 0, math.nan, 0, 0, altitude
            )
            await self._update(
                job,
                "monitoring",
                f"takeoff command accepted; verifying climb to {altitude:.1f} m",
            )
            await self._wait(
                job.sysid,
                lambda item: (item.relative_altitude or 0.0) >= altitude * 0.8,
            )
            await self._succeed(
                job, f"airborne at target band ({altitude:.1f} m requested)"
            )
        elif action == "land":
            await self._command_long(job.sysid, "MAV_CMD_NAV_LAND")
            await self._verify(
                job,
                lambda item: not self._armed(item)
                or (
                    item.relative_altitude is not None
                    and item.relative_altitude <= 1.0
                ),
                "landing verified near ground or disarmed",
                timeout=max(120.0, self.command_timeout),
            )
        elif action == "rtl":
            await self._command_long(job.sysid, "MAV_CMD_NAV_RETURN_TO_LAUNCH")
            await self._succeed(job, "RTL command acknowledged by vehicle")
        elif action == "hold":
            await self._command_long(job.sysid, "MAV_CMD_NAV_LOITER_UNLIM")
            await self._succeed(job, "hold/loiter command acknowledged by vehicle")
        elif action == "goto":
            target = self._resolve_target(job, state)
            altitude = float(
                job.arguments.get(
                    "altitude_m",
                    state.relative_altitude
                    if state.relative_altitude is not None
                    else 10.0,
                )
            )
            start = self._position(state)
            initial = gps_to_vector(start, target).dist
            if initial <= self.arrival_radius:
                await self._succeed(
                    job, f"already within {initial:.1f} m of target"
                )
                return
            await self._goto(job.sysid, target.lat, target.lon, altitude)
            await self._update(
                job,
                "monitoring",
                f"goto transmitted; {initial:.0f} m from target",
            )
            await self._verify_goto(job, target, initial)
        else:
            raise ValueError(f"unsupported action: {action}")

    def _resolve_target(self, job: ControlJob, state):
        if GPSposition is None or gps_to_vector is None or vector_to_gps is None:
            raise RuntimeError("froggeolib is unavailable")
        marker = str(job.arguments.get("target", "")).strip()
        if marker:
            point = self.resolve_point(marker)
            return GPSposition(float(point.latitude), float(point.longitude), 0.0)
        if "latitude" in job.arguments and "longitude" in job.arguments:
            return GPSposition(
                float(job.arguments["latitude"]),
                float(job.arguments["longitude"]),
                0.0,
            )
        if "distance_m" in job.arguments and "bearing_deg" in job.arguments:
            distance = float(job.arguments["distance_m"])
            bearing = float(job.arguments["bearing_deg"])
            if distance <= 0 or distance > 100000:
                raise ValueError("distance_m must be greater than 0 and at most 100000")
            return vector_to_gps(self._position(state), dist=distance, az=bearing)
        raise ValueError(
            "goto requires target marker, latitude/longitude, "
            "or distance_m/bearing_deg"
        )

    async def _verify_goto(self, job: ControlJob, target, initial: float) -> None:
        last_bucket = None
        deadline = time.monotonic() + max(180.0, self.command_timeout)
        while time.monotonic() < deadline:
            state = self._require_fresh(job.sysid)
            remaining = gps_to_vector(self._position(state), target).dist
            if remaining <= self.arrival_radius:
                await self._succeed(job, f"arrived within {remaining:.1f} m of target")
                return
            bucket = int(remaining // 100)
            if bucket != last_bucket and remaining < initial:
                last_bucket = bucket
                await self._update(
                    job, "monitoring", f"en route; {remaining:.0f} m remaining"
                )
            await asyncio.sleep(1.0)
        raise TimeoutError("goto was not verified before the monitoring timeout")

    async def _verify(
        self,
        job: ControlJob,
        predicate: Callable[[Any], bool],
        message: str,
        *,
        timeout: Optional[float] = None,
    ) -> None:
        await self._update(job, "monitoring", "command sent; verifying telemetry")
        await self._wait(job.sysid, predicate, timeout=timeout)
        await self._succeed(job, message)

    async def _wait(
        self,
        sysid: int,
        predicate: Callable[[Any], bool],
        *,
        timeout: Optional[float] = None,
    ):
        deadline = time.monotonic() + (timeout or self.command_timeout)
        while time.monotonic() < deadline:
            state = self._state(sysid)
            if state is not None and self._fresh(state) and predicate(state):
                return state
            await asyncio.sleep(0.5)
        raise TimeoutError("expected telemetry transition was not observed")

    async def _command_long(self, sysid: int, command_name: str, *params) -> None:
        if mavutil is None:
            raise RuntimeError("pymavlink is unavailable")
        command = getattr(mavutil.mavlink, command_name)
        values = list(params) + [0.0] * (7 - len(params))

        sent_at = time.monotonic()

        def send() -> None:
            connection = getattr(self.bridge, "_connection", None)
            state = self._require_fresh(sysid)
            if connection is None:
                raise RuntimeError("MAVLink router connection is unavailable")
            connection.mav.command_long_send(
                sysid,
                state.component_id,
                command,
                0,
                *values[:7],
            )

        async with self._send_lock:
            await asyncio.to_thread(send)
        await self._wait_ack(sysid, command, sent_at)

    async def _goto(self, sysid: int, lat: float, lon: float, alt: float) -> None:
        if mavutil is None:
            raise RuntimeError("pymavlink is unavailable")

        command = mavutil.mavlink.MAV_CMD_DO_REPOSITION
        sent_at = time.monotonic()

        def send() -> None:
            connection = getattr(self.bridge, "_connection", None)
            state = self._require_fresh(sysid)
            if connection is None:
                raise RuntimeError("MAVLink router connection is unavailable")
            connection.mav.command_int_send(
                sysid,
                state.component_id,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                command,
                0,
                0,
                -1.0,
                1.0,
                0.0,
                math.nan,
                int(round(lat * 1e7)),
                int(round(lon * 1e7)),
                float(alt),
            )

        async with self._send_lock:
            await asyncio.to_thread(send)
        await self._wait_ack(sysid, command, sent_at)

    async def _wait_ack(self, sysid: int, command: int, sent_at: float) -> None:
        deadline = time.monotonic() + min(self.command_timeout, 15.0)
        while time.monotonic() < deadline:
            state = self._require_fresh(sysid)
            ack = state.command_acks.get(int(command))
            if ack is not None and ack[1] >= sent_at:
                result = ack[0]
                if result in {0, 5}:
                    return
                raise RuntimeError(
                    f"vehicle rejected MAVLink command {command} with result {result}"
                )
            await asyncio.sleep(0.1)
        raise TimeoutError(f"no COMMAND_ACK received for MAVLink command {command}")

    async def _succeed(self, job: ControlJob, message: str) -> None:
        await self._update(job, "succeeded", message)

    async def _update(self, job: ControlJob, phase: str, message: str) -> None:
        job.phase = phase
        job.message = message
        job.updated_unix = time.time()

    async def _notify_unexpected_failure(self, job: ControlJob) -> None:
        if job.notify_chat_id:
            try:
                await self.notify(
                    job.notify_chat_id,
                    f"{self.callsign(job.sysid)} [{job.id}] failed unexpectedly",
                )
            except Exception as exc:
                logger.warning("MAVLink job %s TAK feedback failed: %s", job.id, exc)

    def _state(self, sysid: int):
        states = getattr(getattr(self.bridge, "tracker", None), "vehicles", {})
        return states.get(int(sysid))

    def _require_fresh(self, sysid: int):
        state = self._state(sysid)
        if state is None:
            raise RuntimeError(f"sysid {sysid} is not present in telemetry")
        if not self._fresh(state):
            raise RuntimeError(f"sysid {sysid} heartbeat is stale")
        return state

    def _fresh(self, state) -> bool:
        last = getattr(state, "last_heartbeat", None)
        freshness = float(getattr(getattr(self.bridge, "tracker", None), "freshness", 5.0))
        return last is not None and 0.0 <= time.monotonic() - last <= freshness

    @staticmethod
    def _armed(state) -> bool:
        return bool((getattr(state, "base_mode", 0) or 0) & MAV_MODE_FLAG_SAFETY_ARMED)

    @staticmethod
    def _guided_mode(state) -> Optional[int]:
        if getattr(state, "autopilot", None) != MAV_AUTOPILOT_ARDUPILOTMEGA:
            return None
        if mavutil is None:
            raise RuntimeError("pymavlink is unavailable")
        mapping = mavutil.mode_mapping_byname(getattr(state, "mav_type", None))
        if not mapping or "GUIDED" not in mapping:
            raise RuntimeError(
                "ArduPilot vehicle does not advertise a supported GUIDED mode"
            )
        return int(mapping["GUIDED"])

    @staticmethod
    def _position(state):
        lat = getattr(state, "latitude", None)
        lon = getattr(state, "longitude", None)
        if lat is None or lon is None:
            raise RuntimeError("fresh vehicle position is unavailable")
        return GPSposition(float(lat), float(lon), float(getattr(state, "altitude", 0.0) or 0.0))

    def _snapshot(self, state, *, now: float) -> dict:
        heartbeat_age = (
            None
            if state.last_heartbeat is None
            else max(0.0, now - state.last_heartbeat)
        )
        return {
            "sysid": state.sysid,
            "callsign": self.callsign(state.sysid),
            "component_id": state.component_id,
            "fresh": self._fresh(state),
            "heartbeat_age_s": heartbeat_age,
            "armed": self._armed(state),
            "base_mode": state.base_mode,
            "custom_mode": state.custom_mode,
            "system_status": state.system_status,
            "latitude": state.latitude,
            "longitude": state.longitude,
            "absolute_altitude_m": state.altitude,
            "relative_altitude_m": state.relative_altitude,
            "ground_speed_m_s": state.ground_speed,
            "course_deg": state.course,
            "battery_remaining": state.battery_remaining,
        }


def json_result(value: dict) -> str:
    return json.dumps(value, sort_keys=True)
