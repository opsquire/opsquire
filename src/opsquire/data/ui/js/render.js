/* Drawing the page: the current view, the banners above it and the tab bar. */
import { conversationOf, inbox, jobs, S } from './state.js';
import { plural, VIEWS } from './records.js';
import { $, btn, copyText, el, toast } from './dom.js';
import { modeTag, pill } from './widgets.js';
import { post } from './api.js';
import { switchView, taskLink } from './nav.js';
import { renderInbox } from './inbox.js';
import { renderHistory } from './history.js';
import { renderSettings } from './settings.js';
import { renderLibrary } from './library.js';
import { liveRuns, locksByRun, renderLiveView } from './liveview.js';
import { renderPane } from './pane.js';
import { paneConversation } from './chat.js';
import { refresh } from './live.js';

export function render() {
  if (!S.data) return;
  const y = window.scrollY;
  renderSeg();
  const main = $('#main');
  const active = document.activeElement;
  const keep = active && active.id && (main.contains(active) || $('#pane').contains(active)) ? { id: active.id, start: active.selectionStart, end: active.selectionEnd } : null;
  const views = { inbox: renderInbox, live: renderLiveView, history: renderHistory, library: renderLibrary, settings: renderSettings };
  main.replaceChildren(...[renderStale(), S.view === 'live' ? null : renderLive(), (views[S.view] || renderInbox)()].filter(Boolean));
  renderPane();
  markShownChatRead();
  if (keep) {
    const n = document.getElementById(keep.id);
    if (n) { n.focus({ preventScroll: true }); if (typeof keep.start === 'number' && n.setSelectionRange) { try { n.setSelectionRange(keep.start, keep.end); } catch (e) { /* not a text field */ } } }
  }
  if (S.scrollTo) {
    const node = main.querySelector(`[data-key="${CSS.escape(S.scrollTo)}"]`);
    S.scrollTo = null;
    if (node) { node.scrollIntoView({ block: 'start' }); return; }
  }
  window.scrollTo(0, y);
}
/* The server keeps the code it started with and this tab the page it loaded. A server with no status at all predates
   the check, and is serving a newer page than itself. */
function staleCopy() {
  const c = S.data.code;
  const command = `opsquire ui --port ${location.port}`;
  const restart = { command, jobs: jobs().filter(j => j.status !== 'done').length };
  if (!c) return { ...restart, title: 'This console is running old code: it is older than this page.', detail: 'What it does may not match the current code until it is restarted.' };
  if (c.stale) {
    const names = c.changed.slice(0, 6).join(', ') + (c.changed.length > 6 ? ` and ${c.changed.length - 6} more` : '');
    const what = [c.changed.length ? `changed since it started: ${names}` : null, c.installed !== c.version ? `installed ${c.installed}, running ${c.version}` : null].filter(Boolean).join('; ');
    return { ...restart, title: 'This console is running old code.', short: what, detail: `On disk, ${what}. It keeps doing what the old code does until it is restarted.` };
  }
  if (S.pageCode && c.page !== S.pageCode) return { title: 'This page is out of date.', detail: 'The console has a newer version of it. Reload the page.' };
  return null;
}
function renderStale() {
  const s = staleCopy();
  if (!s) return null;
  const hint = [s.detail, s.command ? 'After restarting, open the login link it prints: a restart ends this login.' : null].filter(Boolean).join(' ');
  return el('div', { class: 'stale', role: 'alert', id: 'stale', title: hint }, el('b', {}, s.title), el('span', { class: 'why' }, s.short || s.detail),
    s.command ? el('span', { class: 'cmd' }, el('span', {}, s.command), btn('copy', () => copyText(s.command), 'sm', { title: 'copy to clipboard' })) : null,
    s.jobs ? el('span', { class: 'small', title: `stopping the console interrupts ${s.jobs === 1 ? 'it' : 'them'}` }, `${plural(s.jobs, 'job')} running from it: let ${s.jobs === 1 ? 'it' : 'them'} finish first`) : null);
}
function renderSeg() {
  const n = inbox().length, chats = unreadChats().size;
  $('#seg').replaceChildren(...VIEWS.map(([id, label]) => el('button', { role: 'tab', 'aria-pressed': String(S.view === id), onclick: () => switchView(id) }, label,
    id === 'inbox' && n ? el('span', { class: 'n', title: `${n} waiting on you` }, String(n)) : null,
    id === 'inbox' && chats ? el('span', { class: 'n chats', title: `${plural(chats, 'chat')} with news since you last opened ${chats === 1 ? 'it' : 'them'}` }, String(chats)) : null,
    id === 'live' && liveRuns().length ? el('span', { class: 'n chats', title: `${plural(liveRuns().length, 'run')} going now` }, String(liveRuns().length)) : null)));
  $('#brand').title = `home ${S.data.home || ''}`;
  document.title = chats ? `(${chats}) Opsquire` : 'Opsquire';
}
/* chats with a reply you have not opened yet, kept per operator (var/chats-read/<user>.json), so every tab agrees, and
   chats whose agent is waiting on your answer somewhere other than the pane */
const unreadInHome = () => new Set((S.data && S.data.unread_chats) || []);
export function unreadChats() {
  const out = unreadInHome(), shown = paneConversation(S.pane);
  for (const j of jobs()) if (j.mode === 'ask' && j.status === 'waiting' && j.ask_id && conversationOf(j.ask_id) !== shown) out.add(conversationOf(j.ask_id));
  return out;
}
/* the chat on screen counts as read: it posts once per conversation until the state says it is read */
export function markShownChatRead() {
  const conv = paneConversation(S.pane);
  if (!conv || document.hidden || !unreadInHome().has(conv) || S.marking.has(conv)) return;
  S.marking.add(conv);
  post('/api/chatread', { conversation: conv }).then(() => refresh(), err => toast(`cannot mark the chat read: ${err.message}`)).finally(() => S.marking.delete(conv));
}
/* one line, always there, so the page never jumps: what runs and how many machines are locked; the Live tab has it all */
function renderLive() {
  const runs = liveRuns(), groups = locksByRun(), items = [];
  const locked = groups.reduce((n, g) => n + g.locks.length, 0), dead = groups.reduce((n, g) => n + g.locks.filter(l => !l.alive).length, 0);
  for (const r of runs.slice(0, 2)) items.push(el('span', { class: 'live-item', title: r.waiting ? `waiting for your answer: ${r.waiting.question}` : r.in_line_for ? `waits in line for ${r.in_line_for}` : '' }, pill(r.waiting ? 'waiting' : r.in_line_for ? 'in line' : 'running'), taskLink(r.task_id || '?'), modeTag(r.mode)));
  if (runs.length > 2) items.push(el('span', { class: 'muted' }, `+${runs.length - 2} more`));
  if (!runs.length) items.push(el('span', { class: 'faint' }, 'Nothing running'));
  if (locked) items.push(el('span', { class: 'muted' }, plural(locked, 'machine'), ' locked'));
  if (dead) items.push(el('span', { class: 'err' }, plural(dead, 'dead lock')));
  if (S.error) items.push(el('span', { class: 'err' }, S.error));
  if (runs.length || locked) items.push(el('a', { href: '#live', class: 'small', onclick: e => { e.preventDefault(); switchView('live'); } }, 'details'));
  return el('div', { id: 'liveline' }, items);
}
