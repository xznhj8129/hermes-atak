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

### Stream reconnects but old markers vanish

The stream is forward-looking. Query-time OTS reconciliation restores retained
server markers; it does not replay them onto the network.
