# Hermes ATAK

Hermes ATAK makes the normal Hermes agent act as one virtual ATAK user through
OpenTAKServer (OTS). It provides direct GeoChat messaging, delivery/read
receipts, live situational awareness, OTS-retained marker reconciliation, and
an optional persistent multi-vehicle MAVLink telemetry/control service. Geospatial
reasoning uses
[frogcot](https://github.com/xznhj8129/frogcot) and
[froggeolib](https://github.com/xznhj8129/froggeolib).

The integration is intentionally passive. One persistent mutual-TLS CoT client
receives ATAK traffic, forwards GeoChat into Hermes's normal gateway flow, and
returns the main agent's final response. It does not introduce a second
chatbot, phrase router, polling relay, or alternate persona.

## What it provides

- A Hermes ATAK platform plugin using a persistent CoT connection.
- Direct and room GeoChat with delivery and read receipt tracking.
- Live contacts and markers received from the CoT stream.
- Stable, continuously updated friendly UAV markers keyed by MAVLink source
  sysid when the optional router client is enabled.
- One `mavlink_uav` API for targeted arm, disarm, takeoff, land, RTL, hold, and
  goto operations without per-command scripts or connections.
- Immediate command acceptance with a job ID, background telemetry
  verification, and milestone messages to the originating ATAK chat.
- On-demand, read-only reconciliation of markers retained by OTS.
- Range, bearing, elevation, nearest-marker, and relative-marker operations.
- A Hermes skill that teaches the main agent to interpret spatial language
  using neutral froggeolib results.

## Install

Clone the repository and run the installer with the Python interpreter used by
the Hermes gateway:

```bash
git clone git@github.com:xznhj8129/hermes-atak.git
cd hermes-atak

python scripts/install.py \
  --hermes-home ~/.hermes \
  --gateway-python ~/.hermes/hermes-agent/venv/bin/python
```

By default, the installer fetches the versioned frogcot and froggeolib releases
and the pinned pymavlink package used by the telemetry bridge.

### Live development checkouts

To develop all three repositories in parallel, install the library worktrees
in editable mode and link Hermes directly to this repository:

```bash
python scripts/install.py \
  --link \
  --hermes-home ~/.hermes \
  --gateway-python ~/.hermes/hermes-agent/venv/bin/python \
  --frogcot ~/frogcot \
  --froggeolib ~/froggeolib
```

In linked mode, this checkout is the sole source of truth. Edit files here,
not under `~/.hermes/plugins/atak` or
`~/.hermes/skills/productivity/atak`. Restart the gateway after plugin changes;
skill instruction changes are visible on the next skill load.

The installer can preview its file operations with `--dry-run`. It does not
modify Hermes configuration, credentials, certificates, or service state.

## Optional persistent MAVLink service

Set `mavlink_enabled: true` in the ATAK platform extras to connect to
the mavlink-router TCP listener at `tcp:127.0.0.1:5760`. The bridge never binds
the router's UDP `14550` input. It tracks heartbeat, fix, position, and publish
cadence independently for each message's actual source sysid, then sends stable
per-sysid CoT UIDs over the adapter's existing mutual-TLS connection.

The same long-lived routed connection backs `mavlink_uav`. Calls return a job
ID immediately. The controller rejects stale or ambiguous targets, addresses
commands to an explicit sysid/component, performs telemetry verification in
the background, and sends progress directly through the persistent CoT client.
Marker destinations and relative bearing/distance destinations use FrogCoT
situational state and FrogGeoLib WGS84 calculations.

The bridge requires both a fresh airborne-vehicle heartbeat and a fresh valid
3D-fix-backed non-`0,0` location. When either becomes stale, publishing stops
and the marker's CoT stale timestamp lets ATAK remove it naturally. The feature
is disabled when `mavlink_enabled` is missing or explicitly false. See
[references/configuration.md](references/configuration.md) for all defaults.

## Configure

You need:

- a working Hermes gateway;
- an OTS mutual-TLS client certificate and key for Hermes;
- the OTS CA and TLS server name;
- a stable Hermes ATAK UID and callsign;
- an explicitly authorized Hermes position;
- the ATAK device UIDs allowed to message Hermes; and
- an OTS Python environment with `yaml` and `psycopg` for retained-marker
  snapshots.

Follow [references/configuration.md](references/configuration.md) for the
complete Hermes configuration. Keep allowed-user values in `.env`:

```dotenv
ATAK_ALLOWED_USERS=YOUR_ATAK_DEVICE_UID
ATAK_ALLOW_ALL_USERS=false
```

Never commit certificates, private keys, database URIs, credentials, private
coordinates, pairing state, logs, or sessions.

## How marker visibility works

The CoT stream is forward-looking, so a newly connected client may not receive
markers that already exist in ATAK or OTS. Marker-related `atak_state` actions
merge:

1. live marker events retained from the persistent CoT stream; and
2. a short-lived, read-only snapshot of OTS's current marker and point records.

This lets Hermes see both newly received markers and OTS-retained markers
without using the database as a chat transport or adding a polling loop.

## Spatial requests

The plugin exposes neutral geospatial results rather than hardcoded language
rules. For a request such as “which marker is south of me?”, Hermes uses the
sender UID as the origin, obtains range/bearing vectors from froggeolib, and
interprets the requested direction in the main model.

Supported state operations include `status`, `receipts`, `contacts`, `markers`,
`relative_markers`, `nearest_marker`, and `range_bearing`. See
[references/operations.md](references/operations.md) for verification and
troubleshooting.

## Repository layout

- `SKILL.md` — operational instructions loaded by Hermes.
- `plugin/` — ATAK platform adapter and OTS snapshot helper.
- `plugin/mavlink_control.py` — the sole supported UAV command API.
- `scripts/install.py` — copy/link installer and dependency setup.
- `references/` — configuration, architecture, and operations details.

Read [references/architecture.md](references/architecture.md) before changing
identity, message routing, marker reconciliation, or spatial calculations.
