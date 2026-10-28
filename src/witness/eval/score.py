"""Scoring a review against a case's ground truth, and aggregating into metrics.

What counts as "reported": VERIFIED findings with verification on; every hypothesis
(UNTESTED) with ``--no-verify``. A reported finding is a **true positive** when it
matches the case's known bug by either rule:

* **behavioural** (strongest): its repro test fails on the PR head and passes on the
  base. In a bug case the base *is* the upstream fix and the PR diff is exactly the
  revert, so this proves the finding is about behaviour the fix restored. Only
  verified findings can match this way; hypotheses alone have no test.
* **location**: it points into a reintroduced hunk (same file, line ranges overlapping
  within ``LINE_TOLERANCE``). This is the only rule an unverified hypothesis can meet,
  and it is generous: a wrong claim about the right lines still counts.

Every finding reported on a clean case is a false positive. Separately, for verified
findings with a suggested fix, the eval runs the upstream fix's own tests against the
PR plus Witness's fix ("fix resolves hidden tests").

Metrics: precision = true-positive findings / reported findings; recall = bug PRs with
at least one true positive / bug PRs; false-alarm rate = clean PRs with any reported
finding / clean PRs.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from pydantic import BaseModel

from witness.eval.manifest import Case
from witness.models import Finding, ReviewResult, Status

LINE_TOLERANCE = 3
REPORTED = {True: {Status.VERIFIED}, False: {Status.UNTESTED}}


class FindingScore(BaseModel):
    id: str
    status: str
    file: str
    lines: str
    claim: str
    reported: bool
    location_match: bool
    behavioural_match: bool
    fix_resolves_hidden_tests: bool | None = None

    @property
    def true_positive(self) -> bool:
        return self.reported and (self.location_match or self.behavioural_match)


class CaseScore(BaseModel):
    case_id: str
    kind: str
    findings: list[FindingScore]
    hypotheses: int
    verified: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    llm_calls: int
    duration_s: float
    error: str | None = None

    @property
    def reported(self) -> list[FindingScore]:
        return [f for f in self.findings if f.reported]

    @property
    def detected(self) -> bool:
        return any(f.true_positive for f in self.findings)


def location_match(finding: Finding, case: Case) -> bool:
    c = finding.candidate
    return any(
        hunk.file == c.file
        and c.line_start <= hunk.end + LINE_TOLERANCE
        and hunk.start - LINE_TOLERANCE <= c.line_end
        for hunk in case.hunks
    )


def behavioural_match(finding: Finding, case: Case) -> bool:
    return case.kind == "bug" and finding.status is Status.VERIFIED and finding.regression is True


def score_case(case: Case, result: ReviewResult, *, verify: bool) -> CaseScore:
    reported_statuses = REPORTED[verify]
    findings = [
        FindingScore(
            id=f.candidate.id,
            status=f.status.value,
            file=f.candidate.file,
            lines=f"{f.candidate.line_start}-{f.candidate.line_end}",
            claim=f.candidate.claim,
            reported=f.status in reported_statuses,
            location_match=case.kind == "bug" and location_match(f, case),
            behavioural_match=behavioural_match(f, case),
        )
        for f in result.findings
    ]
    return CaseScore(
        case_id=case.id,
        kind=case.kind,
        findings=findings,
        hypotheses=len(result.findings),
        verified=len(result.with_status(Status.VERIFIED)),
        cost_usd=result.usage.cost_usd,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        llm_calls=result.usage.calls,
        duration_s=result.duration_s,
    )


def failed_case(case: Case, error: str, duration_s: float) -> CaseScore:
    return CaseScore(
        case_id=case.id,
        kind=case.kind,
        findings=[],
        hypotheses=0,
        verified=0,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        llm_calls=0,
        duration_s=duration_s,
        error=error,
    )


class Metrics(BaseModel):
    label: str
    bug_cases: int
    clean_cases: int
    errors: int
    reported: int
    true_positives: int
    precision: float | None
    recall: float | None
    clean_flagged: int
    false_alarm_rate: float | None
    hypotheses: int
    verified: int
    fixes_resolving_hidden_tests: int
    cost_per_pr_usd: float
    tokens_per_pr: float
    latency_p50_s: float
    latency_p90_s: float


def aggregate(scores: Sequence[CaseScore], label: str, *, all_hypotheses: bool = False) -> Metrics:
    """Metrics over cases that ran. ``all_hypotheses`` re-scores a verify run as if every
    hypothesis had been reported: the paired ablation, same hypotheses, no verification."""
    ran = [s for s in scores if s.error is None]
    bugs = [s for s in ran if s.kind == "bug"]
    clean = [s for s in ran if s.kind == "clean"]

    def reported(score: CaseScore) -> list[FindingScore]:
        return score.findings if all_hypotheses else score.reported

    def is_tp(f: FindingScore) -> bool:
        return f.location_match or f.behavioural_match

    reported_count = sum(len(reported(s)) for s in ran)
    tp = sum(is_tp(f) for s in ran for f in reported(s))
    detected = sum(any(is_tp(f) for f in reported(s)) for s in bugs)
    flagged = sum(bool(reported(s)) for s in clean)
    durations = sorted(s.duration_s for s in ran) or [0.0]
    return Metrics(
        label=label,
        bug_cases=len(bugs),
        clean_cases=len(clean),
        errors=len(scores) - len(ran),
        reported=reported_count,
        true_positives=tp,
        precision=tp / reported_count if reported_count else None,
        recall=detected / len(bugs) if bugs else None,
        clean_flagged=flagged,
        false_alarm_rate=flagged / len(clean) if clean else None,
        hypotheses=sum(s.hypotheses for s in ran),
        verified=sum(s.verified for s in ran),
        fixes_resolving_hidden_tests=sum(
            bool(f.fix_resolves_hidden_tests) for s in ran for f in s.findings
        ),
        cost_per_pr_usd=statistics.fmean(s.cost_usd for s in ran) if ran else 0.0,
        tokens_per_pr=(
            statistics.fmean(s.input_tokens + s.output_tokens for s in ran) if ran else 0.0
        ),
        latency_p50_s=statistics.median(durations),
        latency_p90_s=durations[min(len(durations) - 1, int(0.9 * len(durations)))],
    )
