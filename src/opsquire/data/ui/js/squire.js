/* The squire: a 16x16 head in the old adventure-game style, mouth moving while the agent works. */
import { S } from './state.js';
import { svg } from './dom.js';

const SQUIRE = ['......RRR.......', '.....RRRR.......', '....hHHHHHHh....', '...hHHHHHHHHh...', '...hHhhhhhhHh...', '...hHSSSSSSHh...', '...hSSSSSSSSh...', '...hSESSSSESh...', '...hSSSSSSSSh...', '...hSSSsSSSSh...', '...hSSSSSSSSh...', '....SSsssSSS....', '....SSSSSSSS....', '.....BSSSSB.....', '......BBBB......', '................'];
const SQUIRE_OPEN = { 11: '....SSMMMMSS....', 12: '....SSSMMSSS....' };
const SQUIRE_COLORS = { R: '#c0392b', H: '#a7b0bc', h: '#5f6b7a', S: '#f2c290', s: '#c98a55', E: '#1b1b1b', B: '#7b4a26', M: '#6d2416',
  A: '#6b4423', K: '#2e5c8a', k: '#1f3f60', W: '#f4efe1', G: '#c9a227', L: '#dfe4ea', l: '#9aa4b1', N: '#9a6a3a', n: '#6e4826', D: '#3b2412', Z: '#6ea6e8', T: '#c3ccd6' };
function squireRects(rows) {
  const out = [];
  rows.forEach((row, y) => [...row].forEach((c, x) => { if (c !== '.') out.push(svg('rect', { x, y, width: 1, height: 1, fill: SQUIRE_COLORS[c] })); }));
  return out;
}
export function squire(talking) {
  const open = SQUIRE.map((r, y) => SQUIRE_OPEN[y] || r);
  return svg('svg', { class: `squire ${talking ? 'talking' : ''}`, viewBox: '0 0 16 16', width: 40, height: 40, 'shape-rendering': 'crispEdges', 'aria-hidden': 'true' },
    svg('g', { class: 'shut' }, squireRects(SQUIRE)), svg('g', { class: 'open' }, squireRects(open)));
}
/* while the agent works without a word, the squire passes the time: 24x16 scenes of four frames, each frame layers
   ([rows, x, y]) and single pixels ({ colour: [[x, y], ...] }) */
const withRows = changes => SQUIRE.map((r, y) => changes[y] || r);
const BARE_HEAD = ['', '', '.....AAAAAA.....', '....AAAAAAAA....', '...AAAAAAAAAA...', '...AASSSSSSAA...', '...ASSSSSSSSA...', '...ASESSSSESA...', '...sSSSSSSSSs...', '....SSSsSSSS....', '....SSSSSSSS....', '....SSsssSSS....', '....SSSSSSSS....', '.....BSSSSB.....', '......BBBB......'];
const HELM = ['..RR...', '.RR....', '.hHHHh.', 'hHHHHHh', 'hHhhhHh', 'hHHHHHh', '.hhhhh.'];
const HORSE = ['....n.n..', '...nNnN..', '..DNNNNN.', '.DDNENNNN', 'DDNNNNNNN', 'DNNNNNNnn', 'DNNNNBnnn', 'DNNNNN...', 'DNNNNN...', 'DNNNNN...', 'DNNNNN...'];
const BOOK = ['KWWWWkWWWWK', 'KWllWkWllWK', 'KWWWWkWWWWK', 'KWlllkWllWK', 'KKKKKkKKKKK'];
const BOOK_TURNING = ['KWWWWkWWWWK', 'KWllWWWllWK', 'KWWWWWWWWWK', 'KWlllkWllWK', 'KKKKKkKKKKK'];
const ZED = ['ZZZZ', '..Z.', '.Z..', 'ZZZZ'];
const pixels = (n, f) => Array.from({ length: n }, (_, i) => f(i));
const SWORD = {
  up: { L: pixels(9, i => [15, 1 + i]), l: pixels(8, i => [16, 2 + i]), G: pixels(4, i => [14 + i, 10]), B: [[15, 11], [15, 12]], S: [[14, 12], [14, 13], [15, 13]] },
  mid: { L: pixels(7, i => [17 + i, 9 - i]), l: pixels(6, i => [18 + i, 9 - i]), G: [[15, 9], [16, 10], [17, 11]], B: [[15, 11], [14, 12]], S: [[13, 12], [13, 13], [14, 13]], T: [[16, 1], [18, 0], [20, 0]] },
  low: { L: pixels(7, i => [17 + i, 11]), l: pixels(6, i => [17 + i, 12]), G: pixels(4, i => [16, 10 + i]), B: [[15, 11], [14, 11]], S: [[13, 11], [13, 12], [14, 12]], T: [[23, 3], [23, 5], [23, 7]] },
};
const SCENES = {
  riding: { caption: 'riding out…', s: 0.8, frames: [0, 1, 0, 1].map(b => [[SQUIRE, 0, b], [HORSE, 15, 6 - b], { S: [[13, 13 + b], [14, 13 + b]], B: [[15, 12 + b]] }]) },
  sword: { caption: 'at sword practice…', s: 1.6, frames: [SWORD.up, SWORD.mid, SWORD.low, SWORD.mid].map(px => [[SQUIRE, 0, 0], px]) },
  helmet: { caption: 'carrying your helmet…', s: 1.2, frames: [0, 1, 0, 1].map(b => [[BARE_HEAD, 0, b], [HELM, 15, 8 - b], { S: [[14, 13 - b], [15, 14 - b]] }]) },
  napping: { caption: 'napping, still on it…', s: 3.2, frames: [0, 1, 2, 3].map(n => [[withRows({ 7: '...hEESSSSEEh...', 11: '....SSSssSSS....' }), 0, 1], ...[[13, 10], [16, 5], [20, 0]].slice(0, n).map(([x, y]) => [ZED, x, y])]) },
  reading: { caption: 'reading up…', s: 3.2, frames: [['d', BOOK], ['s', BOOK], ['d', BOOK], ['s', BOOK_TURNING]].map(([look, book]) => [
    [withRows(look === 'd' ? { 7: '...hSSSSSSSSh...', 8: '...hSESSSSESh...' } : { 7: '...hSSSSSSSSh...', 8: '...hESSSSESSh...' }), 0, 0], [book, 2, 11], { S: [[1, 14], [13, 14]] }]) },
};
function sceneRows(layers) {
  const grid = pixels(16, () => Array(24).fill('.'));
  const set = (x, y, c) => { if (c !== '.' && x >= 0 && x < 24 && y >= 0 && y < 16) grid[y][x] = c; };
  for (const layer of layers) {
    if (Array.isArray(layer)) { const [rows, x, y] = layer; rows.forEach((row, dy) => [...row].forEach((c, dx) => set(x + dx, y + dy, c))); }
    else for (const [c, pts] of Object.entries(layer)) for (const [x, y] of pts) set(x, y, c);
  }
  return grid.map(r => r.join(''));
}
function sceneSvg(scene) {
  const n = scene.frames.length;
  return svg('svg', { class: 'squire scene', viewBox: '0 0 24 16', width: 60, height: 40, 'shape-rendering': 'crispEdges', 'aria-hidden': 'true' },
    scene.frames.map((f, i) => svg('g', { style: `animation: frame4 ${scene.s}s linear ${((i / n) - 1) * scene.s}s infinite` }, squireRects(sceneRows(f)))));
}
/* the squire talks for a few seconds after each thing the agent says, and passes the time with a scene while it works
   without a word; d.live is the avatar that does this (the thinking row, or the reply that just came in) */
const TALK_MS = 3000, SCENE_MS = 12000;
function nextScene(current) { const names = Object.keys(SCENES).filter(n => n !== current); return names[Math.floor(Math.random() * names.length)]; }
export function paintSquire(d) {
  const slot = S.pane === d ? d.live : null;
  if (!slot) { clearInterval(d.squireTimer); d.squireTimer = null; return; }
  const now = Date.now(), texting = now - (d.spoke || 0) < TALK_MS;
  if (slot.caption && !texting && (!d.scene || now - d.sceneAt > SCENE_MS)) { d.scene = nextScene(d.scene); d.sceneAt = now; }
  const key = texting ? 'talk' : slot.caption ? d.scene : 'still';
  if (slot.key === key) return;
  slot.key = key;
  const scene = SCENES[key];
  slot.avatar.replaceChildren(scene ? sceneSvg(scene) : squire(texting));
  if (slot.caption) slot.caption.textContent = scene ? scene.caption : 'writing…';
}
