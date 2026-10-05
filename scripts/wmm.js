// Magnetic declination (variation) from the World Magnetic Model 2025 - the
// official NOAA NCEI / UK BGS model used for aviation charts and GPS units.
// Coefficients: scripts/WMM2025.COF, unchanged from
// https://www.ncei.noaa.gov/products/world-magnetic-model (public domain;
// valid 2025.0-2030.0). Checked against NOAA's published test values in
// scripts/test-wmm.js. Port of NOAA's reference geomag algorithm.

const fs = require('fs');
const path = require('path');

const MAXORD = 12;

function load() {
  const lines = fs.readFileSync(path.join(__dirname, 'WMM2025.COF'), 'utf8').split(/\r?\n/);
  const epoch = Number(lines[0].trim().split(/\s+/)[0]);
  const z = () => Array.from({ length: 13 }, () => new Array(13).fill(0));
  const c = z();
  const cd = z();
  for (const line of lines.slice(1)) {
    const f = line.trim().split(/\s+/);
    if (f.length < 6 || /^9{10}/.test(f[0])) continue;
    const [n, m, gnm, hnm, dgnm, dhnm] = f.map(Number);
    c[m][n] = gnm;
    cd[m][n] = dgnm;
    if (m !== 0) {
      c[n][m - 1] = hnm;
      cd[n][m - 1] = dhnm;
    }
  }
  // Schmidt semi-normalisation, folded into the coefficients.
  const snorm = new Array(169).fill(0);
  const k = z();
  const fn = new Array(13).fill(0);
  const fm = new Array(13).fill(0);
  snorm[0] = 1;
  for (let n = 1; n <= MAXORD; n++) {
    snorm[n] = (snorm[n - 1] * (2 * n - 1)) / n;
    let j = 2;
    for (let m = 0; m <= n; m++) {
      k[m][n] = ((n - 1) * (n - 1) - m * m) / ((2 * n - 1) * (2 * n - 3));
      if (m > 0) {
        const flnmj = ((n - m + 1) * j) / (n + m);
        snorm[n + m * 13] = snorm[n + (m - 1) * 13] * Math.sqrt(flnmj);
        j = 1;
        c[n][m - 1] *= snorm[n + m * 13];
        cd[n][m - 1] *= snorm[n + m * 13];
      }
      c[m][n] *= snorm[n + m * 13];
      cd[m][n] *= snorm[n + m * 13];
    }
    fn[n] = n + 1;
    fm[n] = n;
  }
  k[1][1] = 0;
  return { epoch, c, cd, k, fn, fm };
}

let model = null;

// Declination in degrees (+ = east: true = magnetic + declination) at a
// geodetic position, altitude in km above the WGS84 ellipsoid, decimal year.
function declination(latDeg, lonDeg, altKm = 0, year = decimalYear(new Date())) {
  model = model || load();
  const { epoch, c, cd, k, fn, fm } = model;
  const dt = year - epoch;
  // Each model is valid for 5 years; fail the build rather than quietly use
  // an expired one. Replace WMM2025.COF with the next model from NOAA.
  if (dt < 0 || dt > 5) throw new Error(`WMM${epoch} is not valid for ${year.toFixed(1)} - update scripts/WMM2025.COF from NOAA`);
  const a = 6378.137;
  const b = 6356.7523142;
  const re = 6371.2;
  const a2 = a * a;
  const b2 = b * b;
  const c2 = a2 - b2;
  const a4 = a2 * a2;
  const b4 = b2 * b2;
  const c4 = a4 - b4;
  const rlat = (latDeg * Math.PI) / 180;
  const rlon = (lonDeg * Math.PI) / 180;
  const srlat = Math.sin(rlat);
  const crlat = Math.cos(rlat);
  const srlat2 = srlat * srlat;
  const crlat2 = crlat * crlat;
  const q = Math.sqrt(a2 - c2 * srlat2);
  const q1 = altKm * q;
  const q2 = ((q1 + a2) / (q1 + b2)) ** 2;
  const ct = srlat / Math.sqrt(q2 * crlat2 + srlat2);
  const st = Math.sqrt(1 - ct * ct);
  const r2 = altKm * altKm + 2 * q1 + (a4 - c4 * srlat2) / (q * q);
  const r = Math.sqrt(r2);
  const d = Math.sqrt(a2 * crlat2 + b2 * srlat2);
  const ca = (altKm + d) / r;
  const sa = (c2 * crlat * srlat) / (r * d);

  const sp = new Array(13).fill(0);
  const cp = new Array(13).fill(0);
  sp[0] = 0;
  cp[0] = 1;
  sp[1] = Math.sin(rlon);
  cp[1] = Math.cos(rlon);
  for (let m = 2; m <= MAXORD; m++) {
    sp[m] = sp[1] * cp[m - 1] + cp[1] * sp[m - 1];
    cp[m] = cp[1] * cp[m - 1] - sp[1] * sp[m - 1];
  }
  const z = () => Array.from({ length: 13 }, () => new Array(13).fill(0));
  const p = z();
  const dp = z();
  const tc = z();
  const pp = new Array(13).fill(0);
  p[0][0] = 1;
  pp[0] = 1;
  const aor = re / r;
  let ar = aor * aor;
  let br = 0;
  let bt = 0;
  let bp = 0;
  let bpp = 0;
  for (let n = 1; n <= MAXORD; n++) {
    ar *= aor;
    for (let m = 0; m <= n; m++) {
      if (n === m) {
        p[m][n] = st * p[m - 1][n - 1];
        dp[m][n] = st * dp[m - 1][n - 1] + ct * p[m - 1][n - 1];
      } else if (n === 1 && m === 0) {
        p[m][n] = ct * p[m][n - 1];
        dp[m][n] = ct * dp[m][n - 1] - st * p[m][n - 1];
      } else if (n > 1 && n !== m) {
        if (m > n - 2) {
          p[m][n - 2] = 0;
          dp[m][n - 2] = 0;
        }
        p[m][n] = ct * p[m][n - 1] - k[m][n] * p[m][n - 2];
        dp[m][n] = ct * dp[m][n - 1] - st * p[m][n - 1] - k[m][n] * dp[m][n - 2];
      }
      tc[m][n] = c[m][n] + dt * cd[m][n];
      if (m !== 0) tc[n][m - 1] = c[n][m - 1] + dt * cd[n][m - 1];
      const par = ar * p[m][n];
      let temp1;
      let temp2;
      if (m === 0) {
        temp1 = tc[m][n] * cp[m];
        temp2 = tc[m][n] * sp[m];
      } else {
        temp1 = tc[m][n] * cp[m] + tc[n][m - 1] * sp[m];
        temp2 = tc[m][n] * sp[m] - tc[n][m - 1] * cp[m];
      }
      bt -= ar * temp1 * dp[m][n];
      bp += fm[m] * temp2 * par;
      br += fn[n] * temp1 * par;
      // At the geographic poles the east component needs its own recursion.
      if (st === 0 && m === 1) {
        pp[n] = n === 1 ? pp[n - 1] : ct * pp[n - 1] - k[m][n] * pp[n - 2];
        bpp += fm[m] * temp2 * ar * pp[n];
      }
    }
  }
  bp = st === 0 ? bpp : bp / st;
  const bx = -bt * ca - br * sa;
  const by = bp;
  return (Math.atan2(by, bx) * 180) / Math.PI;
}

function decimalYear(date) {
  const y = date.getUTCFullYear();
  const start = Date.UTC(y, 0, 1);
  return y + (date.getTime() - start) / (Date.UTC(y + 1, 0, 1) - start);
}

module.exports = { declination, decimalYear };
