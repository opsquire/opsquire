/* The inbox: what waits on the operator, what runs now, what needs a decision, what got done today. */
import {
  activeJobFor, inbox, inboxItem, isDeciding, jobs, lastJudgeFor, liveAskJob, run, S, selectedTask, task,
} from './state.js';
import { clock, dayKey, duration } from './time.js';
import {
  agentChips, askLabel, codeName, decisionList, lastRunOf, money, onDay, plural, settledDay, shortName, taskMatches,
  todayStats, truncate, usageChips,
} from './records.js';
import { btn, el, toast } from './dom.js';
import { areaTag, argsTag, chipsRow, modeTag, pill, scrollToKey, sec, tag, tick, when } from './widgets.js';
import { post } from './api.js';
import { readDryApply, saved } from './prefs.js';
import { grow, writeBox } from './textbox.js';
import { agentOf, modelName, retinue } from './agents.js';
import { squire } from './squire.js';
import { selectTask, taskLink } from './nav.js';
import { render, unreadChats } from './render.js';
import { changing, dryApply, dryApplyButton, openJob, renderJob, runButton, splitRun, startRun } from './jobs.js';
import { icon, modeIcons } from './icons.js';
import { decideAll, decideButton, judgeLine } from './judge.js';
import { linkPick, renderLinkBar, renderSchedule, runProgress } from './schedules.js';
import { openLog } from './pane.js';
import { conversations, heldAgents, openAsk, runsState } from './chat.js';
import { refresh } from './live.js';

/* an ask's one-line title: an upkeep ask by what it is (its prompt is the long generated brief), others by their first line */
function askTitle(prompt) {
  const p = String(prompt || '');
  return p.startsWith("Library upkeep of this home's general scripts") ? 'Library upkeep' : p.split('\n')[0] || '(follow-up)';
}

/* every waiting read and dry run at once: none of them changes anything; each still starts as your own run */
function safeItems() { return inbox().filter(it => (it.next === 'read' || it.next === 'dry') && !activeJobFor(it.task_id)); }
async function runSafeAll() {
  if (S.runAll && S.runAll.running) return;
  const ids = safeItems().map(it => [it.task_id, it.next]);
  S.runAll = { running: true, total: ids.length, done: 0, error: null }; render();
  try {
    for (const [id, mode] of ids) { await post('/api/run', { task: id, mode }); S.runAll.done++; render(); }
    toast(`started ${plural(ids.length, 'run')}`);
  } catch (e) { S.runAll.error = e.message; toast(`run all stopped: ${e.message}`); }
  S.runAll.running = false; refresh();
}
export function renderInbox() {
  const q = S.q, parts = [], d = S.data;
  const match = id => !q || taskMatches(task(id) || { id }, q);
  const items = inbox().filter(it => !q || taskMatches(task(it.task_id) || { id: it.task_id, purpose: it.purpose, author: it.author }, q, it.thread));
  const running = (d.running || []).map(run).filter(Boolean);
  const activeJobs = jobs().filter(j => j.status !== 'done');
  const runningIds = new Set(d.running || []);
  const terminalRuns = running.filter(r => !activeJobs.some(j => j.task_id === r.task_id) && !(r.plan_run && runningIds.has(r.plan_run)));
  const runningRows = [...activeJobs.filter(j => match(j.task_id)).map(renderJob),
    ...terminalRuns.filter(r => match(r.task_id)).map(r => renderRunRow(r, true))];
  const asking = activeJobs.filter(j => j.status === 'waiting').length + terminalRuns.filter(r => r.waiting).length;
  const decideAllList = decisionList(d);
  const judged = [...jobs().filter(j => j.mode === 'judge').map(j => j.task_id), ...Object.values(d.tasks || {}).filter(t => t.judged && !t.settled).map(t => t.id)];
  const decideRows = [...new Set([...decideAllList.map(t => t.id), ...judged])].map(task).filter(t => t && match(t.id)).sort((a, b) => b.id.localeCompare(a.id));
  const today = dayKey(new Date().toISOString());
  const settledToday = Object.values(d.tasks || {}).filter(t => t.kind !== 'general' && settledDay(t, d.runs) === today && match(t.id));
  const inSchedule = new Set(settledToday.filter(t => t.kind === 'plan').flatMap(t => t.steps || []));
  const doneToday = settledToday.filter(t => !inSchedule.has(t.id)).sort((a, b) => b.id.localeCompare(a.id));
  const tile = (key, n, label, cls, extra) => el('div', { class: `tile ${n ? cls : 'zero'}`, role: 'button', tabindex: '0', title: `go to ${label}`,
    onclick: e => { if (e.target.closest('button')) return; scrollToKey(key); }, onkeydown: e => { if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') scrollToKey(key); } },
    el('span', { class: 'num' }, String(n)), el('span', { class: 'lbl' }, label), extra || null);
  const da = S.decideAll;
  parts.push(el('div', { class: 'tiles' },
    tile('sec:inbox', inbox().length, 'Waiting on you', 'amber'),
    tile('sec:running', terminalRuns.length + activeJobs.length, 'Running now', 'live'),
    tile('sec:decide', decideAllList.length, 'Needs a decision', 'amber', decideAllList.length || (da && da.running) ? el('span', { class: 'act' },
      el('button', { class: 'sm', type: 'button', disabled: !!(da && da.running), onclick: e => { e.stopPropagation(); decideAll(); } }, da && da.running ? [el('span', { class: 'spin' }), `Deciding ${da.done + 1} of ${da.total}`] : 'Decide all')) : null),
    tile('sec:done', doneToday.length, 'Done today', 'good')));
  const st = todayStats(d, today);
  parts.push(el('div', { class: 'statsline' }, el('span', {}, 'Today: '),
    el('span', { title: 'What the agents\' tokens would cost at API list prices. On a subscription plan this counts against the plan\'s usage limits, not money; asks billed to an API key are shown separately.' },
      'agent usage ', el('b', {}, `≈ ${money(st.spend) || '$0.00'}`), ' at API prices', st.billed ? [', ', el('b', {}, money(st.billed)), ' billed to an API key'] : null),
    el('span', {}, 'your runs ', el('b', {}, st.runs ? duration(st.operator_s) : 'none')),
    st.runs ? el('span', {}, 'runs ', el('b', {}, `${st.ok} ok`), st.failed ? [' / ', el('b', { class: 'err' }, `${st.failed} failed`)] : null) : null));
  if (asking) parts.push(el('div', { class: 'asking', role: 'status' }, el('b', {}, asking === 1 ? 'A run is waiting for your answer.' : `${asking} runs are waiting for your answer.`), ' It does nothing until you answer it under Running now.', btn('Show', () => scrollToKey('sec:running'), 'sm')));
  const sendAsk = async t => {
    const q = t.value.trim(); if (!q) return;
    const ag = agentOf(saved('askAgent')), model = (ag.tiers || {})[saved('askModel')] ? saved('askModel') : 'default';
    try { const r = await post('/api/ask', { request: q, model, agent: ag.name }); t.value = ''; saved('askDraft', ''); grow(t); refresh(); openJob(r.job_id, false, 'ask'); } catch (err) { toast(`ask failed: ${err.message}`); }
  };
  const askText = writeBox('askDraft', { id: 'askq', name: 'q', placeholder: 'Ask your squire: "restart the workers on web1", "why did last night\'s backup fail"…' }, sendAsk);
  const agent = agentOf(saved('askAgent')), tierInfo = agent.tiers || {};
  const tierWhy = { quick: 'lookups and small changes', default: 'building and fixing production tasks', deep: 'the hardest problems; uses the most' };
  const pick = tierInfo[saved('askModel')] ? saved('askModel') : 'default';
  const tierSeg = Object.keys(tierInfo).length ? el('span', { class: 'tiers', role: 'radiogroup', 'aria-label': 'model' }, ['quick', 'default', 'deep'].filter(v => tierInfo[v]).map(v =>
    el('button', { type: 'button', role: 'radio', 'aria-checked': String(v === pick), class: v === pick ? 'on' : '', title: `${tierInfo[v].label}: ${tierWhy[v]}`, onclick: () => { saved('askModel', v); render(); } }, v[0].toUpperCase() + v.slice(1)))) : null;
  const agentPick = retinue().length > 1 ? el('select', { class: 'agentpick', 'aria-label': 'agent', title: 'which agent answers (from retinue.json)', onchange: e => { saved('askAgent', e.target.value); render(); } },
    retinue().map(a => el('option', { value: a.name, selected: a.name === agent.name }, a.name))) : null;
  parts.push(el('form', { class: 'askbox', onsubmit: e => { e.preventDefault(); sendAsk(askText); } },
    askText,
    el('div', { class: 'asktools' },
      el('span', { class: 'askhead' }, squire(false)),
      el('span', { class: 'faint small' }, 'Enter to send · Shift+Enter for a new line'),
      el('span', { class: 'grow' }),
      agentPick, tierSeg,
      el('button', { type: 'submit', class: 'askgo', title: 'send to your squire' }, 'Ask ', el('span', { 'aria-hidden': 'true' }, '↑')))));
  for (const [agent, resets] of Object.entries(heldAgents(d.asks || []))) {
    parts.push(el('div', { class: 'stale', role: 'status' }, el('b', {}, `${retinue().length > 1 ? agent + ': the' : 'The'} agent's usage limit is reached.`),
      el('span', {}, resets ? `Chats waiting on it continue by themselves at ${clock(resets)}.` : 'Its reset time could not be read: continue a chat yourself once it has reset.')));
  }
  const convs = conversations(d.asks || []), unread = unreadChats();
  const recentAsks = convs.filter((c, i) => i < (S.allAsks ? 30 : 4) || unread.has(c.id));
  if (recentAsks.length) {
    parts.push(sec('Recent asks', null, convs.length > 4 ? btn(S.allAsks ? 'fewer' : `all ${convs.length}`, () => { S.allAsks = !S.allAsks; render(); }, 'sm') : null));
    parts.push(el('div', { class: 'block' }, recentAsks.map(c => {
      const a = c.last, live = liveAskJob(c.id);
      const runs = !live && a.status === 'ok' ? runsState(a.actions, a.ended) : 'none';
      const state = live ? (live.status === 'waiting' ? ['warn', 'waiting for you'] : ['live', 'working']) : runs === 'unrun' ? ['warn', 'run waiting'] : runs === 'going' ? ['live', 'run going'] : a.status === 'ok' ? ['good', 'answered'] : a.status === 'waiting' ? ['warn', 'waiting for you'] : a.status === 'limited' ? ['warn', a.resets_at ? `limit until ${clock(a.resets_at)}` : 'limit reached'] : ['bad', a.status || '?'];
      const news = unread.has(c.id);
      return el('div', { class: `lrow ${news ? 'unread' : ''}`, role: 'button', tabindex: '0', title: c.first.prompt || '', onclick: () => live ? openJob(live.job_id, false, 'ask') : openAsk(c.id), onkeydown: e => { if (e.key === 'Enter') e.currentTarget.click(); } },
        news ? el('span', { class: 'unreaddot', title: 'a reply you have not opened yet', 'aria-label': 'unread' }) : null,
        el('span', { class: 'askhead' }, squire(!!live && live.status !== 'waiting')),
        el('div', { class: 'lmain' }, el('div', { class: 'lline one', style: 'color:var(--fg)' }, askTitle(c.first.prompt)),
          c.asks.length > 1 ? el('div', { class: 'lline faint small' }, `${c.asks.length} turns · last: ${truncate(askLabel(a.prompt), 60)}`) : null),
        el('div', { class: 'lright' }, el('span', { class: `pill ${state[0]}` }, state[1]), a.model ? el('span', { class: 'chip' }, (a.agent && retinue().length > 1 ? a.agent + ' · ' : '') + modelName(a.model)) : null, when(a.ended || a.started)));
    })));
  }
  const safe = safeItems(), ra = S.runAll;
  const runAllBtn = safe.length > 1 || (ra && ra.running) ? btn(ra && ra.running ? `Starting ${ra.done + 1} of ${ra.total}…` : `Run all reads and dry runs (${safe.length})`, () => runSafeAll(), 'sm',
    { disabled: !!(ra && ra.running), title: 'start every waiting read and dry run now; none of them changes anything, and applies still wait for you' }) : null;
  parts.push(sec('Waiting on you', items.length ? plural(items.length, 'item') : null, runAllBtn));
  parts[parts.length - 1].dataset.key = 'sec:inbox';
  const linkBar = renderLinkBar();
  parts.push(el('div', { class: 'block' }, items.length ? items.map(it => renderInboxItem(it)) : el('div', { class: 'quiet' }, inbox().length ? `Nothing matches "${S.q}".` : 'Nothing waiting on you.')));
  const prep = (S.data.preparing || []).filter(p => !S.q || `${p.task_id} ${p.purpose}`.toLowerCase().includes(S.q));
  if (prep.length) {
    parts.push(sec('Being prepared', String(prep.length), el('span', { class: 'faint small' }, 'agents are still writing these; each waits on you once its agent hands it over')));
    parts.push(el('div', { class: 'block' }, prep.map(renderPreparingRow)));
  }
  parts.push(sec('Running now', runningRows.length ? String(runningRows.length) : null));
  parts[parts.length - 1].dataset.key = 'sec:running';
  parts.push(el('div', { class: 'block' }, runningRows.length ? runningRows : el('div', { class: 'quiet' }, 'Nothing running.')));
  parts.push(sec('Needs a decision', decideAllList.length ? plural(decideAllList.length, 'task') : null, da && da.error ? el('span', { class: 'err' }, `Decide all stopped: ${da.error}`) : null));
  parts[parts.length - 1].dataset.key = 'sec:decide';
  parts.push(el('div', { class: 'block' }, decideRows.length ? decideRows.map(renderDecideRow) : el('div', { class: 'quiet' }, 'Nothing to decide.')));
  parts.push(sec('Done today', doneToday.length ? plural(doneToday.length, 'task') : null));
  parts[parts.length - 1].dataset.key = 'sec:done';
  parts.push(el('div', { class: 'block' }, doneToday.length ? doneToday.map(renderDoneRow) : el('div', { class: 'quiet' }, 'Nothing settled today yet.')));
  if (S.dev) {
    const js = jobs().filter(j => match(j.task_id));
    if (js.length) {
      parts.push(sec('Jobs', `${js.filter(j => j.status !== 'done').length} active`));
      parts.push(el('div', { class: 'block' }, js.map(renderJob)));
    }
    const runsToday = Object.values(d.runs || {}).filter(r => onDay(r.started, today) && match(r.task_id))
      .sort((a, b) => String(b.started || '').localeCompare(String(a.started || '')));
    parts.push(sec('Today', runsToday.length ? plural(runsToday.length, 'run') : null));
    parts.push(el('div', { class: 'block' }, runsToday.length ? runsToday.map(r => renderRunRow(r, false)) : el('div', { class: 'quiet' }, 'No run today.')));
    parts.push(el('div', { class: 'faint small mono', style: 'margin-top:10px' }, d.home || ''));
  }
  if (linkBar) parts.push(el('div', { class: 'linkspace', 'aria-hidden': 'true' }), linkBar);
  return el('div', {}, parts);
}
function renderRunRow(r, live) {
  const t = task(r.task_id), asks = r.waiting && r.waiting.question;
  return el('div', { class: `tr ${live ? `live k-${r.mode}` : ''}`, role: 'button', tabindex: '0', title: `open the log of ${r.run_id}`,
    onclick: e => { if (e.target.closest('a, button')) return; openLog(r.run_id, null); }, onkeydown: e => { if (e.key === 'Enter') openLog(r.run_id, null); } },
    S.dev ? modeTag(r.mode) : null, pill(asks ? 'waiting' : r.status), taskLink(r.task_id || '?'), !S.dev ? areaTag(t) : null, el('span', { class: 'purpose muted' }, asks ? `Asks in the terminal it runs in: ${asks}` : (t || {}).purpose || ''),
    live ? [el('span', { class: 'muted small' }, 'since '), when(r.started), btn('Watch', () => openLog(r.run_id, null), 'sm')] : [chipsRow(usageChips(r)), when(r.started)],
    r.total ? runProgress(r) : null);
}
/* abandoning an inbox item: one reason, then it leaves the inbox; its runs and records stay */
export function abandonItem(id) {
  return { label: 'Abandon...', icons: [icon('abandon')], cls: 'quiet', title: 'take it out of the inbox with a reason: an open task is closed abandoned, one that already ran ok only loses its handover. Its history stays.',
    run: () => { S.abandon = { task: id, note: '', busy: false }; render(); const n = document.getElementById('abandonnote'); if (n) n.focus(); } };
}
/* the reason box, once Abandon... was picked from the run button's menu */
export function abandonForm(id) {
  const f = S.abandon && S.abandon.task === id ? S.abandon : null;
  if (!f) return null;
  const cancel = () => { S.abandon = null; render(); };
  const go = async () => {
    if (!f.note.trim()) { toast('say why it is abandoned'); return; }
    f.busy = true; render();
    try { const r = await post('/api/abandon', { task: id, note: f.note }); toast(`${r.withdrawn ? 'handover withdrawn' : 'abandoned'}: ${shortName(id)}`); S.abandon = null; refresh(); }
    catch (e) { f.busy = false; toast(`abandon failed: ${e.message}`); render(); }
  };
  return el('div', { class: 'line', style: 'flex-direction:column;align-items:stretch;gap:6px;margin-top:6px' },
    el('input', { id: 'abandonnote', placeholder: 'Why (kept in the record)', value: f.note, oninput: e => { f.note = e.target.value; },
      onkeydown: e => { if (e.key === 'Enter') go(); else if (e.key === 'Escape') cancel(); } }),
    el('div', { class: 'line' }, btn(f.busy ? 'abandoning...' : 'Abandon', go, 'sm', { disabled: f.busy }), btn('Cancel', cancel, 'sm')));
}
/* a plan's waiting steps hang under it as a tree, numbered in the order the plan runs them */
function renderInboxItem(it, nested, pos) {
  if (it.schedule) return renderSchedule(it);
  const kids = it.steps || [];
  if (!kids.length) return renderInboxRow(it, nested, pos);
  const steps = el('div', { class: 'substeps' }, kids.map((st, i) => renderInboxItem(st, true, { n: i + 1, last: i === kids.length - 1 })));
  return el('div', { class: 'ibgroup', 'data-key': 'g:' + it.task_id }, renderInboxRow(it, false), steps);
}
function tierTags(it) {
  const tiers = (S.settings && S.settings.policy && S.settings.policy.tiers) || {}, named = Object.values(tiers).some(v => v.length);
  const tier = it.tier && (it.tier !== 'prod' || named) ? tag(it.tier, it.tier === 'prod' ? 'warn' : '') : null;
  const auto = it.auto ? el('span', { class: 'tag accent', title: `your policy starts its ${it.next} by itself (Settings → Tiers and auto-run)` }, 'auto-runs') : null;
  return [tier, auto];
}
function renderInboxRow(it, nested, pos) {
  const id = it.task_id, t = task(id), sub = nested ? ' sub' : '';
  const tree = pos ? el('span', { class: 'tree', 'aria-hidden': 'true' }, pos.last ? '\u2514\u2500' : '\u251c\u2500') : null;
  const stepNo = pos ? el('span', { class: 'stepno', title: `step ${pos.n} of the plan above, in this order` }, `${pos.n}`) : null;
  const changed = it.changed_since_lint ? el('span', { class: 'muted small', title: 'its files or a script it uses changed after its last lint; the run lints them again first' }, 'changed since lint') : null;
  const handed = it.handed ? el('span', { class: 'muted small', title: `handed over by ${it.handed.by || 'an agent'}: ${it.handed.why || 'no reason given'}` }, it.handed.why ? truncate(it.handed.why, 90) : `handed over by ${it.handed.by || 'an agent'}`) : null;
  const again = it.applied_already ? el('span', { class: 'warnnote', title: it.applied_already }, 'already applied ok, same scripts') : null;
  const shared = (it.same_target || []).length ? el('span', { class: 'muted small', title: `also waiting on the same target: ${it.same_target.join(', ')}. Related runs belong in one plan.` }, `same machine as ${plural(it.same_target.length, 'other item')}`) : null;
  const prob = ((S.data && S.data.start_problems) || {})[id];
  const refused = prob ? el('span', { class: 'warnnote', title: `${prob.mode} did not start (${prob.at}): ${prob.why}` }, `${prob.mode} did not start: ${truncate(prob.why, 110)}`) : null;
  const aged = it.stale ? el('span', { class: 'warnnote', title: it.stale }, `waiting ${Math.floor(it.waiting_days)} days: run, supersede or abandon`) : null;
  const changedNote = changeNote(it);
  const sharedNote = (it.shared_changed || []).length ? el('span', { class: 'warnnote', title: `changed since it was handed over: ${it.shared_changed.join(', ')}` },
    `a shared script it uses changed since it was handed over (${plural(it.shared_changed.length, 'file')}); it never runs by itself until handed over again`) : null;
  const stepsNote = (it.steps || []).length ? el('span', { class: 'plan', title: 'a plan: one run does every step below, in this order' }, `plan \u00b7 ${plural(it.steps.length, 'step')} in order`) : null;
  if (S.dev) return el('div', { class: 'ib' + sub, 'data-key': 'i:' + id },
    nested ? null : linkPick(it), tree, stepNo, modeTag(it.next), ...tierTags(it), taskLink(id), el('span', { class: 'purpose', title: it.purpose || '' }, it.purpose || el('span', { class: 'faint' }, 'no purpose')),
    argsTag(it.args), stepsNote, refused, aged, changed, changedNote, sharedNote, again, shared, handed, el('span', { class: 'who', title: it.author || '' }, it.author || ''), when(it.created), runButton(id, it.next));
  const staged = (it.stages || []).length > 0;
  const label = nested ? 'Run this step alone' : staged ? (it.stage_at && it.stage_at !== it.stages[0] ? `Run from ${it.stage_at}` : 'Run all stages') : KIND_LABEL[it.next] || 'Run';
  const notes = [changedNote, sharedNote, refused, aged, again, changed, shared].filter(Boolean);
  return el('div', { class: `card k-${it.next}` + sub, 'data-key': 'i:' + id },
    nested ? null : linkPick(it),
    el('div', { class: 'card-main' },
      el('div', { class: 'card-title' }, stepNo, taskLink(id)),
      el('div', { class: 'card-purpose', title: it.purpose || '' }, it.purpose || el('span', { class: 'faint' }, 'no purpose')),
      el('div', { class: 'card-meta' }, modeTag(it.next), ...tierTags(it), areaTag(t || { id }), argsTag(it.args), stepsNote, handed),
      staged ? stageBar(it, t) : null,
      notes.length ? el('div', { class: 'card-notes' }, notes) : null),
    el('div', { class: 'card-act' }, cardRun(it, label, nested || staged), nested ? null : abandonForm(id)));
}
const KIND_LABEL = { read: 'Read', dry: 'Dry run', apply: 'Apply', verify: 'Verify' };
/* what changed in the task's code since its last run: its agent's words, else which files */
function changeNote(it) {
  const c = it.changed_note;
  if (!c) return null;
  const files = c.files.map(f => codeName(f, it.task_id)).join(', ');
  return el('span', { class: 'changenote', title: `changed since its last run: ${files}` }, el('b', {}, 'Changed: '), c.text || `${files} since its last run`,
    ' ', el('a', { href: '#task/' + encodeURIComponent(it.task_id), onclick: e => { e.preventDefault(); selectTask(it.task_id); } }, 'see the code'));
}
/* a task an agent is still writing: lint may pass, but it is not handed over since its last edit */
function renderPreparingRow(p) {
  const t = task(p.task_id) || { id: p.task_id, purpose: p.purpose };
  const what = p.since ? `edited since it was ${p.since === 'run' ? 'last run' : 'handed over'}: ${p.own_changed.map(f => codeName(f, p.task_id)).join(', ')}` : 'not handed over yet';
  return listRow(t, 'p:' + p.task_id, { text: what, cls: 'faint' }, [p.author ? el('span', { class: 'who small' }, p.author) : null, when(p.edited)]);
}
/* one button for the next run; its menu holds the other way to run it (a dry run alone, or the dry run then the
   apply, whichever the button does not; Settings picks) and Abandon */
function cardRun(it, label, plain) {
  const id = it.task_id, main = runButton(id, it.next, label, false);
  if (plain) return main;
  const both = (it.next === 'dry' || (it.next === 'apply' && changing(id))) && it.changes_prod && !activeJobFor(id);
  const dryFirst = both && (readDryApply() || S.confirm === id + ':dryapply');
  const other = !both ? null : dryFirst
    ? { label: 'Dry run only', icons: modeIcons('dry'), title: 'the dry run alone: nothing changes', run: () => startRun(id, 'dry') }
    : { label: 'Dry run, then apply', icons: modeIcons('dryapply'), cls: 'k-apply', title: 'one job: the dry run, then the apply if it ends ok; the apply still asks before changing anything', run: () => dryApply(id) };
  return splitRun(dryFirst ? dryApplyButton(id) : main, [other, abandonItem(id)], 'i:' + id);
}
/* a staged task's stages as steps: done ticked, the next one lit, the verify check at the end */
function stageBar(it, t) {
  const at = it.stages.indexOf(it.stage_at), steps = [...it.stages, ...((t && t.verify) ? ['verify'] : [])];
  return el('div', { class: 'stagebar', title: 'one run does every stage left, in order' },
    steps.flatMap((name, i) => [i ? el('span', { class: 'arrow', 'aria-hidden': 'true' }, '\u2192') : null,
      el('span', { class: `stage ${i < at ? 'done' : i === Math.max(at, 0) ? 'next' : ''}` }, i < at ? `\u2713 ${name}` : name)]));
}
/* a list row: a status mark, the task's name and area, one muted line under it, the time on the right; clicking it
   opens the task, where its runs are */
export function listRow(t, key, line, right, extra) {
  return el('div', { class: `lrow ${selectedTask() === t.id ? 'sel' : ''} ${isDeciding(t.id) ? 'busy' : ''}`, 'data-key': key, role: 'button', tabindex: '0', title: t.purpose || '',
    onclick: e => { if (e.target.closest('a, button')) return; selectTask(t.id); }, onkeydown: e => { if (e.key === 'Enter' && e.target === e.currentTarget) selectTask(t.id); } },
    tick(t),
    el('div', { class: 'lmain' },
      el('div', { class: 'ltitle' }, el('span', { class: 'name' }, S.dev ? t.id : shortName(t.id)), areaTag(t)),
      line ? el('div', { class: `lline ${line.cls || ''}`, title: line.text }, line.text) : null,
      extra || null),
    el('div', { class: 'lright' }, right));
}
function renderDecideRow(t) {
  const s = t.suggestions || [], last = lastRunOf(t, S.data.runs), judge = lastJudgeFor(t.id), it = inboxItem(t.id);
  const queued = it && it.next === 'verify';
  const why = t.settled ? null : s.length ? s.map(x => x.text).join('; ') : last && last.status !== 'ok' ? `last run ${last.status}${last.last_error ? ': ' + last.last_error : ''}` : t.purpose || '';
  const line = t.settled && t.outcome ? { text: `${t.outcome.state || 'closed'}: ${t.outcome.note || ''}`, cls: 'good' } : why ? { text: why, cls: s.length || (last && last.status !== 'ok') ? 'warn' : '' } : null;
  const act = queued ? el('a', { class: 'small', href: '#inbox', title: 'run the check from the inbox', onclick: e => { e.preventDefault(); scrollToKey('i:' + t.id); } }, 'check queued in your inbox') : t.settled ? null : decideButton(t.id);
  return listRow(t, 'd:' + t.id, line, act, judge || t.judged ? judgeLine(t.id) : null);
}
function renderDoneRow(t) {
  const line = t.kind === 'plan' ? { text: t.purpose || '' } : t.outcome ? { text: `${t.outcome.state || 'done'}: ${t.outcome.note || ''}` } : { text: t.purpose || '' };
  const last = lastRunOf(t, S.data.runs);
  const steps = t.kind === 'plan' && (t.steps || []).length ? el('div', { class: 'ssteps' }, t.steps.map((sid, i) => { const st = task(sid) || { id: sid };
    return el('div', { class: 'sstep', role: 'button', tabindex: '0', onclick: e => { e.stopPropagation(); selectTask(sid); }, onkeydown: e => { if (e.key === 'Enter') { e.stopPropagation(); selectTask(sid); } } },
      el('span', { class: 'stepno' }, String(i + 1)), el('span', { class: 'purpose', title: st.purpose || '' }, st.purpose || shortName(sid)), el('span', { class: 'st done' }, '\u2713 done')); })) : null;
  return listRow(t, 'done:' + t.id, line, [chipsRow(agentChips(t.agents)), t.outcome ? when(t.outcome.closed) : last ? when(last.ended || last.started) : null], steps);
}
