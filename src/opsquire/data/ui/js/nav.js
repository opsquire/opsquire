/* Where the page is: the view, the open rows and the pane's subject, kept in the address hash. */
import { conversationOf, liveAskJob, S, task } from './state.js';
import { shortName, VIEWS } from './records.js';
import { $, el, toast } from './dom.js';
import { DEV_KEY } from './prefs.js';
import { render } from './render.js';
import { openJob } from './jobs.js';
import { openLog, stopPane } from './pane.js';

function parseHash(h) {
  const i = h.indexOf('/');
  const kind = i < 0 ? h : h.slice(0, i);
  let id = i < 0 ? '' : h.slice(i + 1);
  try { id = decodeURIComponent(id); } catch (e) { /* keep raw */ }
  return { kind, id };
}
export function setDev(on) {
  S.dev = !!on;
  try { localStorage.setItem(DEV_KEY, S.dev ? '1' : '0'); } catch (e) { /* private window */ }
  document.documentElement.classList.toggle('dev', S.dev);
  const b = $('#devtoggle'); if (b) b.setAttribute('aria-pressed', String(S.dev));
  if (S.data) render();
}
export function setHash(h) { S.hash = h; if (location.hash.slice(1) !== h) history.replaceState(null, '', '#' + h); }
export function applyHash() {
  const h = location.hash.slice(1); if (h === S.hash) return;
  const first = S.hash === null;
  S.hash = h;
  const { kind, id } = parseHash(h);
  const focus = (view, key) => { S.view = view; S.open.add(key); S.scrollTo = key; };
  if (VIEWS.some(v => v[0] === kind)) S.view = kind;
  else if (kind === 'thread') focus('history', 't:' + id);
  else if (kind === 'target') { S.view = 'history'; S.hist.machine = id; }
  else if (kind === 'script') { S.view = 'history'; S.hist.script = id; }
  else if (kind === 'task') {
    const t = task(id);
    if (first && t) { if (t.thread) focus('history', 't:' + t.thread); }
    S.pane = { kind: 'task', id };
  } else if (kind === 'run') openLog(id, undefined, true);
  else if (kind === 'job') openJob(id, true);
  else if (kind === 'ask') { const live = liveAskJob(conversationOf(id)); if (live) openJob(live.job_id, true, 'ask'); else S.pane = { kind: 'ask', id, built: false }; }
  render();
}
export function toggle(key, hash) {
  if (S.open.has(key)) S.open.delete(key); else { S.open.add(key); if (hash) setHash(hash); }
  render();
}
export function switchView(view) { S.view = view; setHash(view); render(); window.scrollTo(0, 0); }
export function focusKey(view, key, hash) { S.view = view; S.open.add(key); S.scrollTo = key; setHash(hash); render(); }
export function goToTask(id) {
  const t = task(id); if (!t) { toast(`no task ${id}`); return; }
  if (t.thread) S.open.add('t:' + t.thread);
  S.pane = { kind: 'task', id };
  focusKey('history', 'k:' + id, 'task/' + encodeURIComponent(id));
}
export function selectTask(id) {
  if (!task(id)) { toast(`no task ${id}`); return; }
  stopPane();
  S.pane = { kind: 'task', id };
  setHash('task/' + encodeURIComponent(id));
  render();
}
export function collapsible(key, headParts, bodyFn, hash, cls) {
  const open = S.open.has(key);
  const node = el('div', { class: `row ${cls || ''} ${open ? 'open' : ''}`, 'data-key': key });
  const head = el('div', {
    class: 'head', role: 'button', tabindex: '0', 'aria-expanded': String(open),
    onclick: e => { if (e.target.closest('a, button, input, select, textarea, label, .run, svg')) return; toggle(key, hash); },
    onkeydown: e => { if ((e.key === 'Enter' || e.key === ' ') && e.target === head) { e.preventDefault(); toggle(key, hash); } },
  }, el('span', { class: 'chev' }), headParts);
  node.append(head);
  if (open) node.append(el('div', { class: 'body' }, bodyFn()));
  return node;
}
export function targetLink(name) { return el('a', { class: 'tag', href: '#target/' + encodeURIComponent(name), onclick: e => { e.preventDefault(); historyFilter({ machine: name }); } }, name); }
export function scriptLink(id) { return el('a', { class: 'tag mono', href: '#script/' + encodeURIComponent(id), onclick: e => { e.preventDefault(); historyFilter({ script: id }); } }, id); }
export function taskLink(id) {
  const simple = !S.dev && /^\d{8}-\d{4}_/.test(String(id || ''));
  return el('a', { class: simple ? 'name' : 'id', href: '#task/' + encodeURIComponent(id), title: simple ? id : null, onclick: e => { e.preventDefault(); selectTask(id); } }, simple ? shortName(id) : id);
}
export function historyFilter(change) { Object.assign(S.hist, change, { allRuns: false }); S.view = 'history'; setHash('history'); render(); window.scrollTo(0, 0); }
