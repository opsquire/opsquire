/* Small line icons drawn in the text colour, one per run kind and for menus. */
import { svg } from './dom.js';

const PATHS = {
  read: ['M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z'],
  dry: ['M7 4.5v15l12.5-7.5z'],
  apply: ['M13 2 4 14h7l-1 8 9-12h-7z'],
  verify: ['M20 6 9 17l-5-5'],
  more: ['M6 9l6 6 6-6'],
  abandon: ['M18 6 6 18', 'M6 6l12 12'],
};
export function icon(name) {
  const filled = name === 'apply';
  return svg('svg', { class: `icon i-${name}`, viewBox: '0 0 24 24', width: '14', height: '14', 'aria-hidden': 'true', fill: filled ? 'currentColor' : 'none',
    stroke: 'currentColor', 'stroke-width': filled ? '1.2' : '2.2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' },
  (PATHS[name] || []).map(d => svg('path', { d })));
}
/* the icons of a run kind: a dry run then apply shows both */
export function modeIcons(mode) {
  return mode === 'dryapply' ? [icon('dry'), icon('apply')] : PATHS[mode] ? [icon(mode)] : [];
}
