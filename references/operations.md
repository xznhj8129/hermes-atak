# Operations

## `atak_state` Actions

- `status`: connection, state counts, OTS snapshot state, and provider.
- `receipts`: delivered/read status keyed by GeoChat message ID.
- `contacts`: retained contact identity and time.
- `markers`: merged live-stream and OTS-retained markers.
- `relative_markers`: froggeolib vectors from an exact origin to every marker.
- `nearest_marker`: closest merged marker from an exact origin.
- `range_bearing`: froggeolib vector between exact retained identifiers.

Use the GeoChat sender UID as the origin for “me.” Request coordinates only
when the user explicitly needs them.

## `mavlink_uav` Actions

- `status`: live registry state for one sysid or all vehicles.
- `jobs`: recent jobs or one exact job ID.
- `run`: execute a short in-memory async procedure composed from semantic
  `takeoff`, `goto`, `land`, `rtl`, `hold`, `arm`, `disarm`, live state,
  FrogCoT targets, control flow, and yielding waits.
- `cancel`: stop background monitoring for a job; cancellation does not issue
  a compensating flight command.

State-changing calls require an exact sysid and return immediately with a job
ID. The persistent controller sends ATAK milestones while it performs preflight
and telemetry verification. Do not poll from an agent loop and do not create a
command script.

The procedure functions are motion capabilities, not a list of phrases the
operator is allowed to say. Infer arbitrary orders from meaning and live state.
For example, “follow me” becomes a cancellable loop that updates `goto` from
the sender's live FrogCoT identity; it is not rejected because no function is
literally named `follow`.

## Live Verification

Verify these paths independently:

1. Send a direct ATAK message and confirm it creates a normal Hermes session.
2. Confirm the reply's inner GeoChat `messageId` receives `b-t-f-d` and
   `b-t-f-r`.
3. Create or update a marker while Hermes is connected and confirm it appears
   as a live-stream marker.
4. Restart Hermes, query markers, and confirm an older OTS-retained marker is
   returned with `source: opentakserver`.
5. Ask a natural-language directional question and confirm Hermes obtains
   froggeolib vectors before interpreting the direction.

When MAVLink telemetry is enabled, additionally verify:

1. mavlink-router owns UDP `14550` and listens on TCP `5760`; Hermes appears
   only as a TCP client.
2. `atak_state` status reports `mavlink_enabled`, `mavlink_connected`, and the
   independently observed vehicle count.
3. A fresh sysid 1 vehicle produces one marker with the configured callsign and
   a UID ending in `-uav-1`; updates reuse that UID.
4. A second airborne sysid produces a separate marker and does not move or
   rename the first.
5. Removing heartbeat/fix telemetry stops publication, and ATAK removes the
   marker after its CoT stale time.
6. Through SITL/HITL, submit a safe command and confirm prompt job acceptance,
   milestone GeoChat updates, and a telemetry-verified terminal job phase.

## Common Failures

### Pairing code appears

The CoT transport worked, but gateway authorization rejected the sender. Add
the exact ATAK device UID to `ATAK_ALLOWED_USERS`; do not use pairing as the
steady-state identity mechanism.

### ATAK shows a marker but Hermes does not

Call a marker-related `atak_state` action, which triggers OTS reconciliation.
If `opentakserver_snapshot_error` is set, verify `ots_python`, `ots_config`,
database reachability, and read permissions. Do not add a polling relay.

### Marker metadata is old

OTS may retain an old `markers.cot_id` while updating `markers.point_id`.
Read the current point relationship and newest CoT type by marker UID.

### Reply arrived but no `geochat` row exists

At 0°,0°, OTS may treat the point as absent and skip its `geochat` row while
still routing GeoChat. Use matching delivery/read receipts as evidence.

### Duplicate or inconsistent presence

Stop legacy relays and ad-hoc clients using the Hermes UID. Run exactly one
persistent adapter connection.

### UAV marker is absent

Confirm `mavlink_enabled: true`, `pymavlink` is installed in the gateway
interpreter, and mavlink-router accepts TCP clients on the configured endpoint.
The bridge intentionally rejects UDP and `tcpin:` endpoints. Then confirm the
vehicle emits an airborne heartbeat plus `GPS_RAW_INT` with fix type 3 or
better and a valid non-`0,0` position within `mavlink_freshness`.

### UAV marker freezes or disappears

Check the router stream for fresh heartbeat, fix, and position messages from
the same actual source sysid. Losing any one suppresses further CoT updates.
Router or TCP loss is retried with bounded backoff until the adapter
disconnects; the old marker expires at `mavlink_stale`.

### A UAV command appears to hang

Read the job once with `mavlink_uav {"action":"jobs","job_id":"..."}`. The job
message identifies preflight, transport, stale-telemetry, or verification
failure. Do not launch another script or repeat a state-changing command
blindly. In ATAK, confirm the origin peer remains known and the CoT stream is
connected so milestone messages can be delivered.

### Stream reconnects but old markers vanish

The stream is forward-looking. Query-time OTS reconciliation restores retained
server markers; it does not replay them onto the network.
