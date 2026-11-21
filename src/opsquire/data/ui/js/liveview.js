/* The Live tab: one row per run going now, with the machines it holds; locks that outlived their run on their own. */
import { run, S } from './state.js';
import { plural } from './records.js';
import { btn, el } from './dom.js';
import { empty, modeTag, pill, sec, tag, when } from './widgets.js';
import { taskLink } from './nav.js';
import { openLog } from './pane.js';
import { render } from './render.js';

const SHOWN = 3;

/* the runs going now, a plan's steps left to their plan */
export function liveRuns() {
  const d = S.data, ids = new Set(d.running || []);
  return (d.running || []).map(run).filter(r => r && !(r.plan_run && ids.has(r.plan_run)));
}
/* locks by the run that holds them, in the order they were taken */
export function locksByRun() {
  const groups = new Map();
  for (const l of S.data.locks || []) {
    const key = l.run_id || `${l.task_id}|${l.since}`;
    if (!groups.has(key)) groups.set(key, { run_id: l.run_id, task_id: l.task_id, mode: l.mode, user: l.user, since: l.since, locks: [] });
    groups.get(key).locks.push(l);
  }
  return [...groups.values()];
}
function machines(key, g) {
  if (!g || !g.locks.length) return null;
  const open = S.liveOpen.has(key), all = g.locks.map(l => tag(l.target || '?', l.alive ? '' : 'warn'));
  const folded = all.length > SHOWN && !open;
  const toggle = all.length > SHOWN ? btn(open ? 'fewer' : `${plural(all.length, 'machine')} \u25b8`, () => {
    if (open) S.liveOpen.delete(key); else S.liveOpen.add(key);
    render();
  }, 'link sm') : null;
  return el('span', { class: 'targets', title: g.locks.map(l => l.target).join(', ') }, folded ? toggle : [all, toggle], el('span', { class: 'faint small' }, g.mode));
}
function runRow(r, g) {
  const step = r.total && r.current ? el('span', { class: 'muted small' }, `step ${r.current} of ${r.total}`) : null;
  const state = r.waiting ? 'waiting' : r.in_line_for ? 'in line' : 'running';
  const note = r.waiting ? el('span', { class: 'warnnote' }, `asks: ${r.waiting.question}`)
    : r.in_line_for ? el('span', { class: 'muted small' }, 'behind ', r.in_line_for, ', ', when(r.in_line_since)) : null;
  return el('div', { class: 'liverow' }, pill(state), taskLink(r.task_id || '?'), modeTag(r.mode), step, machines(r.run_id, g), note,
    el('span', { class: 'grow' }), r.user ? el('span', { class: 'muted small' }, r.user) : null, when(r.started),
    btn('tail', () => openLog(r.run_id, null), 'sm'));
}
/* a lock whose run record is not among the runs going now: dead, or held by a run this console cannot see */
function lockRow(g) {
  const dead = g.locks.some(l => !l.alive);
  return el('div', { class: `liverow ${dead ? 'dead' : ''}` }, pill(dead ? 'dead' : 'running'), taskLink(g.task_id || '?'),
    el('span', { class: 'muted small mono' }, g.run_id || ''), machines(g.run_id || g.task_id, g),
    el('span', { class: 'grow' }), g.user ? el('span', { class: 'muted small' }, g.user) : null, when(g.since));
}
export function renderLiveView() {
  const runs = liveRuns(), groups = locksByRun(), byRun = new Map(groups.map(g => [g.run_id, g]));
  const seen = new Set(runs.map(r => r.run_id)), alone = groups.filter(g => !seen.has(g.run_id));
  const dead = alone.filter(g => g.locks.some(l => !l.alive)), other = alone.filter(g => !dead.includes(g));
  return el('div', { class: 'liveview' },
    sec('Running now', runs.length + other.length ? String(runs.length + other.length) : null),
    el('div', { class: 'block pad' }, runs.length + other.length ? [...runs.map(r => runRow(r, byRun.get(r.run_id))), ...other.map(lockRow)] : empty('Nothing running.')),
    dead.length ? [sec('Dead locks', String(dead.length), el('span', { class: 'faint small' }, 'a lock that outlived its run; the next run on these machines reclaims it')),
      el('div', { class: 'block pad' }, dead.map(lockRow))] : null);
}
