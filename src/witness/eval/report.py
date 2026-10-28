"""Markdown for eval runs: the headline comparison table and a per-case breakdown."""

from __future__ import annotations

from collections.abc import Sequence

from witness.eval.runner import EvalRun
from witness.eval.score import Metrics


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def metrics_table(rows: Sequence[Metrics]) -> str:
    lines = [
        "| Mode | PRs (bug / clean) | Reported | Precision | Recall | Clean PRs flagged "
        "| Hypotheses | Verified | Cost / PR | Latency p50 / p90 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for m in rows:
        flagged = f"{m.clean_flagged} / {m.clean_cases}" if m.clean_cases else "n/a"
        lines.append(
            f"| {m.label} | {m.bug_cases} / {m.clean_cases}"
            + (f" ({m.errors} errored)" if m.errors else "")
            + f" | {m.reported} | {_pct(m.precision)} ({m.true_positives}/{m.reported})"
            f" | {_pct(m.recall)} | {flagged} | {m.hypotheses} | {m.verified}"
            f" | ${m.cost_per_pr_usd:.2f} | {m.latency_p50_s:.0f}s / {m.latency_p90_s:.0f}s |"
        )
    return "\n".join(lines)


def case_table(run: EvalRun) -> str:
    lines = [
        "| Case | Kind | Hypotheses | Reported | True positives | Fix passes hidden tests | Time |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: |",
    ]
    for case in run.cases:
        if case.error:
            lines.append(f"| {case.case_id} | {case.kind} | error: {case.error[:60]} | | | | |")
            continue
        fixes = [
            f.fix_resolves_hidden_tests
            for f in case.findings
            if f.fix_resolves_hidden_tests is not None
        ]
        fix_cell = ", ".join("yes" if ok else "no" for ok in fixes) or ""
        true_positives = sum(f.true_positive for f in case.findings)
        lines.append(
            f"| {case.case_id} | {case.kind} | {case.hypotheses} | {len(case.reported)}"
            f" | {true_positives} | {fix_cell} | {case.duration_s:.0f}s |"
        )
    return "\n".join(lines)


def render(runs: Sequence[EvalRun]) -> str:
    first = runs[0]
    parts = [
        f"## Eval results: {first.backend} ({first.model})",
        f"Suite `{first.suite}`, run {first.created_at}.",
        metrics_table([m for run in runs for m in run.metrics]),
    ]
    for run in runs:
        parts += [f"### Per case ({run.mode})", case_table(run)]
    return "\n\n".join(parts) + "\n"
