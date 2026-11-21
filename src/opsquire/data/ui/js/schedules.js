/* Schedules: a plan as one locked item with one button, and a progress bar of its steps. */
import { activeJobFor, inbox, run, S, task } from './state.js';
import { duration, parseTime, relTime } from './time.js';
import { plural, shortName, truncate } from './records.js';
import { btn, el, svg, toast } from './dom.js';
import { areaTag, argsTag, modeTag, when } from './widgets.js';
import { post } from './api.js';
import { selectTask, taskLink } from './nav.js';
import { render } from './render.js';
import { arm, disarm, openJob, splitRun, startRun } from './jobs.js';
import { abandonForm, abandonItem } from './inbox.js';
import { refresh } from './live.js';

const LOCK_SVG = () => svg('svg', { viewBox: '0 0 16 16', class: 'lockico', 'aria-hidden': 'true' },
  svg('rect', { x: '3', y: '7', width: '10', height: '7', rx: '1.5', fill: 'currentColor' }),
  svg('path', { d: 'M5 7V5a3 3 0 0 1 6 0v2', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6' }));
const SEG_WORD = { done: 'done', prerun: 'pre-ran: waits for your confirm', stopped: 'stopped here', now: 'running now', ready: 'not run yet' };
const MODE_WORD = { dry: 'pre-running', apply: 'applying', read: 'reading', verify: 'checking' };
/* one segment per step, filled left to right: done steps green, pre-ran ones yellow, the step running now filling in
   its own kind's colour (dry, apply, read, check), a stopped one red, the rest empty */
function progressBar(segs) {
  const done = segs.filter(x => x.state === 'done').length;
  return el('div', { class: 'pbar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(segs.length), 'aria-valuenow': String(done), 'aria-label': `${done} of ${segs.length} steps done` },
    segs.map((g, i) => el('span', { class: 'seg', title: `step ${i + 1}: ${shortName(g.name || '')} - ${g.state === 'now' ? MODE_WORD[g.mode] || 'running' : SEG_WORD[g.state] || g.state}` },
      el('span', Object.assign({ class: `fill ${g.state} ${g.state === 'now' ? 'k-' + (g.mode || 'dry') : ''}` },
        g.state === 'now' ? { 'data-start': g.start || '', 'data-expect': g.expect || '', 'data-done': g.done ?? '', 'data-total': g.total ?? '', style: `width:${Math.round(stepFraction(g) * 100)}%` } : {})))));
}
/* how far the step running now is: what its script last reported, else its elapsed time against how long its
   past runs took (held below the end until it finishes), else a curve that keeps moving without promising an end */
function stepFraction(g) {
  if (Number(g.total) > 0) return Math.max(0, Math.min(1, Number(g.done) / Number(g.total)));
  const t = parseTime(g.start); if (t === null) return 0;
  const el_s = Math.max(0, (Date.now() - t) / 1000);
  if (Number(g.expect) > 0) return Math.min(0.95, el_s / Number(g.expect));
  return 0.9 * (1 - Math.exp(-el_s / 60));
}
/* seconds the step running now still needs: from its own pace when its script reports progress, else from how
   long its past runs took; null when neither says */
function stepLeft(g) {
  const t = parseTime(g.start), el_s = t === null ? 0 : Math.max(0, (Date.now() - t) / 1000);
  if (Number(g.total) > 0 && Number(g.done) > 0) return el_s * (Number(g.total) - Number(g.done)) / Number(g.done);
  if (Number(g.expect) > 0) return Math.max(Number(g.expect) - el_s, 0);
  return null;
}
export function tickBars() {
  for (const f of document.querySelectorAll('.pbar .fill.now')) {
    const g = { start: f.dataset.start, expect: f.dataset.expect, done: f.dataset.done, total: f.dataset.total };
    f.style.width = `${Math.round(stepFraction(g) * 100)}%`;
  }
}
/* a running plan's bar from its record and its steps' run history */
export function runProgress(r) {
  const total = r.total || (r.steps || []).length; if (!total) return null;
  const plan = task(r.task_id) || {}, live = r.live || { steps: [], now: null };
  const byI = {}; (r.steps || []).forEach((s, k) => { byI[s.i || k + 1] = s; });
  const segs = [];
  for (let i = 1; i <= total; i++) {
    const s = byI[i], info = live.steps[i - 1] || {}, name = (s && s.task) || (plan.steps || [])[i - 1] || '';
    if (r.status === 'running' && r.current === i && !s) {
      const now = live.now || {}, pr = now.progress || {};
      segs.push({ name, state: 'now', mode: now.mode || info.mode, start: now.started, expect: info.expect_s, done: pr.done, total: pr.total });
    } else segs.push({ name, state: !s || s.status === 'skipped' ? 'ready' : s.status === 'ok' ? 'done' : 'stopped' });
  }
  const done = segs.filter(x => x.state === 'done').length, cur = segs.findIndex(x => x.state === 'now');
  const left = cur < 0 ? null : live.steps.slice(cur + 1).reduce((a, x) => a == null || x.expect_s == null ? null : a + x.expect_s, stepLeft(segs[cur]));
  const pr = cur >= 0 && segs[cur].total ? ` · ${segs[cur].done}/${segs[cur].total}` : '';
  return el('div', { class: 'pwrap' }, progressBar(segs),
    el('div', { class: 'pline' }, el('b', {}, `${done} of ${plural(total, 'step')} done`),
      cur >= 0 ? el('span', {}, `${MODE_WORD[segs[cur].mode] || 'running'} step ${cur + 1}: ${shortName(segs[cur].name)}${pr}`) : null,
      cur >= 0 && left ? el('span', { class: 'faint', title: 'from how long these steps took before' }, `about ${duration(left)} left`)
        : cur >= 0 && stepLeft(segs[cur]) ? el('span', { class: 'faint', title: 'this step, from its own pace; the later steps have no run history yet' }, `this step about ${duration(stepLeft(segs[cur]))} left`) : null,
      r.status !== 'running' && r.status !== 'ok' && r.last_error ? el('span', { class: 'err' }, r.last_error) : null,
      run(r.prerun) ? el('span', { class: 'faint' }, 'runs the code pre-run ', when(run(r.prerun).started), ' saw') : null));
}
function scheduleStepRow(st) {
  const role = st.changes_prod ? (st.verify ? 'changes, then checks' : 'changes') : 'reads';
  const word = { done: '✓ done', stopped: '✗ stopped', 'pre-ran': 'pre-ran', ready: 'not run yet' }[st.state] || st.state;
  const cls = st.state === 'pre-ran' ? 'prerun' : st.state;
  return el('div', { class: 'sstep', role: 'button', tabindex: '0', title: `step ${st.i}: runs only as part of this schedule. Open ${st.task_id}`,
    onclick: e => { if (e.target.closest('a')) return; selectTask(st.task_id); }, onkeydown: e => { if (e.key === 'Enter') selectTask(st.task_id); } },
    el('span', { class: 'stepno' }, String(st.i)), el('span', { class: 'purpose', title: st.purpose || '' }, st.purpose || shortName(st.task_id)),
    argsTag(st.args), el('span', { class: 'role' }, role), el('span', { class: `st ${cls}` }, word));
}
export function renderSchedule(it) {
  const id = it.task_id, t = task(id), sc = it.schedule, steps = it.steps || [];
  const left = steps.filter(st => st.state !== 'done').length;
  const stop = sc.stopped;
  const notes = [];
  if (sc.blocked) notes.push(el('span', { class: 'warnnote', title: sc.blocked.reason || '' }, `${sc.blocked.mode === 'apply' ? 'apply' : 'pre-run'} did not start: ${truncate(sc.blocked.reason || 'blocked', 140)}`));
  if (stop && it.next === 'dry') notes.push(el('span', { class: 'warnnote', title: stop.reason || '' }, `stopped at step ${stop.at}: ${truncate(stop.reason || stop.status, 140)}`));
  if (sc.changed_since_prerun.length) notes.push(el('span', { class: 'warnnote', title: sc.changed_since_prerun.join('\n') },
    `${plural(sc.changed_since_prerun.length, 'file')} changed since the pre-run: not in this run until you pre-run again`));
  if (it.stale) notes.push(el('span', { class: 'warnnote', title: it.stale }, `waiting ${Math.floor(it.waiting_days)} days: run, supersede or abandon`));
  const what = it.next === 'apply'
    ? `Pre-ran ${relTime(sc.prerun.started)}. Confirm runs exactly that code${sc.resume_at ? `, from step ${sc.resume_at}` : ''}: ${plural(left, 'step')} in order, each one's check right after it.`
    : sc.resume_at ? `Steps before ${sc.resume_at} are done. The pre-run dry-runs the ${plural(left, 'step')} left: nothing changes.`
      : `The pre-run dry-runs every step in order: nothing changes. Then this locks for your confirm.`;
  const key = id + ':apply', armed = S.confirm === key, active = activeJobFor(id);
  let act;
  if (active) act = btn(active.status, () => openJob(active.job_id), 'sm', { title: 'a run of this schedule is going; open it' });
  else if (it.next === 'apply') {
    act = el('div', { class: 'line', style: 'flex-direction:column;align-items:stretch;gap:6px' },
      el('button', { type: 'button', class: `runbtn k-apply apply ${armed ? 'armed' : 'locked'}`, title: armed ? 'click again within 5 s to apply' : 'scheduled from the pre-run: click to unlock, then confirm',
        onclick: () => { if (armed) { disarm(); startRun(id, 'apply'); } else arm(key); } }, armed ? 'Confirm apply' : [LOCK_SVG(), 'Scheduled']),
      sc.changed_since_prerun.length ? btn('Pre-run again', () => startRun(id, 'dry'), 'sm', { title: 'dry-run the steps left with the files as they are now' }) : null);
  } else act = btn(sc.resume_at ? `Pre-run from step ${sc.resume_at}` : 'Pre-run', () => startRun(id, 'dry'), 'runbtn k-dry', { title: 'dry run of every step left: nothing changes' });
  return el('div', { class: `card sched k-${it.next}`, 'data-key': 'i:' + id },
    el('span', { class: 'linkgap', 'aria-hidden': 'true' }),
    el('div', { class: 'card-main' },
      el('div', { class: 'card-title' }, el('span', { title: it.purpose || '' }, it.purpose || shortName(id))),
      el('div', { class: 'card-meta' }, el('span', { class: 'schedtag', title: 'a schedule: its steps run together, in this order, from one pre-run' }, LOCK_SVG(), `schedule · ${plural(steps.length, 'step')}`),
        areaTag(t || { id }), taskLink(id), it.handed ? el('span', { class: 'muted small', title: it.handed.why || '' }, truncate(it.handed.why || `handed over by ${it.handed.by || 'an agent'}`, 90)) : null),
      progressBar(steps.map(st => ({ name: st.task_id, state: st.state === 'pre-ran' ? 'prerun' : st.state }))),
      el('div', { class: 'pline' }, el('b', {}, `${sc.done.length} of ${plural(steps.length, 'step')} done`), el('span', {}, what)),
      notes.length ? el('div', { class: 'card-notes' }, notes) : null,
      el('div', { class: 'ssteps' }, steps.map(scheduleStepRow))),
    el('div', { class: 'card-act' }, active ? act : splitRun(act, [abandonItem(id)], 'i:' + id), abandonForm(id)));
}
/* linking inbox items into a schedule: tick them, order them, link */
export function linkPick(it) {
  if (it.schedule || (it.steps || []).length) return null;
  const on = S.link.includes(it.task_id);
  return el('input', { type: 'checkbox', class: 'linkpick', checked: on, title: 'pick to link into a schedule', 'aria-label': 'link into a schedule',
    onclick: e => e.stopPropagation(), onchange: () => { S.link = on ? S.link.filter(x => x !== it.task_id) : [...S.link, it.task_id]; render(); } });
}
export function renderLinkBar() {
  const ids = S.link.filter(id => inbox().some(i => i.task_id === id));
  if (ids.length !== S.link.length) S.link = ids;
  if (!ids.length) return null;
  const move = (i, d) => { const a = [...S.link]; [a[i], a[i + d]] = [a[i + d], a[i]]; S.link = a; render(); };
  const purpose = el('input', { id: 'linkpurpose', placeholder: 'What the schedule does (optional)', value: S.linkPurpose || '', oninput: e => { S.linkPurpose = e.target.value; } });
  const go = async () => {
    try { const r = await post('/api/schedule', { tasks: S.link, purpose: S.linkPurpose || '' }); toast(`linked into ${shortName(r.id)}`); S.link = []; S.linkPurpose = ''; refresh(); }
    catch (e) { toast(`link failed: ${e.message}`); }
  };
  return el('div', { class: 'linkbar', role: 'region', 'aria-label': 'link into a schedule' },
    el('b', {}, 'Link into one schedule, in this order'),
    el('ol', {}, ids.map((id, i) => { const it = inbox().find(x => x.task_id === id) || {};
      return el('li', {}, el('span', { class: 'stepno' }, String(i + 1)), el('span', { class: 'lrowtxt' }, it.purpose || shortName(id)), modeTag(it.next),
        btn('↑', () => move(i, -1), 'sm', { disabled: i === 0, 'aria-label': 'earlier' }), btn('↓', () => move(i, 1), 'sm', { disabled: i === ids.length - 1, 'aria-label': 'later' }),
        btn('✕', () => { S.link = S.link.filter(x => x !== id); render(); }, 'sm', { 'aria-label': 'remove' })); })),
    el('div', { class: 'lacts' }, purpose,
      el('button', { type: 'button', class: 'primary', disabled: ids.length < 2, title: ids.length < 2 ? 'pick at least two items' : 'make one schedule: one pre-run, one confirm', onclick: go }, ids.length < 2 ? 'Pick one more' : `Link ${ids.length} into a schedule`),
      btn('Clear', () => { S.link = []; render(); }, 'sm')));
}
