# Site onboarding form

One form per site. Fill it with the operator's power or facilities lead, ideally
at the site or on a video call with someone standing at the equipment.
Photographs of each nameplate (generator, battery, inverter, tank) save
arguments later.

Every answer maps to one line of the site's `[[site]]` block in the
portfolio file. After filling it in, run
`uv run python -m vaticore.pipeline site check --portfolio portfolio.toml` before
uploading the file. The check catches swapped coordinates, prices per drum,
missing tank sizes and more.

## The site

| Question | Example | Goes to |
|---|---|---|
| Operator's short name (lowercase, dashes) | `example-towerco` | `operator_id` |
| The operator's own ID for the site | `lag-ikd-0142` | `site_id` |
| A name the site team recognises | `Macro tower, Ikorodu` | `name` |
| Kind of site | telecom tower, bank branch, C&I, institution, mini-grid | `site_type` (`telecom_tower`, `bank_branch`, `commercial_industrial`, `institution`, `mini_grid`) |
| Position (from Google Maps: long-press the site, copy the two numbers) | `6.6194, 3.5105` | `latitude`, `longitude` (latitude first; Nigeria is about 4 to 14 north, 3 to 15 east) |
| What an hour without power costs this site, in naira, divided by its usual load in kW | `5000` | `value_of_lost_load_per_kwh` |

On the outage cost: ask "If this site lost power for an hour, what would it
cost you?" (lost revenue, SLA penalties, spoiled stock). Divide by the usual
load in kW. Towers with SLA penalties are often ₦3,000 to ₦10,000 per kWh;
write down how the number was reached.

## Battery

| Question | Example | Goes to |
|---|---|---|
| Usable capacity in kWh (nameplate kWh times the depth the operator allows) | `30` | `battery.usable_kwh` |
| Most the inverter or battery can deliver, in kW | `15` | `battery.power_kw` |
| The lowest charge the operator lets it reach, in kWh | `9` | `battery.min_soc_kwh` |
| Chemistry | lead-acid, lithium (LFP) | `battery.chemistry` |

## Generator

| Question | Example | Goes to |
|---|---|---|
| Rated output in kW (nameplate kVA times 0.8 if only kVA is shown) | `16` | `generator.rated_kw` |
| Shortest run once started, in hours (operator's rule) | `2` | `generator.min_run_hours` |
| Can the battery charger load the generator while it runs ("charge at full rate" on the inverter or hybrid controller)? If yes, the generator runs hard, then off, and burns less | yes: `0.8` | `generator.charge_setpoint` |
| Diesel price per litre, delivered to site, in naira | `1250` | `generator.fuel_price_per_l` |
| Tank size in litres | `500` | `generator.tank_l` |
| Does a sensor report the tank level? | yes / no | readings: `fuel_level_l` |

## Grid

| Question | Example | Goes to |
|---|---|---|
| Is there a grid connection? | yes / no | the `[site.grid]` block, or none |
| Connection size in kW | `15` | `grid.capacity_kw` |
| Price per kWh, in naira | `225` | `grid.price_per_kwh` |
| Does the grid charge the battery? | yes | `grid.charges_battery` |
| Is it effectively always on? (almost never true in Nigeria) | no | `grid.reliable` |
| NERC tariff band, from the electricity bill | `Band B` | `grid.tariff_band` |

## Solar (if any)

| Question | Example | Goes to |
|---|---|---|
| Array size in kWp | `12` | `solar.kwp` |

## Data

| Question | Example | Where it goes |
|---|---|---|
| Monitoring platform | Victron VRM, Huawei FusionSolar, a logger exporting CSV | the sources file (docs/data-connectors.md) |
| Login or API access for Vaticore (read-only) | an invited read-only user | the sources file and a secret (never in email) |
| Which of these does it record: load, solar, grid on/off, battery charge, generator output, tank level? | load, grid, battery, generator | decides what plans and the pilot report can use |
| How often does it record? | every 15 minutes | readings are averaged into hours |
| How much history is available? | since March 2025 | at least two weeks; eight is better |

Generator output (kW) matters most for the pilot: without it, the pilot
report cannot measure the site's diesel.

## People

For each person who will receive plans (technician, site manager) or the
weekly summary (supervisor):

| Question | Example | Goes to (recipients file) |
|---|---|---|
| Name and role | `Site manager, Ikorodu` | `name` |
| WhatsApp number | `+234 803 000 0000` | `whatsapp` |
| Email (optional) | `ops@example.com` | `email` |
| Which sites | `lag-ikd-0142` or all | `sites` |
| Language: English or Pidgin | Pidgin | `language` (`en` or `pcm`) |
| Weekly summary? | supervisors only | `weekly_summary` |
| Consent: when and how they agreed | "WhatsApp YES, 12 Oct 2026" | `consent = true`, `consent_note` |

Never put anyone in the file before they have agreed (consent script:
docs/pilot/privacy-and-consent.md).

## The filled-in block

```toml
[[site]]
operator_id = "example-towerco"
site_id = "lag-ikd-0142"
name = "Macro tower, Ikorodu"
site_type = "telecom_tower"
latitude = 6.6194
longitude = 3.5105
timezone = "Africa/Lagos"
currency = "NGN"
value_of_lost_load_per_kwh = 5000.0

[site.battery]
usable_kwh = 30.0
power_kw = 15.0
min_soc_kwh = 9.0
chemistry = "lead-acid"

[site.generator]
rated_kw = 16.0
min_run_hours = 2
fuel_price_per_l = 1250.0
tank_l = 500.0

[site.grid]
capacity_kw = 15.0
price_per_kwh = 225.0
tariff_band = "Band B"
```
