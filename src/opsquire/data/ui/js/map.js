/* Diagrams: a thread's tasks as a flow, and every story on a timeline. */
import { run, S, selectedTask, task } from './state.js';
import { parseTime, stamp, zoneParts } from './time.js';
import { areaOf, KIND_WORD, plural, shortName, taskMatches, tone, truncate } from './records.js';
import { $, btn, el, svg } from './dom.js';
import { empty } from './widgets.js';
import { selectTask } from './nav.js';
import { render } from './render.js';
import { keptByHistory } from './history.js';

const TASK_ID_RE = /\b\d{8}-\d{4}_[a-z0-9-]+_[a-z0-9-]+\b/g;
/* relations between tasks, derived from what the records say about each other */
function deriveEdges(tasks, ids) {
  const set = new Set(ids), seen = new Set(), edges = [];
  const add = (a, b, kind) => {
    if (a === b || !set.has(a) || !set.has(b)) return;
    const k = a + '>' + b; if (seen.has(k)) return;
    seen.add(k); edges.push({ from: a, to: b, kind });
  };
  for (const id of ids) {
    const t = tasks[id]; if (!t) continue;
    for (const s of t.steps || []) if (s) add(id, s, 'step');
    if (t.superseded_by) add(id, t.superseded_by, 'superseded');
    const text = [t.purpose, t.outcome && t.outcome.note, t.abandoned_reason].filter(Boolean).join('\n');
    for (const m of text.match(TASK_ID_RE) || []) add(id, m, 'mention');
  }
  return edges;
}
function wrapLines(text, maxChars, maxLines) {
  const words = String(text || '').split(/\s+/).filter(Boolean), lines = [];
  let i = 0;
  while (i < words.length && lines.length < maxLines) {
    let cur = words[i++];
    while (i < words.length && (cur + ' ' + words[i]).length <= maxChars) cur += ' ' + words[i++];
    lines.push(cur);
  }
  if (i < words.length && lines.length) lines[lines.length - 1] = truncate(lines[lines.length - 1] + ' ' + words.slice(i).join(' '), maxChars);
  return lines.map(l => (l.length > maxChars ? truncate(l, maxChars) : l));
}
function flowLayout(ids, edges, g) {
  const idx = new Map(ids.map((id, i) => [id, i]));
  const pitch = g.bw + g.gap;
  const boxes = ids.map((id, i) => ({ id, x: g.pad + i * pitch, y: g.top }));
  const arcs = [];
  for (const e of edges) {
    const a = idx.get(e.from), b = idx.get(e.to);
    if (a === undefined || b === undefined) continue;
    const d = Math.abs(a - b);
    arcs.push({ from: e.from, to: e.to, kind: e.kind, a, b, lift: d === 1 ? 0 : Math.min(g.top - 2, g.lift + d * g.liftStep) });
  }
  return { w: g.pad * 2 + ids.length * pitch - g.gap, h: g.top + g.bh + g.dots, boxes, arcs };
}
function runDots(t, max) {
  const rs = (t.runs || []).map(run).filter(Boolean);
  return rs.slice(Math.max(0, rs.length - max)).map(r => ({ tone: tone(r.status), title: `${r.run_id} ${r.mode} ${r.status || '?'}` }));
}
export function flowSvg(ids, mini) {
  const g = mini ? { bw: 22, bh: 10, gap: 8, pad: 2, top: 12, dots: 7, lift: 5, liftStep: 2 } : { bw: 124, bh: 36, gap: 22, pad: 4, top: 26, dots: 12, lift: 10, liftStep: 4 };
  const edges = deriveEdges(S.data.tasks || {}, ids);
  const L = flowLayout(ids, edges, g);
  const root = svg('svg', { class: 'flow', width: L.w, height: L.h, viewBox: `0 0 ${L.w} ${L.h}`, role: 'img', 'aria-label': `${plural(ids.length, 'task')} with ${plural(edges.length, 'link')}` });
  const mid = mini ? 'arr-m' : 'arr-f';
  root.append(svg('defs', {}, svg('marker', { id: mid, markerWidth: 6, markerHeight: 6, refX: 5.5, refY: 3, orient: 'auto', markerUnits: 'userSpaceOnUse' }, svg('path', { d: 'M0,0 L6,3 L0,6 z' }))));
  const pos = new Map(L.boxes.map(b => [b.id, b]));
  for (const a of L.arcs) {
    const from = pos.get(a.from), to = pos.get(a.to);
    let d;
    if (a.lift === 0) {
      const cy = g.top + g.bh / 2;
      const x1 = from.x < to.x ? from.x + g.bw : from.x, x2 = from.x < to.x ? to.x : to.x + g.bw;
      d = `M${x1},${cy} L${x2},${cy}`;
    } else {
      const x1 = from.x + g.bw / 2, x2 = to.x + g.bw / 2;
      d = `M${x1},${g.top} Q${(x1 + x2) / 2},${g.top - a.lift * 2} ${x2},${g.top}`;
    }
    root.append(svg('path', { class: `edge ${a.kind}`, d, 'marker-end': `url(#${mid})` }, svg('title', {}, `${a.kind}: ${shortName(a.from)} -> ${shortName(a.to)}`)));
  }
  const sel = selectedTask();
  for (const b of L.boxes) {
    const t = task(b.id) || { id: b.id, status: '?' };
    const st = tone(t.status);
    const box = svg('rect', { class: `box st-${st} ${sel === b.id ? 'sel' : ''}`, x: b.x, y: b.y, width: g.bw, height: g.bh, rx: mini ? 2 : 4, tabindex: '0', role: 'button',
      onclick: () => selectTask(b.id), onkeydown: e => { if (e.key === 'Enter') selectTask(b.id); } },
      svg('title', {}, `${b.id}\n${t.status || '?'}${t.purpose ? '\n' + truncate(t.purpose, 140) : ''}`));
    root.append(box);
    if (!mini) {
      const lines = wrapLines(shortName(b.id), 19, 2);
      lines.forEach((ln, i) => root.append(svg('text', { class: `st-${st}`, x: b.x + g.bw / 2, y: b.y + (lines.length === 1 ? 22 : 15 + i * 12), 'text-anchor': 'middle' }, ln)));
    }
    const dots = runDots(t, mini ? 5 : 14), r = mini ? 1.6 : 3, pitch = mini ? 4.2 : 8;
    const x0 = b.x + g.bw / 2 - ((dots.length - 1) * pitch) / 2;
    dots.forEach((dt, i) => root.append(svg('circle', { class: `dot ${dt.tone}`, cx: x0 + i * pitch, cy: b.y + g.bh + (mini ? 4 : 7), r }, svg('title', {}, dt.title))));
  }
  return root;
}
const MAP_WINDOWS = [[6, '6 hours'], [24, '1 day'], [48, '2 days'], [168, '7 days'], [336, '14 days'], [720, '30 days']];
/* the map: one lane per story (thread), every task a bar from its creation to its last run, with a dot per run.
   The window slides: hours or days, back and forward in steps of one window. */
function taskSpan(t) {
  const t0 = parseTime(t.created); if (t0 === null) return null;
  let t1 = t0, runs = [];
  for (const id of t.runs || []) {
    const r = run(id); if (!r) continue;
    const a = parseTime(r.started), b = parseTime(r.ended) || a;
    if (a === null) continue;
    runs.push({ at: a, mode: r.mode, failed: ['failed', 'blocked', 'partial'].includes(r.status), title: `${KIND_WORD[r.mode] || r.mode} ${r.status || '?'} ${stamp(r.started)}` });
    if (b > t1) t1 = b;
  }
  if (t.outcome && parseTime(t.outcome.closed) > t1) t1 = parseTime(t.outcome.closed);
  return { t0, t1, runs };
}
function mapTicks(start, end) {
  const span = end - start, hourly = span <= 48 * 3600e3, step = hourly ? (span <= 6 * 3600e3 ? 1 : span <= 24 * 3600e3 ? 2 : 6) * 3600e3 : 86400e3;
  const ticks = [];
  const p0 = zoneParts(start), off = Date.UTC(+p0.y, +p0.mo - 1, +p0.d, +p0.h, +p0.mi, +p0.s) - Math.floor(start / 1000) * 1000;
  const frac = ((off % 3600e3) + 3600e3) % 3600e3;
  for (let t = Math.ceil((start - frac) / 3600e3) * 3600e3 + frac; t <= end; t += 3600e3) {
    const p = zoneParts(t);
    if (p.mi !== '00') continue;
    const dayStart = p.h === '00';
    if (hourly ? Number(p.h) % (step / 3600e3) !== 0 : !dayStart) continue;
    const label = hourly && !dayStart ? `${p.h}:00` : `${p.mo}-${p.d}`;
    ticks.push({ t, label, strong: dayStart || !hourly, weekend: !hourly && p.weekend });
  }
  return ticks;
}
export function renderMap() {
  const q = S.q, now = Date.now(), W = S.map;
  const end = Math.min(W.end || now, now), start = end - W.hours * 3600e3;
  const all = Object.values(S.data.tasks || {}).filter(t => t.kind !== 'general' && (!q || taskMatches(t, q)) && keptByHistory(t));
  const spans = new Map(); let outside = 0;
  for (const t of all) {
    const sp = taskSpan(t); if (!sp) continue;
    if (sp.t1 < start || sp.t0 > end) { outside++; continue; }
    spans.set(t.id, sp);
  }
  const setWin = (hours, endMs) => { S.map = { hours, end: endMs }; render(); };
  const ctl = el('div', { class: 'mapctl' },
    el('select', { 'aria-label': 'window', onchange: e => setWin(Number(e.target.value), W.end) },
      MAP_WINDOWS.map(([h, label]) => el('option', { value: h, selected: W.hours === h }, label))),
    btn('◀', () => setWin(W.hours, end - W.hours * 3600e3), 'sm', { title: 'earlier: slide the window back' }),
    btn('▶', () => setWin(W.hours, Math.min(now, end + W.hours * 3600e3)), 'sm', { title: 'later: slide the window forward', disabled: !W.end }),
    W.end ? btn('now', () => setWin(W.hours, null), 'sm', { title: 'back to the window ending now' }) : null,
    el('span', { class: 'muted small' }, `${stamp(new Date(start).toISOString(), false).slice(5)} to ${W.end ? stamp(new Date(end).toISOString(), false).slice(5) : 'now'}`),
    outside ? btn(`${plural(outside, 'task')} earlier`, () => setWin(W.hours, end - W.hours * 3600e3), 'sm link', { title: 'slide back to see them' }) : null,
    el('span', { class: 'grow' }),
    el('span', { class: 'legend' }, ['read', 'dry', 'apply', 'verify'].map(k => el('span', {}, el('i', { class: `dot k-${k}` }), KIND_WORD[k])),
      el('span', {}, el('i', { class: 'dot fail' }), 'failed')));
  if (!spans.size) return el('div', {}, ctl, empty(all.length ? 'Nothing happened in this window.' : q ? `Nothing matches "${S.q}".` : 'No tasks yet.'));
  const main = $('#main'), avail = Math.max(420, (main ? main.clientWidth : 900) - 34);
  const lblW = Math.min(340, Math.max(220, Math.round(avail * 0.26))), plotW = avail - lblW, axH = 26, threadH = 28, rowH = 24, barH = 12;
  const x = t => lblW + Math.max(0, Math.min(1, (t - start) / (end - start))) * plotW;
  const groups = new Map();
  for (const [id, sp] of spans) {
    const t = task(id), key = t.thread || id;
    if (!groups.has(key)) groups.set(key, { key, title: threadTitle(key, t), items: [], last: 0, first: Infinity });
    const g = groups.get(key);
    g.items.push({ id, sp }); g.last = Math.max(g.last, sp.t1); g.first = Math.min(g.first, sp.t0);
  }
  const tree = [...groups.values()].sort((a, b) => b.last - a.last);
  tree.forEach(g => g.items.sort((a, b) => a.sp.t0 - b.sp.t0 || a.id.localeCompare(b.id)));
  const H = axH + tree.reduce((h, g) => h + threadH + g.items.length * rowH + 6, 0), TW = lblW + plotW;
  const root = svg('svg', { class: 'map tree', width: TW, height: H, viewBox: `0 0 ${TW} ${H}`, role: 'img', 'aria-label': 'stories and their tasks over time' });
  const ticks = mapTicks(start, end);
  for (const tk of ticks) {
    const tx = x(tk.t), nearNow = !W.end && x(now) - tx < 34;
    root.append(svg('line', { class: `grid ${tk.strong ? 'strong' : ''}`, x1: tx, y1: axH - 6, x2: tx, y2: H }));
    if (!nearNow) root.append(svg('text', { class: `ax ${tk.strong ? 'strong' : ''}`, x: tx + 3, y: 15 }, tk.label));
  }
  if (!W.end) root.append(svg('line', { class: 'nowline', x1: x(now), y1: axH - 6, x2: x(now), y2: H }), svg('text', { class: 'ax now', x: x(now) - 3, y: 15, 'text-anchor': 'end' }, 'now'));
  const sel = selectedTask();
  let y = axH;
  for (const g of tree) {
    const gy = y;
    root.append(svg('rect', { class: 'trow-bg', x: 0, y: gy, width: TW, height: threadH, rx: 6 }));
    root.append(svg('text', { class: 'tname', x: 10, y: gy + 18 }, truncate(g.title, Math.floor((lblW - 20) / 7.2)), svg('title', {}, g.title)));
    root.append(svg('rect', { class: 'span', x: x(g.first), y: gy + 11, width: Math.max(4, x(g.last) - x(g.first)), height: 6, rx: 3 }));
    y += threadH;
    g.items.forEach((it, i) => {
      const t = task(it.id), st = tone(t.status), ry = y + i * rowH, mid = ry + rowH / 2, last = i === g.items.length - 1;
      root.append(svg('path', { class: 'branch', d: `M16,${ry - (i ? 0 : 4)} V${last ? mid : ry + rowH} M16,${mid} H28` }));
      root.append(svg('text', { class: `leaf st-${st}`, x: 34, y: mid + 4, onclick: () => selectTask(it.id) }, truncate(shortName(it.id), Math.floor((lblW - 44) / 6.6)), svg('title', {}, `${it.id}\n${t.status || '?'}${t.purpose ? '\n' + truncate(t.purpose, 140) : ''}`)));
      const x0 = x(it.sp.t0), x1 = Math.max(x0 + 6, x(it.sp.t1));
      root.append(svg('rect', { class: `box st-${st} ${sel === it.id ? 'sel' : ''}`, x: x0, y: mid - barH / 2, width: x1 - x0, height: barH, rx: 6, tabindex: '0', role: 'button',
        onclick: () => selectTask(it.id), onkeydown: e => { if (e.key === 'Enter') selectTask(it.id); } },
        svg('title', {}, `${it.id}\n${t.status || '?'}\n${plural(it.sp.runs.length, 'run')}`)));
      it.sp.runs.forEach(r => root.append(svg('circle', { class: `dot k-${r.mode} ${r.failed ? 'fail' : ''}`, cx: x(r.at), cy: mid, r: 4 }, svg('title', {}, r.title))));
    });
    y += g.items.length * rowH + 6;
  }
  return el('div', {}, ctl, el('div', { class: 'block scroll', style: 'padding:6px 0' }, root));
}
function threadTitle(key, t) {
  const th = (S.data.threads || []).find(x => x.id === key);
  return th ? th.title : `${shortName(t.id)} (${t.area || areaOf(t.id)})`;
}
