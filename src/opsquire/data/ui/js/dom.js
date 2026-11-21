/* DOM helpers that know nothing of opsquire. */
const NS = 'http://www.w3.org/2000/svg';
function make(n, attrs, children) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  append(n, children);
  return n;
}
export function el(tag, attrs, ...children) { return make(document.createElement(tag), attrs, children); }
export function svg(tag, attrs, ...children) { return make(document.createElementNS(NS, tag), attrs, children); }
function append(n, c) {
  if (c === null || c === undefined || c === false) return;
  if (Array.isArray(c)) { for (const x of c) append(n, x); return; }
  n.append(c instanceof Node ? c : String(c));
}
export function fill(n, ...children) { n.replaceChildren(); append(n, children); return n; }
export const $ = sel => document.querySelector(sel);
export function btn(label, onclick, cls, attrs) { return el('button', Object.assign({ class: cls || '', onclick, type: 'button' }, attrs || {}), label); }
let toastTimer = null;
export function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 1600);
}
export function copyText(text) {
  const done = () => toast('copied'), fail = () => toast('copy failed');
  if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(done, () => (fallbackCopy(text) ? done() : fail())); return; }
  if (fallbackCopy(text)) done(); else fail();
}
function fallbackCopy(text) {
  const ta = el('textarea', { style: 'position:fixed;opacity:0', 'aria-hidden': 'true' }, text);
  document.body.append(ta); ta.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  ta.remove(); return ok;
}
