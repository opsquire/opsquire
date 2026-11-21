/* Runs started from this page: their buttons, prompts and answers, the job list and the job pane. */
import { activeJobFor, conversationOf, inboxItem, jobById, run, S, task } from './state.js';
import { askLabel, KIND_WORD, shortName, truncate } from './records.js';
import { $, btn, el, fill, toast } from './dom.js';
import { argsTag, modeTag, when } from './widgets.js';
import { api, post } from './api.js';
import { grow } from './textbox.js';
import { icon, modeIcons } from './icons.js';
import { setHash, taskLink } from './nav.js';
import { render } from './render.js';
import { runProgress } from './schedules.js';
import { backTo, openLog, paneShell, renderPane, stopPane } from './pane.js';
import { askView, followForm, pollAskChat, renderChat } from './chat.js';
import { refresh } from './live.js';

/* terminal output: strip ANSI escapes (CSI and OSC), fold carriage-return rewrites per line */
export function stripAnsi(text) { return String(text || '').replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '').replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '').replace(/\x1b[@-Z\\-_]/g, ''); }
export function crFold(text) {
  return String(text || '').replace(/\r\n/g, '\n').split('\n').map(line => { const i = line.lastIndexOf('\r'); return i < 0 ? line : line.slice(i + 1); }).join('\n');
}
/* what a runner prompt wants: y/n, the word BREAK, or free text */
function classifyPrompt(prompt) {
  const p = String(prompt || '').trim();
  if (!p) return 'none';
  if (/type\s+BREAK/i.test(p)) return 'break';
  if (/\[\s*y\s*\/\s*n\s*\]|\(\s*y\s*\/\s*n\s*\)|\byes\s*\/\s*no\b/i.test(p)) return 'yn';
  return 'text';
}
function jobTone(job) {
  if (!job) return 'grey';
  if (job.status === 'waiting') return 'warn';
  if (job.status === 'running') return 'live';
  return job.exit_code === 0 ? 'good' : 'bad';
}
export function jobLabel(job) {
  if (!job) return '?';
  if (job.status !== 'done') return job.status;
  return job.exit_code === 0 ? 'ok' : `exit ${job.exit_code === null || job.exit_code === undefined ? '?' : job.exit_code}`;
}
/* run buttons: an apply whose script asks first starts on one click; one that never asks, or ran ok already, needs a second click within 5 s */
export function arm(key) {
  S.confirm = key; clearTimeout(S.confirmTimer);
  S.confirmTimer = setTimeout(() => { S.confirm = null; render(); }, 5000);
  render();
}
export function disarm() { S.confirm = null; clearTimeout(S.confirmTimer); }
/* what another apply is changing among this task's targets right now; an apply never waits for another apply */
export function changing(taskId) {
  const mine = (task(taskId) || {}).targets || [], over = (a, b) => a === b || a.startsWith(b + '/') || b.startsWith(a + '/');
  const h = (S.data.locks || []).find(l => l.alive && l.task_id !== taskId && /_apply$/.test(l.run_id || '') && mine.some(x => over(x, l.target)));
  return h ? `${shortName(h.task_id)} is changing ${h.target}` : '';
}
export function runButton(taskId, mode, label, primary) {
  const active = activeJobFor(taskId);
  if (active) return btn(active.status, () => openJob(active.job_id), 'sm', { title: 'a job for this task is still running; open it' });
  const cls = `runbtn k-${mode}` + (primary ? ' primary' : '');
  const face = text => [modeIcons(mode), el('span', {}, text)];
  if (mode !== 'apply') return btn(face(label || (S.dev ? `Run ${mode}` : 'Run')), () => startRun(taskId, mode), cls, { title: S.dev ? null : mode === 'verify' ? 'read-only check that the change is live: nothing changes' : `${mode === 'read' ? 'read-only' : 'dry'} run: nothing changes` });
  const busy = changing(taskId);
  if (busy) return el('span', { title: `${busy}. An apply never waits for another one; Dry run, then apply queues behind it` }, btn(face(label || 'Run'), null, cls + ' apply', { disabled: true }));
  const t = task(taskId) || {}, key = taskId + ':apply', armed = S.confirm === key, already = t.applied_already;
  if (!already && t.asks_first) return btn(face(label || (S.dev ? 'Run apply' : 'Run')), () => startRun(taskId, 'apply'), cls + ' apply', { title: 'changes production; the script asks for your yes before the first change' });
  return btn(face(armed ? 'Confirm apply' : label || (S.dev ? 'Run apply' : 'Run')), () => { if (armed) { disarm(); startRun(taskId, 'apply'); } else arm(key); },
    cls + ' apply' + (armed ? ' armed' : ''), { title: armed ? 'click again within 5 s to start the apply run' : already ? `${already}; this changes production again and asks for a second click` : 'changes production and the script never asks first; asks for a second click' });
}
/* the dry run and, if it ends ok, the apply, in one job: the apply's own question still asks; a script that never asks
   takes a second click first, as an apply does (the first click arms the dry run button, which then says Confirm) */
export function dryApply(taskId) {
  const t = task(taskId) || {}, key = taskId + ':dryapply';
  if (t.asks_first || S.confirm === key) { disarm(); startRun(taskId, 'dryapply'); } else arm(key);
}
export function dryApplyButton(taskId) {
  if (activeJobFor(taskId)) return null;
  const t = task(taskId) || {}, armed = S.confirm === taskId + ':dryapply';
  return btn([modeIcons('dryapply'), el('span', {}, armed ? 'Confirm dry run, then apply' : 'Dry run, then apply')], () => dryApply(taskId), 'runbtn k-apply' + (armed ? ' armed' : ''),
    { title: armed ? 'click again within 5 s: the apply follows the dry run without asking' : t.asks_first ? 'one job: the dry run, then the apply if it ends ok; the script asks for your yes before the first change' : 'one job: the dry run, then the apply if it ends ok; this script never asks first, so it takes a second click' });
}
/* a run button with a menu of the other things to do with the item: on hover, or on the arrow's click (touch, keys) */
export function splitRun(main, items, key) {
  const shown = items.filter(Boolean);
  if (!main || !shown.length || !main.classList.contains('runbtn')) return main;
  const open = S.menu === key, kind = [...main.classList].find(c => c.startsWith('k-')) || '';
  const toggle = () => { S.menu = open ? null : key; render(); };
  /* after a pick the page redraws under a still pointer: that enter comes from a node that is gone, a real one does not */
  const enter = e => {
    if (!window.matchMedia('(hover: hover)').matches) return;
    if (S.menuHush === key) { if (!(e.relatedTarget && e.relatedTarget.isConnected)) return; S.menuHush = null; }
    S.menu = key; e.currentTarget.classList.add('open');
  };
  const leave = e => { if (S.menuHush === key) S.menuHush = null; if (S.menu === key) { S.menu = null; e.currentTarget.classList.remove('open'); } };
  return el('div', { class: `split ${open ? 'open' : ''}`, onmouseenter: enter, onmouseleave: leave },
    main,
    el('button', { type: 'button', class: `runbtn more ${kind}`, 'aria-haspopup': 'menu', 'aria-expanded': String(open), 'aria-label': 'more ways to run it', title: 'more', onclick: toggle }, icon('more')),
    el('div', { class: 'split-menu', role: 'menu' }, shown.map(it => el('button', { type: 'button', role: 'menuitem', class: `mi ${it.cls || ''}`, title: it.title || null,
      onclick: () => { S.menu = null; S.menuHush = key; it.run(); } }, it.icons || null, el('span', {}, it.label)))));
}
/* every run a task supports, the inbox's next step first and primary; one status button while a job runs */
function runModes(t) {
  if (!t || t.kind === 'general' || t.archived || (t.meta_errors || []).length) return [];
  const modes = t.changes_prod ? ['dry', 'apply'] : ['read'];
  if (t.verify) modes.push('verify');
  return modes;
}
export function runActions(t) {
  const modes = runModes(t); if (!modes.length) return [];
  if (activeJobFor(t.id)) return [runButton(t.id, modes[0])];
  const it = inboxItem(t.id), next = it && modes.includes(it.next) ? it.next : null;
  const ranIn = m => (t.runs || []).some(id => run(id) && run(id).mode === m);
  const order = next ? [next, ...modes.filter(m => m !== next)] : modes;
  return order.map(m => runButton(t.id, m, S.dev ? `Run ${m}` : m === next ? 'Run' : `${ranIn(m) ? 'Run again' : 'Run'} (${m})`, m === next));
}
export async function startRun(taskId, mode, expect, back) {
  try {
    const r = await post('/api/run', expect ? { task: taskId, mode, expect } : { task: taskId, mode });
    toast(r && r.joined ? `${KIND_WORD[mode] || mode} already going: opened it` : `started: ${KIND_WORD[mode] || mode}`);
    refresh();
    if (r && r.job_id) openJob(r.job_id, false, null, back);
  } catch (e) { toast(`run failed: ${e.message}`); }
}
export async function answer(jobId, text) {
  try { await post('/api/answer', { job: jobId, text }); toast(text ? `sent ${text}` : 'sent'); refresh(); } catch (e) { toast(`answer failed: ${e.message}`); }
}
async function interrupt(jobId) {
  try { await post('/api/interrupt', { job: jobId }); toast('interrupt sent'); refresh(); } catch (e) { toast(`interrupt failed: ${e.message}`); }
}
function promptControls(jobId, status, prompt, idSuffix) {
  const parts = [];
  if (status === 'waiting' && prompt) {
    const kind = classifyPrompt(prompt);
    parts.push(el('span', { class: 'prompt' }, String(prompt).trim()));
    const answers = el('span', { class: 'answers' });
    if (kind === 'yn') answers.append(btn('Yes', () => answer(jobId, 'y'), 'primary'), btn('No', () => answer(jobId, 'n')));
    else if (kind === 'break') answers.append(btn('BREAK', () => answer(jobId, 'BREAK'), 'runbtn apply'), btn('Cancel', () => answer(jobId, '')));
    else {
      const input = el('input', { id: `ans-${idSuffix || ''}-${jobId}`, placeholder: 'answer', 'aria-label': 'answer' });
      const send = () => { answer(jobId, input.value); input.value = ''; };
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); send(); } });
      answers.append(input, btn('Send', send, 'primary'));
    }
    parts.push(answers);
  }
  if (status !== 'done') parts.push(btn('Interrupt', () => interrupt(jobId), 'sm', { title: 'Ctrl-C the job' }));
  return parts;
}
export function renderJob(j) {
  const open = () => openJob(j.job_id);
  return el('div', { class: `job ${j.status !== 'done' ? `live k-${j.mode}` : ''}`, role: 'button', tabindex: '0', 'data-key': 'j:' + j.job_id,
    onclick: e => { if (e.target.closest('a, button, input')) return; open(); }, onkeydown: e => { if (e.key === 'Enter' && e.target.tagName !== 'INPUT') open(); } },
    el('span', { class: `pill ${jobTone(j)}` }, jobLabel(j)), j.mode === 'ask' ? [modeTag('ask'), el('span', { class: 'purpose', title: j.args || '' }, j.args || '(follow-up)')] : [taskLink(j.task_id || '?'), modeTag(j.mode), argsTag(j.args)],
    el('span', { class: 'muted' }, 'started ', when(j.started)), j.ended ? el('span', { class: 'muted' }, 'ended ', when(j.ended)) : null,
    el('span', { class: 'grow' }), j.run_id ? btn('run log', () => openLog(j.run_id, null), 'sm') : null,
    j.status === 'waiting' ? el('span', { class: 'prompt' }, String(j.prompt || '').trim()) : null,
    el('span', { class: 'answers' }, j.status === 'waiting' ? promptControls(j.job_id, j.status, j.prompt, 'list').slice(1) : j.status !== 'done' ? promptControls(j.job_id, j.status, '', 'list') : null),
    (r => r && r.total ? runProgress(r) : null)(jobRun(j)));
}
/* the run a job drives: known once it ends; while it runs, the running run of its task */
function jobRun(j) {
  if (j.run_id) return run(j.run_id);
  if (j.status === 'done') return null;
  return ((S.data && S.data.running) || []).map(run).find(r => r && r.task_id === j.task_id && !r.plan_run) || null;
}
/* a job (a run started from this page) in the pane: live terminal output, prompt answers */
export function openJob(jobId, quiet, mode, back) {
  if (S.pane && S.pane.kind === 'job' && S.pane.id === jobId) { if (!quiet) render(); return; }
  stopPane();
  S.pane = { kind: 'job', id: jobId, built: false, mode: mode || null, back: back || null };
  setHash('job/' + encodeURIComponent(jobId));
  if (!quiet) render();
}
export function buildJobPane(d) {
  const j = jobById(d.id);
  d.built = true; d.offset = 0; d.buf = ''; d.first = true; d.timer = null; d.status = null; d.prompt = ''; d.exit = null; d.run_id = (j && j.run_id) || null;
  d.doneNode = document.createTextNode(''); d.tailNode = document.createTextNode('');
  d.pre = el('pre', {}, d.doneNode, d.tailNode);
  d.pill = el('span', {}, el('span', { class: 'pill grey' }, 'connecting'));
  d.foot = d.ctl = el('div', { class: 'line', style: 'margin:0;width:100%' });
  if ((j && j.mode === 'ask') || d.mode === 'ask') {
    d.ask = true; d.ask_id = (j && j.ask_id) || d.ask_id || null; d.chatItems = []; d.chatOffset = 0;
    d.chat = el('div', { class: 'chat' });
    d.body = el('div', {}, d.chat, el('div', { class: 'asklog' }, d.pre));
    d.seg = el('span', { class: 'seg small' }, [['chat', 'Chat'], ['log', 'Log'], ['both', 'Both']].map(([v, l]) => el('button', { type: 'button', 'data-v': v, onclick: () => askView(d, v) }, l)));
    d.follow = followForm((j && j.ask_id) || d.id, () => d.ask_id, true);
    d.foot = el('div', { style: 'margin:0;width:100%;display:flex;flex-direction:column;gap:6px' }, d.ctl, d.follow);
    const opened = j && j.ask_id && (S.data.asks || []).find(a => a.ask_id === conversationOf(j.ask_id)), title = askLabel((opened && opened.prompt) || (j && j.args) || '');
    paneShell([el('span', { class: 'kind k-ask' }, 'ask'), ' ', el('span', { class: 'title', title }, truncate(title, 80)), ' ', d.pill, el('span', { class: 'grow' }), d.seg], d.body, 'ask', d.foot);
    askView(d, S.askView || 'chat');
  } else {
    paneShell([el('span', { class: 'muted small' }, 'job '), j ? [taskLink(j.task_id || '?'), ' ', modeTag(j.mode), ' ', argsTag(j.args), ' '] : [el('span', { class: 'id' }, d.id), ' '], d.pill],
      d.pre, 'log', d.foot, backTo(d));
  }
  pollJob(d);
}
function feedJob(d, chunk) {
  d.buf += stripAnsi(chunk);
  const i = d.buf.lastIndexOf('\n');
  if (i >= 0) { d.doneNode.appendData(crFold(d.buf.slice(0, i + 1))); d.buf = d.buf.slice(i + 1); }
  d.tailNode.data = crFold(d.buf);
}
export function jobFoot(d) {
  const inChat = d.ask && S.askView !== 'log';
  const parts = promptControls(d.id, d.status, inChat ? '' : d.prompt, 'pane');
  if (d.status === 'done') {
    parts.push(el('span', { class: 'muted small' }, 'finished', d.exit === null || d.exit === undefined ? '' : `, exit ${d.exit}`));
    if (d.run_id) parts.push(btn('open run record', () => openLog(d.run_id, { kind: 'job', id: d.id, mode: d.ask ? 'ask' : null, back: d.back || null }), 'sm'));
  }
  fill(d.ctl, parts);
  d.ctl.hidden = !parts.length;
  if (d.follow) {
    d.follow.hidden = d.status !== 'done';
    const t = d.follow.querySelector('textarea');
    if (t && !d.follow.hidden) requestAnimationFrame(() => grow(t));
  }
  $('#pf').hidden = !parts.length && (!d.follow || d.follow.hidden);
}
async function pollJob(d) {
  if (S.pane !== d) return;
  try {
    const r = await api(`/api/job?id=${encodeURIComponent(d.id)}&offset=${d.offset}`);
    if (S.pane !== d) return;
    if (r.mode === 'ask' && !d.ask) { d.mode = 'ask'; d.built = false; renderPane(); return; }
    const box = $('#pb');
    const atBottom = d.first || box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
    if (r.text) feedJob(d, r.text);
    if (typeof r.offset === 'number') d.offset = Math.max(d.offset, r.offset);
    const changed = d.status !== r.status || d.prompt !== (r.prompt || '') || d.exit !== r.exit_code || d.run_id !== (r.run_id || null);
    d.status = r.status || 'running'; d.prompt = r.prompt || ''; d.exit = r.exit_code; d.run_id = r.run_id || d.run_id;
    if (changed || d.first) {
      d.pill.replaceChildren(el('span', { class: `pill ${jobTone({ status: d.status, exit_code: d.exit })}` }, jobLabel({ status: d.status, exit_code: d.exit })));
      jobFoot(d);
    }
    if (!d.doneNode.data && !d.tailNode.data && d.status === 'done') d.tailNode.data = '(no output)';
    if (d.ask) {
      d.ask_id = d.ask_id || r.ask_id || null;
      if (d.ask_id) setHash('ask/' + encodeURIComponent(conversationOf(d.ask_id)));
      const moved = await pollAskChat(d).catch(() => false);
      if (moved || changed || d.first) renderChat(d);
    }
    if (atBottom) box.scrollTop = box.scrollHeight;
    d.first = false;
    if (d.status !== 'done') d.timer = setTimeout(() => pollJob(d), 1000);
  } catch (e) {
    if (S.pane !== d) return;
    d.pill.replaceChildren(el('span', { class: 'err small' }, e.message));
    d.timer = setTimeout(() => pollJob(d), 3000);
  }
}
