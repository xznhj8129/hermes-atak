# Architecture

## Boundaries

The integration consists of one Hermes platform adapter:

1. `PersistentCoTClient` maintains a mutual-TLS CoT stream.
2. The adapter publishes a stable Hermes PLI and refreshes it before expiry.
3. Inbound `b-t-f` GeoChat becomes a gateway `MessageEvent`.
4. `BasePlatformAdapter.handle_message` invokes the normal Hermes main agent.
5. The adapter serializes the final reply as direct or room GeoChat.
6. `b-t-f-d` and `b-t-f-r` events update delivery/read receipt state.

Do not add a second model invocation, separate persona, response generator,
phrase router, cron bridge, or database-backed chat loop.

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

Use `froggeolib.GPSposition` and `gps_to_vector` for distance, true azimuth, and
elevation. Expose neutral results through `atak_state`; let the main model
interpret language such as “south,” “near,” or “behind.” Never hardcode natural
language phrases or compass-sector decisions in the transport adapter.

## Identity and Routing

- Keep one stable Hermes device UID across reconnects.
- Preserve the originating sender UID and callsign in the gateway source.
- Route direct replies to the originating peer.
- Route room replies with the original room identifier.
- Generate unique event/message IDs while preserving the stable device UID.

## Privacy

Do not return coordinates by default. Never log or commit certificate keys,
database URIs, passwords, tokens, private positions, or message content beyond
what is required for the active operation.
