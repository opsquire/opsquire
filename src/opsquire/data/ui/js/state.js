/* The page's state: one object every module reads and changes, and lookups into the home it holds. */
export const S = { link: [], linkPurpose: '', version: null, shownPrompts: new Set(), marking: new Set(), data: null, view: 'inbox', q: '', open: new Set(), hash: null, scrollTo: null, pane: null, closeForm: null, abandon: null, error: null, confirm: null, confirmTimer: null, map: { hours: 24, end: null }, hist: { mode: 'list', machine: '', script: '', sort: 'recent' },
  dev: false, details: new Set(), judging: new Map(), judgeText: new Map(), decideAll: null, code: new Map(), liveOpen: new Set() };
export const task = id => (S.data && S.data.tasks && S.data.tasks[id]) || null;
export const run = id => (S.data && S.data.runs && S.data.runs[id]) || null;
export const inbox = () => (S.data && S.data.inbox) || [];
/* every waiting run, the steps nested under their plan included */
export const inboxAll = () => inbox().flatMap(i => [i, ...(i.steps || [])]);
export const inboxItem = taskId => inboxAll().find(i => i.task_id === taskId) || null;
export const jobs = () => (S.data && S.data.jobs) || [];
export const jobById = id => jobs().find(j => j.job_id === id) || null;
export const activeJobFor = taskId => jobs().find(j => j.task_id === taskId && j.status !== 'done') || null;
const judgeJobsFor = taskId => jobs().filter(j => j.task_id === taskId && j.mode === 'judge').sort((a, b) => String(a.started || '').localeCompare(String(b.started || '')));
export const lastJudgeFor = taskId => { const js = judgeJobsFor(taskId); return js.length ? js[js.length - 1] : null; };
export const isDeciding = taskId => S.judging.has(taskId) || !!jobs().find(j => j.task_id === taskId && j.mode === 'judge' && j.status !== 'done');
export const selectedTask = () => (S.pane && S.pane.kind === 'task' ? S.pane.id : null);
export const conversationOf = askId => ((S.data.asks || []).find(a => a.ask_id === askId) || {}).conversation || askId;
export const liveAskJob = convId => jobs().find(j => j.mode === 'ask' && j.status !== 'done' && j.ask_id && conversationOf(j.ask_id) === convId) || null;
