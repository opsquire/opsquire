/* The state from /api/state, kept current by the server's event stream. */
import { liveAskJob, S } from './state.js';
import { $, toast } from './dom.js';
import { empty } from './widgets.js';
import { api, REQUEST_HEADERS } from './api.js';
import { applyHash } from './nav.js';
import { render } from './render.js';
import { openJob } from './jobs.js';
import { mayTakePane, paneConversation, renderChat, runsKey } from './chat.js';

let refreshTimer = null, loading = false, loadAgain = false, retry = 2000;
export function refresh(delay = 150) { clearTimeout(refreshTimer); refreshTimer = setTimeout(load, delay); }
export async function load() {
  if (loading) { loadAgain = true; return; }
  loading = true;
  try {
    const r = await api('/api/state' + (S.version && S.data ? `?since=${encodeURIComponent(S.version)}` : ''));
    if (r && r.kind === 'full') S.data = r;
    else if (r && r.kind === 'patch' && S.data && r.since === S.version) {
      for (const [key, entries] of Object.entries(r.changed || {})) {
        const into = S.data[key] = S.data[key] || {};
        for (const [id, value] of Object.entries(entries)) { if (value === null) delete into[id]; else into[id] = value; }
      }
      Object.assign(S.data, r.replace || {});
    } else if (r && r.kind !== 'same') { S.version = null; loading = false; return load(); }
    S.data.jobs = r.jobs; S.data.start_problems = r.start_problems || {}; S.data.code = r.code; S.version = r.version;
    const asking = (r.jobs || []).find(j => j.status === 'waiting' && !S.shownPrompts.has(j.job_id));
    if (asking) { S.shownPrompts.add(asking.job_id); if (mayTakePane(asking)) openJob(asking.job_id, true, asking.mode === 'ask' ? 'ask' : null); else toast(`${asking.mode === 'ask' ? 'an ask' : asking.task_id} is waiting for you`); }
    if (S.pane && S.pane.built && S.pane.chat && S.pane.runsKey !== runsKey(S.pane)) renderChat(S.pane);
    const conv = paneConversation(S.pane), live = conv && liveAskJob(conv);
    if (live && !(S.pane.kind === 'job' && S.pane.id === live.job_id)) openJob(live.job_id, true, 'ask');
    S.error = null; S.pageCode = S.pageCode || (r.code && r.code.page);
    retry = 2000;
    if (S.hash === null) applyHash(); else render();
  } catch (e) {
    S.error = `state: ${e.message}`;
    if (S.data) render(); else $('#main').replaceChildren(empty(`Cannot load the home: ${e.message}. Retrying.`));
    if (!loadAgain) refresh(retry);
    retry = Math.min(retry * 2, 30000);
  }
  loading = false;
  if (loadAgain) { loadAgain = false; refresh(); }
}
let es = null, backoff = 1000;
function setConn(state, text) { const c = $('#conn'); c.className = state; c.textContent = text; }
function onStreamEvent(name, data) {
  if (name === 'hello') { backoff = 1000; setConn('live', 'live'); refresh(); }
  else if (name === 'change') { let v = null; try { v = JSON.parse(data).version; } catch (_) {} if (!v || v !== S.version) refresh(); }
}
export async function connect() {
  if (es) es.abort();
  const ctl = es = new AbortController();
  try {
    const r = await fetch('/api/stream', { headers: REQUEST_HEADERS, signal: ctl.signal });
    if (!r.ok || !r.body) throw new Error(`${r.status} ${r.statusText}`);
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value;
      let end;
      while ((end = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, end); buf = buf.slice(end + 2);
        onStreamEvent((block.match(/^event: (.*)$/m) || [])[1], (block.match(/^data: (.*)$/m) || [])[1]);
      }
    }
  } catch (_) {}
  if (es !== ctl) return;
  es = null;
  setConn('down', `reconnecting in ${Math.round(backoff / 1000)}s`);
  setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 30000);
}
