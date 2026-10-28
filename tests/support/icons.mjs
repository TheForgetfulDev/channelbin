/* Calls util.js::icon() in jsdom and prints what it drew as JSON. Called by
   tests/test_icon_registry.py, which owns every assertion and compares the output with the
   Jinja icon() global - this file only reports. The page's <meta name="icon-sprite"> is the
   one a real page rendered, handed in on argv, so the script reads what base.html serves.

   argv: <repo root> <meta tag html> <JSON list of [name, cls] pairs> */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO, META, CALLS] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');

const errors = [];
const consoleErrors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => consoleErrors.push(a.map(String).join(' ')));
const dom = new JSDOM(`<!doctype html><html><head>${META}</head><body></body></html>`, {
  runScripts: 'dangerously', url: 'http://localhost:5000/', virtualConsole: vc,
});
const { window } = dom;
const s = window.document.createElement('script');
s.textContent = UTIL;
window.document.head.appendChild(s);

const drawn = JSON.parse(CALLS).map(([name, cls]) => window.icon(name, cls));
process.stdout.write(JSON.stringify({ drawn, errors, consoleErrors }));
