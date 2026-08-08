# Budbee Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-budbee.svg)](https://github.com/ha-parcel-integrations/ha-budbee/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Budbee](https://budbee.com) parcels — home deliveries and Budbee Box lockers, in the Netherlands, Belgium, Sweden, Denmark, Finland and Norway. No account is needed, and no e-mail address either: you enter the tracking or order number and that is it.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Budbee parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text, the expected delivery window and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery windows
- `budbee.track_parcel` / `budbee.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Parcels you *send* back through Budbee get their own summary sensors and events
- A parcel in a Budbee Box reports `at_pickup_point`, and only counts as delivered once you have actually collected it
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.7 or newer
- A Budbee parcel and its tracking or order number, from the shipping
  confirmation or the tracking link the shop sent you — no account and no
  e-mail address needed

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-budbee` as an **Integration**.
3. Install **Budbee** and restart Home Assistant.

### Manual

Copy `custom_components/budbee` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Budbee**. There is nothing to fill in: the hub is created immediately (Budbee tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`budbee.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is the order number in your shipping confirmation, or the last part of the `track.budbee.com/…` link the shop sent you.

Budbee's own tracking page also asks for the e-mail address the order was placed with. This integration does not: that second factor only unmasks your own name, address and phone number in the response, which the integration has no use for.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Polling | Refresh every | 30 min | How often Budbee is checked. Slower is gentler on their API. |

There is no *parcel history* option here, unlike the other integrations in the family: Budbee publishes no event timeline, so every parcel's `history` attribute is `null`.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Budbee → ⋮ → Delete**. Nothing is stored on Budbee's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.budbee_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.budbee_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.budbee_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.budbee_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.budbee_outgoing_parcels` | Parcels you are sending back through Budbee |
| `sensor.budbee_delivered_outgoing_parcels` | Recently completed outgoing parcels |
| `sensor.budbee_last_successful_update` | Diagnostic: when Budbee was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Booked with Budbee, not collected from the shop yet |
| `in_transit` | Collected, in the network |
| `out_for_delivery` | With the courier today (home deliveries only) |
| `at_pickup_point` | Waiting for you in the Budbee Box |
| `delivered` | Handed over — for a locker parcel, collected by you |
| `returning` | On its way back to the shop |
| `problem` | A delivery attempt failed, or the parcel was not collected in time |
| `unknown` | Budbee does not know this code yet, or reported a status we have not mapped |

Budbee's own status is always available as `raw_status`.

**Budbee Box parcels are the reason this matters.** Budbee reports `Delivered`
the moment a parcel goes *into* the locker, which is not the same as you having
it. This integration maps that to `at_pickup_point`; `delivered` only fires once
Budbee reports `PickedUp`.

## Events

The integration fires these on the event bus (also available as device triggers on the Budbee device):

| Event | When |
|---|---|
| `budbee_parcel_registered` | A new parcel appears in the active list |
| `budbee_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `budbee_parcel_delivered` | A parcel is delivered |
| `budbee_parcel_delivery_time_changed` | The expected delivery window changes |
| `budbee_outgoing_parcel_status_changed` | A parcel you are sending changes status |
| `budbee_outgoing_parcel_delivered` | A parcel you sent reached its destination |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `budbee.track_parcel` | `tracking_code` | Start tracking a parcel |
| `budbee.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.budbee: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Budbee does not know the number yet (their API answers `ORDER_NOT_FOUND` until the shop hands the parcel over), or the number is wrong. It picks up automatically once Budbee registers it.
- **No expected delivery time on a locker parcel until it reaches a terminal** — a Budbee Box order only gets an ETA once it's on its final leg to the locker; before that, `planned_to` is empty. It never gets a *collection* deadline either — that's a different date (when you must pick it up) and stays under the raw attribute, not the delivery window.
- **No status history** — Budbee's tracking API returns no event list at all, so the `history` attribute is always empty.
- **A status logs "Unrecognised Budbee status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-budbee/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration uses the same public tracking endpoint as Budbee's own consumer tracking page, and reads only. It is not affiliated with, endorsed by, or supported by Budbee (Instabee Group). Be gentle with the polling interval.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
