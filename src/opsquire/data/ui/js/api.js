/* the session cookie goes with every same-origin fetch; the header is one no cross-site form can send */
export const REQUEST_HEADERS = { 'X-Opsquire-Request': '1' };
export async function api(path, opts) {
  const r = await fetch(path, { signal: AbortSignal.timeout(path === '/api/state' ? 60000 : 30000), ...opts, headers: { ...REQUEST_HEADERS, ...(opts && opts.headers) } });
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  if (!r.ok) throw new Error((body && body.error) || `${r.status} ${r.statusText}`);
  return body;
}
export function post(path, body) { return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }
