// Sanity checks on the built data BEFORE it's committed - the app reads
// main live, so a source changing format must fail the job, not ship broken
// or near-empty data to every user. Run: node scripts/validate-data.js
const fs = require('fs');
const path = require('path');

const DATA = path.join(__dirname, '..', 'data');
const read = (f) => JSON.parse(fs.readFileSync(path.join(DATA, f), 'utf8'));
const errors = [];
const fail = (msg) => errors.push(msg);

// Well below today's counts (Oct 2026), so normal churn passes but a broken
// source (half the data gone) doesn't.
const FLOORS = {
  'uk-airports.json': 120,
  'uk-vrps.json': 300,
  'uk-airfields.json': 350,
  'us-airports.json': 1400,
  'us-airfields.json': 18000,
  'uk-aerodromes.json': 250,
  'us-aerodromes.json': 10000,
};
for (const [f, min] of Object.entries(FLOORS)) {
  const n = read(f).length;
  if (n < min) fail(`${f}: only ${n} entries (expected at least ${min})`);
}

const finite = (v) => typeof v === 'number' && Number.isFinite(v);
for (const f of ['uk-airports.json', 'uk-vrps.json', 'uk-airfields.json', 'us-airports.json', 'us-airfields.json']) {
  read(f).forEach((p, i) => {
    if (!finite(p.lat) || !finite(p.lon) || Math.abs(p.lat) > 90 || Math.abs(p.lon) > 180) fail(`${f}[${i}] bad position`);
  });
}

for (const f of ['uk-aerodromes.json', 'us-aerodromes.json']) {
  let official = 0;
  for (const a of read(f)) {
    const where = `${f} ${a.icao || a.name}`;
    if (a.source !== 'official' && a.source !== 'community') fail(`${where}: source "${a.source}"`);
    if (a.source === 'official') official++;
    if (a.source === 'official' && !a.icao) fail(`${where}: official entry with no ICAO code`);
    if (!finite(a.lat) || !finite(a.lon)) fail(`${where}: no position`);
    if (!a.runways.length) fail(`${where}: no runways`);
    for (const r of a.runways) {
      if (/X/i.test(r.id)) fail(`${where} ${r.id}: closed runway`);
      if (r.lengthM !== null && !(r.lengthM > 0)) fail(`${where} ${r.id}: length ${r.lengthM}`);
      if (r.ends.length < 2) fail(`${where} ${r.id}: only ${r.ends.length} end(s) - would invent tailwinds`);
      for (const e of r.ends) {
        if (!finite(e.trueBearing) || e.trueBearing < 0 || e.trueBearing > 360) fail(`${where} ${e.id}: bearing ${e.trueBearing}`);
        if (e.uncertaintyDeg !== undefined && !(e.uncertaintyDeg > 0 && e.uncertaintyDeg <= 30)) fail(`${where} ${e.id}: uncertainty ${e.uncertaintyDeg}`);
        if (e.uncertaintyDeg && a.source === 'official') fail(`${where} ${e.id}: approximate direction on official data`);
      }
      // The two ends of a runway point (roughly) opposite ways.
      if (r.ends.length === 2) {
        const diff = Math.abs(((r.ends[0].trueBearing - r.ends[1].trueBearing + 540) % 360) - 180);
        if (diff < 170) fail(`${where} ${r.id}: ends only ${diff.toFixed(0)} deg apart`);
      }
    }
    for (const fr of a.frequencies) {
      for (const m of fr.mhz) {
        const v = Number(m);
        if (!(v >= 118 && v < 137) || Math.abs(v - 121.5) < 0.001) fail(`${where}: frequency ${m}`);
      }
    }
  }
  if (official < (f.startsWith('uk') ? 90 : 2000)) fail(`${f}: only ${official} official entries - has the official source changed format?`);
}

if (errors.length) {
  console.error(`${errors.length} problem(s):\n` + errors.slice(0, 50).join('\n'));
  process.exit(1);
}
console.log('Data OK');
