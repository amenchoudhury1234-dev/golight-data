# golight-data

Auto-refreshed UK aviation reference data for [GoLight](https://github.com/amenchoudhury1234-dev/golight): airports, official CAA/NATS Visual Reference Points, and small unlicensed/uncertificated airfields and heliports.

A GitHub Action re-runs `scripts/build-data.js` weekly (matching the ~28-day AIRAC cycle NATS/CAA publish on) and commits any changes, so the app's reference data updates without needing an app store release. The app fetches these files directly via `raw.githubusercontent.com` and caches them locally, falling back to a bundled snapshot if offline.

## Data files

- `data/uk-airports.json` — UK/IM/JE/GG airports with a valid ICAO code, from [OurAirports.com](https://ourairports.com/data/) (public domain).
- `data/uk-vrps.json` — Official UK Visual Reference Points, from NATS AIS digital datasets.
- `data/uk-airfields.json` — Small unlicensed/uncertificated airfields and heliports (e.g. farm strips), from NATS AIS.
- `data/meta.json` — When this was last generated, record counts, and the exact source URLs used.

## Licensing note

NATS AIS data (`uk-vrps.json`, `uk-airfields.json`) is published with "unrestricted access" but marked **"not for resale"** and **"for aviation use only."** This is fine for GoLight's aviation-reference use case, but the commercial terms for a paid subscription app haven't been confirmed with NATS yet — see GoLight's plan doc legal checklist. Contact: vfrcharts@nats.co.uk.

`uk-airports.json` (OurAirports.com) is public domain with no such restriction.

## Running manually

```bash
node scripts/build-data.js
```

Requires Node 22+ (built-in `fetch`) and `unzip` on PATH.
