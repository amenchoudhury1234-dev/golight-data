// Builds aerodrome detail for Minima - runways (true bearing, length, width,
// surface), field elevation and radio frequencies - from
// the two OFFICIAL sources, both published on the 28-day AIRAC cycle. Wind
// components only need TRUE bearings, so magnetic variation isn't carried
// (the UK and US files use opposite sign conventions for it):
//  - UK: NATS "UK ICAO AIP Dataset" (AIXM 5.1 XML), nats-uk.ead-it.com
//    digital datasets. Same "unrestricted access, not for resale" terms as
//    the NATS VRP data already used - see README.
//  - US: FAA NASR 28-day subscription CSVs (APT_BASE, APT_RWY, APT_RWY_END,
//    FRQ), nfdc.faa.gov - US Government work, public domain.
//
// Output:
//  - data/uk-aerodromes.json (bundled in the app as the offline fallback)
//  - data/us-aerodromes.json (fetched at runtime, like the other US data)
// Each: [{ icao ('' for strips with no ICAO code), name, lat, lon, elevationFt,
//   source: 'official' | 'community', runways: [{ id, lengthM, widthM,
//   surface, ends: [{ id, trueBearing }] }], frequencies: [{ type, callSign, mhz }] }]
//
// Run standalone (node scripts/build-aerodromes.js) or from build-data.js.

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const DATA_DIR = path.join(__dirname, '..', 'data');
const TMP_DIR = path.join(__dirname, '..', '.tmp-aerodromes');
const NATS_DATASETS = 'https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/';
const NASR_BASE = 'https://nfdc.faa.gov/webContent/28DaySub/extra';

async function fetchOk(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Fetch failed ${res.status}: ${url}`);
  return res;
}

const round = (n, dp = 0) => (Number.isFinite(n) ? Math.round(n * 10 ** dp) / 10 ** dp : null);

// ---------------------------------------------------------------------------
// UK - NATS AIXM
// ---------------------------------------------------------------------------

// The AIP dataset is published per AIRAC date; use the latest one already in
// effect (the page also lists the NEXT cycle ahead of time).
function latestEffective(html, re) {
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const all = [...html.matchAll(re)].map((m) => ({ url: m[1], date: m[2] })).sort((a, b) => a.date.localeCompare(b.date));
  const eligible = all.filter((c) => c.date <= today);
  return (eligible.length ? eligible[eligible.length - 1] : all[0])?.url ?? null;
}

const blocks = (xml, tag) => xml.match(new RegExp(`<aixm:${tag} gml:id[\\s\\S]*?</aixm:${tag}>`, 'g')) ?? [];
const field = (b, name) => {
  const m = b.match(new RegExp(`<aixm:${name}(?: [^>]*)?>([^<]*)<`));
  return m ? m[1].trim() : null;
};
const href = (b, name) => {
  const m = b.match(new RegExp(`<aixm:${name}[^>]*href="urn:uuid:([^"]+)"`));
  return m ? m[1] : null;
};
const hrefs = (b, name) => [...b.matchAll(new RegExp(`<aixm:${name}[^>]*href="urn:uuid:([^"]+)"`, 'g'))].map((m) => m[1]);
const uuid = (b) => b.match(/<gml:identifier[^>]*>([^<]+)</)?.[1].trim();

const EMERGENCY = (mhz) => Math.abs(Number(mhz) - 121.5) < 0.001;

// Plain-English frequency labels. Ground/Delivery are TWR-type services in the
// AIXM, told apart only by call sign; A/G radio has no dedicated type.
function freqType(serviceType, callSign) {
  const cs = (callSign || '').toUpperCase();
  if (cs.includes('GROUND')) return 'Ground';
  if (cs.includes('DELIVERY')) return 'Delivery';
  if (cs.includes('RADIO')) return 'A/G Radio';
  switch (serviceType) {
    case 'TWR':
      return 'Tower';
    case 'APP':
      return 'Approach';
    case 'ATIS':
      return 'ATIS';
    case 'AFIS':
      return 'Information (AFIS)';
    case 'OTHER:RADAR':
      return 'Radar';
    default:
      return null;
  }
}

async function buildUk() {
  const html = await (await fetchOk(NATS_DATASETS)).text();
  const zipUrl = latestEffective(
    html,
    /href="(\/cms-nats\/export\/sites\/default\/en\/Publications\/digital-datasets\/ICAO_AIP\/EG_AIP_DS_(\d{8})_XML\.zip)"/g,
  );
  if (!zipUrl) throw new Error('Could not find the UK ICAO AIP dataset on the NATS page');
  fs.mkdirSync(TMP_DIR, { recursive: true });
  const zipPath = path.join(TMP_DIR, 'aip.zip');
  fs.writeFileSync(zipPath, Buffer.from(await (await fetchOk(`https://nats-uk.ead-it.com${zipUrl}`)).arrayBuffer()));
  execSync(`unzip -o -q "${zipPath}" -d "${TMP_DIR}/aip"`);
  const xmlName = fs.readdirSync(path.join(TMP_DIR, 'aip')).find((f) => /FULL.*\.xml$/.test(f));
  const xml = fs.readFileSync(path.join(TMP_DIR, 'aip', xmlName), 'utf8');

  const airports = new Map();
  for (const b of blocks(xml, 'AirportHeliport')) {
    const icao = field(b, 'locationIndicatorICAO');
    if (!icao || !/^E[GI][A-Z]{2}$|^EG[A-Z]{2}$/.test(icao)) continue;
    const pos = b.match(/<aixm:ARP>[\s\S]*?<gml:pos[^>]*>\s*([-\d.]+)\s+([-\d.]+)\s*</);
    airports.set(uuid(b), {
      icao,
      name: field(b, 'name'),
      lat: pos ? round(Number(pos[1]), 5) : null,
      lon: pos ? round(Number(pos[2]), 5) : null,
      elevationFt: round(Number(field(b, 'fieldElevation'))),
      source: 'official',
      runways: [],
      frequencies: [],
    });
  }

  const runways = new Map();
  for (const b of blocks(xml, 'Runway')) {
    if (field(b, 'type') !== 'RWY') continue;
    const ap = airports.get(href(b, 'associatedAirportHeliport'));
    if (!ap) continue;
    const rwy = {
      id: field(b, 'designator'),
      lengthM: round(Number(field(b, 'nominalLength'))),
      widthM: round(Number(field(b, 'nominalWidth'))),
      surface: field(b, 'composition'),
      ends: [],
    };
    runways.set(uuid(b), rwy);
    ap.runways.push(rwy);
  }
  for (const b of blocks(xml, 'RunwayDirection')) {
    const rwy = runways.get(href(b, 'usedRunway'));
    // A missing bearing must be SKIPPED, not read as 0 deg: Number('') is 0,
    // which gave EGBM/EGNL runways a bearing of 000 and nonsense components.
    const raw = field(b, 'trueBearing');
    const bearing = raw ? Number(raw) : NaN;
    if (!rwy || !Number.isFinite(bearing)) continue;
    rwy.ends.push({ id: field(b, 'designator'), trueBearing: round(bearing, 1) });
  }

  const channels = new Map();
  for (const b of blocks(xml, 'RadioCommunicationChannel')) {
    const mhz = field(b, 'frequencyTransmission');
    if (mhz) channels.set(uuid(b), mhz);
  }
  for (const tag of ['AirTrafficControlService', 'InformationService']) {
    for (const b of blocks(xml, tag)) {
      const callSign = field(b, 'callSign');
      const type = freqType(field(b, 'type'), callSign);
      if (!type) continue;
      const mhz = [...new Set(hrefs(b, 'radioCommunication').map((u) => channels.get(u)).filter(Boolean))]
        // VHF voice only - UHF military frequencies aren't usable by GA radios.
        .filter((f) => Number(f) >= 118 && Number(f) < 137 && !EMERGENCY(f));
      if (mhz.length === 0) continue;
      for (const apId of hrefs(b, 'clientAirport')) {
        const ap = airports.get(apId);
        if (!ap) continue;
        if (!ap.frequencies.some((f) => f.type === type && f.mhz.join() === mhz.join())) {
          ap.frequencies.push({ type, callSign, mhz });
        }
      }
    }
  }

  const out = [...airports.values()]
    .filter((a) => a.runways.length > 0)
    .map((a) => ({ ...a, runways: a.runways.filter((r) => r.ends.length > 0) }))
    .sort((a, b) => a.icao.localeCompare(b.icao));
  return { aerodromes: out, sourceUrl: `https://nats-uk.ead-it.com${zipUrl}` };
}

// ---------------------------------------------------------------------------
// US - FAA NASR
// ---------------------------------------------------------------------------

// NASR cycles are the AIRAC dates; walk back from today in 28-day steps from a
// known cycle start until a published file is found.
async function findNasrCycle() {
  const KNOWN = Date.UTC(2026, 9, 1); // 01 Oct 2026 AIRAC
  const day = 86400000;
  const now = Date.now();
  let start = KNOWN + Math.floor((now - KNOWN) / (28 * day)) * 28 * day;
  for (let i = 0; i < 3; i++, start -= 28 * day) {
    const d = new Date(start);
    const label = `${String(d.getUTCDate()).padStart(2, '0')}_${d.toLocaleString('en-GB', { month: 'short', timeZone: 'UTC' })}_${d.getUTCFullYear()}`;
    // nfdc.faa.gov refuses HEAD; a 1-byte ranged GET tells us the file exists.
    const res = await fetch(`${NASR_BASE}/${label}_APT_CSV.zip`, { headers: { Range: 'bytes=0-0' } });
    if (res.ok) return label;
  }
  throw new Error('No current FAA NASR cycle found');
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let cur = '';
  let q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          cur += '"';
          i++;
        } else q = false;
      } else cur += c;
    } else if (c === '"') q = true;
    else if (c === ',') {
      row.push(cur);
      cur = '';
    } else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cur);
      rows.push(row);
      row = [];
      cur = '';
    } else cur += c;
  }
  if (cur || row.length) {
    row.push(cur);
    rows.push(row);
  }
  const [head, ...body] = rows.filter((r) => r.length > 1);
  return body.map((r) => Object.fromEntries(head.map((h, i) => [h, r[i]])));
}

const US_FREQ_TYPES = [
  [/^D?-?ATIS/, 'ATIS'],
  [/^ASOS|^AWOS/, 'ASOS/AWOS'],
  [/^LCL/, 'Tower'],
  [/^GND/, 'Ground'],
  [/^CD/, 'Clearance'],
  [/^CTAF/, 'CTAF'],
  [/^UNICOM/, 'UNICOM'],
  [/^APCH/, 'Approach'],
  [/^DEP/, 'Departure'],
];

async function buildUs() {
  const cycle = await findNasrCycle();
  fs.mkdirSync(path.join(TMP_DIR, 'nasr'), { recursive: true });
  for (const part of ['APT', 'FRQ']) {
    const zip = path.join(TMP_DIR, `${part}.zip`);
    fs.writeFileSync(zip, Buffer.from(await (await fetchOk(`${NASR_BASE}/${cycle}_${part}_CSV.zip`)).arrayBuffer()));
    execSync(`unzip -o -q "${zip}" -d "${TMP_DIR}/nasr"`);
  }
  const read = (f) => parseCsv(fs.readFileSync(path.join(TMP_DIR, 'nasr', f), 'utf8'));

  const bySite = new Map();
  const byFaaId = new Map();
  for (const r of read('APT_BASE.csv')) {
    // Open, public-use airports with an ICAO code - the same set the app
    // already lists as US airports.
    if (!/^K[A-Z0-9]{3}$|^P[A-Z]{3}$/.test(r.ICAO_ID || '') || r.ARPT_STATUS !== 'O' || r.SITE_TYPE_CODE !== 'A') continue;
    const ap = {
      icao: r.ICAO_ID,
      name: r.ARPT_NAME,
      lat: round(Number(r.LAT_DECIMAL), 5),
      lon: round(Number(r.LONG_DECIMAL), 5),
      elevationFt: round(Number(r.ELEV)),
      source: 'official',
      runways: [],
      frequencies: [],
    };
    bySite.set(r.SITE_NO, ap);
    byFaaId.set(r.ARPT_ID, ap);
  }
  const rwyKey = (r) => `${r.SITE_NO}|${r.RWY_ID}`;
  const rwys = new Map();
  for (const r of read('APT_RWY.csv')) {
    const ap = bySite.get(r.SITE_NO);
    // Helipads and water lanes aren't runways for this purpose.
    if (!ap || /^H/.test(r.RWY_ID) || /W$/.test(r.RWY_ID)) continue;
    const rwy = {
      id: r.RWY_ID,
      lengthM: round(Number(r.RWY_LEN) * 0.3048),
      widthM: round(Number(r.RWY_WIDTH) * 0.3048),
      surface: r.SURFACE_TYPE_CODE || null,
      ends: [],
    };
    rwys.set(rwyKey(r), rwy);
    ap.runways.push(rwy);
  }
  for (const r of read('APT_RWY_END.csv')) {
    const rwy = rwys.get(rwyKey(r));
    const bearing = r.TRUE_ALIGNMENT === '' ? NaN : Number(r.TRUE_ALIGNMENT);
    if (!rwy || !Number.isFinite(bearing)) continue;
    rwy.ends.push({ id: r.RWY_END_ID, trueBearing: bearing });
  }
  for (const r of read('FRQ.csv')) {
    const ap = byFaaId.get(r.SERVICED_FACILITY);
    if (!ap) continue;
    const use = (r.FREQ_USE || '').toUpperCase();
    const type = US_FREQ_TYPES.find(([re]) => re.test(use))?.[1];
    const mhz = Number(r.FREQ);
    if (!type || !(mhz >= 118 && mhz < 137) || EMERGENCY(mhz)) continue;
    const callSign = r.TOWER_OR_COMM_CALL || r.PRIMARY_APPROACH_RADIO_CALL || null;
    const existing = ap.frequencies.find((f) => f.type === type);
    if (existing) {
      if (!existing.mhz.includes(r.FREQ)) existing.mhz.push(r.FREQ);
    } else ap.frequencies.push({ type, callSign, mhz: [r.FREQ] });
  }
  const out = [...bySite.values()]
    .map((a) => ({ ...a, runways: a.runways.filter((r) => r.ends.length > 0) }))
    .filter((a) => a.runways.length > 0)
    .sort((a, b) => a.icao.localeCompare(b.icao));
  return { aerodromes: out, sourceUrl: `${NASR_BASE}/${cycle}_APT_CSV.zip` };
}

// ---------------------------------------------------------------------------
// Community fallback - OurAirports (public domain)
// ---------------------------------------------------------------------------
// The official datasets cover licensed aerodromes (UK) and public-use ICAO
// airports (US). Smaller fields - farm strips, private and unlicensed
// airfields - only exist in community data. They're added ONLY where no
// official entry exists (by ident, or an official aerodrome within ~1 NM),
// and marked source: 'community' so the app labels them "community data -
// check the official source". Official data always wins.

const OA_BASE = 'https://davidmegginson.github.io/ourairports-data';
const OA_TYPES = new Set(['small_airport', 'medium_airport', 'large_airport']);
const OA_FREQ_TYPES = {
  TWR: 'Tower',
  GND: 'Ground',
  ATIS: 'ATIS',
  CTAF: 'CTAF',
  UNIC: 'UNICOM',
  'A/G': 'A/G Radio',
  AFIS: 'Information (AFIS)',
  APP: 'Approach',
  RDO: 'A/G Radio',
  INFO: 'Information (AFIS)',
  'A/D': 'A/G Radio',
};

function oaSurface(s) {
  const u = (s || '').toUpperCase();
  if (/^ASP|ASPH|BIT|TAR/.test(u)) return 'ASPH';
  if (/^CON|CONC|PEM/.test(u)) return 'CONC';
  if (/GRS|GRASS|TURF/.test(u)) return 'GRASS';
  if (/GRV|GRAVEL/.test(u)) return 'GRVL';
  if (/DIRT|SOIL|SAND|CLAY/.test(u)) return 'DIRT';
  return u || null;
}

function distanceNmLL(a, b) {
  const k = Math.cos((((a.lat + b.lat) / 2) * Math.PI) / 180);
  return Math.hypot((a.lat - b.lat) * 60, (a.lon - b.lon) * 60 * k);
}

// True bearing from one runway end to the other (great-circle initial bearing).
function bearingBetween(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180;
  const y = Math.sin((lon2 - lon1) * r) * Math.cos(lat2 * r);
  const x = Math.cos(lat1 * r) * Math.sin(lat2 * r) - Math.sin(lat1 * r) * Math.cos(lat2 * r) * Math.cos((lon2 - lon1) * r);
  return ((Math.atan2(y, x) / r) + 360) % 360;
}

// A runway's ends with TRUE bearings, best source first:
//  1. the published true heading;
//  2. computed from the two runway-end coordinates (exact);
//  3. UK only: the runway number x10 (magnetic). The number is the magnetic
//     heading rounded to 10 deg (up to 5 deg off) and UK variation is ~0-2
//     deg, so this is within ~8 deg of true - marked uncertaintyDeg: 8 and the
//     app assumes the WORST crosswind inside that range. Not used for the US,
//     where variation reaches ~16 deg.
function runwayEnds(r, ukStyle) {
  const le = r.le_heading_degT === '' ? NaN : Number(r.le_heading_degT);
  const he = r.he_heading_degT === '' ? NaN : Number(r.he_heading_degT);
  const ends = [];
  // A "true heading" that is exactly the runway number x10 is almost always
  // the MAGNETIC number copied in, not a surveyed true bearing - treat it as
  // approximate: 5 deg of rounding plus variation (UK ~2, US up to ~16).
  const copied = (id, h) => {
    const m = (id || '').match(/^(\d{1,2})[LRCG]?$/);
    return m ? Math.abs(Number(m[1]) * 10 - h) < 0.5 || Math.abs(Number(m[1]) * 10 - h - 360) < 0.5 : false;
  };
  const approx = ukStyle ? 8 : 20;
  for (const [id, h] of [[r.le_ident, le], [r.he_ident, he]]) {
    if (!id || !Number.isFinite(h)) continue;
    ends.push(copied(id, h) ? { id, trueBearing: round(h, 1), uncertaintyDeg: approx } : { id, trueBearing: round(h, 1) });
  }
  if (ends.length) return ends;
  const c = [r.le_latitude_deg, r.le_longitude_deg, r.he_latitude_deg, r.he_longitude_deg].map((v) => (v === '' ? NaN : Number(v)));
  if (c.every(Number.isFinite) && (c[0] !== c[2] || c[1] !== c[3])) {
    const b = bearingBetween(c[0], c[1], c[2], c[3]);
    if (r.le_ident) ends.push({ id: r.le_ident, trueBearing: round(b, 1) });
    if (r.he_ident) ends.push({ id: r.he_ident, trueBearing: round((b + 180) % 360, 1) });
    return ends;
  }
  if (!ukStyle) return [];
  for (const id of [r.le_ident, r.he_ident]) {
    const m = (id || '').match(/^(\d{1,2})[LRCG]?$/);
    if (m && Number(m[1]) >= 1 && Number(m[1]) <= 36) ends.push({ id, trueBearing: Number(m[1]) * 10, uncertaintyDeg: 8 });
  }
  return ends;
}

async function buildCommunity(official, countries) {
  const ukStyle = countries.includes('GB');
  const [airportsCsv, runwaysCsv, freqCsv] = await Promise.all(
    ['airports.csv', 'runways.csv', 'airport-frequencies.csv'].map(async (f) => (await fetchOk(`${OA_BASE}/${f}`)).text()),
  );
  const officialIds = new Set(official.map((a) => a.icao));
  const officialPts = official.filter((a) => a.lat != null && a.lon != null);
  const out = new Map();
  for (const a of parseCsv(airportsCsv)) {
    if (!countries.includes(a.iso_country) || !OA_TYPES.has(a.type)) continue;
    const ids = [a.icao_code, a.gps_code, a.ident].filter(Boolean);
    if (ids.some((id) => officialIds.has(id))) continue;
    const pt = { lat: Number(a.latitude_deg), lon: Number(a.longitude_deg) };
    if (!Number.isFinite(pt.lat) || !Number.isFinite(pt.lon)) continue;
    if (officialPts.some((o) => distanceNmLL(o, pt) < 1)) continue;
    const icao = [a.icao_code, a.gps_code].find((id) => /^[A-Z]{4}$/.test(id || '')) || '';
    out.set(a.ident, {
      icao,
      name: a.name,
      lat: round(pt.lat, 5),
      lon: round(pt.lon, 5),
      elevationFt: a.elevation_ft === '' ? null : round(Number(a.elevation_ft)),
      source: 'community',
      runways: [],
      frequencies: [],
    });
  }
  for (const r of parseCsv(runwaysCsv)) {
    const ap = out.get(r.airport_ident);
    if (!ap || r.closed === '1') continue;
    // Helipads and water lanes aren't runways for this purpose.
    if (/^H\d*$/i.test(r.le_ident || '') || /WATER/i.test(r.surface || '')) continue;
    const ends = runwayEnds(r, ukStyle);
    if (ends.length === 0) continue; // no usable direction - the app checks the total wind instead
    ap.runways.push({
      id: [r.le_ident, r.he_ident].filter(Boolean).join('/'),
      lengthM: r.length_ft ? round(Number(r.length_ft) * 0.3048) : null,
      widthM: r.width_ft ? round(Number(r.width_ft) * 0.3048) : null,
      surface: oaSurface(r.surface),
      ends,
    });
  }
  for (const f of parseCsv(freqCsv)) {
    const ap = out.get(f.airport_ident);
    const type = ap && OA_FREQ_TYPES[(f.type || '').toUpperCase()];
    const mhz = Number(f.frequency_mhz);
    if (!type || !(mhz >= 118 && mhz < 137) || EMERGENCY(mhz)) continue;
    const val = String(f.frequency_mhz);
    const existing = ap.frequencies.find((x) => x.type === type);
    if (existing) {
      if (!existing.mhz.includes(val)) existing.mhz.push(val);
    } else ap.frequencies.push({ type, callSign: f.description || null, mhz: [val] });
  }
  return [...out.values()];
}

// Make each runway usable for wind maths, or drop it:
//  - closed ("X" in the designator) or zero-length entries go;
//  - a runway listed with only ONE end gets the opposite end (+180 deg) so
//    it isn't treated as one-way (which would invent tailwinds);
//  - a runway with no known bearing at all goes (the app then checks the
//    total wind for that airfield rather than guess a direction).
// "06" -> "24", "09L" -> "27R", "18G" -> "36G"; null if not a runway number.
function reciprocalId(id) {
  const m = (id || '').match(/^(\d{1,2})([LRCG]?)$/);
  if (!m || Number(m[1]) < 1 || Number(m[1]) > 36) return null;
  const n = ((Number(m[1]) + 17) % 36) + 1;
  const side = { L: 'R', R: 'L' }[m[2]] || m[2];
  return `${String(n).padStart(2, '0')}${side}`;
}

function cleanRunways(list) {
  return list
    .map((a) => ({
      ...a,
      runways: a.runways
        .filter((r) => r.lengthM && r.lengthM > 0 && !/X/i.test(r.id))
        .map((r) => {
          if (r.ends.length !== 1) return r;
          const [only] = r.ends;
          const ids = r.id.split('/');
          const otherId = ids.find((x) => x !== only.id) || reciprocalId(only.id);
          if (!otherId) return r;
          return {
            ...r,
            id: ids.length > 1 ? r.id : `${only.id}/${otherId}`,
            ends: [only, { ...only, id: otherId, trueBearing: Math.round(((only.trueBearing + 180) % 360) * 10) / 10 }],
          };
        })
        .filter((r) => r.ends.length > 0),
    }))
    .filter((a) => a.runways.length > 0);
}

async function buildAerodromes() {
  const uk = await buildUk();
  const us = await buildUs();
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const ukCommunity = cleanRunways(await buildCommunity(uk.aerodromes, ['GB', 'IM', 'JE', 'GG']));
  const usCommunity = cleanRunways(await buildCommunity(us.aerodromes, ['US']));
  uk.aerodromes = [...cleanRunways(uk.aerodromes), ...ukCommunity];
  us.aerodromes = [...cleanRunways(us.aerodromes), ...usCommunity];
  fs.writeFileSync(path.join(DATA_DIR, 'uk-aerodromes.json'), JSON.stringify(uk.aerodromes));
  fs.writeFileSync(path.join(DATA_DIR, 'us-aerodromes.json'), JSON.stringify(us.aerodromes));
  fs.rmSync(TMP_DIR, { recursive: true, force: true });
  console.log(
    `UK aerodromes: ${uk.aerodromes.length} (${ukCommunity.length} community), US aerodromes: ${us.aerodromes.length} (${usCommunity.length} community)`,
  );
  return { ukCount: uk.aerodromes.length, usCount: us.aerodromes.length, ukSource: uk.sourceUrl, usSource: us.sourceUrl };
}

module.exports = { buildAerodromes };

if (require.main === module) {
  buildAerodromes().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
