# Working in this repository

Home Assistant custom integration for **Budbee** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific notes

**API mechanics live in `carrier-research/api/budbee/` (private research repo)** —
the two read routes, the envelope, both status vocabularies and the payload→
canonical mapping. Do not duplicate them here.

- **Two order types, two status maps, and that is the whole carrier.** A
  keyless `GET /v3/orders/{code}/meta` says `BOX` (locker) or `DELIVERY` (door);
  it picks the read route *and* the map. On the locker map Budbee's own
  `Delivered` means **in the box** → `at_pickup_point`, and `PickedUp` is the
  hand-over → `delivered`. Read the door map on a locker order and the
  integration announces a delivery while the parcel is still in a hatch. Never
  branch on `consignment.type`: that is *direction*, and it reads `DELIVERY` on
  locker orders too. `meta` is cached per code in the client (an order does not
  change type), so a steady poll is one request per parcel.
- **Two failure conventions.** The `/v3/*` routes report `ORDER_NOT_FOUND`
  inside an **HTTP 200** envelope; `/box/` answers a real **404**. `api.py`
  checks both, and either means "unknown code" → `None` → pending placeholder,
  never an error.
- **No credential, on purpose.** Budbee's tracking page asks for an e-mail as
  well; that only unmasks `consumer.*` / `consumerAddress.*` in the response —
  which is the HA user's own data and nothing a sensor reads. So there is no
  auth-error path, no token cache and no reauth flow here. If `receiver` is ever
  wanted, it is an *optional* extra field, never a required one.
- **`*****` is not a name.** The keyless read returns Budbee's mask literal for
  the recipient. `unmasked()` turns it into `None`; without it every parcel
  would report a receiver of `*****`.
- **Deliberate `None`s.** `weight` / `dimensions` (never exposed),
  `history` (**no** `events[]` on either route and every history sub-route
  404s — see below), `receiver` (masked, above), and `planned_from`/`planned_to`
  on locker orders: `latestPickupDate` is a *collection deadline*, not a
  delivery window, so it stays under `raw`. Same call `ha-inpost` makes for
  `expiryDate`.
- **No `include_history` option** — the only carrier in the suite without one.
  A toggle that can never produce anything is worse than no toggle, and
  accumulating a timeline locally from polls was rejected: it would differ per
  user depending on when they installed the integration.
- **Outgoing is unverified.** The direction split reads `consignment.type ∈
  {RETURN, ON_DEMAND_PICKUP}` and the `CollectedShippingLabel` status; neither
  has been seen on a live shipment. The coordinator logs a one-shot WARNING the
  first time it classifies anything as outgoing, per the pre-1.0 rule.
- **Door deliveries are reconstructed.** No `DELIVERY` order has been read on
  the wire. `api.py` warns (keys only — that payload carries an address and a
  door code) when the envelope has no `conspectus`, and an unknown `meta.type`
  warns and then tries both read routes rather than dropping the parcel.
- **Never surface `system`.** `meta.system` is an internal backend name and
  reads `INSTABOX` on a Dutch **Budbee** parcel. `brand` is the user-facing
  axis; the integration hard-codes `carrier: "Budbee"`.
- **Read-only, deliberately.** Budbee's API also exposes `PUT`/`POST` routes
  that change a real delivery (door codes, delivery windows, locker switching,
  ratings) and a STOMP feed. None of it is wired up, and none of it should be.
- **Pre-1.0 reporting is the point, not an afterthought.** One real parcel
  built this integration — an NL locker order that had not moved. So every
  assumption beyond it logs a one-shot WARNING with a prefilled issue link:
  unmapped status (per order type), a payload field we do not know, an order
  type outside `BOX`/`DELIVERY`, a `meta` we could not route on, a sibling
  brand, an ETA on a locker order, the first collected locker parcel (does
  `deliveredAt` mean drop or collection?), the first outgoing shipment, and —
  the valuable one — a **full `path: type` structure dump of any home
  delivery**, keyed on the shape so a richer payload reports again. Types only,
  never values. Locker orders deliberately get no dump: that payload is
  confirmed, and a 100-line WARNING for it would be noise. `_warn_once()` in
  `api.py` / `parcels.py` backs all of it; `tests/conftest.py` clears the sets
  between tests.
- **Redaction is stricter than elsewhere.** Besides the recipient, the payload
  carries locker coordinates and — on a door delivery — `identification.code`
  and `deliveryPinCode`. Those are physical access codes; a diagnostics file
  with one in it is a key posted in public.

## Options and reloads

The options flow is one sectioned form (`data_entry_flow.section`); changes apply
without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default) apply changes live: an update listener
  retunes `coordinator.update_interval` and calls `async_request_refresh()`, so
  added/removed parcel sensors appear immediately.
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+.

The user-tunable poll interval is a deliberate HACS divergence (see
CONVENTIONS.md); a carrier that throttles is generated with a fixed cadence and no
polling option at all.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (HTTP client, error types) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (URLs) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (`_STATUS_MAP`, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` | partly (code validation) |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (`track_parcel` / `untrack_parcel`, account-less only) | no |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Tests on Windows

`tests/conftest.py` carries two Windows-only shims (no-ops elsewhere):
`disable_socket` is neutralised (Windows event loops need AF_INET socketpairs;
the 127.0.0.1 allowlist stays) and HA's `AsyncResolver` is swapped for
`ThreadedResolver` (aiodns refuses the Proactor loop). Do not remove them
"because CI passes" — CI is Linux, development is Windows.

## Running tests

```
python -m pytest tests/ --cov=custom_components.budbee
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in this carrier's directory under the private
`carrier-research/api/`, never in this repo.
