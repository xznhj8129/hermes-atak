---
name: hermes-atak
description: Install, configure, operate, or diagnose the passive Hermes ATAK/OpenTAKServer integration. Use when connecting the normal Hermes main agent to ATAK GeoChat, exposing live and server-retained CoT markers, enabling froggeolib spatial reasoning, resolving ATAK pairing/authorization, or packaging and updating the local ATAK platform plugin.
---

# Hermes ATAK

Treat Hermes as one virtual ATAK user. Keep the platform plugin passive: receive
CoT, translate GeoChat into the normal Hermes gateway message flow, retain
situational state, and send the main agent's final reply. Never introduce a
second chatbot, phrase router, polling relay, or alternate persona.

## Install

1. Read [references/configuration.md](references/configuration.md).
2. Run `python scripts/install.py` from this repository to copy the plugin and
   skill into the selected Hermes home. Supply `--gateway-python` to fetch the
   pinned frogcot/froggeolib releases, or also supply `--frogcot` and
   `--froggeolib` to install live checkouts in editable mode.
4. Add the configuration and authorization values described in the reference.
   Keep certificates, database URIs, user IDs, and coordinates out of the repo.
5. Restart the Hermes gateway once and verify one persistent mutual-TLS ATAK
   connection.

## Operate

- Let inbound `b-t-f` GeoChat create or resume a normal main-agent session.
- Use `send_message` with target `atak` for proactive cross-platform messages.
- Call `atak_state` for contacts, markers, receipts, nearest objects, relative
  markers, range, bearing, or elevation.
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
- Release and push frogcot/froggeolib version tags before publishing an
  integration release that fetches them.
- Keep `plugin/adapter.py` free of response generation and phrase-specific
  decision rules.
- Keep OTS database access read-only and on demand.
- Use stable Hermes and peer UIDs; generate unique event/message IDs.
- Preserve CA validation and the configured TLS server name.
- Validate changes against the live server and inspect the resulting receipts.
- Never commit `.env`, certificates, keys, credentials, database URIs, private
  locations, pairing state, logs, or sessions.

## Bundled Resources

- `plugin/`: Hermes ATAK platform plugin and OTS snapshot helper.
- `scripts/install.py`: deterministic file installer; it does not edit secrets
  or live configuration.
- `references/configuration.md`: portable installation and configuration.
- `references/architecture.md`: data flow and design boundaries.
- `references/operations.md`: live verification and troubleshooting.
