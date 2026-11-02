/* Drives util.js's colorPickerHtml()/wireColorPicker() in jsdom and prints what the dot
   beside the custom box showed after each step, as JSON. Called by
   tests/test_color_picker_js.py, which owns every assertion. argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const dom = new JSDOM('<!doctype html><html><head><meta name="csrf-token" content="t"></head><body></body></html>',
  { runScripts: 'dangerously', url: 'http://localhost:5000/', virtualConsole: vc });
const w = dom.window;
const d = w.document;
w.eval(UTIL);
const colorPickerHtml = w.eval('colorPickerHtml');
const wireColorPicker = w.eval('wireColorPicker');

const PRESETS = [{ hex: '#bc8cff', label: 'Purple' }, { hex: '#2bb3a6', label: 'Teal' }];

function mount(color) {
  const body = d.createElement('div');
  body.innerHTML = colorPickerHtml('t-', color, '#bc8cff', PRESETS);
  d.body.replaceChildren(body);
  return { body, read: wireColorPicker(body, 't-', '#bc8cff') };
}

function dot(body, read) {
  const el = body.querySelector('#t-color-preview');
  return { hidden: el.hidden, bg: el.style.backgroundColor, value: read() };
}

function type(body, text) {
  const input = body.querySelector('#t-color-custom');
  input.value = text;
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
}

function pick(body, hex) {
  const radio = body.querySelector(`.color-radio[value="${hex}"]`);
  radio.checked = true;
  radio.dispatchEvent(new w.Event('change', { bubbles: true }));
}

const out = {};
let m = mount('#bc8cff');
out.presetOpen = dot(m.body, m.read);
pick(m.body, '#2bb3a6');
out.afterPick = dot(m.body, m.read);
type(m.body, '#ff0000');
out.afterTyping = dot(m.body, m.read);
type(m.body, '#ff00');
out.halfTyped = dot(m.body, m.read);
type(m.body, '');
out.cleared = dot(m.body, m.read);
type(m.body, '#123456');
pick(m.body, '#bc8cff');
out.pickAfterCustom = dot(m.body, m.read);
m = mount('#e8b4fb');
out.customOpen = dot(m.body, m.read);

console.log(JSON.stringify({ errors, out }));
