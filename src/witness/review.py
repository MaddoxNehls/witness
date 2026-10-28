"""The pipeline: context, hypothesize, prove. Reporting is left to the caller."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from witness import git, tracing
from witness.context import DEFAULT_BUDGET_TOKENS, build_context
from witness.hypothesize import hypothesize
from witness.llm import LLM
from witness.models import Finding, ReviewResult, Status
from witness.project import detect_project
from witness.prove import Prover, Sandbox, Trees
from witness.sandbox import DockerSandbox, Runner, runner_for

SandboxFactory = Callable[[Runner, Path], Sandbox]


@dataclass(frozen=True)
class ReviewOptions:
    max_candidates: int = 5
    max_attempts: int = 3
    propose_fixes: bool = True
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    # False reproduces a conventional AI reviewer: every hypothesis is reported untested.
    verify: bool = True


def review(
    repo: Path,
    base_ref: str,
    head_ref: str,
    llm: LLM,
    *,
    options: ReviewOptions | None = None,
    sandbox_factory: SandboxFactory = DockerSandbox.create,
) -> ReviewResult:
    options = options or ReviewOptions()
    started = time.monotonic()
    base, head = git.resolve(repo, base_ref), git.resolve(repo, head_ref)
    with (
        tracing.run(
            "witness review",
            input={"repo": str(repo), "base": base, "head": head},
            tags=["witness", llm.backend.name],
        ) as run,
        tempfile.TemporaryDirectory(prefix="witness-") as tmp,
    ):
        trees = Trees(
            head=git.export_tree(repo, head, Path(tmp) / "head"),
            base=git.export_tree(repo, base, Path(tmp) / "base"),
        )
        project = detect_project(trees.head)
        runner = runner_for(project)
        context = build_context(
            repo, base, head, trees.head, project, budget_tokens=options.budget_tokens
        )
        candidates = hypothesize(llm, context, max_candidates=options.max_candidates)
        findings: list[Finding] = []
        if candidates and not options.verify:
            findings = [
                Finding(candidate=c, status=Status.UNTESTED, reason="verification disabled")
                for c in candidates
            ]
        elif candidates:
            sandbox = sandbox_factory(runner, trees.head)
            prover = Prover(
                llm,
                sandbox,
                context,
                trees,
                max_attempts=options.max_attempts,
                propose_fixes=options.propose_fixes,
            )
            findings = [prover.prove(candidate) for candidate in candidates]

        result = ReviewResult(
            repo=str(repo),
            base=base,
            head=head,
            backend=llm.backend.name,
            model=llm.backend.model,
            language=project.language,
            test_runner=runner.name,
            findings=findings,
            usage=llm.usage,
            duration_s=round(time.monotonic() - started, 2),
            context_tokens=context.tokens,
            context_omissions=context.omitted,
        )
        run.set_output({f.candidate.id: f.status.value for f in findings})
        return result
