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
1.2.0 and `froggeolib` 1.1.0 Git tags. To develop the libraries in parallel,
provide the live worktrees:

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

For Telegram control, enable the `atak` toolset as shown above and set the
Telegram home channel through the normal Hermes `/sethome` flow.

## Activate

Restart the gateway once after installing or changing plugin code. Confirm:

- exactly one Hermes connection to the TLS CoT port;
- ATAK and Telegram adapters are connected;
- the sender UID is authorized without a pairing prompt; and
- a direct ATAK message enters a normal Hermes main-agent session.
