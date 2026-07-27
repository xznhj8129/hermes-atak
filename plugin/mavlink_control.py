"""Persistent, targeted flight runtime for the Hermes ATAK plugin.

The gateway owns one long-lived controller. The public functions are reusable
motion capabilities, not a whitelist of natural-language orders. Tool calls
enqueue jobs and return immediately; jobs may be one-shot outcomes or
long-running procedures composed from those capabilities. No per-command
Python scripts or per-command MAVLink connections are created.
"""

from __future__ import annotations

import ast
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
ResolveContact = Callable[[str], Any]

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
        resolve_contact: Optional[ResolveContact] = None,
        command_timeout: float = 60.0,
        arrival_radius: float = 10.0,
        follow_cadence: float = 2.0,
        follow_deadband: float = 5.0,
    ):
        self.bridge = bridge
        self.callsign = callsign
        self.resolve_point = resolve_point
        self.notify = notify
        self.resolve_contact = resolve_contact
        self.command_timeout = max(5.0, float(command_timeout))
        self.arrival_radius = max(1.0, float(arrival_radius))
        self.follow_cadence = min(10.0, max(1.0, float(follow_cadence)))
        self.follow_deadband = min(100.0, max(2.0, float(follow_deadband)))
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
            "run",
            "arm",
            "disarm",
            "takeoff",
            "land",
            "rtl",
            "hold",
            "goto",
            "follow",
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
        if action == "follow":
            target = str(args.get("target", "")).strip()
            if not target:
                return {
                    "success": False,
                    "error": (
                        "follow requires a live ATAK target contact/callsign; "
                        "use it from an ATAK GeoChat session to infer 'me'"
                    ),
                }
            try:
                self._resolve_follow_contact(target)
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                return {"success": False, "error": str(exc)}

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
            if job.action == "follow":
                await self._cancel_follow(job)
            else:
                await self._update(
                    job, "cancelled", "monitoring job cancelled; no retry sent"
                )
        except Exception as exc:
            logger.warning("MAVLink control job %s failed: %s", job.id, exc)
            await self._update(job, "failed", str(exc))

    async def _execute(self, job: ControlJob, state) -> None:
        action = job.action
        if action == "run":
            await self._run_procedure(job)
        elif action == "arm":
            await self._command_long(
                job.sysid,
                "MAV_CMD_COMPONENT_ARM_DISARM",
                1.0,
                operator_label="arm",
            )
            await self._verify(job, lambda item: self._armed(item), "armed")
        elif action == "disarm":
            await self._command_long(
                job.sysid,
                "MAV_CMD_COMPONENT_ARM_DISARM",
                0.0,
                operator_label="disarm",
            )
            await self._verify(job, lambda item: not self._armed(item), "disarmed")
        elif action == "takeoff":
            altitude = float(job.arguments.get("altitude_m", 10.0))
            if not 1.0 <= altitude <= 120.0:
                raise ValueError("takeoff altitude_m must be between 1 and 120")
            await self._takeoff(job, state, altitude, finish=True)
        elif action == "land":
            await self._command_long(
                job.sysid,
                "MAV_CMD_NAV_LAND",
                operator_label="land",
            )
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
            original_mode = state.custom_mode
            expected_mode = self._ardupilot_mode(state, "RTL")
            await self._command_long(
                job.sysid,
                "MAV_CMD_NAV_RETURN_TO_LAUNCH",
                operator_label="return home",
            )
            await self._verify(
                job,
                (
                    (lambda item: item.custom_mode == expected_mode)
                    if expected_mode is not None
                    else (lambda item: item.custom_mode != original_mode)
                ),
                "return-to-home flight state verified",
            )
        elif action == "hold":
            original_mode = state.custom_mode
            expected_mode = self._ardupilot_mode(state, "LOITER")
            await self._command_long(
                job.sysid,
                "MAV_CMD_NAV_LOITER_UNLIM",
                operator_label="hold position",
            )
            await self._verify(
                job,
                (
                    (lambda item: item.custom_mode == expected_mode)
                    if expected_mode is not None
                    else (lambda item: item.custom_mode != original_mode)
                ),
                "hold flight state verified",
            )
        elif action == "goto":
            current_altitude = float(state.relative_altitude or 0.0)
            altitude = float(
                job.arguments.get(
                    "altitude_m",
                    current_altitude if current_altitude > 1.0 else 10.0,
                )
            )
            if not 1.0 <= altitude <= 120.0:
                raise ValueError("flight altitude must be between 1 and 120 m")
            if not self._airborne(state):
                await self._takeoff(job, state, altitude, finish=False)
                state = self._require_fresh(job.sysid)
            await self._ensure_navigation_ready(job, state)
            target = self._resolve_target(job, state)
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
            if bool(job.arguments.get("wait", True)):
                await self._verify_goto(job, target, initial)
        elif action == "follow":
            await self._follow(job, state)
        else:
            raise ValueError(f"unsupported action: {action}")

    async def _follow(self, job: ControlJob, state) -> None:
        target_name = str(job.arguments.get("target", "")).strip()
        contact = self._resolve_follow_contact(target_name)
        target_uid = str(getattr(contact, "uid", "") or target_name)
        current_altitude = float(state.relative_altitude or 0.0)
        altitude = float(
            job.arguments.get(
                "altitude_m",
                current_altitude if current_altitude > 1.0 else 10.0,
            )
        )
        if not 1.0 <= altitude <= 120.0:
            raise ValueError("follow altitude_m must be between 1 and 120 m")
        if not self._airborne(state):
            await self._takeoff(job, state, altitude, finish=False)
            state = self._require_fresh(job.sysid)
        await self._ensure_navigation_ready(job, state)

        label = str(
            getattr(contact, "callsign", "")
            or getattr(contact, "uid", target_name)
        )
        await self._update(
            job,
            "monitoring",
            f"following {label} at {altitude:.1f} m relative altitude",
        )
        last_target = None
        updates = 0
        while True:
            self._require_fresh(job.sysid)
            contact = self._resolve_follow_contact(target_uid)
            point = self._contact_position(contact)
            moved = (
                math.inf
                if last_target is None
                else gps_to_vector(last_target, point).dist
            )
            if moved >= self.follow_deadband:
                await self._goto(job.sysid, point.lat, point.lon, altitude)
                last_target = point
                updates += 1
                job.message = (
                    f"following {label}; target update {updates} sent "
                    f"({moved:.1f} m movement)"
                    if math.isfinite(moved)
                    else f"following {label}; initial target sent"
                )
                job.updated_unix = time.time()
            await asyncio.sleep(self.follow_cadence)

    async def _cancel_follow(self, job: ControlJob) -> None:
        message = "follow cancelled; target updates stopped"
        try:
            await self._command_long(
                job.sysid,
                "MAV_CMD_NAV_LOITER_UNLIM",
                operator_label="hold after follow cancellation",
            )
            message += "; hold acknowledged"
        except Exception as exc:
            logger.warning(
                "MAVLink follow job %s could not confirm hold: %s", job.id, exc
            )
            message += "; hold could not be confirmed"
        await self._update(job, "cancelled", message)

    async def _run_procedure(self, job: ControlJob) -> None:
        """Run a model-composed procedure against semantic in-process functions."""
        source = str(job.arguments.get("code", "")).strip()
        if not source:
            raise ValueError("run requires flight procedure code")
        tree = self._validate_procedure(source)

        async def invoke(action: str, **arguments):
            child = ControlJob(
                id=job.id,
                action=action,
                sysid=job.sysid,
                arguments={"sysid": job.sysid, **arguments},
            )
            await self._execute(child, self._require_fresh(job.sysid))
            job.message = child.message
            job.updated_unix = time.time()

        async def takeoff(altitude_m: float = 10.0):
            await invoke("takeoff", altitude_m=altitude_m)

        async def goto(
            target=None,
            *,
            latitude=None,
            longitude=None,
            distance_m=None,
            bearing_deg=None,
            altitude_m=None,
            wait=True,
        ):
            arguments = {"wait": bool(wait)}
            if target is not None:
                arguments["target"] = str(target)
            if latitude is not None:
                arguments["latitude"] = float(latitude)
            if longitude is not None:
                arguments["longitude"] = float(longitude)
            if distance_m is not None:
                arguments["distance_m"] = float(distance_m)
            if bearing_deg is not None:
                arguments["bearing_deg"] = float(bearing_deg)
            if altitude_m is not None:
                arguments["altitude_m"] = float(altitude_m)
            await invoke("goto", **arguments)

        async def simple(action: str):
            await invoke(action)

        def distance(target: str) -> float:
            state = self._require_fresh(job.sysid)
            probe = ControlJob(
                id=job.id,
                action="goto",
                sysid=job.sysid,
                arguments={"target": str(target)},
            )
            point = self._resolve_target(probe, state)
            return float(gps_to_vector(self._position(state), point).dist)

        async def progress(message: str):
            await self._update(job, "monitoring", str(message))

        environment = {
            "__builtins__": {},
            "takeoff": takeoff,
            "goto": goto,
            "arm": lambda: simple("arm"),
            "disarm": lambda: simple("disarm"),
            "hold": lambda: simple("hold"),
            "rtl": lambda: simple("rtl"),
            "land": lambda: simple("land"),
            "sleep": asyncio.sleep,
            "progress": progress,
            "distance": distance,
            "armed": lambda: self._armed(self._require_fresh(job.sysid)),
            "airborne": lambda: self._airborne(self._require_fresh(job.sysid)),
            "running": lambda: True,
        }
        await self._update(job, "monitoring", "flight procedure running")
        compiled = compile(
            tree,
            "<flight-procedure>",
            "exec",
            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
        procedure = eval(compiled, environment, environment)
        if procedure is not None:
            await procedure
        await self._succeed(job, "requested flight procedure completed")

    @staticmethod
    def _validate_procedure(source: str) -> ast.Module:
        """Allow control flow and calls only to the semantic runtime surface."""
        if len(source) > 4000:
            raise ValueError("flight procedure is too large")
        try:
            tree = ast.parse(source, mode="exec")
        except SyntaxError as exc:
            raise ValueError(f"invalid flight procedure: {exc.msg}") from exc

        allowed_calls = {
            "takeoff",
            "goto",
            "arm",
            "disarm",
            "hold",
            "rtl",
            "land",
            "sleep",
            "progress",
            "distance",
            "armed",
            "airborne",
            "running",
        }
        allowed_nodes = (
            ast.Module,
            ast.Expr,
            ast.Await,
            ast.Call,
            ast.Name,
            ast.Load,
            ast.Store,
            ast.Constant,
            ast.keyword,
            ast.Assign,
            ast.While,
            ast.If,
            ast.Compare,
            ast.BoolOp,
            ast.BinOp,
            ast.UnaryOp,
            ast.Break,
            ast.Continue,
            ast.Pass,
            ast.Eq,
            ast.NotEq,
            ast.Lt,
            ast.LtE,
            ast.Gt,
            ast.GtE,
            ast.And,
            ast.Or,
            ast.Not,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.Mod,
            ast.USub,
        )
        nodes = list(ast.walk(tree))
        if len(nodes) > 250:
            raise ValueError("flight procedure is too complex")
        for node in nodes:
            if not isinstance(node, allowed_nodes):
                raise ValueError(
                    f"flight procedure construct is unavailable: "
                    f"{type(node).__name__}"
                )
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in allowed_calls:
                    raise ValueError("flight procedures may call semantic functions only")
            if isinstance(node, ast.Name) and node.id.startswith("_"):
                raise ValueError("private names are unavailable in flight procedures")
            if isinstance(node, ast.Assign):
                if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                    raise ValueError("flight procedures allow simple variable assignment only")
            if isinstance(node, ast.While) and not any(
                isinstance(child, ast.Await) for child in ast.walk(node)
            ):
                raise ValueError("every flight procedure loop must yield with await")
        return tree

    async def _takeoff(
        self,
        job: ControlJob,
        state,
        altitude: float,
        *,
        finish: bool,
    ) -> None:
        """Make the vehicle airborne; callers never manage arm/mode mechanics."""
        state = await self._ensure_armed(job, state)
        state = await self._ensure_navigation_ready(job, state)
        if not self._airborne(state):
            await self._update(
                job,
                "commanding",
                f"aircraft ready; initiating takeoff to {altitude:.1f} m",
            )
            await self._command_long(
                job.sysid,
                "MAV_CMD_NAV_TAKEOFF",
                0,
                0,
                0,
                math.nan,
                0,
                0,
                altitude,
                operator_label="takeoff",
            )
        await self._update(
            job,
            "monitoring",
            f"takeoff underway; climbing to {altitude:.1f} m",
        )
        await self._wait(
            job.sysid,
            lambda item: (item.relative_altitude or 0.0) >= altitude * 0.8,
        )
        if finish:
            await self._succeed(
                job, f"airborne at target band ({altitude:.1f} m requested)"
            )

    async def _ensure_armed(self, job: ControlJob, state):
        if self._armed(state):
            return state
        await self._update(job, "commanding", "arming aircraft")
        await self._command_long(
            job.sysid,
            "MAV_CMD_COMPONENT_ARM_DISARM",
            1.0,
            operator_label="arm",
        )
        state = await self._wait(job.sysid, lambda item: self._armed(item))
        await self._update(job, "commanding", "aircraft armed")
        return state

    async def _ensure_navigation_ready(self, job: ControlJob, state):
        """Select whatever autonomous-control state the detected stack needs."""
        guided_mode = self._guided_mode(state)
        if guided_mode is None or state.custom_mode == guided_mode:
            return state
        await self._update(job, "commanding", "preparing autonomous flight control")
        await self._command_long(
            job.sysid,
            "MAV_CMD_DO_SET_MODE",
            float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
            float(guided_mode),
            operator_label="prepare autonomous flight",
        )
        return await self._wait(
            job.sysid,
            lambda item: item.custom_mode == guided_mode,
        )

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

    def _resolve_follow_contact(self, target: str):
        if self.resolve_contact is None:
            raise RuntimeError("live ATAK contact resolution is unavailable")
        try:
            contact = self.resolve_contact(target)
        except KeyError as exc:
            raise ValueError(
                f"live ATAK PLI is unavailable for contact/callsign {target!r}"
            ) from exc
        if contact is None:
            raise ValueError(
                f"live ATAK PLI is unavailable for contact/callsign {target!r}"
            )
        return contact

    @staticmethod
    def _contact_position(contact):
        if GPSposition is None or gps_to_vector is None:
            raise RuntimeError("froggeolib is unavailable")
        point = getattr(contact, "point", None)
        if point is None:
            raise ValueError("live ATAK contact has no PLI position")
        return GPSposition(
            float(point.latitude),
            float(point.longitude),
            float(getattr(point, "hae", 0.0) or 0.0),
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

    async def _command_long(
        self,
        sysid: int,
        command_name: str,
        *params,
        operator_label: str = "flight command",
    ) -> None:
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
        await self._wait_ack(sysid, command, sent_at, operator_label)

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
        await self._wait_ack(sysid, command, sent_at, "fly-to")

    async def _wait_ack(
        self,
        sysid: int,
        command: int,
        sent_at: float,
        operator_label: str,
    ) -> None:
        deadline = time.monotonic() + min(self.command_timeout, 15.0)
        while time.monotonic() < deadline:
            state = self._require_fresh(sysid)
            ack = state.command_acks.get(int(command))
            if ack is not None and ack[1] >= sent_at:
                result = ack[0]
                if result in {0, 5}:
                    return
                raise RuntimeError(
                    f"aircraft rejected {operator_label}: "
                    f"{self._result_name(result)}"
                )
            await asyncio.sleep(0.1)
        raise TimeoutError(f"aircraft did not acknowledge {operator_label}")

    async def _succeed(self, job: ControlJob, message: str) -> None:
        await self._update(job, "succeeded", message)

    async def _update(self, job: ControlJob, phase: str, message: str) -> None:
        previous_phase = job.phase
        job.phase = phase
        job.message = message
        job.updated_unix = time.time()
        should_notify = phase in TERMINAL_PHASES or (
            phase == "monitoring" and previous_phase != "monitoring"
        )
        if job.notify_chat_id and should_notify:
            try:
                await self.notify(
                    job.notify_chat_id,
                    f"{self.callsign(job.sysid)}: {message}",
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

    @classmethod
    def _airborne(cls, state) -> bool:
        return cls._armed(state) and (getattr(state, "relative_altitude", 0.0) or 0.0) > 1.0

    @staticmethod
    def _result_name(result: int) -> str:
        if mavutil is not None:
            entry = getattr(mavutil.mavlink, "enums", {}).get("MAV_RESULT", {}).get(
                int(result)
            )
            if entry is not None:
                return str(getattr(entry, "name", result)).removeprefix("MAV_RESULT_").lower()
        return f"result {result}"

    @staticmethod
    def _guided_mode(state) -> Optional[int]:
        return MavlinkControlService._ardupilot_mode(state, "GUIDED")

    @staticmethod
    def _ardupilot_mode(state, name: str) -> Optional[int]:
        if getattr(state, "autopilot", None) != MAV_AUTOPILOT_ARDUPILOTMEGA:
            return None
        if mavutil is None:
            raise RuntimeError("pymavlink is unavailable")
        mapping = mavutil.mode_mapping_byname(getattr(state, "mav_type", None))
        normalized = str(name).upper()
        if not mapping or normalized not in mapping:
            raise RuntimeError(
                f"aircraft does not advertise a supported {normalized} flight state"
            )
        return int(mapping[normalized])

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
