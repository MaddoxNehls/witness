# Witness

**An AI code reviewer that only reports bugs it can prove.**

AI pull-request reviewers are noisy. They flag plausible-sounding problems that are not
real, so developers learn to ignore them. Witness takes the opposite trade: **precision
over recall**. Every finding it reports comes with evidence produced by execution, a test
that fails on the change and passes without it, run in an isolated sandbox. A hypothesis
that cannot be reproduced is dropped, however convincing it sounds.

**On 10 real open-source PRs** (5 that reintroduce a known bug, 5 labelled clean), the
same model's hypotheses were reported with and without the verification step:

| | Precision | Recall | Clean PRs flagged |
| --- | ---: | ---: | ---: |
| Witness (only verified findings) | **80%** | 100% | **2 / 5** |
| Same hypotheses, unverified | 67% | 100% | 3 / 5 |

Verification dropped 3 hypotheses it could not reproduce, all on clean PRs, without
losing a real bug. One remaining "false positive" on a clean PR turned out to be a real
bug that upstream fixed later, on the same line. The sample is small; the [results section](#results-a-small-real-run-10-prs)
has the caveats and the raw recordings.

> Status: milestones 1-4 are code-complete. The CLI, hardened Docker sandbox (pytest and
> vitest), GitHub App (webhook, Postgres queue, worker, check runs, inline reviews) and the
> eval harness with a 55-case benchmark are done. The App is not registered or deployed
> yet (milestone 5). See [docs/spec.md](docs/spec.md) and [the eval results](#eval).

## What it looks like

The repo ships a small invoicing project whose HEAD commit introduces two real bugs and
one harmless refactor. With recorded model responses the whole pipeline runs offline,
including the Docker sandbox. Two bugs are proven, each with a repro test and a fix
verified in the sandbox; the third hypothesis is rejected because its repro passed:

![witness review on the demo repo: 2 verified findings with repro tests and verified fixes, 1 rejected hypothesis](docs/demo.svg)

<sub>Rendered from a real run by `scripts/render_demo_svg.py`. The same output as text:</sub>

```text
$ witness review --repo .demo/demo-repo --base main --head float-discounts \
      --backend replay --fixtures fixtures/demo-repo/replay.json
Witness  b3bcf4d7 -> a5a672ab  |  python/pytest  |  replay (claude-opus-5-5)
2 verified   0 pre-existing   1 rejected   0 unverified   (from 3 hypotheses)

┌─ C1 VERIFIED  high  src/invoicing/dates.py:15-18 ──────────────────────────────────┐
│ iter_billing_days now loops while day < end, but end is inclusive ...              │
│ Evidence: failed on the declared assertion; the same test passes on BASE           │
│ Fails on HEAD with: AssertionError: assert Decimal('30.00') == Decimal('31.00')    │
│ Attempts: 1 assertion                                                              │
│ Repro test: ...                                                                    │
│ Suggested fix (verified: the repro passes with this patch)                         │
│ -    while day < end:                                                              │
│ +    while day <= end:                                                             │
└────────────────────────────────────────────────────────────────────────────────────┘
┌─ C2 VERIFIED  high  src/invoicing/money.py:20-21 ──────────────────────────────────┐
│ apply_discount now does the arithmetic in binary floats and rounds with round() ...│
│ Fails on HEAD with: AssertionError: assert Decimal('0.57') == Decimal('0.58')      │
│ Attempts: 1 test_error (ModuleNotFoundError) -> 2 assertion                        │
│ ...                                                                                │
└────────────────────────────────────────────────────────────────────────────────────┘
Not reported: hypotheses that failed verification
│ C3 │ rejected │ Invoice.subtotal now uses sum() ... returns the int 0 │ a well-formed repro
│    │          │ rather than a Decimal ...                             │ passed on HEAD ...
```

C3 is the point of the project. It is exactly the kind of comment a conventional AI
reviewer would post: specific, plausible, wrong (`sum()` is given `start=Decimal("0.00")`).
Witness wrote a test for it, the test passed on HEAD, and the claim was discarded.

## How it works

```mermaid
flowchart LR
    subgraph host["Host (trusted)"]
        CLI["witness review"] --> CTX["Context builder<br/>diff + tree-sitter symbols<br/>token budget"]
        CTX --> HYP["Hypothesize<br/>1 structured LLM call"]
        HYP -->|candidates| PROVE["Prove loop<br/>per candidate, at most 3 attempts"]
        PROVE <-->|"write test / feedback"| LLM[("LLM backend<br/>anthropic | openrouter<br/>claude-cli | replay")]
        PROVE --> CLS["Classifier<br/>JUnit XML to outcome<br/>outcome to verdict"]
        CLS --> REP["Reporters<br/>terminal | JSON | (GitHub)"]
    end
    subgraph sandbox["Docker sandbox (untrusted)"]
        HEAD["repro on HEAD"]
        BASE["repro on BASE"]
        FIX["repro on HEAD + fix"]
    end
    PROVE --> HEAD & BASE & FIX
    HEAD & BASE & FIX -->|"stdout + JUnit XML"| CLS
```

1. **Context.** `git diff BASE HEAD`, the changed files, and the enclosing function or
   class of every changed hunk (tree-sitter, Python and TypeScript), shown with HEAD line
   numbers. The test runner is detected from `pyproject.toml` / `package.json` and can be
   overridden by `.witness.yml`.
2. **Hypothesize.** One LLM call with a JSON schema returns candidate bugs (claim, file,
   line range, trigger, severity, confidence), validated with pydantic. Candidates outside
   the changed files are dropped.
3. **Prove.** For each candidate the model writes a minimal test and *declares in advance*
   how it will fail. Witness runs it in the sandbox on HEAD and classifies the result. If
   it failed for the declared reason, the same test runs on BASE. Otherwise the classified
   outcome goes back to the model, which can retry (up to 3 attempts) or concede.
4. **Fix (optional).** For a verified bug, the model proposes exact-match edits. Witness
   applies them to a scratch copy of HEAD and reruns the repro; the fix is marked
   verified only if the test now passes.
5. **Report.** Rich terminal output and a JSON document of the full `ReviewResult`
   (every attempt, test, outcome and fix). Reporters share one `Reporter` protocol, so a
   GitHub review reporter slots in next to them.

### What counts as proof

The verdict comes from deterministic code in [`classify.py`](src/witness/classify.py)
and [`prove.py`](src/witness/prove.py), never from the model's opinion of its own test.

| Test result on HEAD | Matches the declared failure? | Result on BASE | Status |
|---|---|---|---|
| assertion fails, or the declared exception is raised from a changed file | yes | passes | **verified** (regression) |
| same | yes | cannot run it (e.g. the code is new) | **verified** (new code) |
| same | yes | fails the same way | **pre-existing** (real bug, not caused by this change) |
| fails for a different reason | no | not run | retry, then **unverified** |
| import, syntax or name error, fixture error, skip, or an exception raised by the test's own body | no | not run | retry, then **unverified** |
| passes | no | not run | retry or concede, then **rejected** |

Only *verified* findings are reported in full. Rejected and pre-existing ones get one line
each; unverified ones are hidden unless `--show-unverified` is given.

## Sandbox security model

Everything from the repository under review, and everything the model writes, is
untrusted. The model never gets a shell: it only produces text (a test file, a list of
edits) that Witness places into a container.

| Control | How |
|---|---|
| No network | `network_mode=none` for every test run |
| Resource limits | 1 CPU, 1 GiB memory with no extra swap, 256 PIDs, 120 s wall clock (`--timeout`), then `docker kill` |
| Host filesystem | the source snapshot is bind-mounted **read-only** at `/repo` and copied into a 512 MiB `tmpfs` at `/work`; the repro file is a separate read-only mount. No host path is writable from inside. |
| Container | read-only root filesystem, `noexec` tmpfs mounts, `cap_drop=ALL`, `no-new-privileges`, unprivileged uid 10001 |
| Output | capped at 200 KB inside the container and again on the host |
| Snapshots | `git archive` of the exact commit, extracted with tarfile's `data` filter (no absolute paths, `..` escapes, devices or outward links); no `.git`, no untracked files |
| Dependencies | installed once into an image tagged by a hash of the Dockerfile and all dependency manifests and lockfiles. The build context holds **only those manifests**, never repo code; npm runs with `--ignore-scripts`. |
| Model edits | fix edits must target existing files inside the snapshot (resolved-path check), must match exactly once, may not touch the repro, and are applied to a scratch copy |
| Prompt injection | repo content is wrapped in `<repository>` tags and labelled as data; and since verdicts come from execution, a manipulated model can at worst fail to prove a bug |

Known limits, stated plainly:

- Dependency installation (`pip install`) has network access and may run package build
  hooks from PyPI. It never runs code from the repository under review.
- Code inside the container controls its own output, so a hostile repository could forge
  a "pass" or "fail" for its own review. Isolation protects the host, not the verdict on an
  adversarial repo.
- Docker shares the host kernel. For hostile multi-tenant use (the future GitHub App),
  the plan is gVisor (`--runtime=runsc`) or Firecracker microVMs.

## Context budget

The model sees sections in priority order: project summary and file list, the diff (one
section per file), the enclosing symbols of each hunk, then the full HEAD text of each
changed file. Sections are added greedily against the token budget (`--budget-tokens`,
default 60k, estimated at 4 characters per token). The first section that does not fit
is cut at a line boundary with a `[truncated: N more lines]` marker, and every later
section is named in an "Omitted" footer whose cost also comes out of the budget. Higher
layers summarize lower ones, so what survives is the most useful view of the change.

## Running it

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), git, and Docker.

```bash
uv sync --all-extras                                    # includes the tracing extra
```

### Offline demo (no API key)

```bash
uv run python fixtures/demo-repo/build_demo_repo.py     # builds .demo/demo-repo
uv run witness review --repo .demo/demo-repo --base main --head float-discounts \
    --backend replay --fixtures fixtures/demo-repo/replay.json --json .demo/result.json
```

The first run builds the dependency image (about a minute); later runs reuse it and take
a few seconds. The demo's git history is generated by a script with fixed authors and
dates, so the commit hashes are identical on every machine and no nested `.git` is
committed.

The replay fixture was written by hand in the same format a live run records with
`--record`. The demo therefore shows Witness's own machinery (sandbox, classification,
verdicts, fix verification) running for real, not the quality of a particular model.

### Against a real change

```bash
export ANTHROPIC_API_KEY=...
uv run witness review --repo ../some-repo --base main --head my-branch
uv run witness review ... --backend openrouter            # OPENROUTER_API_KEY
uv run witness review ... --backend claude-cli            # local `claude -p`, dev only
uv run witness review ... --record run.json               # save replies for --backend replay
```

The Anthropic backend defaults to `claude-opus-5-5` with JSON-schema structured output,
prompt caching of the shared repo context, and server-side refusal fallback enabled.

### `.witness.yml`

```yaml
language: python                  # python | typescript
runner: pytest                    # pytest | vitest  (jest is detected but not run yet)
test_command: python -m pytest -p no:randomly   # base command; Witness appends the repro path and report flags
```

### Tracing

With the `tracing` extra (the Tracewright SDK, a sibling project, via a uv path source)
and `TRACEWRIGHT_API_KEY` set, each review is recorded as a trace:

```text
witness review > hypothesize > chat claude-opus-5-5
               > candidate C2 > attempt 1 > chat, sandbox exec head
                              > attempt 2 > chat, sandbox exec head, sandbox exec base
                              > fix > chat, sandbox exec fix
```

Without the extra, the import is guarded and every span is a no-op.

## GitHub App

```mermaid
flowchart LR
    GH["GitHub<br/>pull_request webhook"] -->|"POST /webhooks/github"| WEB["witness serve<br/>FastAPI: verify HMAC,<br/>dedupe, enqueue"]
    WEB -->|"one transaction"| PG[("Postgres<br/>deliveries, jobs, reviews")]
    PG -->|"claim: FOR UPDATE SKIP LOCKED"| WK["witness worker"]
    WK -->|"fetch base + head<br/>(installation token)"| GH2["GitHub"]
    WK -->|"review()"| SB["Docker sandbox"]
    WK -->|"check run, PR review,<br/>supersede old comments"| GH2
```

- **Webhook** (`witness serve`). The `X-Hub-Signature-256` HMAC is checked in constant
  time *before* the body is parsed; anything else is a 401. `pull_request`
  `opened`/`synchronize`/`reopened` on an open PR becomes a job. Every other event is
  recorded and ignored. The `X-GitHub-Delivery` id is the primary key of `deliveries`,
  so GitHub's redeliveries are acknowledged and dropped.
- **Queue: a Postgres table, not arq/Redis.** Postgres is already needed for review
  history, so the queue adds no infrastructure. Recording the delivery, superseding
  queued jobs for the same PR and enqueueing the new one happen in **one transaction**.
  Workers claim with `FOR UPDATE SKIP LOCKED`, so any number can run without blocking
  each other. A review takes minutes, so queue throughput is irrelevant. Delivery is at
  least once: a job whose worker died is reclaimed after a 30-minute lease, at most 3
  attempts.
- **Worker** (`witness worker`). Opens an `in_progress` check run, fetches exactly the
  base and head commits (`--depth=1`; the installation token is sent as an HTTP header
  and never written to the remote URL or `.git/config`), runs the same `review()` as the
  CLI, then posts:
  - a **check run** with a summary table (verified, pre-existing, rejected and unverified
    counts). The conclusion is `failure` when a bug is verified (it only blocks merging
    if the repo makes the check required), `success` otherwise, and `neutral` if Witness
    itself fails;
  - one **PR review** with inline comments **only for VERIFIED findings**. Each has the
    claim, the evidence, the repro test and the sandbox output in collapsible blocks, and
    the verified fix as a GitHub ```` ```suggestion ```` block anchored to exactly the lines
    it replaces. Comments are placed only on lines the PR diff shows (GitHub rejects
    others); a finding outside the diff goes into the review body instead.
- **Re-runs on `synchronize`.** A newer push supersedes queued jobs at enqueue time. A
  running job checks before posting and drops its stale result. Once the new review is
  posted, earlier Witness reviews on the PR have their body replaced with "superseded by
  the review of `abc1234`" and their inline comments minimized as *outdated* (GraphQL
  `minimizeComment`), so the PR never accumulates duplicate threads.
- **History.** Every posted review is stored in `reviews` (head sha, check run id,
  comment node ids, counts, and the full `ReviewResult` as JSONB).

### Setting it up

The App is described by [`github-app-manifest.json`](github-app-manifest.json). It
requests only checks (write), pull requests (write), contents (read) and metadata
(read), and subscribes only to `pull_request`. Registration uses GitHub's one-click
[manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest):

```bash
docker compose up -d                       # Postgres on localhost:5449
# expose port 8000 publicly first (e.g. cloudflared, ngrok, smee.io)
uv run python scripts/register_github_app.py --webhook-url https://<public-host>/webhooks/github
#   opens github.com with the manifest filled in; after you click "Create", GitHub redirects
#   back and the script writes .env.github-app plus the private key (both gitignored)
set -a; . ./.env.github-app; set +a
export ANTHROPIC_API_KEY=...               # or WITNESS_BACKEND=openrouter / claude-cli
uv run witness serve --host 0.0.0.0 --port 8000
uv run witness worker                      # run several for parallel reviews
```

Settings come from the environment: `WITNESS_GITHUB_APP_ID`,
`WITNESS_GITHUB_PRIVATE_KEY_PATH` (or `WITNESS_GITHUB_PRIVATE_KEY`),
`WITNESS_GITHUB_WEBHOOK_SECRET`, `WITNESS_DATABASE_URL` (default
`postgresql://witness:witness@localhost:5449/witness`), `WITNESS_BACKEND` and
`WITNESS_MODEL`. The worker needs a Docker daemon for the sandbox. Install the App on a
repository and open a PR to trigger a review.

## Eval

The spec's headline question: **does verification buy precision, and what does it cost
in recall?** `witness eval` answers it on a benchmark of real bugs.

### The benchmark

[`benchmark/manifest.json`](benchmark/manifest.json) holds **30 bug cases and 25 clean
cases** from MIT/BSD repositories: more-itertools, sqlparse, humanize, marshmallow and
tabulate (Python), and ufo, pathe and std-env (TypeScript, vitest). A ninth repo, defu,
was mined but none of its candidates survived validation, and clean candidates were
scarcer, hence 25 rather than 30 (see `benchmark/mining-report.json`). The manifest
contains commit references only. `uv run python benchmark/build_benchmark.py` regenerates it
(network plus Docker), and `witness eval` re-creates each PR locally from a mirror cache.

- **Bug case.** A real upstream fix commit F with 1-3 source files, at least one test
  file, and a message that says it fixes something (typo, docs, CI, typing, lint and
  dependency fixes are excluded). The PR's base is F with its test changes held back, and
  the PR's head reverts F's source files. So the PR diff is exactly "un-fix", and F's own
  tests are hidden ground truth.
- **Clean case.** A refactor or feature commit (1-3 source files, with tests) whose files
  no bug fix touched in the next 50 commits ("no known bug").
- **Every case is validated by execution before it is kept.** For a bug case, the hidden
  tests must fail on the PR head with an assertion or exception (not an import error) and
  pass on the fixed base. For a clean case, its tests must pass. Candidates that fail
  validation are discarded and counted in `benchmark/mining-report.json`.

### Scoring

A finding is **reported** if it is VERIFIED (or, under `--no-verify`, any hypothesis). A
reported finding is a **true positive** if either:

- **behavioural** (strongest): its repro test fails on the PR head and passes on the base.
  In a bug case the base *is* the upstream fix and the diff is exactly the revert, so this
  ties the finding to the behaviour the fix restored. Only verified findings can match
  this way.
- **location**: it points into a reintroduced hunk (same file, line ranges overlapping
  within 3 lines). This is the only rule an untested hypothesis can meet, and it is
  generous: a wrong claim about the right lines still counts, which flatters the
  no-verify baseline.

Every finding reported on a clean PR is a false positive. **Precision** = true-positive
findings / reported findings; **recall** = bug PRs with at least one true positive / bug
PRs; **clean PRs flagged** = clean PRs with any reported finding. The eval also applies
Witness's suggested fix to the PR and runs the upstream fix's own hidden tests ("fix
passes hidden tests").

Two ablations use the same model and prompts:

- `--no-verify`: an independent run that reports every hypothesis, like a conventional
  AI reviewer.
- "all hypotheses (paired)": the verify run re-scored as if every hypothesis had been
  reported. The hypotheses are identical, so the difference is due to verification
  alone.

```bash
uv run witness eval --suite benchmark/manifest.json --backend claude-cli --limit 5
uv run witness eval --suite benchmark/manifest.json --backend claude-cli --limit 5 --no-verify
uv run witness eval-report benchmark/results/*.json --out benchmark/results/RESULTS.md
```

`--limit N` takes N bug and N clean cases, round-robin across repositories; `--cases`
picks ids; `--concurrency` caps parallel reviews. Each run writes JSON (every case,
finding and score) and markdown.

The harness itself is tested offline: `fixtures/eval-demo/` builds a small "upstream"
repo with two real fixes and a refactor, the real miner turns it into 2 bug cases and 1
clean case, and replayed responses drive `witness eval` through materialization, review,
scoring and the hidden-test fix check.

### Results: a small real run (10 PRs)

`claude-cli` backend (Claude Opus 5.5 through the local `claude -p`), 5 bug and 5 clean
PRs (`--limit 5`, one per repository), `--max-candidates 3 --max-attempts 2`, concurrency 3,
run on 2026-10-28.

| Mode | PRs (bug / clean) | Reported | Precision | Recall | Clean PRs flagged | Hypotheses | Verified |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Witness (verify) | 5 / 5 | 15 | **80%** (12/15) | 100% (5/5) | **2 / 5** | 18 | 15 |
| Same hypotheses, no verification | 5 / 5 | 18 | 67% (12/18) | 100% (5/5) | 3 / 5 | 18 | 0 |

- **Cost:** 44 `claude -p` calls in total (4.4 per PR), about 162k tokens per PR. The CLI
  reports a list-price equivalent of **$1.32 per PR**; it ran on a subscription, so
  nothing was billed.
- **Latency:** p50 43 s and p90 77 s per PR, including Docker image builds on the first
  run of each dependency set.
- **Fixes:** 9 of the 12 verified bug-case fixes made the upstream fix's own hidden
  tests pass.
- **No-verify row:** this is the paired ablation. The recorded hypotheses from the verify
  run were replayed with `--no-verify`, so the hypotheses are identical and it cost no
  extra calls. Its cost and latency are not meaningful and are left out.

What the numbers say, and what they do not:

- **Verification removed 3 of 18 hypotheses, all on clean PRs, and lost no true
  positive.** On these 10 PRs that is the whole precision gain (67% to 80%) and one fewer
  clean PR flagged. All 12 bug-case hypotheses reproduced, so recall cost nothing here.
- **Two of the three "false positives" are verified behaviour changes in commits the
  benchmark labelled clean.** Reading them:
  - `humanize-clean-711df91a`: `activate(None)` now raises `AttributeError`. Upstream
    later fixed exactly this, on exactly that line (`6fdd7ac`, "Allow
    i18n.activate(None)"), so this is a **real bug** that the "no known bug" heuristic
    missed. That heuristic only looks for later commits whose message says "fix".
  - `marshmallow-clean-07c9c18d` (2 findings): `load`/`dump` gained a `context` argument
    but `loads`/`dumps` did not, so `loads(..., context=...)` raises `TypeError`. That is
    true at that commit, but upstream later redesigned and removed the parameter instead
    of "fixing" it. It is scored as a false positive.

  With the humanize label corrected, precision is 13/15 (87%) and 1 of 4 clean PRs is
  flagged. The table keeps the strict, as-labelled scores.
- **Caveats.**
  - **Sample size:** 10 PRs, one run, one model. The confidence intervals are wide, and
    100% recall on 5 PRs says little.
  - **Easy bug cases:** these PRs are small, single-purpose reverts with an existing test
    suite, easier than real-world PRs.
  - **Duplicates:** several true positives in one PR describe the same bug in different
    ways, which inflates the finding counts.
  - **Generous location rule:** it can only help the no-verify row.
  - **Capped search:** candidates and attempts were capped to keep the run small.

Every model reply is saved in [`benchmark/results/recorded/`](benchmark/results/recorded/).
Re-scoring or re-running the analysis is free and offline:
`witness eval --backend replay --fixtures benchmark/results/recorded --limit 5`. The full
per-case and per-finding results are in [`benchmark/results/`](benchmark/results/).

## Development

```bash
docker compose up -d              # Postgres for the App tests (port 5449)
uv run ruff check . && uv run ruff format --check .
uv run mypy                       # strict, src and tests
uv run pytest                     # everything; needs Docker for the integration tests
uv run pytest -m "not integration and not postgres"
```

Tests never call a real LLM or GitHub. The unit tests drive the prove loop with the replay
backend and a scripted sandbox. The integration tests use real Docker for the sandbox's
isolation properties (uid, read-only mounts, no network, timeout kill), the vitest
runner, the full demo review including its trace tree, and the offline eval. The App
tests use a fresh Postgres schema per test (dedupe, the job lifecycle, `SKIP LOCKED`
under 5 concurrent workers, lease reclaim) and respx-mocked GitHub, asserting the exact
review payload and the check-run lifecycle.

## Layout

```text
src/witness/
  cli.py            argument parsing, wiring, exit codes
  review.py         the pipeline: snapshots, context, hypothesize, prove
  context.py        prompt context and the token budget
  symbols.py        tree-sitter: enclosing functions and classes of changed lines
  project.py        language and runner detection, .witness.yml
  hypothesize.py    stage 1: candidate bugs
  prove.py          stage 2: the bounded attempt loop, verdicts, fix verification
  classify.py       JUnit XML to outcome, outcome to "proved or not" (pure)
  prompts.py        all prompt text
  models.py         Candidate, TestOutcome, Attempt, Finding, ReviewResult
  llm/              backend protocol, anthropic, openrouter, claude-cli, replay/record
  sandbox/          Docker sandbox, cached dependency images, pytest and vitest runners
  report/           Reporter protocol, terminal and JSON reporters
  tracing.py        optional Tracewright spans
  app/              GitHub App: webhook, signature, Postgres store/queue, worker,
                    githubkit gateway, review/check-run rendering, manifest registration
  eval/             benchmark manifest, miner, PR materialization, runner, scoring, report
benchmark/          build_benchmark.py, manifest.json, mining-report.json, results/
fixtures/demo-repo/ base/ and head/ trees, the history builder, recorded responses
fixtures/eval-demo/ offline mini-benchmark builder and replayed responses
scripts/            register_github_app.py (manifest flow)
github-app-manifest.json, docker-compose.yml
```
