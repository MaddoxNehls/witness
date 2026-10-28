"""witness: review a change, run the benchmark, or run the GitHub App.

witness review --repo PATH --base REF --head REF
witness eval --suite benchmark/manifest.json --backend claude-cli --limit 5
witness eval-report RUN.json [RUN.json ...]
witness serve | witness worker
"""

from __future__ import annotations

import argparse
import functools
import io
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from witness import tracing
from witness.git import GitError
from witness.llm import BACKENDS, LLM, BackendError, RecordingBackend, create_backend
from witness.project import ProjectError
from witness.report import JsonReporter, Reporter, TerminalReporter
from witness.review import ReviewOptions, review
from witness.sandbox import DockerSandbox, Limits, SandboxError

USER_ERRORS = (GitError, ProjectError, SandboxError, BackendError, ValueError)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="witness", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    cmd = commands.add_parser("review", help="review the change between two commits")
    cmd.add_argument("--repo", type=Path, default=Path("."), help="path to a git repository")
    cmd.add_argument("--base", required=True, help="base ref (the code before the change)")
    cmd.add_argument("--head", required=True, help="head ref (the code under review)")
    _backend_args(cmd)
    cmd.add_argument("--record", type=Path, help="save live LLM replies as a replay fixture")
    cmd.add_argument("--json", type=Path, dest="json_out", help="also write the result as JSON")
    cmd.add_argument("--max-candidates", type=int, default=5)
    cmd.add_argument("--max-attempts", type=int, default=3)
    cmd.add_argument("--timeout", type=float, default=120.0, help="per test run, in seconds")
    cmd.add_argument("--budget-tokens", type=int, default=ReviewOptions.budget_tokens)
    cmd.add_argument("--no-fix", action="store_true", help="skip proposing and verifying fixes")
    cmd.add_argument(
        "--no-verify", action="store_true", help="report every hypothesis untested (ablation)"
    )
    cmd.add_argument("--show-unverified", action="store_true")

    ev = commands.add_parser("eval", help="run the benchmark and score precision and recall")
    ev.add_argument("--suite", type=Path, default=Path("benchmark/manifest.json"))
    _backend_args(ev, fixtures_help="replay: a directory of <case id>.json fixtures")
    ev.add_argument("--limit", type=int, help="at most N bug and N clean cases")
    ev.add_argument("--cases", help="comma-separated case ids (overrides --limit)")
    ev.add_argument("--concurrency", type=int, default=3)
    ev.add_argument("--no-verify", action="store_true", help="report every hypothesis untested")
    ev.add_argument(
        "--no-fix-check", action="store_true", help="skip running hidden tests on fixes"
    )
    ev.add_argument("--max-candidates", type=int, default=5)
    ev.add_argument("--max-attempts", type=int, default=3)
    ev.add_argument("--record", type=Path, help="cache-through directory of per-case fixtures")
    ev.add_argument("--out", type=Path, default=Path("benchmark/results"))

    rep = commands.add_parser("eval-report", help="combine eval runs into one markdown report")
    rep.add_argument("runs", type=Path, nargs="+")
    rep.add_argument("--out", type=Path, help="write here instead of stdout")

    srv = commands.add_parser("serve", help="GitHub App webhook service")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    commands.add_parser("worker", help="GitHub App worker: review queued pull requests")

    for sub in (cmd, ev, rep, srv):
        sub.add_argument("-v", "--verbose", action="store_true")
    return parser


def _backend_args(cmd: argparse.ArgumentParser, fixtures_help: str | None = None) -> None:
    cmd.add_argument("--backend", choices=BACKENDS, default="anthropic")
    cmd.add_argument("--model", help="model id for the backend (default: backend's default)")
    cmd.add_argument(
        "--fixtures", type=Path, help=fixtures_help or "recorded responses for --backend replay"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    logging.basicConfig(level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING)
    console = Console()
    try:
        if args.command == "review":
            return _review(args, console)
        if args.command == "eval":
            return _eval(args, console)
        if args.command == "eval-report":
            return _eval_report(args, console)
        from witness.app import main as app

        if args.command == "serve":
            app.serve(args.host, args.port)
        else:
            app.work()
        return 0
    except USER_ERRORS as exc:
        console.print(f"[bold red]error:[/] {exc}")
        return 2


def _review(args: argparse.Namespace, console: Console) -> int:
    tracing.init()
    backend = create_backend(args.backend, model=args.model, fixtures=args.fixtures)
    if args.record:
        backend = RecordingBackend(backend, args.record)
    options = ReviewOptions(
        max_candidates=args.max_candidates,
        max_attempts=args.max_attempts,
        propose_fixes=not args.no_fix,
        budget_tokens=args.budget_tokens,
        verify=not args.no_verify,
    )
    try:
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
    finally:
        tracing.flush()
    reporters: list[Reporter] = [TerminalReporter(console, show_unverified=args.show_unverified)]
    if args.json_out:
        reporters.append(JsonReporter(args.json_out))
    for reporter in reporters:
        reporter.report(result)
    return 0


def _eval(args: argparse.Namespace, console: Console) -> int:
    from witness.eval.manifest import Case, Manifest
    from witness.eval.report import render
    from witness.eval.runner import EvalOptions, run_eval

    tracing.init()
    manifest = Manifest.load(args.suite)

    def make_llm(case: Case) -> LLM:
        fixtures = args.fixtures / f"{case.id}.json" if args.fixtures else None
        backend = create_backend(args.backend, model=args.model, fixtures=fixtures)
        if args.record:
            args.record.mkdir(parents=True, exist_ok=True)
            backend = RecordingBackend(backend, args.record / f"{case.id}.json")
        return LLM(backend)

    options = EvalOptions(
        verify=not args.no_verify,
        concurrency=args.concurrency,
        limit=args.limit,
        case_ids=tuple(args.cases.split(",")) if args.cases else (),
        check_fixes=not args.no_fix_check,
        max_candidates=args.max_candidates,
        max_attempts=args.max_attempts,
    )
    try:
        run = run_eval(manifest, make_llm, options, suite=str(args.suite), progress=console.print)
    finally:
        tracing.flush()

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    stem = args.out / f"{stamp}-{args.backend}-{run.mode}"
    stem.with_suffix(".json").write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
    markdown = render([run])
    stem.with_suffix(".md").write_text(markdown, encoding="utf-8")
    console.print(markdown, markup=False, highlight=False, soft_wrap=True)
    console.print(f"wrote {stem}.json and {stem}.md")
    return 0


def _eval_report(args: argparse.Namespace, console: Console) -> int:
    from witness.eval.report import render
    from witness.eval.runner import EvalRun

    runs = [EvalRun.model_validate_json(path.read_text(encoding="utf-8")) for path in args.runs]
    markdown = render(runs)
    if args.out:
        args.out.write_text(markdown, encoding="utf-8")
    else:
        console.print(markdown, markup=False, highlight=False, soft_wrap=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
