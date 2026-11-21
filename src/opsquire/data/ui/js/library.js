/* The Library view: how the general scripts are used, and the upkeep an agent can do on them (opsquire tend). */
import { S } from './state.js';
import { btn, el, toast } from './dom.js';
import { empty, sec, when } from './widgets.js';
import { api, post } from './api.js';
import { render } from './render.js';
import { openJob } from './jobs.js';
import { refresh } from './live.js';
import { setHash } from './nav.js';

function load() {
  S.libBusy = true;
  api('/api/tend').then(r => { S.lib = r; S.libError = null; }, e => { S.libError = e.message; }).finally(() => { S.libBusy = false; render(); });
}
async function fix() {
  S.libFixing = true; render();
  try { const r = await post('/api/tendfix', {}); refresh(); openJob(r.job_id, false, 'ask'); } catch (e) { toast(`could not start the agent: ${e.message}`); }
  S.libFixing = false; render();
}
function showScript(id) { S.view = 'history'; S.hist.script = id; setHash('script/' + encodeURIComponent(id)); render(); window.scrollTo(0, 0); }
function scriptLink(id) { return el('a', { href: '#script/' + encodeURIComponent(id), onclick: e => { e.preventDefault(); showScript(id); } }, id); }
function list(items, line) { return el('div', { class: 'block pad' }, items.map(line)); }

function renderFindings(r) {
  const n = r.copies.filter(c => c.fixable !== false).length + r.hand_imports.length + r.untested.length;
  const parts = [sec('Upkeep', n ? String(n) : null)];
  parts.push(el('div', { class: 'block pad' },
    el('div', { class: 'rule' },
      el('strong', {}, n ? `${n} findings an agent can fix` : 'Nothing for an agent to fix.'),
      el('span', { class: 'grow' }),
      n ? btn(S.libFixing ? 'starting…' : 'Let an agent fix these', fix, 'primary sm', { disabled: !!S.libFixing, title: 'starts an ask that stops at $10' }) : null),
    el('div', { class: 'faint small' }, 'The agent changes how code is shared, never what it does, and leaves settled tasks alone. It stops at $10. Every task whose scripts change needs a fresh dry run before its next apply.')));
  if (r.copies.length) {
    parts.push(sec('Copied code', String(r.copies.length), el('span', { class: 'faint small' }, 'the same function in 3 or more folders')));
    parts.push(list(r.copies, c => el('div', { class: 'line' },
      el('code', {}, c.names.join(' / ')), el('span', { class: 'muted small' }, `${c.lines} lines, in ${c.folders.length} folders`),
      c.generals.length ? el('span', { class: 'small' }, 'defined in ', c.generals.map((g, i) => [i ? ', ' : '', scriptLink(g)])) : el('span', { class: 'small muted' }, 'in no general script yet'))));
  }
  if (r.hand_imports.length) {
    parts.push(sec('Loading shared scripts by hand', String(r.hand_imports.length), el('span', { class: 'faint small' }, 'importlib or sys.path instead of sq.use')));
    parts.push(list(r.hand_imports, h => el('div', { class: 'line' }, scriptLink(h.id), el('span', { class: 'muted small mono' }, h.files.join(', ')))));
  }
  if (r.untested.length) {
    parts.push(sec('Without tests', String(r.untested.length)));
    parts.push(list(r.untested, g => el('div', { class: 'line' }, scriptLink(g))));
  }
  if (r.unused.length) {
    parts.push(sec('Unused', String(r.unused.length), el('span', { class: 'faint small' }, 'no open task uses them and nothing ran them for 30 days')));
    parts.push(list(r.unused, g => el('div', { class: 'line' }, scriptLink(g), el('span', { class: 'cmd small' }, `opsquire offboard ${g}`))));
  }
  return parts;
}
function renderUsage(r) {
  const rows = r.usage.map(u => el('tr', { class: 'click', title: u.purpose, onclick: e => { if (!e.target.closest('a')) showScript(u.id); } },
    el('td', {}, scriptLink(u.id), u.tested ? null : el('span', { class: 'small err' }, ' no tests')),
    el('td', { class: 'num' }, String(u.runs_recent)),
    el('td', { class: 'num' }, u.applies ? `${u.applies_ok}/${u.applies}` : '-'),
    el('td', { class: 'num' }, `${u.users_open.length}/${u.users}`),
    el('td', {}, u.last_used ? when(u.last_used) : el('span', { class: 'muted' }, 'never'))));
  return [sec('General scripts', String(r.usage.length), el('span', { class: 'faint small' }, 'runs in the last 30 days, alone or in a task using them')),
    r.usage.length ? el('div', { class: 'block pad scroll' }, el('table', { class: 'tbl lib' },
      el('thead', {}, el('tr', {}, ['script', 'runs', 'applies ok', 'users open/all', 'last used'].map(h => el('th', {}, h)))), el('tbody', {}, rows)))
      : empty('No general scripts yet.')];
}
export function renderLibrary() {
  if (!S.lib && !S.libBusy && !S.libError) load();
  const head = el('div', { class: 'line' }, el('span', { class: 'grow' }),
    S.lib ? el('span', { class: 'faint small' }, 'as of ', when(S.lib.generated)) : null,
    btn(S.libBusy ? 'reading…' : 'Refresh', load, 'sm', { disabled: !!S.libBusy }));
  if (S.libError) return el('div', { class: 'library' }, head, el('div', { class: 'err' }, `Cannot read the library: ${S.libError}`));
  if (!S.lib) return el('div', { class: 'library' }, head, el('div', { class: 'quiet' }, 'Reading the scripts…'));
  return el('div', { class: 'library' }, head, ...renderFindings(S.lib), ...renderUsage(S.lib));
}
