export const meta = {
  name: 'dicos_pipeline',
  description: 'Run dicos slices sequentially: implement (gpt) -> spec+code review -> bounded fix loop; stop at first failure',
  phases: [{ title: 'Implement' }, { title: 'Review' }],
};

const IMPL = 'cliproxyapi/gpt-6.1-sol';
const REVIEW = 'cliproxyapi/claude-sonnet-5-5-high';
const base = `Project: /home/novis/research/dicos (git repo). Spec: /home/novis/research/dicos/docs/spec.md (read it fully first). Lab CLI conventions (read-only): /home/novis/.local/share/uv/tools/lab/lib/python3.13/site-packages/lab/.
Safety: never print or log passwords/TOTP secrets. Never delete or modify anything in ~/.dicos except through the dicos CLI commands named in the verification. Never run \`dicos init\` and never try to log in by other means. Live commands are allowed only while the SSH master is alive (check: ssh -o ControlPath=$HOME/.dicos/cm.sock -O check x); if it is not alive, skip live checks and report "live skipped". A login failure is latched by dicos; never clear it (no dicos logout). Never read, print or copy ~/.dicos/credentials.json.`;

const verdictSchema = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
    blocking: { type: 'array', items: { type: 'string' } },
    nonBlocking: { type: 'array', items: { type: 'string' } },
  },
  required: ['verdict', 'blocking', 'nonBlocking'],
};
const reviewerRules = 'Read-only: do not create, modify or delete any file (scratch files only under /tmp/dicos-review/). Only real defects or spec violations are blocking; cite file:line.';

const results = [];
for (const s of args.slices) {
  phase('Implement');
  const head = await agent(`Run: cd /home/novis/research/dicos && git rev-parse --short HEAD. Reply with only the hash.`, { model: IMPL, label: `head-${s.slice}` });
  let impl = await agent(
    `${base}\n\nTask: implement slice ${s.slice} ONLY (earlier slices are committed).\n${s.brief}\n\nBoundary: write only inside /home/novis/research/dicos. Do not implement later slices.\nVerification (must run; report the summarized result): ${s.verify}\nWhen done: git add -A && git commit -m "${s.slice}: <summary>" (no co-author trailer). Report: files changed, test count, verification result (including live output), any deviation from spec.`,
    { agentType: 'general', model: IMPL, label: `impl-${s.slice}` },
  );
  const base_rev = String(head || '').trim();
  let status = 'FAIL';
  const reviews = [];
  for (let round = 1; round <= 3; round++) {
    phase('Review');
    const ctx = `${base}\n\nReview slice ${s.slice}: commits after ${base_rev} (git log ${base_rev}..HEAD, git diff ${base_rev}..HEAD). Slice brief:\n${s.brief}\n${reviewerRules}`;
    const [spec, code] = await parallel([
      () => agent(`${ctx}\nFocus: missing, extra, or misunderstood behaviour versus spec.md and the brief.`, { agentType: 'spec-reviewer', model: REVIEW, label: `spec-${s.slice}-r${round}`, schema: verdictSchema }),
      () => agent(`${ctx}\nFocus: correctness, security (file modes, secret leakage, login safety), error handling, test quality. You may run: cd /home/novis/research/dicos && uv run pytest -q and read-only `--help` commands. Do NOT run live dicos commands that submit, cancel or clean jobs; the implementer report contains the live verification output.`, { agentType: 'code-reviewer', model: REVIEW, label: `code-${s.slice}-r${round}`, schema: verdictSchema }),
    ]);
    reviews.push({ round, spec, code });
    if (!spec || !code) { status = 'ERROR: reviewer returned no result'; break; }
    const ok = (r) => r.verdict === 'PASS' && r.blocking.length === 0;
    if (ok(spec) && ok(code)) { status = 'PASS'; break; }
    if (round === 3) break;
    phase('Implement');
    impl = await agent(
      `${base}\n\nFix ONLY these blocking review findings for slice ${s.slice}; no other changes.\nSpec review blocking: ${JSON.stringify(spec.blocking)}\nCode review blocking: ${JSON.stringify(code.blocking)}\nRe-run verification: ${s.verify}\nCommit as "fix(${s.slice}): <summary>". Report what changed and the verification result.`,
      { agentType: 'general', model: IMPL, label: `fix-${s.slice}-r${round}` },
    );
  }
  results.push({ slice: s.slice, status, impl: String(impl).slice(0, 2500), reviews });
  log(`${s.slice}: ${status}`);
  if (status !== 'PASS') break;
}
return { results };
