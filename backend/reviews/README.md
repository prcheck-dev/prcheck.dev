# Code-review agent (`reviews`)

An autonomous PR code-review agent. Given a GitHub repo + PR number it fetches
the diff, reviews it with an LLM, runs an adversarial second pass, computes a
verdict, stores findings, and can publish them back to the PR.

The review quality core is ported from
[`shipwright-agent`](https://github.com/) — the tuned reviewer system prompt,
PR sharding, finding dedup/merge, the schema-validated single-choke-point model
call with one retry and clean degradation, budget enforcement, the adversarial
verifier with citation gating, and the verdict ordering.

## Pipeline

```
fetch PR snapshot  ->  skip lockfiles/generated/vendored files  ->  number the diff
   ->  fast reviewer shards OR deep per-file issue lists (+ per-file verify)
   ->  ground findings against the diff  ->  adversarial gate
   ->  merge + dedupe  ->  PR-level selection (top-K)  ->  compute verdict
   ->  persist  ->  (optional) publish
```

Noise control is enforced in code, not left to the prompt:

- **Numbered diff** ([`diffs.py`](diffs.py)) — every new-side line carries its
  head-file line number, so the model copies `line` instead of counting from
  `@@` headers.
- **Grounding** ([`findings.py`](findings.py)) — a finding survives only if it
  sits on a line inside the diff of a changed file; near misses (±3 lines) snap
  to the nearest added line, everything else is dropped.
- **Low-signal files** — lockfiles, minified/build output, snapshots, images,
  vendored and generated code are never sent to the model. Extend the list with
  `PRCHECK_REVIEW_IGNORE_GLOBS`.
- **Severity rubric** — one shared rubric in every stage; the deep verifier may
  lower an overstated severity, since the verdict is computed from it.
- **PR-level selection** ([`selection.py`](selection.py)) — one final call sees
  every surviving finding together, drops cross-file duplicates and hedged
  "may not accept"-style guesses, and keeps at most `PRCHECK_REVIEW_TOP_K`
  (default 5) in priority order. Human reviewers leave ~3-4 comments per PR;
  posting every defensible finding was the main source of benchmark false
  positives. A degraded call falls back to severity ranking.
- **Publishing** — one batched GitHub review per run (one notification), no
  re-posting of comments an earlier push already left, low-severity notes kept
  in the collapsed part of the summary, and a run superseded by a newer push
  does not publish.

- **Reviewer** ([`reviewer.py`](reviewer.py)) — strict prompt that only reports
  concrete, diff-introduced defects with an exact `path:line`; prefers zero
  findings over an unsupported one. Large PRs are split into balanced shards
  reviewed in parallel, then merged and deduped by severity.
- **Adversary** ([`adversary.py`](adversary.py)) — a fresh-context pass that
  tries to refute "ready to merge". Findings whose `file:line` citation is not
  on a changed line are discarded; an unsubstantiated BLOCK is downgraded.
  Verified blockers become critical findings, so a blocked PR always shows why.
  It can only *tighten* the verdict.
- **Verdict** ([`verdict.py`](verdict.py)) — pure function:
  `critical -> block`, `high -> request-changes`, `medium/low ->
  approve-with-conditions`, else `approve`. Degraded runs cap at
  approve-with-conditions.
- **LLM choke point** ([`llm.py`](llm.py)) — every call is budget-ticked and
  schema-validated with exactly one retry; a failed/unconfigured call returns
  `None` (degrade) so a review always finishes with a verdict.
- **Deep context retrieval** ([`github_client.py`](github_client.py)) — when
  deep mode is enabled, a bounded GitHub tree lookup resolves likely imported
  modules and base/interface definitions. These definitions are supplied only
  to the relevant file and verifier prompts, approximating LSP definition
  lookup without cloning the repository. Disable it with
  `PRCHECK_DEEP_RELATED_DEFINITIONS=False` if the extra GitHub reads are not
  desired.
- **Trusted repository guidance** — review rules in `.qwen/review-rules.md`,
  `AGENTS.md`, `QWEN.md`, `CONTRIBUTING.md`, and the Qwen-style
  `.qwen/review-context.json` manifest are read from the PR's base commit and
  scoped to changed paths. This makes the review repository-aware without
  allowing the PR to rewrite its own policy. Disable it with
  `PRCHECK_REPO_GUIDANCE=False` if needed.
- **CI evidence gate** — existing GitHub check runs and commit statuses are
  summarized before finalizing a clean review. Failed or pending external
  checks cap an otherwise clean `approve` at `approve-with-conditions`; missing
  permissions degrade safely instead of becoming a false CI failure.

## API

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/reviews/` | user | Trigger a review: `{"repo":"owner/name","pr_number":123}` → `202` with the review |
| GET | `/api/reviews/` | user | List reviews (filter `?repo=` `?pr_number=`) |
| GET | `/api/reviews/{id}/` | user | Review + findings |
| POST | `/api/reviews/webhook/github/` | HMAC | GitHub `pull_request` webhook (opened/synchronize/reopened) |

Reviews run in a background thread; poll `GET /api/reviews/{id}/` for
`status` (`pending → running → completed/failed`).

## CLI

```bash
python manage.py run_review owner/name 123
```

## Benchmark

Score against the golden-comment benchmark without touching GitHub (publishing,
check runs and the CI gate are forced off; the token only needs read access):

```bash
PRCHECK_GITHUB_TOKEN=$(gh auth token) python manage.py benchmark_reviews \
    ../experiments/golden_comments/*.json --out run.json --concurrency 3
```

The output uses the golden-comment schema. Each PR also carries `unselected`
(findings before PR-level selection), so one run measures selection's effect.
Reruns resume: completed PRs are skipped, failed or degraded ones retried.

### Results

50 golden PRs (sentry, keycloak, grafana, discourse, cal.com), deep mode,
gpt-5.2 reviewer, gpt-5.2 judge, "core" category profile. Scored with
`experiments/prcheck_comments/judge_prcheck.py`, which follows the official
benchmark's match prompt but not its extract/dedup steps, so leaderboard
comparisons are approximate. One run per row; treat ±0.02 as noise.

| Date | Version | Precision | Recall | F1 | Findings/PR |
|------|---------|-----------|--------|----|-------------|
| 2026-09-26 | deep, no PR-level selection | 0.177 | 0.614 | 0.275 | 11.8 |
| 2026-09-26 | deep + PR-level selection (8ae2da6) | 0.423 | 0.449 | 0.436 | 3.5 |
| 2026-09-27 | same code, rerun with retry fix (mean of 4) | 0.39 | 0.43 | 0.408 | 3.6 |
| 2026-09-27 | + definitions of called functions (mean of 4) | 0.42 | 0.45 | 0.435 | 3.6 |
| 2026-09-27 | + call sites of changed functions, 30 related files (mean of 4) | 0.43 | 0.46 | 0.443 | 3.6 |
| 2026-09-27 | + selector with medium reasoning, fills its quota (mean of 3) | 0.43 | 0.49 | 0.461 | 3.7 |
| 2026-09-27 | same, fresh end-to-end run (1 run) | 0.45 | 0.50 | 0.473 | 3.6 |
| 2026-09-27 | + picks adjusted by generator confidence (mean of 3 selection draws) | 0.45 | 0.49 | 0.468 | 3.6 |
| 2026-09-27 | + majority vote of 3 selector runs (same findings) | 0.46 | 0.50 | 0.480 | 3.5 |
| 2026-09-27 | same code, second fresh run (v17) | 0.44 | 0.49 | 0.464 | 3.6 |
| 2026-09-27 | + related files for Java, Ruby, Go, src/ Python (mean of 2 runs) | 0.47 | 0.50 | **0.485** | 3.6 |

The last row reran only the selection stage on the previous row's saved
findings (three draws); reasoning for selection alone, without the fill
wording, gave 0.458 at precision 0.52 and 2.5 findings per PR.

The confidence adjustment drops picks the generator rated below 0.7 and adds
findings it rated 0.9 or more. It was found on one run's findings (+0.026
over six selection draws) and held but shrank on a fresh run (+0.003), so
treat it as a small gain. The last two rows use the same fresh run's findings
with three independent selector draws; majority voting over them also beat
single draws on the earlier capture (0.491 vs 0.485).

Before selection, the reviewer's findings cover 0.633 of golden issues at
baseline, 0.646 with definitions and 0.677 with call sites added.

The 0.436 first run was at the lucky end of the noise: the same code scores
0.408 on average once dropped model connections are retried. Rows marked
"mean of 4" average one full run and three reruns of the selection stage on
its saved findings, which removes most of the selector's run-to-run noise.

### Official scoring

The rows above use the local judge. The upstream pipeline
(`code-review-benchmark/offline`: LLM extraction, dedup, judge; Core profile,
gpt-5.2 judge) is what the leaderboard reports; runs are staged with
`experiments/.../official_score.py` and scored three times each because the
pipeline itself varies by about +-0.02 on identical input.

| Version | Official Core F1 |
|---------|------------------|
| deep + PR-level selection (the 0.436 row) | 0.423 |
| majority-vote selection, run v16 / run v17 | 0.487 / 0.439 |
| + language-aware related files, run v18a / v18b | 0.465 / 0.480 |

Leaderboard (Core, gpt-5.2 judge): qodo-extended-v2 0.589, cubic-v2 0.587,
augment 0.545, qodo-v2 0.534, bugbot 0.485, qodo-extended-summary 0.484,
devin 0.483, gitlab 0.465, greptile-v4-1 0.450. prcheck's current estimate,
~0.47 averaged over runs, sits between devin and gitlab; a single run can land
anywhere in roughly +-0.025 of it.

## Configuration

See [`.env.example`](.env.example). Key settings: `PRCHECK_LLM_BACKEND`
(`azure-ai-foundry` | `anthropic` | `openai-compatible` | `deterministic`), the provider key/model,
`PRCHECK_GITHUB_TOKEN` (repo read; PR write if publishing),
`PRCHECK_PUBLISH_REVIEWS`, `PRCHECK_GITHUB_WEBHOOK_SECRET`, and
`PRCHECK_ENABLE_ADVERSARY`, `PRCHECK_MAX_INLINE_COMMENTS` (default 15),
`PRCHECK_INLINE_MIN_SEVERITY` (default `medium`), and
`PRCHECK_DEEP_MIN_CONFIDENCE` (default 0.3).

Azure AI Foundry Qwen deployments use the first-class `azure-ai-foundry`
backend. Set `PRCHECK_AZURE_AI_FOUNDRY_BASE_URL` to
`https://<resource>.services.ai.azure.com/openai/v1`, set the deployment name
in `PRCHECK_AZURE_AI_FOUNDRY_MODEL`, and provide either an API key or bearer
token according to `PRCHECK_AZURE_AI_FOUNDRY_AUTH`.

Without a model key the backend resolves to `deterministic`: reviews complete,
marked `degraded`, with no findings — nothing crashes.

## Tests

```bash
python manage.py test reviews
```

Fully offline tests (model and GitHub calls are faked): schema validation,
sharding, dedup/merge ordering, verdict ordering, diff numbering, finding
grounding, low-signal file filtering, adversary citation gating, per-file
verification and severity recalibration, PR-level selection, GitHub read retries, batched/deduplicated publishing, the LLM retry/degrade path, related-definition retrieval,
repository guidance, CI gating, deep-review context propagation,
end-to-end orchestration, and the API + webhook.
