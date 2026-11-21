import { S } from './state.js';

export function parseTime(iso) { const t = iso ? Date.parse(iso) : NaN; return Number.isNaN(t) ? null : t; }
function pad(n) { return String(n).padStart(2, '0'); }
/* times: records are UTC; people read them in the zone the settings name (UTC until they load) */
export function zoneSetting() { return (S.settings && S.settings.time_zone) || 'UTC'; }
const ZONE_FORMATS = new Map();
export function zoneParts(ms) {
  const z = zoneSetting();
  let f = ZONE_FORMATS.get(z);
  if (!f) {
    const opts = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', weekday: 'short', hourCycle: 'h23', timeZoneName: 'short' };
    try { f = new Intl.DateTimeFormat('en-US', { ...opts, timeZone: z === 'local' ? undefined : z }); }
    catch (e) { f = new Intl.DateTimeFormat('en-US', { ...opts, timeZone: 'UTC' }); }
    ZONE_FORMATS.set(z, f);
  }
  const p = {};
  for (const x of f.formatToParts(new Date(ms))) p[x.type] = x.value;
  return { y: p.year, mo: p.month, d: p.day, h: p.hour, mi: p.minute, s: p.second, weekend: p.weekday === 'Sat' || p.weekday === 'Sun', zone: p.timeZoneName === 'GMT' ? 'UTC' : p.timeZoneName };
}
export function stamp(iso, seconds = true) {
  const t = parseTime(iso); if (t === null) return iso ? String(iso) : '';
  const p = zoneParts(t);
  return `${p.y}-${p.mo}-${p.d} ${p.h}:${p.mi}${seconds ? `:${p.s}` : ''} ${p.zone}`;
}
export function relTime(iso, nowMs) {
  const t = parseTime(iso); if (t === null) return iso ? String(iso) : '-';
  let s = Math.round(((nowMs === undefined ? Date.now() : nowMs) - t) / 1000);
  const future = s < 0; s = Math.abs(s);
  if (s < 45) return 'just now';
  const txt = s < 90 ? '1 min' : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${Math.round(s / 3600)} h`
    : s < 86400 * 14 ? `${Math.round(s / 86400)} d` : s < 86400 * 60 ? `${Math.round(s / 86400 / 7)} w` : `${Math.round(s / 86400 / 30)} mo`;
  return future ? `in ${txt}` : `${txt} ago`;
}
export function duration(sec) {
  if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return '';
  const s = Math.max(0, Number(sec));
  if (s < 1) return '<1s';
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  if (m < 60) return `${m}m ${pad(r)}s`;
  return `${Math.floor(m / 60)}h ${pad(m % 60)}m`;
}
export function dayKey(iso) { const t = parseTime(iso); if (t === null) return null; const p = zoneParts(t); return `${p.y}-${p.mo}-${p.d}`; }
export const clock = t => { const p = parseTime(t) === null ? null : zoneParts(parseTime(t)); return p ? `${p.h}:${p.mi} ${p.zone}` : String(t || ''); };
