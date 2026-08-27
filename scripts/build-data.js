// Refreshes GoLight's UK aviation reference data (airports, VRPs, small
// airfields) from official/open sources. Run on a schedule via GitHub
// Actions so the app's data can update without an app store release.
//
// Sources:
//  - OurAirports.com (public domain) - full world airport list, filtered
//    to UK + Crown Dependencies with a valid ICAO code.
//  - NATS AIS digital datasets (nats-uk.ead-it.com) - official CAA/NATS
//    Visual Reference Points and unlicensed/uncertificated aerosites.
//    Licensed "unrestricted access" but "not for resale" - see README.

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const DATA_DIR = path.join(__dirname, '..', 'data');
const TMP_DIR = path.join(__dirname, '..', '.tmp');

async function fetchText(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Fetch failed ${res.status}: ${url}`);
  return res.text();
}

async function fetchBuffer(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Fetch failed ${res.status}: ${url}`);
  return Buffer.from(await res.arrayBuffer());
}

function parseCsvLine(line) {
  const fields = [];
  let cur = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQuotes) {
      if (c === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++; }
        else inQuotes = false;
      } else cur += c;
    } else {
      if (c === '"') inQuotes = true;
      else if (c === ',') { fields.push(cur); cur = ''; }
      else cur += c;
    }
  }
  fields.push(cur);
  return fields;
}

function parseDms(coord) {
  if (!coord) return null;
  const m = coord.trim().match(/^(\d{2,3})(\d{2})(\d{2}(?:\.\d+)?)([NSEW])$/);
  if (!m) return null;
  const [, deg, min, sec, hem] = m;
  let val = parseInt(deg, 10) + parseInt(min, 10) / 60 + parseFloat(sec) / 3600;
  if (hem === 'S' || hem === 'W') val = -val;
  return Math.round(val * 10000) / 10000;
}

// Find the most recent dated dataset URL on a NATS page matching a pattern
// like EG_VRP_DS_AREA1_FULL_20260806_CSV.zip, choosing the latest one whose
// effective date has already passed (today's usable dataset).
function findLatestDatedUrl(html, hrefRegex) {
  const matches = [...html.matchAll(hrefRegex)];
  if (matches.length === 0) return null;
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const candidates = matches
    .map((m) => ({ url: m[1], date: m[2] }))
    .sort((a, b) => a.date.localeCompare(b.date));
  const eligible = candidates.filter((c) => c.date <= today);
  return (eligible.length > 0 ? eligible[eligible.length - 1] : candidates[0]).url;
}

async function buildAirports() {
  const csv = await fetchText('https://davidmegginson.github.io/ourairports-data/airports.csv');
  const lines = csv.split('\n');
  const header = parseCsvLine(lines[0]);
  const idx = Object.fromEntries(header.map((h, i) => [h, i]));
  const ALLOWED_COUNTRIES = new Set(['GB', 'IM', 'JE', 'GG']);
  const ALLOWED_TYPES = new Set(['small_airport', 'medium_airport', 'large_airport']);

  const results = [];
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    const f = parseCsvLine(line);
    if (!ALLOWED_COUNTRIES.has(f[idx['iso_country']]) || !ALLOWED_TYPES.has(f[idx['type']])) continue;
    const icao = (f[idx['icao_code']] || '').trim();
    if (icao.length !== 4) continue;
    const lat = parseFloat(f[idx['latitude_deg']]);
    const lon = parseFloat(f[idx['longitude_deg']]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    results.push({
      icao,
      iata: (f[idx['iata_code']] || '').trim() || undefined,
      name: f[idx['name']],
      city: f[idx['municipality']] || '',
      lat: Math.round(lat * 10000) / 10000,
      lon: Math.round(lon * 10000) / 10000,
    });
  }
  results.sort((a, b) => a.icao.localeCompare(b.icao));
  return results;
}

async function buildVrpsAndAirfields() {
  fs.mkdirSync(TMP_DIR, { recursive: true });

  const datasetsHtml = await fetchText('https://nats-uk.ead-it.com/cms-nats/opencms/en/Publications/digital-datasets/');
  const vrpZipUrl = findLatestDatedUrl(
    datasetsHtml,
    /href="(\/cms-nats\/export\/sites\/default\/en\/Publications\/digital-datasets\/VRP\/EG_VRP_DS_AREA1_FULL_(\d{8})_CSV\.zip)"/g,
  );
  if (!vrpZipUrl) throw new Error('Could not find current VRP dataset URL on NATS digital-datasets page');
  const zipBuf = await fetchBuffer(`https://nats-uk.ead-it.com${vrpZipUrl}`);
  const zipPath = path.join(TMP_DIR, 'vrp.zip');
  fs.writeFileSync(zipPath, zipBuf);
  execSync(`unzip -o -q "${zipPath}" -d "${TMP_DIR}/vrp"`);
  const vrpCsvName = fs.readdirSync(path.join(TMP_DIR, 'vrp')).find((f) => f.endsWith('.csv'));
  const vrpCsv = fs.readFileSync(path.join(TMP_DIR, 'vrp', vrpCsvName), 'utf8');
  const vrpLines = vrpCsv.split('\n').filter((l) => l.trim());
  const vrps = [];
  for (let i = 1; i < vrpLines.length; i++) {
    const f = parseCsvLine(vrpLines[i]);
    const [name, latRaw, lonRaw, aerodrome] = f;
    const lat = parseDms(latRaw);
    const lon = parseDms(lonRaw);
    if (lat === null || lon === null || !name) continue;
    vrps.push({ name: name.trim(), aerodrome: aerodrome?.trim() || undefined, lat, lon });
  }

  const resourcesHtml = await fetchText('https://nats-uk.ead-it.com/cms-nats/opencms/en/Charts/vfr-charts/Resources/');
  const aeroMatch = resourcesHtml.match(
    /href="(\/cms-nats\/opencms\/en\/Charts\/vfr-charts\/Resources\/VFR-Database-Unlicensed-Military-and-Foreign-Aerosites-[^"]+\.csv)"/,
  );
  if (!aeroMatch) throw new Error('Could not find current aerosites dataset URL on NATS resources page');
  const aeroCsv = await fetchText(`https://nats-uk.ead-it.com${aeroMatch[1]}`);
  const aeroLines = aeroCsv.split('\n').filter((l) => l.trim());
  const header = parseCsvLine(aeroLines[0]);
  const idx = Object.fromEntries(header.map((h, i) => [h.trim(), i]));
  const airfields = [];
  for (let i = 2; i < aeroLines.length; i++) {
    const f = parseCsvLine(aeroLines[i]);
    const name = f[0];
    const status = f[idx['Status']];
    const type = f[idx['Type']];
    if (!name || status !== 'ACTIVE' || (type !== 'AD' && type !== 'HELI')) continue;
    const lat = parseDms(f[idx['ARP LAT - WGS84 ddmmss.ss']]);
    const lon = parseDms(f[idx['ARP LONG - WGS84 ddmmss.ss']]);
    if (lat === null || lon === null) continue;
    airfields.push({ kind: type === 'HELI' ? 'heliport' : 'airfield', name: name.trim(), lat, lon });
  }

  return { vrps, airfields, vrpSourceUrl: vrpZipUrl, aeroSourceUrl: aeroMatch[1] };
}

async function main() {
  fs.mkdirSync(DATA_DIR, { recursive: true });

  const airports = await buildAirports();
  const { vrps, airfields, vrpSourceUrl, aeroSourceUrl } = await buildVrpsAndAirfields();

  fs.writeFileSync(path.join(DATA_DIR, 'uk-airports.json'), JSON.stringify(airports));
  fs.writeFileSync(path.join(DATA_DIR, 'uk-vrps.json'), JSON.stringify(vrps));
  fs.writeFileSync(path.join(DATA_DIR, 'uk-airfields.json'), JSON.stringify(airfields));
  fs.writeFileSync(
    path.join(DATA_DIR, 'meta.json'),
    JSON.stringify(
      {
        generatedAt: new Date().toISOString(),
        counts: { airports: airports.length, vrps: vrps.length, airfields: airfields.length },
        sources: {
          airports: 'https://davidmegginson.github.io/ourairports-data/airports.csv',
          vrps: `https://nats-uk.ead-it.com${vrpSourceUrl}`,
          airfields: `https://nats-uk.ead-it.com${aeroSourceUrl}`,
        },
      },
      null,
      2,
    ),
  );

  fs.rmSync(TMP_DIR, { recursive: true, force: true });
  console.log(`Airports: ${airports.length}, VRPs: ${vrps.length}, airfields: ${airfields.length}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
