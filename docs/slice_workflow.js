export const meta = {
  name: 'dicos_slice',
  description: 'Implement one dicos slice, then spec-review + code-review with bounded fix loop',
  phases: [{ title: 'Implement' }, { title: 'Review' }],
};

const { slice, brief, verify } = args;
const base = `Project: /home/novis/research/dicos (git repo). Spec: /home/novis/research/dicos/docs/spec.md (read it fully first; follow its Decisions and Commands sections). Reference prototype (read-only): /home/novis/research/dicos-login/src/dicos_login/. Lab CLI for conventions (read-only): /home/novis/.local/share/uv/tools/lab/lib/python3.13/site-packages/lab/.
Safety: never print or log passwords/TOTP secrets. Live DiCOS login: at most ONE attempt per verification run; if it fails, stop and report, never retry (5 failures ban the shared jump-host IP).`;

const verdictSchema = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
    blocking: { type: 'array', items: { type: 'string' } },
    nonBlocking: { type: 'array', items: { type: 'string' } },
  },
  required: ['verdict', 'blocking', 'nonBlocking'],
};

phase('Implement');
let impl = await agent(
  `${base}\n\nTask: implement slice ${slice} ONLY.\n${brief}\n\nBoundary: write only inside /home/novis/research/dicos. Do not implement later slices. Do not touch ~/.dicos unless the brief says so.\nVerification (must run; report the summarized result): ${verify}\nWhen done: git add -A && git commit -m "${slice}: <summary>" (no co-author trailer). Report: files changed, verification result, any deviation from spec.`,
  { agentType: 'general', label: `impl-${slice}` },
);

const reviews = [];
for (let round = 1; round <= 3; round++) {
  phase('Review');
  const [spec, code] = await parallel([
    () => agent(
      `${base}\n\nSpec-review slice ${slice} as implemented in the latest commit(s) (use git log / git diff). Slice brief:\n${brief}\nCheck missing, extra, or misunderstood behaviour versus spec.md and the brief. Read-only. Only real spec violations are blocking; cite file:line.`,
      { agentType: 'spec-reviewer', label: `spec-${slice}-r${round}`, schema: verdictSchema },
    ),
    () => agent(
      `${base}\n\nCode-review slice ${slice} (latest commit(s), git diff). Focus: correctness, security (file modes, secret leakage, login retry safety), error handling, test quality. You may run: ${verify}. Do not edit files. Only real defects are blocking; cite file:line.`,
      { agentType: 'code-reviewer', label: `code-${slice}-r${round}`, schema: verdictSchema },
    ),
  ]);
  reviews.push({ round, spec, code });
  const ok = (r) => r && r.verdict === 'PASS' && (!r.blocking || r.blocking.length === 0);
  if (ok(spec) && ok(code)) {
    return { slice, status: 'PASS', rounds: round, impl: String(impl).slice(0, 3000), reviews };
  }
  if (round === 3) return { slice, status: 'FAIL', impl: String(impl).slice(0, 3000), reviews };
  phase('Implement');
  impl = await agent(
    `${base}\n\nFix ONLY these blocking review findings for slice ${slice}; no other changes.\nSpec review blocking: ${JSON.stringify(spec && spec.blocking)}\nCode review blocking: ${JSON.stringify(code && code.blocking)}\nRe-run verification: ${verify}\nCommit as "fix(${slice}): <summary>". Report what changed and the verification result.`,
    { agentType: 'general', label: `fix-${slice}-r${round}` },
  );
}
