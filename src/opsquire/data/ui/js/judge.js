import { activeJobFor, isDeciding, lastJudgeFor, S, task } from './state.js';
import { relTime } from './time.js';
import { decisionList, lastLine, shortName, verdictWords } from './records.js';
import { btn, el, toast } from './dom.js';
import { api, post } from './api.js';
import { render } from './render.js';
import { crFold, stripAnsi } from './jobs.js';
import { refresh } from './live.js';

/* the judge: an agent reads the task's record and closes it, or keeps it open and says why */
async function awaitJob(jobId) {
  let offset = 0, text = '';
  for (;;) {
    const r = await api(`/api/job?id=${encodeURIComponent(jobId)}&offset=${offset}`);
    if (r.text) text += r.text;
    if (typeof r.offset === 'number') offset = Math.max(offset, r.offset);
    if (r.status === 'done') { S.judgeText.set(jobId, crFold(stripAnsi(text))); return { exit: r.exit_code, text: S.judgeText.get(jobId) }; }
    await new Promise(res => setTimeout(res, 1000));
  }
}
async function decide(taskId) {
  if (isDeciding(taskId)) return null;
  let jobId;
  try { jobId = (await post('/api/judge', { task: taskId })).job_id; } catch (e) { toast(`decide failed: ${e.message}`); throw e; }
  S.judging.set(taskId, jobId); refresh(); render();
  try {
    const r = await awaitJob(jobId);
    toast(r.exit === 0 ? 'closed' : r.exit === 1 ? 'kept open' : `judge exit ${r.exit}`);
    return r;
  } finally { S.judging.delete(taskId); refresh(); render(); }
}
export async function decideAll() {
  if (S.decideAll && S.decideAll.running) return;
  const ids = decisionList(S.data).map(t => t.id).filter(id => !isDeciding(id));
  S.decideAll = { running: true, total: ids.length, done: 0, error: null, current: null }; render();
  try {
    for (const id of ids) {
      S.decideAll.current = id; render();
      await decide(id);
      S.decideAll.done++;
    }
  } catch (e) { S.decideAll.error = `${shortName(S.decideAll.current || '')}: ${e.message}`; }
  S.decideAll.running = false; S.decideAll.current = null; render();
}
function ensureJudgeText(job) {
  if (!job || job.status !== 'done' || S.judgeText.has(job.job_id) || S.judgeText.has('loading:' + job.job_id)) return;
  S.judgeText.set('loading:' + job.job_id, true);
  awaitJob(job.job_id).then(() => render(), () => S.judgeText.delete('loading:' + job.job_id));
}
function judgedLine(t) {
  const jd = t && t.judged; if (!jd || !jd.note) return null;
  return el('span', { class: 'verdict', title: `${jd.by || 'the judge'}, ${relTime(jd.when)}` }, `${jd.decision === 'verify' ? 'Needs the check' : 'Kept open'}: ${jd.note}`);
}
export function judgeLine(taskId) {
  const j = lastJudgeFor(taskId); if (!j) return judgedLine(task(taskId));
  if (j.status !== 'done') return el('span', { class: 'verdict' }, el('span', { class: 'spin' }), 'Deciding…');
  ensureJudgeText(j);
  const text = S.judgeText.get(j.job_id);
  if (text === undefined) return judgedLine(task(taskId)) || el('span', { class: 'verdict' }, 'reading the decision…');
  const line = verdictWords(lastLine(text)) || (j.exit_code === 0 ? 'closed' : 'kept open');
  return el('span', { class: `verdict ${j.exit_code === 0 ? 'good' : ''}`, title: 'the judge, ' + relTime(j.ended || j.started) }, line);
}
export function decideButton(taskId) {
  if (isDeciding(taskId)) return el('span', { class: 'muted' }, el('span', { class: 'spin' }), 'Deciding…');
  if (activeJobFor(taskId)) return null;
  return btn('Decide', () => decide(taskId).catch(() => {}), 'sm decide', { title: 'an agent reads the record and closes the task, or says why it stays open' });
}
