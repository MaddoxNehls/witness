# Witness: Spec (v1)

> An AI code reviewer that only reports bugs it can prove.

## Problem
AI PR reviewers are noisy. They flag plausible-sounding issues that aren't real, so developers learn to ignore them. Precision matters more than recall: one false positive costs more trust than one missed bug.

## Core idea
Every finding must come with **evidence produced by execution**: a failing test, a crashing input, or a reproduced error. These run in an isolated sandbox against the PR's code. Findings that can't be reproduced are dropped, or downgraded to "unverified" and hidden by default.

## Pipeline
1. **Trigger:** a GitHub App webhook (`pull_request` opened/synchronize), or the CLI `witness review <repo> <pr>`
2. **Context:** diff, changed files, related symbols (tree-sitter index), and the repo's test command (auto-detected or from `.witness.yml`)
3. **Hypothesize:** an LLM proposes candidate bugs with a structured schema: claim, location, triggering condition
4. **Prove:** for each candidate, an agent writes a minimal reproducing test, runs it in the sandbox on the PR head, and confirms it fails there. Where possible it also confirms the test passes on the base commit (a regression) or is fixed by a suggested patch.
5. **Report:** a PR review with inline comments, each carrying the repro test, sandbox output, and a suggested fix. A summary check run lists verified and unverified counts.

## Sandbox
- Docker containers with no network, CPU/mem/time limits, a read-only repo mount, and a writable overlay
- Language support v1: Python (pytest) and TypeScript (vitest/jest)

## Evals (the headline of the README)
- A benchmark of real bug-fix commits mined from open-source repos: revert the fix, so the PR reintroduces the bug
- Plus clean PRs to measure false positives
- Report **precision, recall, cost per PR, and latency**, comparing Witness against a "no verification" baseline using the same model
- Everything is traced to **Tracewright** (dogfooding)

## Stack
Python 3.13, FastAPI webhook service, GitHub App (Octokit-equivalent via `githubkit`), Docker SDK, Claude via Anthropic SDK, with OpenRouter as an alternative, tree-sitter, Postgres for review history. Tests use recorded LLM responses (offline).

## Milestones
1. CLI against a local repo + diff: hypothesize → prove → report to the terminal
2. Sandbox hardening and the Python + TS runners
3. GitHub App + inline review comments + check run
4. Eval harness + benchmark dataset + results table
5. Deploy (Fly.io), README with metrics, write-up

## Non-goals
Style/lint feedback, auto-merging, languages beyond Python/TS in v1.
