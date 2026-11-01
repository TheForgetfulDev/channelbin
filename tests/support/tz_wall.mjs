/* Reads util.js's wall-clock-to-instant conversion for a list of `datetime-local` values and
   prints what it answered as JSON. Called by tests/test_tz_wall_js.py, which owns every
   assertion and compares the answers with app/tz_utils.py's. The shipped util.js is
   evaluated as base.html's <script src> would have, not stubbed.

   argv: <repo root> <IANA zone> <JSON list of 'YYYY-MM-DDTHH:MM' values>. */
import fs from 'fs';
import { JSDOM } from 'jsdom';

const [, , REPO, TZ, VALUES] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');

const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', {
  runScripts: 'dangerously', url: 'http://localhost:5000/',
});
const w = dom.window;
w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
w.eval(UTIL);
const tzWallInstants = w.eval('tzWallInstants');
const tzInputValueToDate = w.eval('tzInputValueToDate');

const out = {};
for (const v of JSON.parse(VALUES)) {
  const r = tzWallInstants(v, TZ);
  out[v] = {
    first: r.first.toISOString(),
    second: r.second ? r.second.toISOString() : null,
    skipped: r.skipped,
    toDate: tzInputValueToDate(v, TZ).toISOString(),
  };
}
process.stdout.write(JSON.stringify(out));
