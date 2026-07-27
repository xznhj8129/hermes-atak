from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from plugin import mavlink_control as mavlink_control_module
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
        "sysid": 7,
        "component_id": 1,
        "mav_type": 2,
        "autopilot": 3,
        "base_mode": 0,
        "custom_mode": 0,
        "last_heartbeat": time.monotonic(),
        "relative_altitude": 0.0,
        "command_acks": {},
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def control_service(state, notifications):
    async def notify(chat_id, message):
        notifications.append((chat_id, message))

    bridge = SimpleNamespace(
        tracker=SimpleNamespace(vehicles={state.sysid: state}, freshness=5.0),
        _connection=object(),
    )
    return MavlinkControlService(
        bridge=bridge,
        callsign=lambda sysid: f"UAV-{sysid}",
        resolve_point=lambda _name: None,
        notify=notify,
        command_timeout=5.0,
    )


async def test_takeoff_auto_arms_verifies_mode_and_climb(monkeypatch):
    state = vehicle_state()
    notifications = []
    service = control_service(state, notifications)
    commands = []

    async def command_long(sysid, command, *params):
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
        sysid=state.sysid,
        notify_chat_id="chat-1",
        arguments={"altitude_m": 20.0},
    )

    await service._run_job(job)

    assert [item[1] for item in commands] == [
        "MAV_CMD_COMPONENT_ARM_DISARM",
        "MAV_CMD_DO_SET_MODE",
        "MAV_CMD_NAV_TAKEOFF",
    ]
    assert commands[0][2] == (1.0,)
    assert commands[1][2] == (1.0, 4.0)
    assert commands[2][2][-1] == 20.0
    assert job.phase == "succeeded"
    assert notifications == []


async def test_takeoff_stops_when_armed_telemetry_is_not_observed(monkeypatch):
    state = vehicle_state()
    notifications = []
    service = control_service(state, notifications)
    commands = []

    async def command_long(sysid, command, *params):
        commands.append((sysid, command, params))

    async def fail_wait(_sysid, _predicate, *, timeout=None):
        raise TimeoutError("expected telemetry transition was not observed")

    monkeypatch.setattr(service, "_command_long", command_long)
    monkeypatch.setattr(service, "_wait", fail_wait)
    job = ControlJob(
        id="takeoff-failed",
        action="takeoff",
        sysid=state.sysid,
        notify_chat_id="chat-1",
        arguments={"altitude_m": 20.0},
    )
    service.jobs[job.id] = job

    await service._run_job(job)

    assert [item[1] for item in commands] == [
        "MAV_CMD_COMPONENT_ARM_DISARM",
    ]
    assert job.phase == "failed"
    assert job.message == (
        "TimeoutError: expected telemetry transition was not observed"
    )
    assert notifications == [
        ("chat-1", "UAV-7 [takeoff-failed] failed unexpectedly")
    ]
    assert service.jobs_status(job.id)["job"]["message"] == (
        "TimeoutError: expected telemetry transition was not observed"
    )
