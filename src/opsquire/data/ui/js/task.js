/* A task in the pane: what happened, how to run it, its runs, closing it. */
import { inboxItem, isDeciding, jobs, lastJudgeFor, run, S } from './state.js';
import {
  agentChips, codeName, lastRunOf, nextLabel, plural, runCommand, shortName, statsText, statusWords, TICK,
  tone, usageChips,
} from './records.js';
import { btn, copyText, el, toast } from './dom.js';
import { chipsRow, modeTag, pill, prodMarker, tag, tick, when } from './widgets.js';
import { api, post } from './api.js';
import { focusKey, taskLink } from './nav.js';
import { render } from './render.js';
import { jobLabel, openJob, runActions } from './jobs.js';
import { decideButton, judgeLine } from './judge.js';
import { renderChips } from './history.js';
import { openFile, openLog } from './pane.js';
import { refresh } from './live.js';

/* the files a run of the task executes, as they are now, against its last run; each opens in the operator's editor */
function loadCode(id) {
  const c = S.code.get(id);
  if (c && (c.busy || Date.now() - c.at < 10000)) return;
  S.code.set(id, { ...(c || {}), busy: true, at: Date.now() });
  api(`/api/code?task=${encodeURIComponent(id)}`)
    .then(r => { S.code.set(id, { data: r, at: Date.now() }); }, e => { S.code.set(id, { error: e.message, at: Date.now() }); })
    .finally(render);
}
function renderCode(t) {
  loadCode(t.id);
  const c = S.code.get(t.id), d = c && c.data;
  const head = el('h4', {}, 'Code', el('span', { class: 'faint small' }, d && d.since ? ' against its last run, click to open' : ' click to open'));
  if (!d) return el('div', { class: 'code' }, head, el('div', { class: 'faint small' }, c && c.error ? `Cannot list the code: ${c.error}` : 'Reading the files…'));
  return el('div', { class: 'code' }, head,
    d.changed ? el('div', { class: 'small' }, el('b', {}, 'Changed: '), d.changed) : null,
    el('div', { class: 'codefiles' }, d.files.map(f => el('div', { class: 'codefile' },
      el('span', { class: `cstate ${f.state}` }, f.state),
      el('a', { href: f.url, title: `open ${f.path} in your editor` }, codeName(f.name, t.id)),
      el('span', { class: 'muted small mono' }, f.state === 'changed' ? `+${f.added} \u2212${f.removed}` : '')))));
}
export function fileButtons(t) {
  const b = [];
  if (t.has_design) b.push(btn(t.has_result ? 'design.md (has result)' : 'design.md', () => openFile(t, 'design.md'), 'sm'));
  if (t.entry) b.push(btn(t.entry, () => openFile(t, t.entry), 'sm'));
  if (t.verify) b.push(btn(t.verify, () => openFile(t, t.verify), 'sm', { title: 'the read-only check that the change is live' }));
  b.push(btn('meta.json', () => openFile(t, 'meta.json'), 'sm'));
  if (t.kind !== 'plan') b.push(btn('tests', () => openFile(t, 'test_main.py'), 'sm'));
  return b;
}
export function renderTaskPane(t) {
  const parts = [], closable = t.kind !== 'general' && !t.archived;
  const it = inboxItem(t.id), words = statusWords(t, it, t.decide);
  const threadLink = t.thread ? el('a', { class: 'small', href: '#thread/' + encodeURIComponent(t.thread), onclick: e => { e.preventDefault(); focusKey('history', 't:' + t.thread, 'thread/' + encodeURIComponent(t.thread)); } }, 'thread') : null;
  if (!S.dev) {
    parts.push(el('div', { class: 'pane-name' }, tick(t), ' ', shortName(t.id)));
    parts.push(el('div', { class: `status-words ${words.tone}` }, isDeciding(t.id) ? [el('span', { class: 'spin' }), 'Deciding…'] : words.text));
    parts.push(el('div', {}, t.purpose || el('span', { class: 'faint' }, 'no purpose')));
    parts.push(el('div', { class: 'line small', style: 'margin-top:4px' }, t.changes_prod ? tag('changes production', 'warn') : null, el('span', { class: 'muted' }, 'created ', when(t.created)), threadLink));
    parts.push(el('h4', {}, 'What happened'));
    parts.push(renderWhatHappened(t));
    parts.push(renderCode(t));
    const chips = agentChips(t.agents); if (chips.length) parts.push(chipsRow(chips));
    for (const s of t.suggestions || []) parts.push(el('div', { class: 'sugg small' }, s.text || ''));
    if ((t.meta_errors || []).length) parts.push(el('div', { class: 'err small' }, 'The task record has a problem: ', t.meta_errors.join('; ')));
    const open = S.details.has(t.id), actions = [];
    if (closable) {
      if (it) actions.push(el('span', { class: `next ${it.next === 'apply' ? 'apply' : ''}` }, nextLabel(it.next)));
      actions.push(runActions(t));
      if (!t.settled && t.status !== 'draft' && !(it && it.next === 'verify')) { const db = decideButton(t.id); if (db) actions.push(db); }
    }
    actions.push(btn(open ? 'Hide details' : 'Show details', () => { if (open) S.details.delete(t.id); else S.details.add(t.id); render(); }, 'sm'));
    parts.push(el('div', { class: 'actions' }, actions));
    if (!open) return el('div', {}, parts);
    parts.push(el('div', { class: 'details' }, renderTaskDetails(t, closable)));
    return el('div', {}, parts);
  }
  parts.push(el('div', {}, t.purpose || el('span', { class: 'faint' }, 'no purpose')));
  parts.push(el('div', { class: 'line small', style: 'margin-top:6px' }, t.kind && t.kind !== 'task' ? tag(t.kind, 'accent') : null, prodMarker(t), t.area ? tag(t.area) : null,
    t.settled ? el('span', { class: 'tick done', title: 'settled' }, TICK.done) : null,
    el('span', { class: 'muted' }, 'created ', when(t.created)), t.author ? el('span', { class: 'muted' }, `by ${t.author}`) : null, threadLink));
  const chips = renderChips(t); if (chips) parts.push(chips);
  parts.push(el('div', { class: 'line small' }, el('span', { class: 'muted' }, statsText(t.stats, t.changes_prod)), chipsRow(agentChips(t.agents))));
  if ((t.meta_errors || []).length) parts.push(el('div', { class: 'err small' }, 'meta.json: ', t.meta_errors.join('; ')));
  for (const s of t.suggestions || []) parts.push(el('div', { class: 'sugg small' }, tag(s.kind || 'note', 'warn'), s.text || ''));
  if (t.outcome) parts.push(el('div', { class: `note small ${tone(t.outcome.state)}` }, `${t.outcome.state || 'closed'}: ${t.outcome.note || ''}`,
    el('span', { class: 'faint' }, ` (${t.outcome.closed_by || '?'}, `, when(t.outcome.closed), t.outcome.run ? `, run ${t.outcome.run})` : ')')));
  else if (t.abandoned_reason) parts.push(el('div', { class: 'note small' }, 'abandoned: ', t.abandoned_reason));
  const jl = judgeLine(t.id); if (jl) parts.push(el('div', { class: 'line small' }, el('span', { class: 'muted' }, 'judge'), jl));
  parts.push(renderCode(t));
  parts.push(renderTaskDetails(t, closable));
  return el('div', {}, parts);
}
function renderWhatHappened(t) {
  const last = lastRunOf(t, S.data.runs), lines = [];
  if (t.outcome) lines.push(el('div', {}, el('b', {}, t.outcome.state === 'abandoned' ? 'Abandoned' : t.outcome.state === 'partial' ? 'Partly done' : 'Done'), `: ${t.outcome.note || ''}`),
    el('div', { class: 'faint small' }, `closed by ${t.outcome.closed_by || '?'}, `, when(t.outcome.closed)));
  else if (t.abandoned_reason) lines.push(el('div', {}, el('b', {}, 'Abandoned'), `: ${t.abandoned_reason}`));
  else if (t.superseded_by) lines.push(el('div', {}, 'Replaced by ', taskLink(t.superseded_by)));
  if (last) {
    const what = last.status === 'ok' ? 'went ok' : last.status === 'running' ? 'is running' : last.status;
    lines.push(el('div', { class: t.outcome ? 'small muted' : '' }, `Last ${last.mode === 'apply' ? 'apply' : 'check'} ${what}, `, when(last.started),
      ' ', btn('log', () => openLog(last.run_id, { kind: 'task', id: t.id }), 'sm')));
    if (last.last_error) lines.push(el('div', { class: 'err' }, last.last_error));
    if ((last.results || []).length) lines.push(el('ul', {}, last.results.map(x => el('li', {}, x))));
    const uc = usageChips(last); if (uc.length) lines.push(chipsRow(uc));
  } else if (!t.outcome) lines.push(el('div', { class: 'muted' }, t.status === 'draft' ? 'Still a draft: the agent has not finished preparing it.' : 'Not run yet.'));
  const jl = judgeLine(t.id); if (jl) lines.push(el('div', { class: 'small' }, el('span', { class: 'muted' }, 'The judge: '), jl));
  return el('div', { class: 'what' }, lines);
}
function renderTaskDetails(t, closable) {
  const parts = [];
  if (!S.dev) parts.push(el('div', { class: 'line small' }, el('span', { class: 'id' }, t.id), btn('copy', () => copyText(t.id), 'sm'), pill(t.status || 'draft'), t.kind && t.kind !== 'task' ? tag(t.kind, 'accent') : null,
    t.author ? el('span', { class: 'muted' }, `by ${t.author}`) : null));
  if (!S.dev) { const chips = renderChips(t); if (chips) parts.push(chips); parts.push(el('div', { class: 'muted small' }, statsText(t.stats, t.changes_prod))); }
  if (closable) {
    parts.push(el('h4', {}, 'Run it'));
    const modes = t.changes_prod ? ['dry', 'apply'] : ['read']; if (t.verify) modes.push('verify');
    if (S.dev) parts.push(el('div', { class: 'actions' }, runActions(t)));
    parts.push(el('div', { class: 'cmds' }, modes.map(m => { const c = runCommand(t.id, m); return el('span', { class: 'cmd' }, el('span', {}, c), btn('copy', () => copyText(c), 'sm', { title: 'copy to clipboard' })); })));
    const js = jobs().filter(j => j.task_id === t.id);
    if (js.length) parts.push(el('div', { class: 'line small' }, el('span', { class: 'muted' }, 'jobs'), js.map(j => btn(`${j.mode} ${jobLabel(j)}`, () => openJob(j.job_id), 'sm'))));
    const judge = lastJudgeFor(t.id), text = judge && S.judgeText.get(judge.job_id);
    if (text) parts.push(el('h4', {}, 'The judge’s reasoning'), el('pre', { class: 'reason small' }, text));
  }
  parts.push(el('h4', {}, 'Files'));
  const fb = fileButtons(t);
  if (closable) fb.push(btn('Close...', () => { S.closeForm = S.closeForm && S.closeForm.task === t.id ? null : { task: t.id, outcome: 'done', note: '', run: '', busy: false, error: null, result: null }; render(); }, 'sm'));
  parts.push(el('div', { class: 'line' }, fb));
  if (t.path) parts.push(el('div', { class: 'faint small mono', title: 'folder' }, t.path));
  if (S.closeForm && S.closeForm.task === t.id) parts.push(renderCloseForm(t));
  const runs = (t.runs || []).map(run).filter(Boolean).reverse();
  parts.push(el('h4', {}, `Runs (${runs.length})`));
  parts.push(runs.length ? el('div', { class: 'runs' }, runs.map(r => renderRun(r, t))) : el('div', { class: 'muted' }, 'Never run.'));
  return parts;
}
const ORIGIN_LABEL = { console: 'started from the console', terminal: 'started in a terminal', unattended: 'started unattended' };
export function originLabel(r) { const label = ORIGIN_LABEL[r.origin]; return label ? el('span', { class: 'faint small', title: 'where this run was started' }, label) : null; }
function renderRun(r, t) {
  const targets = Object.entries(r.targets || {});
  const open = () => openLog(r.run_id, t ? { kind: 'task', id: t.id } : null);
  return el('div', { class: 'run', role: 'button', tabindex: '0', title: `open the log of ${r.run_id}`, onclick: e => { if (e.target.closest('a')) return; open(); },
    onkeydown: e => { if (e.key === 'Enter') open(); } },
    modeTag(r.mode), pill(r.status || '?'), when(r.started), chipsRow(usageChips(r)),
    r.plan_run ? el('span', { class: 'muted small' }, 'step of plan run ', el('span', { class: 'id' }, r.plan_run)) : null,
    r.steps ? el('span', { class: 'muted small' }, plural(r.steps.length, 'step')) : null,
    r.user ? el('span', { class: 'faint small' }, r.user) : null,
    originLabel(r),
    el('span', { class: 'grow' }), el('span', { class: 'id' }, r.run_id),
    targets.length ? el('span', { class: 'full line' }, targets.map(([n, st]) => el('span', { class: `pill ${tone(st)} mono` }, `${n} ${st || '?'}`))) : null,
    r.last_error ? el('span', { class: 'full err small' }, r.last_error) : null,
    (r.results || []).length ? el('ul', { class: 'small' }, r.results.map(x => el('li', {}, x))) : null,
    (r.warnings || []).length ? el('ul', { class: 'small sugg' }, r.warnings.map(x => el('li', {}, x))) : null);
}
function renderCloseForm(t) {
  const f = S.closeForm;
  if (f.result) {
    return el('div', { class: 'form' }, el('div', { class: 'note good' }, `closed ${(f.result.outcome && f.result.outcome.state) || f.outcome}: ${t.id}`),
      (f.result.notes || []).map(n => el('div', { class: 'note warn small' }, n)),
      el('div', {}, btn('dismiss', () => { S.closeForm = null; render(); })));
  }
  const runs = (t.runs || []).slice().reverse();
  const outcomes = ['done', 'partial', 'abandoned'].map(o => el('label', {}, el('input', { type: 'radio', name: 'outcome', value: o, checked: f.outcome === o, onchange: () => { f.outcome = o; } }), ' ', o));
  const sel = el('select', { onchange: e => { f.run = e.target.value; } }, el('option', { value: '' }, 'no run'),
    runs.map(id => el('option', { value: id, selected: f.run === id }, `${id}${run(id) ? ' ' + run(id).status : ''}`)));
  const note = el('textarea', { id: 'close-note', placeholder: 'What actually happened (required)', oninput: e => { f.note = e.target.value; } }, f.note);
  return el('form', { class: 'form', onsubmit: e => { e.preventDefault(); submitClose(t); } },
    el('div', {}, el('span', { class: 'muted' }, 'outcome '), outcomes),
    note,
    el('div', { class: 'line' }, el('span', { class: 'muted' }, 'run '), sel),
    f.error ? el('div', { class: 'err' }, f.error) : null,
    el('div', { class: 'line' }, el('button', { class: 'primary', type: 'submit', disabled: f.busy }, f.busy ? 'closing...' : 'Close task'),
      btn('Cancel', () => { S.closeForm = null; render(); })));
}
async function submitClose(t) {
  const f = S.closeForm; if (!f || f.busy) return;
  if (!f.note.trim()) { f.error = 'The note is required: say what actually happened.'; render(); return; }
  f.busy = true; f.error = null; render();
  try {
    const body = { task: t.id, outcome: f.outcome, note: f.note.trim() }; if (f.run) body.run = f.run;
    f.result = await post('/api/close', body);
    toast('task closed'); refresh();
  } catch (e) { f.error = e.message; }
  f.busy = false; render();
}
