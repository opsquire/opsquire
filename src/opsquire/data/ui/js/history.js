import { inboxItem, isDeciding, S, selectedTask, task } from './state.js';
import { asksForThread, money, plural, scriptName, statsText, statusWords, targetIndex, taskMatches, threadAgents, threadMatches, threadSummary, TICK, tone, truncate } from './records.js';
import { btn, el } from './dom.js';
import { empty, linkTag, modeTag, noMatch, pill, prodMarker, tag, when } from './widgets.js';
import { markdown } from './markdown.js';
import { saved } from './prefs.js';
import { collapsible, goToTask, historyFilter, scriptLink, selectTask, setHash, targetLink, taskLink, toggle } from './nav.js';
import { render } from './render.js';
import { listRow } from './inbox.js';
import { flowSvg, renderMap } from './map.js';
import { openLog } from './pane.js';
import { fileButtons } from './task.js';

const HIST_SORTS = [['recent', 'Recent'], ['attention', 'Needs attention'], ['busiest', 'Most runs'], ['oldest', 'Oldest'], ['name', 'A-Z']];
function threadRuns(th) { return (th.tasks || []).reduce((n, id) => n + ((task(id) || {}).runs || []).length, 0); }
function sortThreads(threads, how) {
  const by = {
    recent: (a, b) => String(b.updated || '').localeCompare(String(a.updated || '')),
    oldest: (a, b) => String(a.started || '').localeCompare(String(b.started || '')),
    busiest: (a, b) => threadRuns(b) - threadRuns(a),
    attention: (a, b) => (a.state === 'done') - (b.state === 'done') || (b.suggestions || 0) - (a.suggestions || 0) || String(b.updated || '').localeCompare(String(a.updated || '')),
    name: (a, b) => String(a.title || a.id).localeCompare(String(b.title || b.id)),
  };
  return [...threads].sort(by[how] || by.recent);
}
function renderThreads() {
  const threads = sortThreads(S.data.threads || [], S.hist.sort);
  if (!threads.length) return empty('No threads yet: they appear once an agent creates a task.');
  const q = S.q, shown = [];
  for (const th of threads) {
    const whole = threadMatches(th, q);
    const filtered = S.hist.machine || S.hist.script;
    const ids = (whole && !filtered ? th.tasks || [] : (th.tasks || []).filter(id => task(id) && (whole || taskMatches(task(id), q, th.title)))).filter(id => !filtered || keptByHistory(task(id)));
    if (ids.length || (whole && !filtered)) shown.push(renderThread(th, ids));
  }
  return shown.length ? el('div', {}, shown) : noMatch();
}
function renderThread(th, taskIds) {
  const ids = (th.tasks || []).slice().sort();
  const done = th.state === 'done', open = ids.filter(id => task(id) && !task(id).settled).length;
  const summary = threadSummary(threadAgents(th, S.data.tasks || {}));
  const head = [
    done ? el('span', { class: 'tick done', title: 'every task settled' }, TICK.done) : null,
    el('span', { class: 'title' }, th.title || th.id),
    S.dev ? pill(th.state || 'open') : !done && open ? el('span', { class: 'next', title: 'tasks still open' }, `${open} to do`) : null,
    S.dev && (th.areas || []).length ? el('span', { class: 'muted small' }, th.areas.join(', ')) : null,
    th.suggestions ? el('span', { class: 'suggn', title: plural(th.suggestions, 'suggestion') }, `${th.suggestions} !`) : null,
    th.offboarded ? el('span', { class: 'muted small', title: 'settled tasks of this thread that left after the retention period; opsquire find still finds them, opsquire restore brings one back' }, `+${th.offboarded} offboarded`) : null,
    el('span', { class: 'grow' }),
    el('span', { class: 'mini' }, flowSvg(ids, true)),
    when(th.updated),
    summary ? el('span', { class: 'full muted small' }, summary) : null,
  ];
  return collapsible('t:' + th.id, head, () => renderThreadBody(th, taskIds, ids), 'thread/' + encodeURIComponent(th.id), th.explicit ? '' : 'inferred');
}
function renderThreadBody(th, taskIds, ids) {
  const parts = [];
  if (th.result) parts.push(el('div', { class: 'result' }, markdown(th.result)));
  parts.push(el('div', { class: 'flowwrap' }, flowSvg(ids, false)));
  if (S.dev) {
    parts.push(el('div', { class: 'line small' },
      (th.targets || []).length ? el('span', { class: 'line' }, el('span', { class: 'muted' }, 'targets'), th.targets.map(targetLink)) : null,
      (th.links || []).length ? el('span', { class: 'line' }, el('span', { class: 'muted' }, 'links'), th.links.map(linkTag)) : null,
      el('span', { class: 'muted' }, 'started ', when(th.started)), el('span', { class: 'id faint', title: 'thread id' }, th.id)));
    const asks = asksForThread(S.data.asks, th);
    if (asks.length) parts.push(el('div', { class: 'line small' }, el('span', { class: 'muted' }, 'asks'), asks.map(renderAsk)));
  } else if ((th.links || []).length) parts.push(el('div', { class: 'line small' }, th.links.map(linkTag)));
  const tasks = taskIds.map(task).filter(Boolean).sort((a, b) => a.id.localeCompare(b.id));
  parts.push(el('div', { class: 'trows' }, tasks.length ? tasks.map(S.dev ? renderTaskRow : renderTaskLine) : empty('No task in this thread matches.')));
  return parts;
}
function renderTaskLine(t) {
  const words = statusWords(t, inboxItem(t.id), t.decide);
  const why = () => { S.pane = { kind: 'task', id: t.id }; S.details.delete(t.id); setHash('task/' + encodeURIComponent(t.id)); render(); };
  return listRow(t, 'k:' + t.id, { text: t.purpose || '' }, [
    el('span', { class: `chip ${words.tone === 'warn' ? 'warnchip' : ''}` }, words.text),
    isDeciding(t.id) ? el('span', { class: 'spin' }) : null,
    el('a', { class: 'why small', href: '#task/' + encodeURIComponent(t.id), title: 'what happened and why', onclick: e => { e.preventDefault(); why(); } }, 'Why')]);
}
function renderAsk(a) {
  const rounds = (a.rounds || []).length;
  return el('span', { class: 'line', title: a.prompt || '' },
    pill(a.status || '?'), el('span', {}, truncate(a.prompt, 110)),
    typeof a.cost_usd === 'number' ? el('span', { class: 'muted' }, money(a.cost_usd)) : null,
    rounds ? el('span', { class: 'muted' }, plural(rounds, 'round')) : null,
    a.waiting_on ? el('span', { class: 'sugg' }, `waiting on ${a.waiting_on}`) : null,
    (a.pieces || []).map(p => tag(`${p.name || '?'} ${p.status || ''}`)), when(a.started));
}
function renderTaskRow(t) {
  const key = 'k:' + t.id, open = S.open.has(key), sel = selectedTask() === t.id;
  const n = (t.suggestions || []).length;
  const row = el('div', { class: `trow ${sel ? 'sel' : ''} ${open ? 'open' : ''}`, 'data-key': key, role: 'button', tabindex: '0', title: t.purpose || '',
    onclick: e => { if (e.target.closest('a, button')) return; if (e.target.closest('.chev')) { toggle(key); return; } selectTask(t.id); },
    onkeydown: e => { if (e.key === 'Enter') selectTask(t.id); } },
    el('span', { class: 'chev', title: open ? 'hide details' : 'show details' }), el('span', { class: 'id' }, t.id), pill(t.status || 'draft'),
    t.kind && t.kind !== 'task' ? tag(t.kind, 'accent') : null,
    el('span', { class: 'purpose muted' }, t.purpose || ''),
    n ? el('span', { class: 'suggn', title: t.suggestions.map(s => s.text).join('\n') }, `${n} !`) : null,
    (t.meta_errors || []).length ? el('span', { class: 'err small', title: t.meta_errors.join('; ') }, 'meta') : null,
    when(t.created));
  if (!open) return row;
  return el('div', { class: 'row open', style: 'border:0;margin:0' }, row, el('div', { class: 'tdetail small' },
    renderChips(t),
    (t.suggestions || []).map(s => el('div', { class: 'sugg' }, tag(s.kind || 'note', 'warn'), s.text || '')),
    t.outcome ? el('div', { class: `note ${tone(t.outcome.state)}` }, `${t.outcome.state || 'closed'}: ${t.outcome.note || ''}`) : null,
    !t.outcome && t.abandoned_reason ? el('div', { class: 'note' }, 'abandoned: ', t.abandoned_reason) : null,
    el('div', { class: 'line muted' }, statsText(t.stats, t.changes_prod), t.author ? `by ${t.author}` : null)));
}
export function renderChips(t) {
  const chips = [];
  if ((t.targets || []).length) chips.push(el('span', { class: 'muted small' }, 'targets'), t.targets.map(targetLink));
  if ((t.uses || []).length) chips.push(el('span', { class: 'muted small' }, 'uses'), t.uses.map(scriptLink));
  if ((t.steps || []).length) chips.push(el('span', { class: 'muted small' }, 'steps'), t.steps.map(s => (s ? taskLink(s) : null)));
  if (t.superseded_by) chips.push(el('span', { class: 'muted small' }, 'superseded by'), taskLink(t.superseded_by));
  if ((t.links || []).length) chips.push(t.links.map(linkTag));
  return chips.length ? el('div', { class: 'line' }, chips) : null;
}
/* history: every story, as a list or a timeline, narrowed to one machine or one script */
export function keptByHistory(t) {
  if (!t) return false;
  const h = S.hist;
  return (!h.machine || (t.targets || []).some(x => x === h.machine || x.startsWith(h.machine + '/'))) && (!h.script || t.id === h.script || (t.uses || []).includes(h.script));
}
export function renderHistory() {
  const h = S.hist, machines = targetIndex(S.data), scripts = Object.values(S.data.scripts || {}).sort((a, b) => String(a.id).localeCompare(String(b.id)));
  const pick = (label, value, options, onchange) => el('label', { class: 'histpick' }, el('span', { class: 'muted small' }, label),
    el('select', { onchange: e => onchange(e.target.value) }, el('option', { value: '' }, 'all'), options.map(([v, l]) => el('option', { value: v, selected: v === value }, l))));
  const bar = el('div', { class: 'histbar' },
    el('span', { class: 'tiers' }, [['list', 'Stories'], ['timeline', 'Timeline']].map(([v, l]) => el('button', { type: 'button', class: h.mode === v ? 'on' : '', 'aria-pressed': String(h.mode === v), onclick: () => { h.mode = v; render(); } }, l))),
    el('label', { class: 'histpick' }, el('span', { class: 'muted small' }, 'sort'),
      el('select', { onchange: e => { h.sort = e.target.value; saved('histSort', h.sort); render(); } }, HIST_SORTS.map(([v, l]) => el('option', { value: v, selected: v === h.sort }, l)))),
    pick('machine', h.machine, machines.map(g => [g.name, `${g.name} (${g.entries.length} runs)`]), v => historyFilter({ machine: v })),
    pick('script', h.script, scripts.map(x => [x.id, scriptName(x.id)]), v => historyFilter({ script: v })),
    h.machine || h.script ? btn('clear', () => historyFilter({ machine: '', script: '' }), 'sm') : null);
  const about = [];
  if (h.machine) { const g = machines.find(x => x.name === h.machine); if (g) about.push(el('div', { class: 'block histabout' }, el('div', { class: 'title mono' }, g.name), renderTargetBody(g, S.hist.allRuns ? 0 : 6))); }
  if (h.script) { const x = (S.data.scripts || {})[h.script]; if (x) about.push(el('div', { class: 'block histabout' }, el('div', { class: 'title' }, scriptName(x.id), ' ', x.lint ? pill(x.lint) : null), el('div', { class: 'muted small' }, x.purpose || ''), renderScriptBody(x, task(x.id)))); }
  return el('div', {}, bar, about, h.mode === 'timeline' ? renderMap() : renderThreads());
}
function renderTargetBody(g, limit) {
  const parts = [];
  const silent = g.declared.filter(id => !g.entries.some(e => e.run.task_id === id));
  if (silent.length) parts.push(el('div', { class: 'line small' }, el('span', { class: 'muted' }, 'declared but never run here'), silent.map(taskLink)));
  if (!g.entries.length) { parts.push(el('div', { class: 'muted' }, 'No run has touched this target.')); return parts; }
  const rows = g.entries.slice(0, limit || g.entries.length).map(e => el('tr', { class: 'click', title: `open the log of ${e.run.run_id}`, onclick: ev => { if (ev.target.closest('a')) return; openLog(e.run.run_id, null); } },
    el('td', {}, when(e.time)), el('td', {}, taskLink(e.run.task_id || '?')), el('td', {}, modeTag(e.run.mode)),
    el('td', {}, pill(e.run.status)), el('td', {}, pill(e.tstatus)),
    el('td', { class: 'small' }, e.run.last_error ? el('span', { class: 'err' }, e.run.last_error) : (e.run.results || [])[0] || '')));
  parts.push(el('div', { class: 'scroll' }, el('table', { class: 'tbl' },
    el('thead', {}, el('tr', {}, ['when', 'task', 'mode', 'run', 'target', 'note'].map(h => el('th', {}, h)))), el('tbody', {}, rows))));
  if (limit && g.entries.length > limit) parts.push(btn(`show all ${g.entries.length} runs`, () => { S.hist.allRuns = true; render(); }, 'sm'));
  return parts;
}
function renderScriptBody(s, t) {
  const parts = [];
  if (t) parts.push(el('div', { class: 'line' }, fileButtons(t), t.path ? el('span', { class: 'faint small mono' }, t.path) : null));
  const users = (s.used_by || []).map(task).filter(Boolean);
  parts.push(el('h4', {}, `Used by (${users.length})`));
  parts.push(users.length ? users.map(u => el('div', { class: 'line' }, taskLink(u.id), pill(u.status), prodMarker(u), u.area ? tag(u.area) : null,
    el('span', { class: 'muted small' }, statsText(u.stats, u.changes_prod)),
    u.thread ? el('a', { class: 'small', href: '#task/' + encodeURIComponent(u.id), onclick: e => { e.preventDefault(); goToTask(u.id); } }, 'open in thread') : null,
    el('span', { class: 'full small muted' }, u.purpose || ''))) : el('div', { class: 'muted' }, 'No task uses this script yet.'));
  return parts;
}
