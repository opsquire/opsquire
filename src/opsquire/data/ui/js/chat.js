/* An ask as a chat: you on the right, the squire on the left, its work as small lines between. */
import { conversationOf, jobById, jobs, S, task } from './state.js';
import { clock } from './time.js';
import { askLabel, FOLLOWUP_HEAD, plural, tone, truncate } from './records.js';
import { btn, el, fill, toast } from './dom.js';
import { when } from './widgets.js';
import { markdown } from './markdown.js';
import { api, post } from './api.js';
import { saved } from './prefs.js';
import { writeBox } from './textbox.js';
import { modelName, retinue, spentText, usageText } from './agents.js';
import { paintSquire, squire } from './squire.js';
import { setHash } from './nav.js';
import { render } from './render.js';
import { answer, jobFoot, openJob, startRun } from './jobs.js';
import { openLog, paneShell, stopPane } from './pane.js';
import { refresh } from './live.js';

/* what a tool request is about, in one line; the whole command (a heredoc can be a file) only on request */
function approveSummary(d, it) {
  const shell = String(it.text || '').startsWith('shell: '), whole = shell ? it.text.slice(7) : String(it.text || '');
  const lines = whole.split('\n'), more = lines.length - 1;
  const first = truncate(lines[0].replace(/\/[^\s;'"]*\/\.worktrees\/[^/\s]+\/(\d+)/g, 'copy $1'), 140);
  const key = `${it.at}|${it.text}`, open = (d.openApprove || new Set()).has(key);
  const long = more > 0 || lines[0].length > 140 || (it.detail && !shell);
  const toggle = () => { d.openApprove = d.openApprove || new Set(); if (open) d.openApprove.delete(key); else d.openApprove.add(key); renderChat(d); };
  return [
    el('div', { class: 'ask-what' }, shell ? 'Wants to run ' : 'Wants to use ', el('code', {}, first), more ? el('span', { class: 'faint small' }, ` (+${plural(more, 'line')})`) : null,
      long ? btn(open ? 'hide' : 'show the command', toggle, 'sm link') : null),
    open ? el('pre', { class: 'what small' }, shell ? whole : `${whole}${it.detail ? '\n' + it.detail : ''}`) : null,
  ];
}
function chatItem(d, it) {
  if (it.kind === 'you' && String(it.text || '').startsWith(FOLLOWUP_HEAD)) return el('div', { class: 'msg report' }, el('div', { class: 'bubble' },
    el('div', { class: 'faint small' }, 'Your runs finished; the console sent them to the squire'),
    it.text.split('\n').filter(l => l.startsWith('- ')).map(l => reportRow(d, l))));
  if (it.kind === 'you') return el('div', { class: 'msg you' }, el('div', { class: 'bubble' }, it.text));
  if (it.kind === 'end') {
    const runs = it.last && it.text === 'ok' && it.reply ? runsState(it.reply.actions, it.reply.at) : 'none';
    if (runs === 'unrun') return el('div', { class: 'msg end warn' }, 'waiting for your run');
    if (runs === 'going') return el('div', { class: 'msg end warn' }, 'your run is going');
    if (runs === 'ran') return el('div', { class: 'msg end good' }, 'your run finished');
    if (it.text === 'limited') return el('div', { class: 'msg end warn' }, it.resets_at ? `stopped at the usage limit; continues by itself at ${clock(it.resets_at)}` : 'stopped at the usage limit; its reset time could not be read',
      it.last && !it.resets_at ? btn('Continue', () => continueChat(d), 'sm', { title: 'once the limit has reset: carry on where it stopped' }) : null);
    return el('div', { class: `msg end ${it.text === 'ok' ? 'good' : 'warn'}` }, it.text === 'ok' ? 'done' : it.waiting ? `waiting: ${it.waiting}` : it.text);
  }
  if (it.kind === 'approve') return el('div', { class: 'msg agent approve' }, el('div', { class: 'avatar' }), el('div', { class: 'bubble' },
    approveSummary(d, it),
    it.pending ? el('div', { class: 'quick' }, btn('Allow', () => replyAsk(d, 'y'), 'sm primary', { title: 'let it run this once' }), btn('Deny', () => replyAsk(d, 'n'), 'sm', { title: 'refuse; the agent is told you said no' }),
      el('span', { class: 'faint small' }, 'Always allow it under Settings.')) : null));
  const shown = (it.actions || []).some(a => a.kind === 'RUN') ? String(it.text || '').split('\n').filter(l => !/^RUN:\s*opsquire run /.test(l.trim())).join('\n') : it.text || '';
  const body = [el('div', { class: 'md' }, markdown(shown))];
  if (it.kind === 'question' && it.pending) {
    body.push(el('div', { class: 'quick' }, btn(`✓ ${it.recommend || 'recommended'}`, () => replyAsk(d, ''), 'sm primary', { title: 'accept the recommended answer' })));
  }
  const runs = (it.actions || []).filter(a => a.kind === 'RUN');
  if (runs.length) body.push(el('div', { class: 'runs-cards' }, runs.map(a => runCard(d, a, it.at))));
  if (it.cost_usd || it.model || it.usage) body.push(el('div', { class: 'faint small', title: 'What the agent reported for the tokens this ask used, at API list prices. Whether that is money depends on how the agent is billed, which its retinue entry says.' },
    [it.agent && retinue().length > 1 ? it.agent : '', it.model ? modelName(it.model) : '', spentText(it), usageText(it.usage)].filter(Boolean).join(' · ')));
  return el('div', { class: 'msg agent' }, el('div', { class: 'avatar' }), el('div', { class: 'bubble' }, body));
}
/* agents whose latest ask stopped at the usage limit and whose reset (with the console's two-minute margin) has not
   come, as followup.held_agents decides on the server */
export function heldAgents(asks) {
  const latest = {};
  for (const a of [...asks].sort((x, y) => String(x.started || '').localeCompare(String(y.started || '')))) latest[a.agent] = a;
  const out = {};
  for (const [agent, a] of Object.entries(latest)) if (a.status === 'limited' && (!a.resets_at || Date.now() < Date.parse(a.resets_at) + 120000)) out[agent] = a.resets_at || null;
  return out;
}
/* An ask and its follow-ups are one conversation (the first ask's id); newest conversation first. */
export function conversations(asks) {
  const by = new Map();
  for (const a of [...asks].sort((x, y) => String(x.started || '').localeCompare(String(y.started || '')))) {
    const id = a.conversation || a.ask_id;
    if (!by.has(id)) by.set(id, { id, first: a, asks: [] });
    const c = by.get(id); c.asks.push(a); c.last = a;
  }
  return [...by.values()].sort((x, y) => String(y.last.started || '').localeCompare(String(x.last.started || '')));
}
/* the conversation the pane shows: a chat, or a run started from one once it ended (its chat comes back when the
   agent resumes); null for anything else */
export function paneConversation(p) {
  if (!p) return null;
  if (p.kind === 'ask') return conversationOf(p.id);
  if (p.kind !== 'job') return null;
  const j = jobById(p.id), askId = p.ask_id || (j && j.mode === 'ask' && j.ask_id);
  if (askId) return conversationOf(askId);
  if (p.back && (p.back.kind === 'ask' || p.back.mode === 'ask') && (!j || j.status === 'done')) return paneConversation(p.back);
  return null;
}
/* a job that starts waiting opens by itself only where it does not take the place of something else */
export function mayTakePane(j) {
  const p = S.pane;
  if (!p || (p.kind === 'job' && p.id === j.job_id)) return true;
  if (j.mode === 'ask') return !!j.ask_id && paneConversation(p) === conversationOf(j.ask_id);
  const shown = p.kind === 'task' ? p.id : p.kind === 'job' ? (jobById(p.id) || {}).task_id : null;
  return !!j.task_id && shown === j.task_id;
}
function shlexSplit(text) {
  const out = []; let cur = null, q = null;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === q) q = null; else if (c === '\\' && q === '"' && i + 1 < text.length) cur += text[++i]; else cur += c; continue; }
    if (/\s/.test(c)) { if (cur !== null) { out.push(cur); cur = null; } continue; }
    if (cur === null) cur = '';
    if (c === "'" || c === '"') q = c; else if (c === '\\' && i + 1 < text.length) cur += text[++i]; else cur += c;
  }
  if (cur !== null) out.push(cur);
  return out;
}
/* a RUN line an agent ended with: the run it means, and what became of it since the agent said it */
function runLine(text) {
  const m = /^opsquire run (\S+)((?:\s+--\w[\w-]*)*)(?:\s+--\s+(.*))?\s*$/.exec(String(text || '').trim());
  if (!m) return null;
  const flags = (m[2] || '').trim().split(/\s+/), t = task(m[1]);
  const mode = flags.includes('--apply') ? 'apply' : flags.includes('--verify') ? 'verify' : t && !t.changes_prod ? 'read' : 'dry';
  return { task: m[1], mode, args: m[3] ? shlexSplit(m[3]) : [] };
}
function runOfLine(line, since) {
  const t = task(line.task), walk = !line.args.length && t && (t.stages || []).length && line.mode === 'apply';
  const modes = walk ? ['dry', 'apply', 'verify'] : [line.mode];
  const live = jobs().find(j => j.task_id === line.task && modes.includes(j.mode) && j.status !== 'done') || null;
  const same = r => r.task_id === line.task && modes.includes(r.mode) && String(r.started || '') >= String(since || '')
    && (walk || JSON.stringify(r.argv || []) === JSON.stringify(line.args));
  const runs = Object.values((S.data && S.data.runs) || {}).filter(same).sort((a, b) => String(a.started || '').localeCompare(String(b.started || '')));
  return { live, run: runs[runs.length - 1] || null };
}
/* where the runs an agent's reply handed over stand: 'none' (no RUN lines), 'unrun', 'going' or 'ran' */
export function runsState(actions, since) {
  const lines = (actions || []).filter(a => a.kind === 'RUN').map(a => runLine(a.text)).filter(Boolean);
  if (!lines.length) return 'none';
  const seen = lines.map(l => runOfLine(l, since));
  if (seen.some(x => x.live || (x.run && x.run.status === 'running'))) return 'going';
  return seen.some(x => !x.run) ? 'unrun' : 'ran';
}
/* what the chat's run cards show, so a change in a run or job redraws the chat and nothing else does */
export function runsKey(d) {
  return JSON.stringify((d.chatItems || []).filter(it => it.kind === 'agent' && (it.actions || []).length).map(it => (it.actions || []).map(a => {
    const line = a.kind === 'RUN' && runLine(a.text); if (!line) return null;
    const { live, run } = runOfLine(line, it.at);
    const problem = cardProblem(line);
    return [live && live.status, run && run.run_id, run && run.status, problem && problem.at];
  })));
}
function cardProblem(line) { const p = ((S.data && S.data.start_problems) || {})[line.task]; return p && p.mode === line.mode ? p : null; }
function chatBack(d) { return d.kind === 'ask' ? { kind: 'ask', id: d.id, built: false } : { kind: 'job', id: d.id, built: false, mode: 'ask' }; }
function runCard(d, a, since) {
  const line = runLine(a.text), kind = line ? line.mode : 'dry';
  const head = el('div', { class: 'cmdline' }, `▶ ${a.text}`);
  if (!line || !task(line.task)) return el('div', { class: `runcard k-${kind}` }, head, el('div', { class: 'runstate faint small' }, line ? 'this task is not in the home' : ''));
  const { live, run } = runOfLine(line, since), back = chatBack(d);
  const state = [];
  if (live) state.push(btn(live.status === 'waiting' ? 'waiting for you' : 'running…', () => openJob(live.job_id, false, null, back), 'sm', { title: 'open the run' }));
  else if (run) {
    state.push(el('span', { class: `pill ${tone(run.status)}` }, run.status || '?'));
    if (run.last_error && run.status !== 'ok') state.push(el('span', { class: 'small runerr', title: run.last_error }, truncate(run.last_error, 140)));
    state.push(btn('record', () => openLog(run.run_id, back), 'sm', { title: 'open the run record' }));
  } else state.push(el('span', { class: 'faint small' }, 'not run yet'));
  const problem = !live && cardProblem(line);
  if (problem) state.push(el('span', { class: 'small runerr', title: problem.why }, `did not start: ${truncate(problem.why, 160)}`));
  const settled = run && run.status === 'ok' && line.mode === 'apply';
  if (!live && !settled) state.push(btn(run ? 'run again' : 'Run', () => startRun(line.task, line.mode, line.args, back), `sm ${run ? '' : 'primary'}`, { title: `run it here: ${a.text}` }));
  return el('div', { class: `runcard k-${kind}` }, head, el('div', { class: 'runstate' }, state));
}
/* one run of the console's report: `- \`opsquire run ...\` ended <status> (run <id>)[: <error>]` */
function reportRow(d, line) {
  const m = /^- `opsquire run \S+[^`]*` ended (\S+) \(run (\S+)\)(?:: (.*))?$/.exec(line);
  if (!m) return el('div', { class: 'md' }, markdown(line));
  const [, status, runId, error] = m, mode = runId.split('_').pop();
  return el('div', { class: 'reportrow' }, el('span', { class: 'small' }, mode), el('span', { class: `pill ${tone(status)}` }, status),
    btn('record', () => openLog(runId, chatBack(d)), 'sm', { title: runId }), error ? el('span', { class: 'small runerr', title: error }, truncate(error, 160)) : null);
}
export function renderChat(d) {
  const stick = !d.rendered || d.chat.scrollTop + d.chat.clientHeight >= d.chat.scrollHeight - 24;
  const focused = document.activeElement && document.activeElement.id === 'askreply' ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  const talking = d.status && d.status !== 'done' && d.status !== 'waiting';
  const kids = [];
  let steps = [];
  const flush = () => {
    if (!steps.length) return;
    const run = steps; steps = [];
    const key = run[0].at, open = (d.openSteps || new Set()).has(key);
    kids.push(el('div', { class: 'msg steps' },
      el('button', { type: 'button', class: 'stepfold', 'aria-expanded': String(open), onclick: () => { d.openSteps = d.openSteps || new Set(); if (open) d.openSteps.delete(key); else d.openSteps.add(key); renderChat(d); } },
        `${open ? '▾' : '▸'} ${plural(run.length, 'step')}`, open ? null : el('span', { class: 'faint' }, ` · ${truncate(run[run.length - 1].text, 70)}`)),
      open ? el('div', { class: 'steplist' }, run.map(it => el('div', {}, `${it.piece ? it.piece + ' · ' : ''}${it.text}`))) : null));
  };
  const lastApprove = d.chatItems.map(it => it.kind).lastIndexOf('approve');
  /* an approval waits while its ask runs, whether or not the job's output ended on the question: other agents of the
     same ask keep printing their steps after it */
  const pendingApprove = (d.status === 'waiting' || d.status === 'running') && lastApprove >= 0 && !d.chatItems.slice(lastApprove + 1).some(it => it.kind === 'you');
  const lastQuestion = d.chatItems.map(it => it.kind).lastIndexOf('question');
  const pendingQuestion = d.kind === 'job' && d.status === 'waiting' && lastQuestion >= 0 && !d.chatItems.slice(lastQuestion + 1).some(it => it.kind === 'you');
  let lastReply = null;
  d.chatItems.forEach((it, i) => {
    if (it.kind === 'step') { steps.push(it); return; }
    if (it.kind === 'agent') lastReply = it; else if (it.kind === 'you') lastReply = null;
    flush();
    kids.push(chatItem(d, i === lastApprove ? { ...it, pending: pendingApprove } : i === lastQuestion ? { ...it, pending: pendingQuestion } : it.kind === 'end' ? { ...it, reply: lastReply, last: i === d.chatItems.length - 1 } : it));
  });
  flush();
  const agents = [...kids].filter(n => n.classList.contains('agent'));
  const typing = talking && (!agents.length || d.chatItems[d.chatItems.length - 1].kind !== 'agent');
  agents.forEach(n => n.querySelector('.avatar').replaceChildren(squire(false)));
  d.live = talking && !typing && agents.length ? { avatar: agents[agents.length - 1].querySelector('.avatar'), caption: null, key: null } : null;
  if (typing) {
    const avatar = el('div', { class: 'avatar' }), caption = el('div', { class: 'bubble faint' });
    kids.push(el('div', { class: 'msg agent typing' }, avatar, caption));
    d.live = { avatar, caption, key: null };
  }
  if (d.status === 'waiting' && d.prompt && !pendingApprove) kids.push(el('div', { class: 'msg answerbox' }, el('form', { onsubmit: e => { e.preventDefault(); replyAsk(d, e.target.reply.value); } },
    writeBox('reply.' + d.id, { name: 'reply', id: 'askreply', placeholder: 'your answer (empty = the recommended one)' }, t => replyAsk(d, t.value)), btn('Send', null, 'sm primary', { type: 'submit' }))));
  loadQueue(d);
  const queue = renderQueue(d);
  if (queue) kids.push(queue);
  fill(d.chat, kids);
  paintSquire(d);
  if (d.live && !d.squireTimer) d.squireTimer = setInterval(() => paintSquire(d), 500);
  if (stick) d.chat.scrollTop = d.chat.scrollHeight;
  d.rendered = true;
  d.runsKey = runsKey(d);
  const reply = focused && document.getElementById('askreply');
  if (reply) { reply.focus({ preventScroll: true }); reply.setSelectionRange(focused[0], focused[1]); }
}
async function continueChat(d) {
  try { const r = await post('/api/ask', { request: 'Continue where you stopped.', resume: d.ask_id || d.id }); refresh(); openJob(r.job_id, false, 'ask'); } catch (err) { toast(`continue failed: ${err.message}`); }
}
/* every tool request waiting in a live ask, not just the one its terminal asks: the agents of a split ask queue up */
function loadQueue(d) {
  if (!['running', 'waiting'].includes(d.status) || d.queueBusy || Date.now() - (d.queueAt || 0) < 2000) return;
  d.queueBusy = true; d.queueAt = Date.now();
  api(`/api/approvals?job=${encodeURIComponent(d.id)}`).then(r => { d.queue = r.pending || []; }, () => { d.queue = []; })
    .finally(() => { d.queueBusy = false; renderChat(d); });
}
async function answerQueued(d, ids, allow) {
  for (const id of ids) {
    try { d.queue = (await post('/api/approve', { job: d.id, id, allow })).pending || []; } catch (e) { toast(`could not answer: ${e.message}`); break; }
  }
  renderChat(d);
}
function renderQueue(d) {
  const q = ['running', 'waiting'].includes(d.status) ? d.queue || [] : [];
  if (q.length < 2) return null;
  const reads = q.filter(r => r.read_only);
  return el('div', { class: 'msg agent queue' }, el('div', { class: 'avatar' }), el('div', { class: 'bubble' },
    el('div', { class: 'qhead' }, el('strong', {}, `${q.length} requests waiting`), el('span', { class: 'faint small' }, ' the agents wait until each is answered'),
      el('span', { class: 'grow' }), reads.length ? btn(`Allow all read-only (${reads.length})`, () => answerQueued(d, reads.map(r => r.id).reverse(), true), 'sm primary', { title: 'commands that only read and print: they change nothing' }) : null),
    q.map((r, i) => el('div', { class: 'qrow' },
      el('span', { class: `tag ${r.read_only ? '' : 'warn'}` }, r.read_only ? 'reads only' : 'changes'),
      el('span', { class: 'qlabel mono', title: r.label }, truncate(r.label.replace(/^shell: /, ''), 160)),
      i === 0 ? el('span', { class: 'faint small' }, 'asked now') : null,
      btn('Allow', () => answerQueued(d, [r.id], true), 'sm'), btn('Deny', () => answerQueued(d, [r.id], false), 'sm')))));
}
function replyAsk(d, text) { saved('reply.' + d.id, ''); answer(d.id, text); }
export async function pollAskChat(d) {
  if (!d.ask_id) return;
  const r = await api(`/api/askchat?id=${encodeURIComponent(d.ask_id)}&offset=${d.chatOffset}`);
  if ((r.items || []).length) {
    if (d.chatOffset > 0 && r.items.some(it => it.kind !== 'you' && it.kind !== 'end')) d.spoke = Date.now();
    d.chatItems.push(...r.items); d.chatOffset = r.offset; return true;
  }
  return false;
}
export function askView(d, view) {
  S.askView = view;
  d.body.className = `askbody show-${view}`;
  d.seg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === view)));
  if (d.foot) jobFoot(d);
}
export function openAsk(askId) {
  stopPane();
  S.pane = { kind: 'ask', id: askId, built: false };
  setHash('ask/' + encodeURIComponent(askId));
  render();
}
export function followForm(key, resume, hidden) {
  const send = async t => { const text = t.value.trim(), id = resume(); if (!text || !id) return;
    try { const r = await post('/api/ask', { request: text, resume: id }); t.value = ''; saved('follow.' + id, ''); refresh(); openJob(r.job_id, false, 'ask'); } catch (err) { toast(`ask failed: ${err.message}`); } };
  const box = writeBox('follow.' + key, { name: 'q', placeholder: 'follow up in this conversation (Enter sends, Shift+Enter for a new line)' }, send);
  return el('form', { class: 'follow', hidden, onsubmit: e => { e.preventDefault(); send(box); } }, box, btn('Send', null, 'sm', { type: 'submit' }));
}
export async function buildAskPane(d) {
  d.built = true; d.ask = true; d.ask_id = d.id; d.chatItems = []; d.chatOffset = 0; d.status = 'done'; d.prompt = '';
  d.chat = el('div', { class: 'chat' });
  const rec = (S.data.asks || []).find(a => a.ask_id === d.id) || {};
  const foot = followForm(d.id, () => d.id, false);
  paneShell([el('span', { class: 'kind k-ask' }, 'ask'), ' ', el('span', { class: 'title', title: askLabel(rec.prompt) }, truncate(askLabel(rec.prompt) || d.id, 80)), ' ', when(rec.started)], el('div', { class: 'askbody show-chat' }, d.chat), 'ask', el('div', { style: 'width:100%' }, foot));
  const poll = async () => {
    if (S.pane !== d) return;
    try { if (await pollAskChat(d)) renderChat(d); } catch (e) { fill(d.chat, el('div', { class: 'muted' }, `cannot read this conversation: ${e.message}`)); return; }
    const last = d.chatItems[d.chatItems.length - 1];
    if (!d.chatItems.length) fill(d.chat, el('div', { class: 'muted' }, 'This ask kept no conversation (it ran before chats were recorded).'));
    if (!last || last.kind !== 'end') d.timer = setTimeout(poll, 2000);
  };
  poll();
}
