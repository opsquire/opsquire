/* What ask agents may use, and the settings kept in the config file. */
import { S } from './state.js';
import { stamp, zoneSetting } from './time.js';
import { btn, el, toast } from './dom.js';
import { sec, when } from './widgets.js';
import { api, post } from './api.js';
import { readDryApply, setDryApply } from './prefs.js';
import { render } from './render.js';

const CHOICES = [['allow', 'Allow', 'runs without asking'], ['confirm', 'Confirm', 'asks you in the chat first'], ['off', 'Off', 'the agent may not use it']];
function choiceSeg(value, pick, inherit) {
  return el('span', { class: 'tiers choice', role: 'radiogroup' }, [...(inherit ? [['', 'Default', `follows the default (${inherit})`]] : []), ...CHOICES].map(([v, label, why]) =>
    el('button', { type: 'button', role: 'radio', 'aria-checked': String(v === value), class: v === value ? `on c-${v || 'inherit'}` : '', title: why, onclick: () => { if (v !== value) pick(v); } }, label)));
}
async function loadTools(refresh) {
  S.toolsBusy = refresh ? 'checking' : 'loading';
  render();
  try { S.tools = await api('/api/tools' + (refresh ? '?refresh=1' : '')); S.toolsError = null; } catch (e) { S.toolsError = e.message; }
  S.toolsBusy = null;
  render();
}
async function changeTools(change) {
  try { S.tools = { ...S.tools, ...(await post('/api/tools', change)) }; } catch (e) { toast(`settings: ${e.message}`); }
  render();
}
function ruleRows(scope, rules, seen, inherit, placeholder) {
  const rows = Object.entries(rules).sort((a, b) => a[0].localeCompare(b[0])).map(([pattern, value]) =>
    el('div', { class: 'rule' }, el('code', {}, pattern), el('span', { class: 'grow' }), choiceSeg(value, v => changeTools({ kind: 'rule', scope, pattern, value: v || null }), inherit)));
  const loose = (seen || []).filter(t => !Object.keys(rules).some(p => globMatch(p, t)));
  for (const t of loose) rows.push(el('div', { class: 'rule seen' }, el('code', {}, t), el('span', { class: 'faint small' }, 'used, no rule'), el('span', { class: 'grow' }),
    choiceSeg('', v => v && changeTools({ kind: 'rule', scope, pattern: t, value: v }), inherit)));
  const input = el('input', { type: 'text', placeholder, 'aria-label': 'pattern' });
  rows.push(el('form', { class: 'rule add', onsubmit: e => { e.preventDefault(); const pattern = input.value.trim(); if (pattern) changeTools({ kind: 'rule', scope, pattern, value: 'allow' }); } },
    input, btn('Add rule', null, 'sm', { type: 'submit', title: 'adds it as Allow; change it after' })));
  return rows;
}
/* a rule's pattern as the server's fnmatch reads it; one that is no valid class ("[" alone) matches nothing */
function globMatch(pattern, text) {
  try {
    return new RegExp('^' + pattern.replace(/[.+^${}()|\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.') + '$').test(text);
  } catch (e) {
    return false;
  }
}
/* settings kept in the config file (opsquire init writes the same ones) */
export function loadSettings() {
  S.settingsBusy = true;
  api('/api/settings').then(r => { S.settings = r; S.settingsError = null; }, e => { S.settingsError = e.message; }).finally(() => { S.settingsBusy = false; render(); });
}
async function saveSettings(body) {
  try { S.settings = await post('/api/settings', body); } catch (e) { toast(`cannot save the setting: ${e.message}`); }
  render();
}
function renderFollowUps() {
  if (!S.settings && !S.settingsBusy && !S.settingsError) loadSettings();
  const v = S.settings ? S.settings.follow_up_fresh_after_usd : undefined, on = v !== null && v !== undefined;
  const setAmount = e => { const n = Number(e.target.value); if (e.target.value !== '' && Number.isFinite(n) && n >= 0) { S.lastFresh = n; saveSettings({ follow_up_fresh_after_usd: n }); } else toast('a number of dollars, 0 or more'); };
  return [sec('Follow-ups'), el('div', { class: 'block pad' },
    S.settingsError ? el('div', { class: 'err' }, `Cannot read the settings: ${S.settingsError}`) : null,
    el('div', { class: 'rule' },
      el('label', { class: 'switch', title: on ? 'switch off: follow-ups always resume the whole chat' : 'switch on' },
        el('input', { type: 'checkbox', checked: on, disabled: !S.settings, onchange: e => saveSettings({ follow_up_fresh_after_usd: e.target.checked ? (S.lastFresh ?? 5) : null }) }),
        el('strong', {}, 'Fresh sessions for long chats')),
      el('span', { class: 'grow' }),
      on ? el('label', { class: 'small' }, 'once a turn cost $ ', el('input', { id: 'freshafter', type: 'number', min: '0', step: '0.5', value: String(v), style: 'width:5em', onchange: setAmount })) : null),
    el('div', { class: 'faint small' }, "When a run a chat handed over finishes, the console wakes the chat's agent. Once the chat's last turn cost this much, it starts a new agent session briefed from the records instead of resuming the whole chat, which spares the plan's usage limit. Off: it always resumes the whole chat."))];
}
function renderTimes() {
  const z = zoneSetting(), named = z !== 'UTC' && z !== 'local', naming = named || S.zoneNaming;
  let here = '';
  try { here = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { /* no Intl zone */ }
  const pick = v => { S.zoneNaming = v === 'zone'; if (v === 'zone') render(); else saveSettings({ time_zone: v }); };
  const name = e => { const v = e.target.value.trim(); if (v) saveSettings({ time_zone: v }); };
  return [sec('Times'), el('div', { class: 'block pad' },
    el('div', { class: 'rule' },
      el('label', { class: 'switch' }, el('strong', {}, 'Show times in'),
        el('select', { id: 'timezone', 'aria-label': 'time zone', disabled: !S.settings, onchange: e => pick(e.target.value) },
          el('option', { value: 'UTC', selected: z === 'UTC' && !naming }, 'UTC'),
          el('option', { value: 'local', selected: z === 'local' && !naming }, here ? `this machine (${here})` : 'this machine'),
          el('option', { value: 'zone', selected: naming }, 'a zone by name'))),
      naming ? el('input', { id: 'zonename', 'aria-label': 'zone name', placeholder: 'America/New_York', value: named ? z : '', style: 'width:14em', onchange: name }) : null),
    el('div', { class: 'faint small' }, `Now: ${stamp(new Date().toISOString())}. The console and the command line show times in this zone, and read typed times such as --since 9:30 in it (9:30Z is UTC). Run records and JSON stay in UTC.`))];
}
const TIER_WHY = { local: 'this machine and throwaway sandboxes', dev: 'development hosts', staging: 'pre-production' };
const AUTO_MODES = ['read', 'dry', 'verify', 'apply'];
function renderPolicy() {
  const pol = (S.settings && S.settings.policy) || { tiers: {}, auto_run: {} }, off = !S.settings;
  const setPatterns = (tier, text) => saveSettings({ policy: { tiers: { [tier]: text.split(/[\s,]+/).filter(Boolean) } } });
  const setMode = (tier, mode, on) => {
    const now = new Set(pol.auto_run[tier] || []); if (on) now.add(mode); else now.delete(mode);
    saveSettings({ policy: { auto_run: { [tier]: AUTO_MODES.filter(m => now.has(m)) } } });
  };
  const rows = ['local', 'dev', 'staging'].map(tier => el('div', { class: 'rule' },
    el('strong', { style: 'min-width:5em' }, tier),
    el('input', { type: 'text', 'aria-label': `${tier} target patterns`, placeholder: `targets, e.g. ${tier === 'local' ? 'laptop localhost' : tier + '-*'}`, disabled: off,
      value: (pol.tiers[tier] || []).join(' '), title: TIER_WHY[tier], style: 'flex:1 1 200px;min-width:0', onchange: e => setPatterns(tier, e.target.value) }),
    el('span', { class: 'small muted' }, 'auto-run'),
    AUTO_MODES.map(m => el('label', { class: 'switch small' }, el('input', { type: 'checkbox', disabled: off, checked: (pol.auto_run[tier] || []).includes(m), onchange: e => setMode(tier, m, e.target.checked) }), m))));
  const parts = [sec('Tiers and auto-run'), el('div', { class: 'block pad' },
    ...rows,
    el('div', { class: 'rule' }, el('strong', { style: 'min-width:5em' }, 'prod'), el('span', { class: 'muted' }, 'every target no pattern names; never auto-runs')),
    el('div', { class: 'faint small' }, 'Patterns match target names (* and ? wildcards; a part such as web1/nginx matches through its host). A task takes the strictest tier of its targets. With auto-run on, the console starts a waiting run of that tier and mode by itself, as your own run, and its record names the rule. Auto-run trusts the targets each task declares, so turn it on only where a wrong one cannot hurt. Saving here seals the rules in the home\'s var/, where agents cannot write; rules changed anywhere else start nothing until sealed.'))];
  const ticked = Object.values(pol.auto_run || {}).some(v => v.length);
  if (S.settings && !pol.sealed && ticked) parts.splice(1, 0, el('div', { class: 'stale', role: 'status' },
    el('b', {}, 'Not sealed: these rules start nothing.'), el('span', {}, 'They changed outside opsquire policy and this page (an agent\'s shell or an editor, say). Check them, then seal them.'),
    btn('Seal these rules', () => saveSettings({ policy: {} }), 'sm')));
  return parts;
}
function renderCheckpoints() {
  const v = (S.settings && S.settings.checkpoint_command) || '', last = S.settings && S.settings.checkpoint_last;
  return [sec('Ledger checkpoints'), el('div', { class: 'block pad' },
    el('div', { class: 'rule' }, el('strong', {}, 'Send each checkpoint to'),
      el('input', { type: 'text', 'aria-label': 'checkpoint command', placeholder: "a command, e.g. sh -c 'cat >> /mnt/backup/checkpoints.txt'", value: v, disabled: !S.settings,
        style: 'flex:1 1 260px;min-width:0', onchange: e => saveSettings({ checkpoint_command: e.target.value.trim() }) })),
    last ? el('div', { class: last.ok ? 'small' : 'small err' }, last.ok ? `last sent ${last.checkpoint} ` : `last one NOT sent (${last.error || 'failed'}) `, when(last.at)) : null,
    el('div', { class: 'faint small' }, 'After every run the ledger keeps a checkpoint (<size>:<root>). This command gets each new one on stdin and in OPSQUIRE_CHECKPOINT, so a copy lives where nobody on this machine can rewrite it; `opsquire audit --against` proves the history up to it unchanged. A failed send never fails the run. Empty turns it off.'))];
}
function renderRunButtons() {
  const on = readDryApply(), url = (S.settings && S.settings.editor_url) || '', dflt = (S.settings && S.settings.editor_url_default) || '';
  const editor = [sec('Code links'), el('div', { class: 'block pad' },
    el('div', { class: 'rule' }, el('strong', {}, 'Open code in'),
      el('input', { type: 'text', 'aria-label': 'editor link', placeholder: dflt, value: url === dflt ? '' : url, disabled: !S.settings,
        style: 'flex:1 1 260px;min-width:0', onchange: e => saveSettings({ editor_url: e.target.value.trim() }) })),
    el('div', { class: 'faint small' }, `Files in the task pane, and in the list opsquire run prints before a script starts, open with this link; {path} is the file. Empty uses the default: ${dflt}`))];
  return [...editor, sec('Run buttons', null, el('span', { class: 'faint small' }, 'this browser')), el('div', { class: 'block pad' },
    el('div', { class: 'rule' },
      el('label', { class: 'switch', title: on ? 'switch off: the button runs the dry run alone' : 'switch on' },
        el('input', { type: 'checkbox', checked: on, onchange: e => { setDryApply(e.target.checked); render(); } }),
        el('strong', {}, 'Dry run, then apply, from one click'))),
    el('div', { class: 'faint small' }, "A changing task's run button does the dry run and, if it ends ok, the apply, which still asks before it changes anything. Its menu (the arrow, or hover) has the dry run alone."))];
}
export function renderSettings() {
  if (!S.tools && !S.toolsBusy && !S.toolsError) loadTools(false);
  const t = S.tools, parts = [...renderTimes(), ...renderRunButtons(), ...renderPolicy(), ...renderCheckpoints(), ...renderFollowUps()];
  parts.push(sec('Tools for ask agents', null, el('span', {}, btn(S.toolsBusy === 'checking' ? 'checking…' : 'Check servers', () => loadTools(true), 'sm', { disabled: !!S.toolsBusy, title: "ask the agent's CLI which servers it has (takes a few seconds)" }))));
  if (S.toolsError) parts.push(el('div', { class: 'err' }, `Cannot read the tool settings: ${S.toolsError}`));
  if (!t) { parts.push(el('div', { class: 'quiet' }, S.toolsBusy ? 'Loading…' : '')); return el('div', { class: 'settings' }, parts); }
  if (!t.supported) parts.push(el('div', { class: 'stale' }, el('b', {}, 'The default agent cannot ask you before a tool runs.'), el('span', {}, 'Its retinue entry has no tools.approval_argv, so these settings do not reach it.')));
  parts.push(el('div', { class: 'block pad' },
    el('div', { class: 'rule' }, el('span', {}, 'Anything without a rule'), el('span', { class: 'grow' }), choiceSeg(t.default, v => changeTools({ kind: 'default', value: v }))),
    el('div', { class: 'faint small' }, 'Allow runs without asking. Confirm shows Allow and Deny in the ask\'s chat and waits; with nobody to answer it is denied. Off keeps it from the agent. The longest matching pattern wins.')));
  parts.push(sec('Servers', t.servers.length ? String(t.servers.length) : null, t.listed_at ? el('span', { class: 'faint small' }, 'checked ', when(t.listed_at)) : null));
  if (!t.servers.length) parts.push(el('div', { class: 'quiet' }, t.can_list ? 'No servers yet: Check servers asks the agent which it has.' : 'No servers yet.'));
  for (const sv of t.servers) {
    const on = sv.enabled !== false;
    parts.push(el('div', { class: `block pad server ${on ? '' : 'disabled'}` },
      el('div', { class: 'rule' },
        el('label', { class: 'switch', title: on ? 'switch off: the agent will not see this server' : 'switch on' }, el('input', { type: 'checkbox', checked: on, onchange: e => changeTools({ kind: 'server', id: sv.id, enabled: e.target.checked }) }), el('strong', {}, sv.name)),
        sv.status ? el('span', { class: 'chip' }, sv.status) : null, sv.source === 'added' ? el('span', { class: 'chip' }, 'added here') : null,
        el('span', { class: 'grow' }),
        on ? choiceSeg(sv.default || '', v => changeTools({ kind: 'server', id: sv.id, default: v }), t.default) : null,
        sv.source === 'added' ? btn('Remove', () => changeTools({ kind: 'remove', name: sv.name }), 'sm', { title: 'take it away from ask agents' }) : null),
      on ? ruleRows(sv.id, sv.tools || {}, sv.seen, sv.default || t.default, 'tool name or pattern, e.g. read_*') : null));
  }
  parts.push(sec('Shell commands'));
  const mode = t.shell_mode || 'outward';
  const MODES = [['outward', 'Only to reach out', 'ask only before a command talks to another machine or acts as someone else (ssh, curl, git push, sudo, opsquire run); everything else runs'],
    ['reads', 'Unless it only reads', 'run commands that only read and print; ask before anything else'], ['confirm', 'Always', 'ask before every command without a rule']];
  parts.push(el('div', { class: 'block pad' },
    el('div', { class: 'rule' }, el('strong', {}, 'Ask before a command'), el('span', { class: 'grow' }),
      el('span', { class: 'tiers choice', role: 'radiogroup' }, MODES.map(([v, label, why]) =>
        el('button', { type: 'button', role: 'radio', 'aria-checked': String(v === mode), class: v === mode ? 'on c-allow' : '', title: why, onclick: () => { if (v !== mode) changeTools({ kind: 'shell_mode', value: v }); } }, label)))),
    ruleRows('shell', t.shell || {}, [], t.default, 'command pattern, e.g. gh pr view *'),
    el('div', { class: 'faint small' }, "Your rules win, and an Off default keeps everything off. Without a rule, the choice above decides for every command in the line, inside loops and $(...) too. A line that hides its commands (a command name from a variable, eval, sh -c) always asks. The agent works in its own copy, and every task it changes waits for a fresh dry run.")));
  if ((t.agent_tools_seen || []).length || Object.keys(t.agent_tools || {}).length) {
    parts.push(sec("The agent's own tools"));
    parts.push(el('div', { class: 'block pad' }, ruleRows('agent_tools', t.agent_tools || {}, t.agent_tools_seen, t.default, 'tool name, e.g. WebFetch')));
  }
  parts.push(sec('Add a server'));
  const name = el('input', { type: 'text', placeholder: 'name', 'aria-label': 'server name' });
  const where = el('input', { type: 'text', placeholder: 'https://… or a command, e.g. npx -y some-mcp-server', 'aria-label': 'server URL or command' });
  parts.push(el('form', { class: 'block pad addserver', onsubmit: e => {
    e.preventDefault();
    const w = where.value.trim(), words = w.split(/\s+/);
    const change = /^https?:\/\//.test(w) ? { kind: 'add', name: name.value, url: w } : { kind: 'add', name: name.value, command: words[0] || '', args: words.slice(1) };
    changeTools(change);
  } }, name, where, btn('Add', null, 'sm primary', { type: 'submit' }),
    el('div', { class: 'faint small' }, 'For ask agents only: it is passed to each ask, never written into the agent\'s own configuration. Its tools follow the rules above.')));
  return el('div', { class: 'settings' }, parts);
}
