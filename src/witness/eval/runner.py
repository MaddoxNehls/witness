"""``witness eval``: review every selected case in parallel, score it, aggregate.

Each case is materialized into its own temp repo and reviewed exactly like
``witness review`` (same pipeline, same sandbox), with a bounded thread pool. Reviews
spend most of their time waiting on the LLM or on containers, so threads suffice.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain, zip_longest
from pathlib import Path

from pydantic import BaseModel

from witness.classify import classify_run
from witness.eval.manifest import Case, Manifest
from witness.eval.materialize import DEFAULT_CACHE, Materialized, export_with_tests, materialize
from witness.eval.score import CaseScore, Metrics, aggregate, failed_case, score_case
from witness.llm import LLM
from witness.models import Finding, OutcomeKind, Status
from witness.project import detect_project
from witness.prove import EditError, apply_edits
from witness.review import ReviewOptions, review
from witness.sandbox import DockerSandbox, runner_for

logger = logging.getLogger(__name__)

LLMFactory = Callable[[Case], LLM]


@dataclass(frozen=True)
class EvalOptions:
    verify: bool = True
    concurrency: int = 3
    limit: int | None = None  # per kind, spread across repositories
    case_ids: tuple[str, ...] = ()
    cache: Path = DEFAULT_CACHE
    check_fixes: bool = True
    max_candidates: int = 5
    max_attempts: int = 3


class EvalRun(BaseModel):
    created_at: str
    suite: str
    backend: str
    model: str
    mode: str  # "verify" | "no-verify"
    metrics: list[Metrics]
    cases: list[CaseScore]


def select(manifest: Manifest, *, limit: int | None, case_ids: tuple[str, ...] = ()) -> list[Case]:
    """Explicit ids, or up to ``limit`` cases per kind, taken round-robin across repos."""
    if case_ids:
        by_id = {c.id: c for c in manifest.cases}
        missing = [i for i in case_ids if i not in by_id]
        if missing:
            raise ValueError(f"unknown case ids: {', '.join(missing)}")
        return [by_id[i] for i in case_ids]
    chosen: list[Case] = []
    for kind in ("bug", "clean"):
        by_repo: dict[str, list[Case]] = {}
        for case in manifest.cases:
            if case.kind == kind:
                by_repo.setdefault(case.repo, []).append(case)
        spread = [c for c in chain.from_iterable(zip_longest(*by_repo.values())) if c is not None]
        chosen += spread[:limit] if limit is not None else spread
    return chosen


def run_eval(
    manifest: Manifest,
    make_llm: LLMFactory,
    options: EvalOptions,
    *,
    suite: str,
    progress: Callable[[str], None] = logger.info,
) -> EvalRun:
    cases = select(manifest, limit=options.limit, case_ids=options.case_ids)
    backend = model = ""

    def one(case: Case) -> CaseScore:
        nonlocal backend, model
        llm = make_llm(case)
        backend, model = llm.backend.name, llm.backend.model
        score = run_case(case, llm, options)
        outcome = score.error or (
            f"{score.hypotheses} hypotheses, {score.verified} verified, "
            f"{sum(f.true_positive for f in score.findings)} true positive"
        )
        progress(f"{case.id}: {outcome} ({score.duration_s:.0f}s)")
        return score

    with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
        scores = list(pool.map(one, cases))

    mode = "verify" if options.verify else "no-verify"
    metrics = [aggregate(scores, f"Witness ({mode})")]
    if options.verify:
        metrics.append(aggregate(scores, "same run, all hypotheses (paired)", all_hypotheses=True))
    return EvalRun(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        suite=suite,
        backend=backend,
        model=model,
        mode=mode,
        metrics=metrics,
        cases=scores,
    )


def run_case(case: Case, llm: LLM, options: EvalOptions) -> CaseScore:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="witness-eval-") as tmp:
        root = Path(tmp)
        try:
            mat = materialize(case, options.cache, root / "repo")
            review_options = ReviewOptions(
                verify=options.verify,
                propose_fixes=options.verify,
                max_candidates=options.max_candidates,
                max_attempts=options.max_attempts,
            )
            result = review(mat.path, mat.base, mat.head, llm, options=review_options)
        except Exception as exc:
            logger.exception("%s failed", case.id)
            return failed_case(
                case, f"{type(exc).__name__}: {exc}"[:500], time.monotonic() - started
            )

        score = score_case(case, result, verify=options.verify)
        if case.kind == "bug" and options.check_fixes:
            for finding, finding_score in zip(result.findings, score.findings, strict=True):
                if finding.status is Status.VERIFIED and finding.fix is not None:
                    finding_score.fix_resolves_hidden_tests = fix_resolves_hidden_tests(
                        case, mat, finding, root / f"fix-{finding.candidate.id}"
                    )
    return score


def fix_resolves_hidden_tests(case: Case, mat: Materialized, finding: Finding, dest: Path) -> bool:
    """Does Witness's fix, applied to the PR, make the upstream fix's own tests pass?"""
    assert finding.fix is not None
    tree = export_with_tests(mat.path, mat.head, case, dest)
    try:
        apply_edits(tree, finding.fix.edits, protected=case.hidden_tests[0])
    except EditError:
        return False
    sandbox = DockerSandbox.create(runner_for(detect_project(tree)), tree)
    result = sandbox.run_tests(tree, case.hidden_tests, label="hidden tests + fix")
    return classify_run(result, case.hidden_tests[0]).kind is OutcomeKind.PASSED
