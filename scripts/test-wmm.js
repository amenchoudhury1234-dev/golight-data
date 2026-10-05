// Checks scripts/wmm.js against NOAA's official WMM2025 test values
// (declination, field 5 of WMM2025_TestValues.txt). Run: node scripts/test-wmm.js
const { declination } = require('./wmm');

// year, alt km, lat, lon, declination - from NOAA's WMM2025_TestValues.txt
const CASES = require('fs')
  .readFileSync(require('path').join(__dirname, 'WMM2025_TestValues.txt'), 'utf8')
  .split(/\r?\n/)
  .filter((l) => l.trim() && !l.startsWith('#'))
  .map((l) => l.trim().split(/\s+/).slice(0, 5).map(Number));

let worst = 0;
for (const [year, alt, lat, lon, expected] of CASES) {
  const got = declination(lat, lon, alt, year);
  const err = Math.abs(((got - expected + 540) % 360) - 180);
  worst = Math.max(worst, err);
  if (err > 0.01) {
    console.error(`FAIL ${year} ${alt}km ${lat},${lon}: got ${got.toFixed(3)} expected ${expected}`);
    process.exitCode = 1;
  }
}
console.log(`${CASES.length} NOAA test points, worst error ${worst.toFixed(4)} deg${process.exitCode ? ' - FAILED' : ' - OK'}`);
