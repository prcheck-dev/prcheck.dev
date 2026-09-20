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
fetch PR snapshot  ->  shard large diffs  ->  reviewer (concurrent shards)
   ->  merge + dedupe findings  ->  adversarial verify (verdict-gating)
   ->  compute verdict  ->  persist  ->  (optional) publish to the PR
```

- **Reviewer** ([`reviewer.py`](reviewer.py)) — strict prompt that only reports
  concrete, diff-introduced defects with an exact `path:line`; prefers zero
  findings over an unsupported one. Large PRs are split into balanced shards
  reviewed in parallel, then merged and deduped by severity.
- **Adversary** ([`adversary.py`](adversary.py)) — a fresh-context pass that
  tries to refute "ready to merge". Findings without a citation to a changed
  file are discarded; an unsubstantiated BLOCK is downgraded. It can only
  *tighten* the verdict.
- **Verdict** ([`verdict.py`](verdict.py)) — pure function:
  `critical -> block`, `high -> request-changes`, `medium/low ->
  approve-with-conditions`, else `approve`. Degraded runs cap at
  approve-with-conditions.
- **LLM choke point** ([`llm.py`](llm.py)) — every call is budget-ticked and
  schema-validated with exactly one retry; a failed/unconfigured call returns
  `None` (degrade) so a review always finishes with a verdict.

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

## Configuration

See [`.env.example`](.env.example). Key settings: `PRCHECK_LLM_BACKEND`
(`anthropic` | `openai` | `deterministic`), the provider key/model,
`PRCHECK_GITHUB_TOKEN` (repo read; PR write if publishing),
`PRCHECK_PUBLISH_REVIEWS`, `PRCHECK_GITHUB_WEBHOOK_SECRET`, and
`PRCHECK_ENABLE_ADVERSARY`.

Without a model key the backend resolves to `deterministic`: reviews complete,
marked `degraded`, with no findings — nothing crashes.

## Tests

```bash
python manage.py test reviews
```

25 tests, fully offline (model and GitHub calls are faked): schema validation,
sharding, dedup/merge ordering, verdict ordering, diff parsing, adversary
citation gating, the LLM retry/degrade path, end-to-end orchestration, and the
API + webhook.
