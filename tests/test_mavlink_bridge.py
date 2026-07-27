from __future__ import annotations

import asyncio
import datetime
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from frogcot import ATAKClient

from plugin.adapter import ATAKAdapter
import plugin.mavlink_bridge as mavlink_bridge_module
from plugin.mavlink_bridge import (
    MAV_TYPE_FIXED_WING,
    MAV_TYPE_GCS,
    MavlinkTelemetryBridge,
    MavlinkTelemetryTracker,
)


class Message:
    def __init__(self, message_type: str, sysid: int, **fields):
        self._message_type = message_type
        self._sysid = sysid
        for name, value in fields.items():
            setattr(self, name, value)

    def get_type(self):
        return self._message_type

    def get_srcSystem(self):
        return self._sysid


def heartbeat(sysid: int, mav_type: int = MAV_TYPE_FIXED_WING):
    return Message(
        "HEARTBEAT",
        sysid,
        type=mav_type,
        autopilot=3,
        base_mode=81,
        system_status=4,
    )


def gps(sysid: int, lat: int, lon: int, *, fix_type: int = 3, alt: int = 123000):
    return Message(
        "GPS_RAW_INT",
        sysid,
        fix_type=fix_type,
        lat=lat,
        lon=lon,
        alt=alt,
        eph=120,
        epv=180,
        vel=900,
        cog=1234,
        satellites_visible=12,
    )


def test_tracks_actual_source_sysid_without_conflating_gcs_or_vehicles():
    tracker = MavlinkTelemetryTracker(freshness=5.0, cadence=1.0)

    tracker.ingest(heartbeat(1), now=10.0)
    tracker.ingest(gps(1, 451234567, -751234567), now=10.1)
    tracker.ingest(heartbeat(2), now=10.2)
    tracker.ingest(gps(2, 461234567, -761234567), now=10.3)
    tracker.ingest(heartbeat(255, MAV_TYPE_GCS), now=10.4)
    tracker.ingest(gps(255, 471234567, -771234567), now=10.5)

    due = {item.sysid: item for item in tracker.due(now=10.6)}

    assert set(due) == {1, 2}
    assert due[1].latitude == pytest.approx(45.1234567)
    assert due[1].longitude == pytest.approx(-75.1234567)
    assert due[2].latitude == pytest.approx(46.1234567)
    assert due[2].longitude == pytest.approx(-76.1234567)


@pytest.mark.parametrize(
    "messages,now",
    [
        ([heartbeat(1)], 1.0),
        ([heartbeat(1), gps(1, 451234567, -751234567, fix_type=2)], 1.0),
        ([heartbeat(1), gps(1, 0, 0)], 1.0),
        ([heartbeat(1), gps(1, 451234567, -751234567)], 7.0),
    ],
)
def test_requires_fresh_uav_heartbeat_and_nonzero_3d_fix(messages, now):
    tracker = MavlinkTelemetryTracker(freshness=5.0, cadence=1.0)
    for message in messages:
        tracker.ingest(message, now=1.0)

    assert tracker.due(now=now) == []


def test_cadence_is_independent_per_sysid_and_stale_vehicles_stop():
    tracker = MavlinkTelemetryTracker(freshness=3.0, cadence=1.0)
    for sysid in (1, 2):
        tracker.ingest(heartbeat(sysid), now=1.0)
        tracker.ingest(gps(sysid, 450000000 + sysid, -750000000), now=1.0)

    first = tracker.due(now=1.0)
    tracker.mark_published(1, now=1.0)
    assert [item.sysid for item in tracker.due(now=1.5)] == [2]
    tracker.mark_published(2, now=1.5)
    assert tracker.due(now=1.9) == []
    assert [item.sysid for item in tracker.due(now=2.0)] == [1]
    assert tracker.due(now=4.1) == []


def test_global_position_requires_a_recent_gps_fix_from_the_same_sysid():
    tracker = MavlinkTelemetryTracker(freshness=5.0, cadence=1.0)
    tracker.ingest(heartbeat(1), now=1.0)
    tracker.ingest(
        Message(
            "GLOBAL_POSITION_INT",
            1,
            lat=451234567,
            lon=-751234567,
            alt=125000,
            vx=300,
            vy=400,
            hdg=9000,
        ),
        now=1.0,
    )
    assert tracker.due(now=1.0) == []

    tracker.ingest(gps(2, 461234567, -761234567), now=1.0)
    assert tracker.due(now=1.0) == []

    tracker.ingest(gps(1, 451234567, -751234567), now=1.1)
    tracker.ingest(
        Message(
            "GLOBAL_POSITION_INT",
            1,
            lat=451234600,
            lon=-751234600,
            alt=125000,
            vx=300,
            vy=400,
            hdg=9000,
        ),
        now=1.2,
    )
    vehicle = tracker.due(now=1.2)[0]
    assert vehicle.sysid == 1
    assert vehicle.altitude == 125.0
    assert vehicle.ground_speed == 5.0
    assert vehicle.course == 90.0


@pytest.mark.parametrize("endpoint", ["udp:127.0.0.1:14550", "tcpin:0.0.0.0:5760"])
def test_bridge_refuses_udp_and_listening_endpoints(endpoint):
    with pytest.raises(ValueError):
        MavlinkTelemetryBridge(
            endpoint=endpoint,
            publish=lambda _vehicle: asyncio.sleep(0, result=True),
        )


def test_pymavlink_tcp_client_is_passive_and_surfaces_eof(monkeypatch):
    opened = SimpleNamespace()

    class FakeMavutil:
        @staticmethod
        def mavlink_connection(endpoint, **options):
            opened.endpoint = endpoint
            opened.options = options
            return opened

    monkeypatch.setattr(mavlink_bridge_module, "mavutil", FakeMavutil)

    connection = MavlinkTelemetryBridge._open_connection(
        "tcp:127.0.0.1:5760"
    )

    assert connection is opened
    assert opened.endpoint == "tcp:127.0.0.1:5760"
    assert opened.options["autoreconnect"] is False
    assert opened.options["input"] is True
    assert opened.options["source_system"] == 255
    with pytest.raises(EOFError):
        connection.handle_eof()


def adapter_config(**extra):
    values = {
        "host": "cot.example.test",
        "port": 8089,
        "server_hostname": "cot.example.test",
        "ca": "/certs/ca.pem",
        "client_certificate": "/certs/client.pem",
        "client_key": "/certs/client.key",
        "callsign": "Hermes",
        "uid": "HERMES-STABLE",
        "position": {"lat": 1, "lon": 2, "alt": 3, "ce": 4, "le": 5},
    }
    values.update(extra)
    return SimpleNamespace(extra=values, home_channel=None)


def test_serialized_marker_has_stable_uid_coordinates_callsign_type_and_stale():
    adapter = ATAKAdapter(
        adapter_config(
            mavlink_enabled=True,
            mavlink_callsign_prefix="Falcon",
            mavlink_callsigns={"2": "Survey Two"},
            mavlink_cot_type="a-f-A-M-H-Q",
            mavlink_stale=12.0,
        )
    )
    tracker = MavlinkTelemetryTracker(freshness=5.0, cadence=1.0)
    tracker.ingest(heartbeat(2), now=1.0)
    tracker.ingest(gps(2, 451234567, -751234567), now=1.0)
    vehicle = tracker.due(now=1.0)[0]

    first = ET.fromstring(adapter._serialize_uav_marker(vehicle))
    second = ET.fromstring(adapter._serialize_uav_marker(vehicle))

    assert first.get("uid") == second.get("uid") == "HERMES-STABLE-uav-2"
    assert first.get("type") == "a-f-A-M-H-Q"
    assert first.find("./detail/contact").get("callsign") == "Survey Two"
    assert adapter._uav_callsign(3) == "Falcon-3"
    assert first.find("point").attrib == {
        "lat": "45.1234567",
        "lon": "-75.1234567",
        "hae": "123.0",
        "ce": "9999999.0",
        "le": "9999999.0",
    }
    assert first.find("./detail/track").attrib == {
        "speed": "9.0",
        "course": "12.34",
    }
    stale = datetime.datetime.fromisoformat(first.get("stale").replace("Z", "+00:00"))
    event_time = datetime.datetime.fromisoformat(first.get("time").replace("Z", "+00:00"))
    assert 11.0 <= (stale - event_time).total_seconds() <= 13.0


class FakeConnection:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.closed = False

    def recv_match(self, *, blocking, timeout):
        outcome = next(self.outcomes, None)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


async def test_bridge_reconnects_and_closes_connection_on_stop():
    first = FakeConnection([ConnectionResetError("lost")])
    second = FakeConnection([heartbeat(1), gps(1, 451234567, -751234567)])
    connections = iter([first, second])
    connected = asyncio.Event()

    def factory(endpoint):
        connection = next(connections)
        if connection is second:
            connected.set()
        return connection

    bridge = MavlinkTelemetryBridge(
        endpoint="tcp:127.0.0.1:5760",
        publish=lambda _vehicle: asyncio.sleep(0, result=True),
        connection_factory=factory,
        reconnect_initial=0.001,
        reconnect_max=0.002,
        receive_timeout=0.001,
    )

    bridge.start()
    await asyncio.wait_for(connected.wait(), timeout=1.0)
    await bridge.stop()

    assert first.closed
    assert second.closed
    assert bridge.task is None


class FakeCoTClient:
    connected = True

    def __init__(self):
        self.sent = []
        self.received = []
        self.closed = False

    def connect(self):
        pass

    def send(self, payload):
        self.sent.append(payload)

    def receive(self, _timeout):
        return self.received.pop(0) if self.received else None

    def close(self):
        self.closed = True
        self.connected = False


async def test_missing_mavlink_config_leaves_presence_and_geochat_unchanged():
    adapter = ATAKAdapter(adapter_config())
    client = FakeCoTClient()
    adapter._client = client

    await adapter._send_presence()
    peer = SimpleNamespace(uid="ATAK-PEER", callsign="Peer")
    sender = ATAKClient("Peer")
    sender.uid = peer.uid
    inbound = sender.geochat(
        "status",
        dest=SimpleNamespace(uid=adapter.uid, callsign=adapter.callsign),
        pos=adapter.position,
    )
    await adapter._handle_cot(inbound)

    assert adapter._mavlink_bridge is None
    assert len(client.sent) == 1
    assert len(adapter.handled_messages) == 1
    assert adapter.handled_messages[0].text == "status"


@pytest.mark.parametrize("configured", [False, "false", "off", 0])
def test_explicit_false_values_disable_mavlink(configured):
    adapter = ATAKAdapter(adapter_config(mavlink_enabled=configured))

    assert adapter.mavlink_enabled is False
    assert adapter._mavlink_bridge is None


class FakeBridge:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


async def test_adapter_starts_and_stops_enabled_bridge_with_its_lifecycle(monkeypatch):
    adapter = ATAKAdapter(adapter_config(mavlink_enabled=True))
    client = FakeCoTClient()
    bridge = FakeBridge()
    adapter._client_factory = lambda **_kwargs: client
    adapter._mavlink_bridge = bridge
    monkeypatch.setattr("plugin.adapter.pymavlink_available", lambda: True)

    assert await adapter.connect()
    assert bridge.started == 1

    await adapter.disconnect()

    assert bridge.stopped == 1
    assert client.closed
