import { el } from './dom.js';
import { saved } from './prefs.js';

const BOX_HEIGHT = new Map();
export function grow(t) { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight + 2, Math.round(window.innerHeight * 0.45)) + 'px'; if (t.dataset.key) BOX_HEIGHT.set(t.dataset.key, t.style.height); }
/* a text box that grows with its text; Enter sends, Shift+Enter is a new line; its draft is kept under `key` */
export function writeBox(key, attrs, send) {
  const t = el('textarea', Object.assign({ rows: 1, autocomplete: 'off', spellcheck: 'true' }, attrs));
  t.value = saved(key); t.dataset.key = key;
  if (BOX_HEIGHT.has(key)) t.style.height = BOX_HEIGHT.get(key);
  t.addEventListener('input', () => { saved(key, t.value); grow(t); });
  t.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(t); } });
  requestAnimationFrame(() => grow(t));
  return t;
}
