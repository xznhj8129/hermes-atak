---
name: hermes-atak
description: Install, configure, operate, or diagnose Hermes ATAK/OpenTAKServer, including persistent MAVLink UAV control with live GeoChat feedback.
---

# Hermes ATAK

Treat Hermes as one virtual ATAK user. CoT messaging remains a transport and
translation layer. UAV control is exposed only through the plugin's persistent
`mavlink_uav` API. Never introduce a second chatbot, phrase router, polling
relay, per-command script, or alternate persona.

## Install

1. Read [references/configuration.md](references/configuration.md).
2. Run `python scripts/install.py` from this repository to copy the plugin and
   skill into the selected Hermes home. Supply `--gateway-python` to fetch the
   pinned frogcot/froggeolib releases, or also supply `--frogcot` and
   `--froggeolib` to install live checkouts in editable mode.
3. For local development, add `--link`. Treat this repository as the sole
   source of truth and never edit the linked paths under `~/.hermes`.
4. Add the configuration and authorization values described in the reference.
   Keep certificates, database URIs, user IDs, and coordinates out of the repo.
5. Restart the Hermes gateway once and verify one persistent mutual-TLS ATAK
   connection.

## Operate

- Let inbound `b-t-f` GeoChat create or resume a normal main-agent session.
- Use `send_message` with target `atak` for proactive cross-platform messages.
- Call `atak_state` for contacts, markers, receipts, nearest objects, relative
  markers, range, bearing, or elevation.
- Call `mavlink_uav` for UAV status and explicitly requested commands. A
  command returns a job immediately; background milestones go directly to the
  originating ATAK chat. Do not wait in an agent loop.
- Treat UAV identity as the actual MAVLink source sysid. Never infer a vehicle
  from the local GCS sysid.
- Interpret spatial language in the model. For “find a marker south of me,” use
  the sender UID as the origin, obtain neutral froggeolib vectors, interpret
  true bearings linguistically, and select the appropriate marker.
- Omit raw coordinates unless the user requests them.

Marker queries combine:

- point-bearing events received on Hermes's persistent CoT stream; and
- an on-demand snapshot of OpenTAKServer's current `markers` and `points`
  records.

This lets Hermes see OTS-retained markers that predate its connection without
turning the database into a chat transport or running a polling loop.

## Diagnose

Read [references/operations.md](references/operations.md) before changing the
transport. Treat delivery/read receipts as authoritative GeoChat evidence.
A pairing code means the transport worked but the sender UID was not
authorized. An empty live marker list after reconnect does not prove OTS has no
markers; run a marker-related `atak_state` action to reconcile server state.

Read [references/architecture.md](references/architecture.md) before changing
the adapter, identity, marker reconciliation, or spatial calculations.

## Maintain

- Edit the reusable source under `plugin/`, then install it into Hermes.
- In linked development mode, edit only this repository. Skill updates become
  visible on the next skill load; restart the gateway after plugin changes.
- Release and push frogcot/froggeolib version tags before publishing an
  integration release that fetches them.
- Keep `plugin/adapter.py` free of response generation and phrase-specific
  decision rules.
- Keep one routed pymavlink connection and address every control command to an
  explicitly discovered sysid/component. Never create per-command processes.
- Keep OTS database access read-only and on demand.
- Use stable Hermes and peer UIDs; generate unique event/message IDs.
- Preserve CA validation and the configured TLS server name.
- Validate changes against the live server and inspect the resulting receipts.
- Never commit `.env`, certificates, keys, credentials, database URIs, private
  locations, pairing state, logs, or sessions.

## Bundled Resources

- `plugin/`: Hermes ATAK platform plugin and OTS snapshot helper.
- `scripts/install.py`: deterministic file installer; it does not edit secrets
  or live configuration and supports copy or symlink mode.
- `references/configuration.md`: portable installation and configuration.
- `references/architecture.md`: data flow and design boundaries.
- `references/operations.md`: live verification and troubleshooting.
