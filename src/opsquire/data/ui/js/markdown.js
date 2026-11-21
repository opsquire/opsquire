import { S } from './state.js';
import { el } from './dom.js';

/* minimal markdown: headings, paragraphs, lists, tables, fenced code, rules, inline code, links, bold. Only text nodes, never HTML. */
const extLink = (href, label) => el('a', { href, target: '_blank', rel: 'noopener' }, label);
/* Plain text with its links made clickable: bare URLs, owner/repo#123, and name#123 for a repo in the home's repos.json. */
function linkify(text) {
  const out = [], slugs = (S.data && S.data.repos) || {};
  const re = /(https?:\/\/[^\s<>()]*[^\s<>().,;:!?'"])|\b([\w.-]+\/[\w.-]+|[\w.-]+)#(\d+)\b/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    const slug = m[2] && (m[2].includes('/') ? m[2] : slugs[m[2]]);
    if (!m[1] && !slug) continue;
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1] ? extLink(m[1], m[1]) : extLink(`https://github.com/${slug}/issues/${m[3]}`, m[0]));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
function inlineMd(text) {
  const out = []; const re = /(`[^`]+`)|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\*\*([^*]+)\*\*/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(...linkify(text.slice(last, m.index)));
    if (m[1]) out.push(el('code', {}, m[1].slice(1, -1)));
    else if (m[2]) out.push(extLink(m[3], m[2]));
    else out.push(el('strong', {}, linkify(m[4])));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(...linkify(text.slice(last)));
  return out;
}
/* A GFM table row's cells: the outer pipes are optional and `\|` is a pipe inside a cell. */
function tableCells(line) {
  const s = line.trim().replace(/^\|/, ''), cells = [];
  let cur = '';
  for (let i = 0; i < s.length; i++) {
    if (s[i] === '\\' && s[i + 1] === '|') { cur += '|'; i++; }
    else if (s[i] === '|') { cells.push(cur.trim()); cur = ''; }
    else cur += s[i];
  }
  if (cur.trim() || !s.endsWith('|')) cells.push(cur.trim());
  return cells;
}
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const NUMERIC_CELL = /^[-+−]?[$€£]?(\d[\d,]*)?\.?\d+(e[-+]?\d+)?\s*(%|[a-zµ]{1,3})?$/i;
/* A column without its own alignment is right-aligned when every filled cell in it is a number. */
function mdTable(header, sep, rows) {
  const align = tableCells(sep).map(c => (c.startsWith(':') && c.endsWith(':') ? 'c' : c.endsWith(':') ? 'r' : c.startsWith(':') ? 'l' : ''));
  const cols = align.length, fit = cells => Array.from({ length: cols }, (_, k) => cells[k] || '');
  const body = rows.map(r => fit(tableCells(r)));
  const cls = align.map((a, k) => {
    if (a) return a === 'l' ? null : a;
    const filled = body.map(r => r[k]).filter(Boolean);
    return filled.length && filled.every(v => NUMERIC_CELL.test(v.replace(/\*\*/g, ''))) ? 'r' : null;
  });
  const row = (tag, cells) => el('tr', {}, cells.map((v, k) => el(tag, { class: cls[k] }, inlineMd(v))));
  return el('div', { class: 'md-table' }, el('table', {}, el('thead', {}, row('th', fit(tableCells(header)))), el('tbody', {}, body.map(r => row('td', r)))));
}
export function markdown(text) {
  const root = el('div', { class: 'md' });
  const lines = String(text || '').replace(/\r/g, '').split('\n');
  let para = [], list = null, i = 0;
  const flushPara = () => { if (para.length) { root.append(el('p', {}, inlineMd(para.join(' ')))); para = []; } };
  const flush = () => { flushPara(); list = null; };
  while (i < lines.length) {
    const line = lines[i]; let m;
    if (line.startsWith('```')) {
      flush(); const buf = []; i++;
      while (i < lines.length && !lines[i].startsWith('```')) buf.push(lines[i++]);
      i++; root.append(el('pre', {}, el('code', {}, buf.join('\n')))); continue;
    }
    if ((m = /^(#{1,6})\s+(.*?)\s*#*$/.exec(line))) { flush(); root.append(el(`h${Math.min(m[1].length + 2, 6)}`, {}, inlineMd(m[2]))); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); root.append(el('hr')); i++; continue; }
    if (line.includes('|') && TABLE_SEP.test(lines[i + 1] || '') && tableCells(line).length === tableCells(lines[i + 1]).length) {
      flush(); const header = line, sep = lines[i + 1], rows = []; i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(lines[i++]);
      root.append(mdTable(header, sep, rows)); continue;
    }
    if ((m = /^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/.exec(line))) {
      flushPara();
      const tag = m[1] ? 'ol' : 'ul';
      if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); root.append(list); }
      list.append(el('li', {}, inlineMd(m[2]))); i++; continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    if (list && /^\s{2,}\S/.test(line)) { list.lastChild.append(' ', ...inlineMd(line.trim())); i++; continue; }
    list = null; para.push(line.replace(/^>\s?/, '').trim()); i++;
  }
  flush();
  return root;
}
