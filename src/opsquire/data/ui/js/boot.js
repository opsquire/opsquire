/* Start-up: the entry module, so it runs once every other module is loaded. */
import { S } from './state.js';
import { $ } from './dom.js';
import { readDev, readTheme, saved, setTheme, THEMES } from './prefs.js';
import { applyHash, setDev } from './nav.js';
import { markShownChatRead, render } from './render.js';
import { tickBars } from './schedules.js';
import { loadSettings } from './settings.js';
import { closePane } from './pane.js';
import { connect, load, refresh } from './live.js';

setInterval(tickBars, 1000);
function boot() {
  S.dev = readDev();
  setTheme(readTheme());
  $('#themetoggle').addEventListener('click', () => setTheme(THEMES[(THEMES.indexOf(readTheme()) + 1) % THEMES.length]));
  S.hist.sort = saved('histSort') || 'recent';
  document.documentElement.classList.toggle('dev', S.dev);
  const devBtn = $('#devtoggle');
  devBtn.setAttribute('aria-pressed', String(S.dev));
  devBtn.addEventListener('click', () => setDev(!S.dev));
  const search = $('#search');
  let qTimer = null;
  search.addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.q = search.value.trim().toLowerCase(); render(); }, 120); });
  document.addEventListener('keydown', e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    if (e.key === 'Escape') { if (S.menu) { S.menu = null; render(); } else if (typing) e.target.blur(); else if (S.pane) closePane(); }
    else if (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); search.focus(); search.select(); }
  });
  document.addEventListener('click', e => { if (S.menu && !e.target.closest('.split')) { S.menu = null; render(); } });
  window.addEventListener('hashchange', () => { if (S.data) applyHash(); });
  let rTimer = null;
  window.addEventListener('resize', () => { if (S.view === 'history' && S.hist.mode === 'timeline' && S.data) { clearTimeout(rTimer); rTimer = setTimeout(render, 150); } });
  setInterval(() => { if (S.data && !document.hidden) render(); }, 60000);
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); if (S.data) markShownChatRead(); } });
  connect();
  load();
  loadSettings();
}
boot();
