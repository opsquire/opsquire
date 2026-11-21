/* Pure helpers over the home's records: what a status, a task or a run means, in words. No DOM. */
import { dayKey, duration, parseTime } from './time.js';

export const VIEWS = [['inbox', 'Inbox'], ['live', 'Live'], ['history', 'History'], ['library', 'Library'], ['settings', 'Settings']];
const TONE = {
  ok: 'good', done: 'good', failed: 'bad', blocked: 'bad', partial: 'warn', running: 'live', open: 'live',
  ready: 'neutral', draft: 'grey', abandoned: 'grey', superseded: 'grey', archived: 'grey', skipped: 'grey',
  unchanged: 'grey', interrupted: 'grey', aborted: 'grey', waiting: 'warn', 'in line': 'warn', dead: 'grey',
  'lint-ok': 'good', 'lint-failing': 'bad', unchecked: 'grey',
};
export function tone(status) { return TONE[status] || 'neutral'; }
export function linkLabel(url) {
  const u = String(url || '');
  let m = /github\.com\/[^/]+\/([^/]+)\/(?:pull|issues)\/(\d+)/.exec(u);
  if (m) return `${m[1]}#${m[2]}`;
  m = /github\.com\/[^/]+\/([^/]+)/.exec(u);
  if (m) return m[1];
  if (/slack\.com\//.test(u)) return 'slack';
  m = /^[a-z]+:\/\/([^/]+)/.exec(u);
  return m ? m[1] : u;
}
export function safeHref(url) { return /^https?:\/\//.test(String(url || '')) ? String(url) : null; }
export function money(x) { return typeof x === 'number' ? `$${x.toFixed(2)}` : ''; }
export function truncate(text, n) { const s = String(text || ''); return s.length > n ? s.slice(0, n - 1).trimEnd() + '...' : s; }
export function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }
export function statsText(stats, changesProd) {
  if (!stats || !stats.runs) return 'never run';
  let t = `${plural(stats.runs, 'run')}, ${stats.ok} ok ${changesProd ? 'apply' : 'read'}`;
  if (changesProd && stats.ok) t += `, ${stats.per_ok} runs per ok apply`;
  return t;
}
function hay(parts) { return parts.flat().filter(x => x !== null && x !== undefined).join(' ').toLowerCase(); }
export function taskMatches(task, q, extra) {
  if (!q) return true;
  return hay([task.id, task.purpose, task.area, task.targets || [], task.uses || [], task.status, task.author, extra || '']).includes(q);
}
export function threadMatches(thread, q) { return !q || hay([thread.id, thread.title, thread.areas || [], thread.targets || []]).includes(q); }
export function targetIndex(data) {
  const map = new Map();
  const get = name => { if (!map.has(name)) map.set(name, { name, entries: [], declared: [] }); return map.get(name); };
  for (const t of Object.values(data.tasks || {})) for (const name of t.targets || []) get(name).declared.push(t.id);
  for (const r of Object.values(data.runs || {})) {
    for (const [name, st] of Object.entries(r.targets || {})) get(name).entries.push({ time: String(r.ended || r.started || ''), run: r, tstatus: st });
  }
  for (const g of map.values()) {
    g.entries.sort((a, b) => b.time.localeCompare(a.time));
    g.last = g.entries[0] || null;
  }
  return [...map.values()].sort((a, b) => ((b.last && b.last.time) || '').localeCompare((a.last && a.last.time) || '') || a.name.localeCompare(b.name));
}
export function asksForThread(asks, thread) {
  const ids = new Set(thread.tasks || []);
  return (asks || []).filter(a => (a.task_ids || []).some(id => ids.has(id)));
}
/* a home-relative file name as people read it: a task's own file by its name, another task's under that task's
   name (a plan's steps), a general script's in full */
export function codeName(name, taskId) {
  const m = /^tasks\/[^/]+\/([^/]+)\/(.+)$/.exec(String(name || ''));
  if (!m) return String(name || '');
  return m[1] === taskId ? m[2] : `${shortName(m[1])}/${m[2]}`;
}
export function shortName(id) { const m = /^\d{8}-\d{4}_[^_]+_(.+)$/.exec(String(id || '')); return (m ? m[1] : String(id || '')).replace(/-/g, ' '); }
export function runCommand(id, mode) { return `opsquire run ${id}${mode === 'apply' ? ' --apply' : mode === 'verify' ? ' --verify' : ''}`; }
export function areaOf(id) { const m = /^\d{8}-\d{4}_([^_]+)_/.exec(String(id || '')); return m ? m[1] : ''; }
export function scriptName(id) { const m = /^[a-z0-9-]+_([a-z0-9-]+)$/.exec(String(id || '')); return (m ? m[1] : String(id || '')).replace(/-/g, ' '); }
export function nextLabel(mode) { return mode === 'apply' ? 'Apply' : mode === 'verify' ? 'Check it landed' : 'Check first'; }
export function lastRunOf(t, runs) { const ids = (t && t.runs) || []; for (let i = ids.length - 1; i >= 0; i--) { const r = runs && runs[ids[i]]; if (r) return r; } return null; }
export function decisionList(data) {
  return Object.values(data.tasks || {}).filter(t => t.decide).sort((a, b) => b.id.localeCompare(a.id));
}
export function settledDay(t, runs) {
  if (!t || !t.settled) return null;
  if (t.outcome && t.outcome.closed) return dayKey(t.outcome.closed);
  const last = lastRunOf(t, runs);
  return last ? dayKey(last.ended || last.started) : null;
}
export function tickOf(t) {
  if (!t) return 'open';
  if (t.settled) return ['abandoned', 'superseded', 'archived'].includes(t.status) || (t.outcome && t.outcome.state === 'abandoned') ? 'gone' : 'done';
  return 'open';
}
export const TICK = { done: '✓', open: '◯', gone: '✗' };
export function statusWords(t, inboxItem, decide) {
  if (!t) return { text: '?', tone: 'grey' };
  const st = t.status;
  if (st === 'superseded') return { text: 'Superseded', tone: 'grey' };
  if (st === 'archived') return { text: 'Archived', tone: 'grey' };
  if (st === 'abandoned' || (t.outcome && t.outcome.state === 'abandoned')) return { text: 'Abandoned', tone: 'grey' };
  if (t.settled) return { text: t.outcome && t.outcome.state === 'partial' ? 'Partly done' : 'Done', tone: 'good' };
  if (st === 'running') return { text: 'Running', tone: 'live' };
  if (inboxItem) return { text: inboxItem.next === 'apply' ? 'Waiting for apply' : inboxItem.next === 'verify' ? 'Waiting for you to check it landed' : 'Waiting for your check', tone: 'warn' };
  if (decide) return { text: 'Needs a decision', tone: 'warn' };
  if (st === 'draft') return { text: 'Draft', tone: 'grey' };
  if (st === 'done') return { text: 'Ran ok, not closed', tone: 'warn' };
  if (st === 'failed' || st === 'blocked') return { text: 'Failed', tone: 'bad' };
  return { text: st ? st[0].toUpperCase() + st.slice(1) : 'Draft', tone: tone(st) };
}
export function lastLine(text) { const lines = String(text || '').split('\n').map(l => l.trim()).filter(Boolean); return lines.length ? lines[lines.length - 1] : ''; }
export function verdictWords(line) { const s = String(line || ''); const i = s.indexOf(': '); return /^\d{8}-\d{4}_/.test(s) && i > 0 ? s.slice(i + 2) : s; }
function mb(x) { return typeof x === 'number' ? (x >= 1024 ? `${(x / 1024).toFixed(1)} GB` : `${Math.round(x)} MB`) : ''; }
export function usageChips(r) {
  const out = [];
  if (r && r.duration_s !== null && r.duration_s !== undefined) out.push(['took', duration(r.duration_s)]);
  const u = r && r.usage;
  if (u && typeof u.cpu_s === 'number') out.push(['cpu', duration(u.cpu_s)]);
  if (u && typeof u.max_rss_mb === 'number') out.push(['peak', mb(u.max_rss_mb)]);
  return out;
}
export function agentChips(a) {
  if (!a) return [];
  const out = [];
  if (a.asks) out.push(['asks', String(a.asks)]);
  if (a.pieces) out.push(['agents spun', String(a.pieces)]);
  if (typeof a.cost_usd === 'number' && a.cost_usd > 0) out.push(['agent spend', money(a.cost_usd)]);
  if (a.operator_s) out.push(['your runs', duration(a.operator_s)]);
  return out;
}
export function threadAgents(thread, tasks) {
  const sum = { cost_usd: 0, operator_s: 0, pieces: 0, asks: 0 };
  for (const id of thread.tasks || []) {
    const a = tasks[id] && tasks[id].agents; if (!a) continue;
    sum.cost_usd += Number(a.cost_usd) || 0; sum.operator_s += Number(a.operator_s) || 0; sum.pieces += Number(a.pieces) || 0; sum.asks += Number(a.asks) || 0;
  }
  sum.cost_usd = Math.round(sum.cost_usd * 100) / 100;
  return sum;
}
export function threadSummary(sum) {
  const bits = [];
  if (sum.cost_usd > 0) bits.push(`${money(sum.cost_usd)} agents`);
  if (sum.operator_s > 0) bits.push(`${duration(sum.operator_s)} of your runs`);
  if (sum.pieces > 0) bits.push(plural(sum.pieces, 'agent') + ' spun');
  return bits.join(' · ');
}
/* a day is at most 25 hours, so the cheap age check skips the zone formatting for every older record */
export function onDay(iso, day) { const t = parseTime(iso); return t !== null && Date.now() - t < 2 * 864e5 && dayKey(iso) === day; }
export function todayStats(data, today) {
  const out = { spend: 0, operator_s: 0, ok: 0, failed: 0, runs: 0 };
  for (const r of Object.values(data.runs || {})) {
    if (!onDay(r.started, today)) continue;
    out.runs++; out.operator_s += Number(r.duration_s) || 0;
    if (r.status === 'ok') out.ok++; else if (r.status !== 'running') out.failed++;
  }
  out.billed = 0;
  for (const a of data.asks || []) if (onDay(a.started, today)) { const c = Number(a.cost_usd) || 0; out.spend += c; if (a.billing === 'api-key') out.billed += c; }
  out.spend = Math.round(out.spend * 100) / 100; out.billed = Math.round(out.billed * 100) / 100;
  return out;
}
export const KIND_WORD = { read: 'read', dry: 'dry run', apply: 'apply', dryapply: 'dry run, then apply', verify: 'verify', judge: 'judge', ask: 'ask' };
export const FOLLOWUP_HEAD = 'The operator ran what you handed over';
export const askLabel = text => (String(text || '').startsWith(FOLLOWUP_HEAD) ? 'your runs finished (sent by the console)' : String(text || ''));
