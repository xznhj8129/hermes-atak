# Configuration

## Prerequisites

- A working Hermes gateway checkout and Python environment.
- OpenTAKServer with a mutual-TLS client certificate for Hermes.
- An explicitly authorized Hermes position. Do not invent a location merely to
  make the ATAK contact appear.

## Install Files

For a deployed copy:

```bash
python scripts/install.py \
  --hermes-home ~/.hermes \
  --gateway-python /path/to/hermes/python
```

For a local source-of-truth checkout, use symlinks:

```bash
python scripts/install.py \
  --link \
  --hermes-home ~/.hermes \
  --gateway-python /path/to/hermes/python \
  --frogcot /path/to/live/frogcot \
  --froggeolib /path/to/live/froggeolib
```

This makes the repository authoritative for both the platform plugin and
Hermes skill. Do not edit `~/.hermes/plugins/atak` or
`~/.hermes/skills/productivity/atak`; those paths point back here.

When checkout paths are omitted, the installer fetches the pinned `frogcot`
1.2.0 and `froggeolib` 1.1.0 Git tags. It also installs the pinned
`pymavlink` dependency. To develop the libraries in parallel, provide the live
worktrees:

```bash
python scripts/install.py \
  --hermes-home ~/.hermes \
  --gateway-python /path/to/hermes/python \
  --frogcot /path/to/live/frogcot \
  --froggeolib /path/to/live/froggeolib
```

The editable installs preserve independent repositories and immediately expose
library changes to Hermes. The installer does not edit `.env`, `config.yaml`,
credentials, certificates, or service state.

The OTS snapshot helper runs under the configured OpenTAKServer Python
environment, which must provide `yaml` and `psycopg`.

## Hermes Configuration

Merge these values into `config.yaml`; preserve unrelated settings:

```yaml
plugins:
  enabled:
    - atak-platform

platform_toolsets:
  telegram:
    - atak
    - hermes-telegram
  atak:
    - atak
    - hermes-atak

gateway:
  platforms:
    atak:
      enabled: true
      home_channel:
        platform: atak
        chat_id: YOUR_ATAK_DEVICE_UID
        name: YOUR_ATAK_CALLSIGN
      extra:
        host: 127.0.0.1
        port: 8089
        server_hostname: OTS_CERTIFICATE_DNS_NAME
        ca: /path/to/ots/ca.pem
        client_certificate: /path/to/hermes.pem
        client_key: /path/to/hermes.nopass.key
        callsign: Hermes
        uid: STABLE-HERMES-ATAK-UID
        ots_python: /path/to/opentakserver/python
        ots_config: /path/to/ots/config.yml
        ots_snapshot_ttl: 2.0
        mavlink_enabled: true
        mavlink_endpoint: tcp:127.0.0.1:5760
        mavlink_publish_cadence: 1.0
        mavlink_freshness: 5.0
        mavlink_stale: 10.0
        mavlink_control_timeout: 60.0
        mavlink_arrival_radius: 10.0
        mavlink_callsign_prefix: UAV
        mavlink_callsigns:
          "1": Survey-UAV
        mavlink_cot_type: a-f-A-M-F-Q
        position:
          lat: AUTHORIZED_LATITUDE
          lon: AUTHORIZED_LONGITUDE
          alt: AUTHORIZED_ALTITUDE
          ce: POSITION_CIRCULAR_ERROR
          le: POSITION_LINEAR_ERROR
```

Keep the following in `.env`, not in the repository:

```dotenv
ATAK_ALLOWED_USERS=YOUR_ATAK_DEVICE_UID
ATAK_ALLOW_ALL_USERS=false
```

### MAVLink telemetry keys

`mavlink_enabled` is opt-in: a missing value or explicit `false` disables the
bridge. When enabled, these defaults apply:

| Key | Default | Meaning |
| --- | --- | --- |
| `mavlink_endpoint` | `tcp:127.0.0.1:5760` | mavlink-router TCP listener to connect to. UDP and listening endpoints are rejected. |
| `mavlink_publish_cadence` | `1.0` seconds | Minimum interval between CoT updates, independently per sysid. |
| `mavlink_freshness` | `5.0` seconds | Maximum age of heartbeat, valid fix, and position required to publish. |
| `mavlink_stale` | `10.0` seconds | CoT lifetime after each update; values below cadence are raised to cadence. |
| `mavlink_callsign_prefix` | `UAV` | Default callsign prefix, producing `UAV-1`, `UAV-2`, and so on. |
| `mavlink_callsigns` | `{}` | Optional string-keyed sysid-to-callsign overrides. |
| `mavlink_cot_type` | `a-f-A-M-F-Q` | Friendly air UAV CoT type; override for a different known airframe symbol. |
| `mavlink_reconnect_initial` | `1.0` seconds | Initial router reconnect delay. |
| `mavlink_reconnect_max` | `30.0` seconds | Maximum router reconnect delay. |
| `mavlink_control_timeout` | `60.0` seconds | Default telemetry-transition verification timeout. |
| `mavlink_arrival_radius` | `10.0` metres | Radius within which a goto job is verified as arrived. |

The marker UID is derived as `<stable Hermes uid>-uav-<source sysid>`. Changing
the Hermes UID therefore changes UAV marker identities. The configured
callsign map changes labels only, not identities.

For Telegram control, enable the `atak` toolset as shown above and set the
Telegram home channel through the normal Hermes `/sethome` flow.

`mavlink_uav` is registered in the `atak` toolset. A call originating in ATAK
captures that conversation automatically for asynchronous job milestones.

## Activate

Restart the gateway once after installing or changing plugin code. Confirm:

- exactly one Hermes connection to the TLS CoT port;
- ATAK and Telegram adapters are connected;
- the sender UID is authorized without a pairing prompt; and
- a direct ATAK message enters a normal Hermes main-agent session.
