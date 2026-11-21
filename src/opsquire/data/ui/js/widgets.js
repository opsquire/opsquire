/* Small pieces every view shows: pills, tags, times, section heads. */
import { S } from './state.js';
import { relTime, stamp } from './time.js';
import { areaOf, KIND_WORD, linkLabel, safeHref, TICK, tickOf, tone, truncate } from './records.js';
import { $, el } from './dom.js';

const STATUS_MARK = { ok: '\u2713', done: '\u2713', failed: '\u2715', blocked: '\u2298', partial: '\u25D0', waiting: '\u23F8', running: '\u25CF', aborted: '\u21A9', interrupted: '\u23F9' };
export function pill(status) { const m = STATUS_MARK[status]; return el('span', { class: `pill ${tone(status)}` }, m ? `${m} ${status}` : status || '?'); }
export function when(iso) { return el('span', { class: 'muted', title: stamp(iso) }, relTime(iso)); }
export function tag(text, cls) { return el('span', { class: `tag ${cls || ''}` }, text); }
export function modeTag(mode) { return el('span', { class: `kind k-${mode || 'none'}`, title: KIND_WORD[mode] || mode || '' }, KIND_WORD[mode] || mode || '?'); }
export function linkTag(url) {
  const href = safeHref(url);
  return href ? el('a', { class: 'tag accent', href, target: '_blank', rel: 'noopener', title: url }, linkLabel(url)) : tag(linkLabel(url));
}
export function empty(text) { return el('div', { class: 'empty' }, text); }
export function noMatch() { return empty(`Nothing matches "${S.q}".`); }
export function areaTag(t) { const a = (t && t.area) || areaOf(t && t.id); return a ? el('span', { class: 'area', title: 'area' }, a) : null; }
export function tick(t) { const k = tickOf(t); return el('span', { class: `tick ${k}`, title: k === 'done' ? 'settled' : k === 'gone' ? 'abandoned' : 'open' }, TICK[k]); }
export function chipsRow(pairs) { return pairs.length ? el('span', { class: 'chips' }, pairs.map(([k, v]) => el('span', { class: 'chip' }, `${k} `, el('b', {}, v)))) : null; }
export function scrollToKey(key) { const n = $('#main').querySelector(`[data-key="${CSS.escape(key)}"]`); if (n) n.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
export function prodMarker(t) { return t.changes_prod ? tag('changes production', 'warn') : tag('read-only'); }
export function sec(title, extra, more) { return el('h3', { class: 'sec' }, title, extra ? el('span', { class: 'n' }, extra) : null, more || null); }
export const argsTag = args => args ? el('span', { class: 'mono small muted', title: `runs with these arguments: -- ${args}` }, `-- ${truncate(args, 60)}`) : null;
