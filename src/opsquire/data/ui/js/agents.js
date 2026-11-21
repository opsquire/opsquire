import { S } from './state.js';
import { money, plural } from './records.js';

/* agents, their tiers and how they bill come from the home's retinue: opsquire itself knows no provider */
export const retinue = () => (S.data && S.data.retinue) || [];
export function agentOf(name) { return retinue().find(a => a.name === name) || retinue().find(a => a.default) || retinue()[0] || { name: '', tiers: {}, billing: {} }; }
export function modelName(m) {
  const names = {};
  for (const a of retinue()) for (const t of Object.values(a.tiers || {})) if (t.id) names[t.id] = t.label || t.id;
  return String(m).replace(/[A-Za-z0-9][A-Za-z0-9._-]+/g, x => names[x] || x);
}
/* the tokens the agent reported for the ask: what it re-read from its cache, what was new to it, what it wrote */
const tokens = n => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
export function usageText(u) {
  if (!u) return '';
  const parts = [u.cache_read ? `${tokens(u.cache_read)} cached` : '', (u.input || u.cache_write) ? `${tokens((u.input || 0) + (u.cache_write || 0))} new` : '',
    u.output ? `${tokens(u.output)} out` : '', u.calls ? plural(u.calls, 'call') : ''].filter(Boolean);
  return parts.join(' · ');
}
export function spentText(it) {
  const b = agentOf(it.agent).billing || {};
  if (!it.cost_usd) return '';
  if (it.billing === 'api-key') return `${money(it.cost_usd)} billed to ${b.key || 'an API key'}`;
  if (it.billing === 'plan') return `≈ ${money(it.cost_usd)} at API prices, counted against ${b.plan || 'your plan'}`;
  return `≈ ${money(it.cost_usd)} reported by the agent`;
}
