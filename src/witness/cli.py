"""``witness review --repo PATH --base REF --head REF``"""

from __future__ import annotations

import argparse
import functools
import io
import logging
import sys
from pathlib import Path

from rich.console import Console

from witness import tracing
from witness.git import GitError
from witness.llm import BACKENDS, LLM, BackendError, RecordingBackend, create_backend
from witness.project import ProjectError
from witness.report import JsonReporter, Reporter, TerminalReporter
from witness.review import ReviewOptions, review
from witness.sandbox import DockerSandbox, Limits, SandboxError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="witness", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("review", help="review the change between two commits")
    cmd.add_argument("--repo", type=Path, default=Path("."), help="path to a git repository")
    cmd.add_argument("--base", required=True, help="base ref (the code before the change)")
    cmd.add_argument("--head", required=True, help="head ref (the code under review)")
    cmd.add_argument("--backend", choices=BACKENDS, default="anthropic")
    cmd.add_argument("--model", help="model id for the backend (default: backend's default)")
    cmd.add_argument("--fixtures", type=Path, help="recorded responses for --backend replay")
    cmd.add_argument("--record", type=Path, help="save live LLM replies as a replay fixture")
    cmd.add_argument("--json", type=Path, dest="json_out", help="also write the result as JSON")
    cmd.add_argument("--max-candidates", type=int, default=5)
    cmd.add_argument("--max-attempts", type=int, default=3)
    cmd.add_argument("--timeout", type=float, default=120.0, help="per test run, in seconds")
    cmd.add_argument("--budget-tokens", type=int, default=ReviewOptions.budget_tokens)
    cmd.add_argument("--no-fix", action="store_true", help="skip proposing and verifying fixes")
    cmd.add_argument("--show-unverified", action="store_true")
    cmd.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    console = Console()
    tracing.init()

    try:
        backend = create_backend(args.backend, model=args.model, fixtures=args.fixtures)
        if args.record:
            backend = RecordingBackend(backend, args.record)
        options = ReviewOptions(
            max_candidates=args.max_candidates,
            max_attempts=args.max_attempts,
            propose_fixes=not args.no_fix,
            budget_tokens=args.budget_tokens,
        )
        with console.status("Reviewing (hypothesize, then prove in the sandbox)..."):
            result = review(
                args.repo,
                args.base,
                args.head,
                LLM(backend),
                options=options,
                sandbox_factory=functools.partial(
                    DockerSandbox.create, limits=Limits(timeout_s=args.timeout)
                ),
            )
    except (GitError, ProjectError, SandboxError, BackendError, ValueError) as exc:
        console.print(f"[bold red]error:[/] {exc}")
        return 2
    finally:
        tracing.flush()

    reporters: list[Reporter] = [TerminalReporter(console, show_unverified=args.show_unverified)]
    if args.json_out:
        reporters.append(JsonReporter(args.json_out))
    for reporter in reporters:
        reporter.report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
