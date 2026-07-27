from __future__ import annotations

import datetime
import json
import sys
import time
import types
from types import SimpleNamespace

import pytest

import plugin.adapter as adapter_module
from plugin import mavlink_control as mavlink_control_module
from plugin.adapter import ATAKAdapter, mavlink_uav_tool, register
from plugin.mavlink_control import (
    ControlJob,
    MAV_MODE_FLAG_SAFETY_ARMED,
    MavlinkControlService,
)


@pytest.fixture(autouse=True)
def fake_mavutil(monkeypatch):
    fake = SimpleNamespace(mode_mapping_byname=lambda _mav_type: {"GUIDED": 4})
    monkeypatch.setattr(mavlink_control_module, "mavutil", fake)


def vehicle_state(**values):
    defaults = {
        "sysid": 1,
        "component_id": 1,
        "mav_type": 2,
        "autopilot": 3,
        "base_mode": MAV_MODE_FLAG_SAFETY_ARMED,
        "custom_mode": 4,
        "last_heartbeat": time.monotonic(),
        "latitude": 45.0,
        "longitude": -75.0,
        "altitude": 135.0,
        "relative_altitude": 35.0,
        "command_acks": {},
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def contact(uid="ATAK-USER", callsign="Raven", lat=45.001, lon=-75.001):
    return SimpleNamespace(
        uid=uid,
        callsign=callsign,
        point=SimpleNamespace(latitude=lat, longitude=lon, hae=100.0),
    )


def control_service(state, target, notifications=None, **options):
    notifications = notifications if notifications is not None else []

    async def notify(chat_id, message):
        notifications.append((chat_id, message))

    bridge = SimpleNamespace(
        tracker=SimpleNamespace(vehicles={state.sysid: state}, freshness=5.0),
        _connection=object(),
    )
    service = MavlinkControlService(
        bridge=bridge,
        callsign=lambda sysid: f"UAV-{sysid}",
        resolve_point=lambda _name: None,
        resolve_contact=lambda _name: target(),
        notify=notify,
        command_timeout=5.0,
        follow_cadence=options.get("follow_cadence", 1.0),
        follow_deadband=options.get("follow_deadband", 5.0),
    )
    return service


async def test_follow_uses_current_altitude_deadband_and_holds_on_cancel(monkeypatch):
    state = vehicle_state()
    current = contact()
    service = control_service(state, lambda: current)
    gotos = []
    commands = []
    sleeps = 0

    async def goto(sysid, lat, lon, altitude):
        gotos.append((sysid, lat, lon, altitude))

    async def command_long(sysid, command, *params, **kwargs):
        commands.append((sysid, command, params, kwargs))

    async def sleep(_seconds):
        nonlocal current, sleeps
        sleeps += 1
        if sleeps == 1:
            current = contact(lat=45.00101)
        elif sleeps == 2:
            current = contact(lat=45.00110)
        else:
            raise asyncio_cancelled()

    def asyncio_cancelled():
        return mavlink_control_module.asyncio.CancelledError()

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_command_long", command_long)
    monkeypatch.setattr(mavlink_control_module.asyncio, "sleep", sleep)
    job = ControlJob(
        id="follow-live",
        action="follow",
        sysid=1,
        arguments={"sysid": 1, "target": "ATAK-USER"},
    )

    await service._run_job(job)

    assert len(gotos) == 2
    assert all(item[0] == 1 and item[3] == 35.0 for item in gotos)
    assert commands[0][1] == "MAV_CMD_NAV_LOITER_UNLIM"
    assert job.phase == "cancelled"
    assert job.message == "follow cancelled; target updates stopped; hold acknowledged"


def test_follow_submit_requires_exact_sysid_and_live_pli():
    state = vehicle_state()
    service = control_service(state, lambda: None)
    service.loop = SimpleNamespace(is_running=lambda: True)

    missing_sysid = service.submit("follow", {"target": "Raven"})
    missing_pli = service.submit("follow", {"sysid": 1, "target": "Raven"})

    assert missing_sysid["error"] == "an explicit integer sysid is required"
    assert "live ATAK PLI is unavailable" in missing_pli["error"]


def adapter_config(**extra):
    values = {
        "host": "cot.example.test",
        "ca": "/certs/ca.pem",
        "client_certificate": "/certs/client.pem",
        "client_key": "/certs/client.key",
        "callsign": "Hermes",
        "uid": "HERMES",
        "mavlink_enabled": False,
    }
    values.update(extra)
    return SimpleNamespace(extra=values, home_channel=None)


def test_follow_contact_rejects_missing_marker_and_stale_pli():
    adapter = ATAKAdapter(adapter_config(mavlink_follow_pli_freshness=10.0))
    stale = SimpleNamespace(
        uid="STALE",
        callsign="Old",
        point=SimpleNamespace(latitude=45.0, longitude=-75.0),
        time=datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=11),
    )
    marker = SimpleNamespace(uid="MARKER")
    adapter.situational_awareness = SimpleNamespace(
        get_contact=lambda value: stale if value == "STALE" else None,
        get_marker=lambda _value: marker,
    )

    with pytest.raises(KeyError):
        adapter._resolve_follow_contact("MARKER")
    with pytest.raises(ValueError, match="is stale"):
        adapter._resolve_follow_contact("STALE")


def test_follow_me_infers_current_atak_sender_uid(monkeypatch):
    submitted = {}

    class Control:
        def submit(self, action, args, *, notify_chat_id):
            submitted.update(
                action=action, args=args, notify_chat_id=notify_chat_id
            )
            return {"success": True}

    adapter = SimpleNamespace(_mavlink_control=Control())
    monkeypatch.setattr(adapter_module, "_live_adapter", lambda: adapter)
    session_context = types.ModuleType("gateway.session_context")
    values = {
        "HERMES_SESSION_PLATFORM": "atak",
        "HERMES_SESSION_CHAT_ID": "ROOM-1",
        "HERMES_SESSION_USER_ID": "ATAK-SENDER",
    }
    session_context.get_session_env = lambda name, default="": values.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)

    result = json.loads(
        mavlink_uav_tool({"action": "follow", "sysid": 1, "notify": True})
    )

    assert result["success"] is True
    assert submitted == {
        "action": "follow",
        "args": {
            "action": "follow",
            "sysid": 1,
            "notify": True,
            "target": "ATAK-SENDER",
        },
        "notify_chat_id": "ROOM-1",
    }


def test_registered_tool_exposes_follow_target_altitude_and_exact_sysid():
    tools = {}

    class Context:
        def register_platform(self, **_kwargs):
            pass

        def register_tool(self, **kwargs):
            tools[kwargs["name"]] = kwargs

    register(Context())
    parameters = tools["mavlink_uav"]["schema"]["parameters"]

    assert "follow" in parameters["properties"]["action"]["enum"]
    assert parameters["properties"]["sysid"]["minimum"] == 1
    assert parameters["properties"]["sysid"]["maximum"] == 254
    assert "target" in parameters["properties"]
    assert "altitude_m" in parameters["properties"]


async def test_takeoff_still_auto_arms_selects_guided_and_climbs(monkeypatch):
    state = vehicle_state(base_mode=0, custom_mode=0, relative_altitude=0.0)
    service = control_service(state, lambda: contact())
    commands = []

    async def command_long(sysid, command, *params, **_kwargs):
        commands.append((sysid, command, params))
        if command == "MAV_CMD_COMPONENT_ARM_DISARM":
            state.base_mode |= MAV_MODE_FLAG_SAFETY_ARMED
        elif command == "MAV_CMD_DO_SET_MODE":
            state.custom_mode = int(params[1])
        elif command == "MAV_CMD_NAV_TAKEOFF":
            state.relative_altitude = float(params[6])

    monkeypatch.setattr(service, "_command_long", command_long)
    job = ControlJob(
        id="takeoff-ok",
        action="takeoff",
        sysid=1,
        arguments={"altitude_m": 20.0},
    )

    await service._run_job(job)

    assert [item[1] for item in commands] == [
        "MAV_CMD_COMPONENT_ARM_DISARM",
        "MAV_CMD_DO_SET_MODE",
        "MAV_CMD_NAV_TAKEOFF",
    ]
    assert job.phase == "succeeded"


async def test_goto_still_sends_one_target_at_requested_altitude(monkeypatch):
    state = vehicle_state()
    target = SimpleNamespace(latitude=45.01, longitude=-75.01)
    service = control_service(state, lambda: contact())
    service.resolve_point = lambda _name: target
    gotos = []

    async def goto(sysid, lat, lon, altitude):
        gotos.append((sysid, lat, lon, altitude))

    monkeypatch.setattr(service, "_goto", goto)
    job = ControlJob(
        id="goto-ok",
        action="goto",
        sysid=1,
        arguments={
            "target": "DESTINATION",
            "altitude_m": 42.0,
            "wait": False,
        },
    )

    await service._run_job(job)

    assert gotos == [(1, 45.01, -75.01, 42.0)]
    assert job.phase == "monitoring"
