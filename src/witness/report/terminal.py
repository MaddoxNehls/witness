"""Human-readable output with rich: proven findings in full, everything else in one line."""

from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from witness.models import Finding, OutcomeKind, ReviewResult, Status

STATUS_STYLE = {
    Status.VERIFIED: "bold red",
    Status.PRE_EXISTING: "yellow",
    Status.REJECTED: "green",
    Status.UNVERIFIED: "dim",
}
LEXER = {"python": "python", "typescript": "typescript"}


class TerminalReporter:
    def __init__(self, console: Console | None = None, *, show_unverified: bool = False) -> None:
        self.console = console or Console()
        self.show_unverified = show_unverified

    def report(self, result: ReviewResult) -> None:
        c = self.console
        c.print(
            Text.assemble(
                ("Witness", "bold"),
                f"  {result.base[:8]} -> {result.head[:8]}",
                f"  |  {result.language}/{result.test_runner}",
                f"  |  {result.backend} ({result.model})",
            )
        )
        c.print(_summary(result))
        c.print()

        for finding in result.with_status(Status.VERIFIED):
            c.print(_finding_panel(finding, LEXER.get(result.language, "text")))

        discarded = [f for f in result.findings if f.status is not Status.VERIFIED]
        if not self.show_unverified:
            discarded = [f for f in discarded if f.status is not Status.UNVERIFIED]
        if discarded:
            c.print(_discarded_table(discarded))

        hidden = len(result.with_status(Status.UNVERIFIED))
        usage = result.usage
        llm = f"{usage.calls} LLM calls"
        if usage.input_tokens or usage.output_tokens:
            llm += f", {usage.input_tokens:,} in / {usage.output_tokens:,} out tokens"
        if usage.cost_usd:
            llm += f", ${usage.cost_usd:.2f}"
        footer = f"context {result.context_tokens:,} tokens  |  {llm}  |  {result.duration_s:.1f}s"
        if hidden and not self.show_unverified:
            footer += f"  |  {hidden} unverified hidden (--show-unverified)"
        if result.context_omissions:
            footer += f"  |  {len(result.context_omissions)} context sections omitted"
        c.print(Text(footer, style="dim"))


def _summary(result: ReviewResult) -> Text:
    text = Text()
    for status, label in (
        (Status.VERIFIED, "verified"),
        (Status.PRE_EXISTING, "pre-existing"),
        (Status.REJECTED, "rejected"),
        (Status.UNVERIFIED, "unverified"),
    ):
        count = len(result.with_status(status))
        text.append(f"{count} {label}", style=STATUS_STYLE[status] if count else "dim")
        text.append("   ")
    candidates = len(result.findings)
    text.append(f"(from {candidates} hypothes{'is' if candidates == 1 else 'es'})", style="dim")
    return text


def _finding_panel(finding: Finding, lexer: str) -> Panel:
    candidate, attempt = finding.candidate, finding.evidence
    assert attempt is not None
    parts: list[RenderableType] = [
        Text(candidate.claim),
        Text.assemble(("Trigger: ", "bold"), candidate.trigger),
        Text.assemble(("Evidence: ", "bold"), finding.reason),
        Text.assemble(("Fails on HEAD with: ", "bold"), attempt.head.summary),
        Text.assemble(("Attempts: ", "bold"), _attempt_trail(finding)),
        Text("Repro test:", style="bold"),
        Syntax(attempt.test_code.rstrip(), lexer, theme="ansi_dark", word_wrap=True),
    ]
    if finding.fix is not None:
        fix = finding.fix
        label, style = (
            ("verified: the repro passes with this patch", "green")
            if fix.verified
            else ("not verified", "yellow")
        )
        parts.append(Text.assemble(("Suggested fix ", "bold"), (f"({label})", style)))
        parts.append(Text(fix.explanation))
        for edit in fix.edits:
            diff = "\n".join(
                [f"--- {edit.file}", f"+++ {edit.file}"]
                + [f"-{line}" for line in edit.find.splitlines()]
                + [f"+{line}" for line in edit.replace.splitlines()]
            )
            parts.append(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True))
    title = (
        f"[bold red]{candidate.id} VERIFIED[/]  {candidate.severity.value}  "
        f"{candidate.file}:{candidate.line_start}-{candidate.line_end}"
    )
    return Panel(Group(*parts), title=title, title_align="left", border_style="red")


def _attempt_trail(finding: Finding) -> str:
    """e.g. "1 test_error (ModuleNotFoundError) -> 2 assertion" """
    steps = []
    for attempt in finding.attempts:
        step = f"{attempt.number} {attempt.head.kind.value}"
        if attempt.head.kind is OutcomeKind.TEST_ERROR and attempt.head.exception_type:
            step += f" ({attempt.head.exception_type})"
        steps.append(step)
    return " -> ".join(steps)


def _discarded_table(findings: list[Finding]) -> Table:
    table = Table(
        title="Not reported: hypotheses that failed verification",
        title_justify="left",
        title_style="bold",
        expand=True,
    )
    table.add_column("id", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("claim", ratio=1)
    table.add_column("why", ratio=1)
    for finding in findings:
        table.add_row(
            finding.candidate.id,
            Text(finding.status.value, style=STATUS_STYLE[finding.status]),
            finding.candidate.claim,
            finding.reason,
        )
    return table
