/* The right pane: its frame, a run's log and a task's file. */
import { run, S, task } from './state.js';
import { $, btn, el, fill, toast } from './dom.js';
import { areaTag, modeTag, pill } from './widgets.js';
import { markdown } from './markdown.js';
import { api } from './api.js';
import { setHash, taskLink } from './nav.js';
import { render } from './render.js';
import { buildJobPane } from './jobs.js';
import { originLabel, renderTaskPane } from './task.js';
import { buildAskPane } from './chat.js';

export function stopPane() { if (S.pane && S.pane.timer) clearTimeout(S.pane.timer); }
export function closePane() {
  stopPane(); S.pane = null;
  if (S.hash && /^(run|job|task|ask)\//.test(S.hash)) setHash(S.view);
  render();
}
export function paneShell(titleParts, body, cls, foot, back) {
  const p = $('#pane');
  fill($('#ph'), back ? btn('← ' + back.label, back.go, 'sm') : null, el('span', { class: 'title' }, titleParts), el('span', { class: 'grow' }), btn('close', closePane, 'sm', { title: 'close (Esc)' }));
  const pb = $('#pb'); pb.className = `pb ${cls || ''}`; fill(pb, body);
  const pf = $('#pf'); fill(pf, foot); pf.hidden = !foot;
  p.classList.add('open'); $('#app').classList.add('has-pane');
}
export function renderPane() {
  const p = S.pane, aside = $('#pane');
  if (!p) {
    aside.classList.remove('open'); $('#app').classList.remove('has-pane');
    fill($('#ph'), el('span', { class: 'muted small' }, 'Details'));
    const pb = $('#pb'); pb.className = 'pb'; fill(pb, el('div', { class: 'placeholder' }, 'Select a task, a job or a run.'));
    $('#pf').hidden = true;
    return;
  }
  if (p.kind === 'task') {
    const pb = $('#pb'), top = pb.scrollTop, t = task(p.id);
    if (!t) { paneShell([el('span', { class: 'id' }, p.id)], el('div', { class: 'muted' }, 'This task is no longer in the home.')); return; }
    paneShell(S.dev ? [el('span', { class: 'id' }, t.id), pill(t.status || 'draft')] : [el('span', { class: 'muted small' }, 'Task '), areaTag(t)], renderTaskPane(t));
    pb.scrollTop = top;
  } else if (p.kind === 'job' && !p.built) { buildJobPane(p); }
  else if (p.kind === 'ask' && !p.built) { buildAskPane(p); }
  else if (p.kind === 'log' && !p.built) { buildLogPane(p); }
  else aside.classList.add('open');
}
export function backTo(p) {
  if (!p.back) return null;
  const chat = p.back.kind === 'ask' || p.back.mode === 'ask';
  return { label: p.back.kind === 'task' ? 'task' : chat ? 'chat' : p.back.kind === 'job' ? 'job' : 'back', go: () => { stopPane(); S.pane = p.back; setHash(`${p.back.kind}/${encodeURIComponent(p.back.id)}`); p.back.built = false; render(); } };
}
export function openLog(runId, back, quiet) {
  if (S.pane && S.pane.kind === 'log' && S.pane.run === runId) { if (!quiet) render(); return; }
  stopPane();
  const r = run(runId);
  S.pane = { kind: 'log', run: runId, back: back === undefined ? (r && r.task_id ? { kind: 'task', id: r.task_id } : null) : back, built: false };
  setHash('run/' + encodeURIComponent(runId));
  if (!quiet) render();
}
function buildLogPane(d) {
  const r = run(d.run);
  d.built = true; d.start = null; d.offset = 0; d.first = true; d.loading = false; d.timer = null;
  d.pre = el('pre', {}); d.status = el('span', {});
  d.earlier = btn('load earlier', () => loadEarlier(d), 'sm', { hidden: true });
  paneShell([el('span', { class: 'id' }, d.run), ' ', r ? [modeTag(r.mode), ' ', taskLink(r.task_id || ''), ' ', originLabel(r)] : null, ' ', d.status],
    el('div', {}, el('div', { class: 'line' }, d.earlier), d.pre), 'log', null, backTo(d));
  pollLog(d);
}
function showEarlier(d) { d.earlier.hidden = !(d.start > 0); d.earlier.disabled = d.loading; }
async function pollLog(d) {
  if (S.pane !== d) return;
  try {
    const r = await api(`/api/log?run=${encodeURIComponent(d.run)}${d.start !== null && d.offset > 0 ? '&offset=' + d.offset : ''}`);
    if (S.pane !== d) return;
    const box = $('#pb');
    const atBottom = d.first || box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
    if (d.start === null) { d.start = r.start || 0; showEarlier(d); }
    if (r.text) { if (d.pre.firstChild && d.pre.firstChild.nodeType !== Node.TEXT_NODE) d.pre.replaceChildren(r.text); else d.pre.append(r.text); }
    d.offset = Math.max(d.offset, r.offset || 0);
    if (!d.pre.textContent) d.pre.replaceChildren(el('span', { class: 'muted' }, r.status === 'running' ? '(no output yet)' : '(no output)'));
    d.status.replaceChildren(pill(r.status || 'no record'));
    if (atBottom) box.scrollTop = box.scrollHeight;
    d.first = false;
    if (r.status === 'running') d.timer = setTimeout(() => pollLog(d), 1500);
  } catch (e) {
    if (S.pane !== d) return;
    d.status.replaceChildren(el('span', { class: 'err' }, e.message));
    d.timer = setTimeout(() => pollLog(d), 4000);
  }
}
async function loadEarlier(d) {
  if (S.pane !== d || d.loading || !(d.start > 0)) return;
  d.loading = true; showEarlier(d);
  const from = Math.max(0, d.start - 512 * 1024);
  try {
    const r = await api(`/api/log?run=${encodeURIComponent(d.run)}&offset=${from}&until=${d.start}`);
    if (S.pane !== d) return;
    const box = $('#pb'), before = box.scrollHeight;
    if (d.pre.firstChild && d.pre.firstChild.nodeType !== Node.TEXT_NODE) d.pre.replaceChildren();
    d.pre.prepend(r.text || '');
    box.scrollTop += box.scrollHeight - before;
    d.start = from;
  } catch (e) { toast(`load earlier failed: ${e.message}`); }
  d.loading = false; showEarlier(d);
}
export async function openFile(t, name) {
  stopPane();
  const body = el('div', { class: 'muted' }, 'loading...');
  const p = { kind: 'file', task: t.id, name, back: { kind: 'task', id: t.id }, built: true };
  S.pane = p;
  paneShell([el('span', { class: 'id' }, t.id), ' / ', name], body, '', null, backTo(p));
  try {
    const r = await api(`/api/file?task=${encodeURIComponent(t.id)}&name=${encodeURIComponent(name)}`);
    if (S.pane !== p) return;
    if (r.text === null || r.text === undefined) body.replaceWith(el('div', { class: 'muted' }, `No ${name} in this task.`));
    else body.replaceWith(name.endsWith('.md') ? markdown(r.text) : el('pre', {}, r.text));
  } catch (e) { body.replaceWith(el('div', { class: 'err' }, e.message)); }
}
