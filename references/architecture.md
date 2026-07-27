# Architecture

## Boundaries

The integration consists of one Hermes platform adapter:

1. `PersistentCoTClient` maintains a mutual-TLS CoT stream.
2. The adapter publishes a stable Hermes PLI and refreshes it before expiry.
3. Inbound `b-t-f` GeoChat becomes a gateway `MessageEvent`.
4. `BasePlatformAdapter.handle_message` invokes the normal Hermes main agent.
5. The adapter serializes the final reply as direct or room GeoChat.
6. `b-t-f-d` and `b-t-f-r` events update delivery/read receipt state.
7. When enabled, one persistent pymavlink TCP client observes mavlink-router and
   publishes fresh UAV positions through the same `PersistentCoTClient` and
   send lock used by presence and GeoChat.
8. `mavlink_uav` enqueues explicitly targeted commands on that service, returns
   immediately, verifies telemetry in the background, and sends milestone
   GeoChat updates to the originating TAK peer.

Do not add a second model invocation, separate persona, response generator,
phrase router, cron bridge, or database-backed chat loop.

## MAVLink control boundary

The bridge uses one pymavlink client for the shared
`tcp:127.0.0.1:5760` mavlink-router stream. It never binds UDP `14550` or opens
another ATAK socket. Every vehicle key comes from
the received MAVLink message's `get_srcSystem()`; pymavlink's local
`source_system=255` identifies the client/GCS side and is never treated as a
target vehicle.

The `mavlink_uav` runtime is the only control boundary. The main Hermes agent
interprets operator intent using conversation and live state, then submits a
short in-memory async procedure composed from semantic motion and FrogCoT
spatial functions. The adapter never routes hardcoded phrases. Each procedure
requires an explicit discovered sysid and is addressed to that vehicle and its
observed autopilot component. Calls enqueue a background job instead of
creating a script or connection. A per-sysid lock serializes maneuvers; no
state-changing command is blindly retried.

The core records `COMMAND_ACK` by source sysid and command ID. Its public
surface is semantic: takeoff, fly to a destination, hold, return, and land.
Each function detects the connected flight stack and owns its mode, readiness,
arming, command, and telemetry-verification mechanics. Raw MAVLink identifiers
never cross the operator boundary.

Procedures may use bounded control flow and long-running cancellable loops.
Only the semantic runtime names are available; imports, attributes, filesystem,
network, raw protocol, and function-definition access are absent. This lets
the agent infer “follow me” as a live FrogCoT target loop without a generated
file, phrase-specific router, new process, connection, or model polling loop.

Each source sysid has independent heartbeat, fix, position, state, and
last-publish timestamps. Only airborne MAV types with a fresh heartbeat and a
fresh 3D-fix-backed valid location are eligible. Stable CoT identity is
`<Hermes uid>-uav-<sysid>`; callsign customization never changes that UID.
Telemetry loss stops updates, and the short configured CoT stale time removes
the disconnected marker from ATAK naturally.

Jobs progress through `queued`, `preflight`, `commanding`, `monitoring`, and a
terminal `succeeded`, `failed`, or `cancelled` phase. The tool returns after
queueing. When invoked from ATAK, the origin chat ID is captured from Hermes
session context and progress is sent without another model turn.

## Situational State

Use two complementary sources:

- `frogcot.SituationalAwareness` retains contacts and markers received after
  the current persistent stream connected.
- Marker-related queries invoke `ots_snapshot.py`, which reads OTS's current
  marker relationships once and returns a point snapshot.

OpenTAKServer can update `markers.point_id` while `markers.cot_id` still points
to an older event. Reconstruct server marker state from:

- marker UID and callsign;
- the current row referenced by `markers.point_id`; and
- the newest CoT type for that marker UID, falling back to the original CoT.

Merge records by UID and prefer the newer event time. Keep OTS access read-only,
bounded by a short timeout, and cached briefly within a single agent turn.

## Spatial Reasoning

Use FrogCoT retained marker points with `froggeolib.GPSposition`,
`gps_to_vector`, and `vector_to_gps` for distance, true azimuth, elevation, and
destination calculation. Expose neutral results through `atak_state`; let the main model
interpret language such as “south,” “near,” or “behind.” Never hardcode natural
language phrases or compass-sector decisions in the transport adapter.

## Identity and Routing

- Keep one stable Hermes device UID across reconnects.
- Preserve the originating sender UID and callsign in the gateway source.
- Keep UAV identities keyed by actual MAVLink source sysid, never the local GCS
  sysid or a connection-level target field.
- Route direct replies to the originating peer.
- Route room replies with the original room identifier.
- Generate unique event/message IDs while preserving the stable device UID.

## Privacy

Do not return coordinates by default. Never log or commit certificate keys,
database URIs, passwords, tokens, private positions, or message content beyond
what is required for the active operation.
