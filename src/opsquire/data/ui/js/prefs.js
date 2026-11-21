/* What this browser keeps: theme, developer mode, run-button and view choices, drafts. */
import { $ } from './dom.js';

export const DEV_KEY = 'opsquire.dev';
const THEME_KEY = 'opsquire.theme';
export const THEMES = ['auto', 'light', 'dark'];
export function readTheme() { try { const t = localStorage.getItem(THEME_KEY); return THEMES.includes(t) ? t : 'auto'; } catch (e) { return 'auto'; } }
export function setTheme(t) {
  try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* private window */ }
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  const b = $('#themetoggle'); if (b) b.textContent = t[0].toUpperCase() + t.slice(1);
}
/* the dry run button's main action: the dry run alone, or the dry run then the apply (the menu offers the other) */
const DRYAPPLY_KEY = 'opsquire.dryapply';
export function readDryApply() { try { return localStorage.getItem(DRYAPPLY_KEY) === '1'; } catch (e) { return false; } }
export function setDryApply(on) { try { if (on) localStorage.setItem(DRYAPPLY_KEY, '1'); else localStorage.removeItem(DRYAPPLY_KEY); } catch (e) { /* private window */ } }
export function readDev() { try { return localStorage.getItem(DEV_KEY) === '1'; } catch (e) { return false; } }
/* drafts survive redraws, reloads and console restarts: kept in this browser only */
export function saved(key, value) {
  try { if (value === undefined) return localStorage.getItem('opsquire.' + key) || ''; if (value) localStorage.setItem('opsquire.' + key, value); else localStorage.removeItem('opsquire.' + key); } catch (e) { /* storage blocked */ }
  return value || '';
}
