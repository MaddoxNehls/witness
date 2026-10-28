"""Turn a ``ReviewResult`` into GitHub payloads: the check run output and the PR review.

Pure functions, so the exact payload is easy to test. Only VERIFIED findings become
inline comments. GitHub rejects review comments on lines outside the diff, so each
comment is anchored to lines the PR diff actually shows; a finding with nothing to
anchor to is listed in the review body instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from witness.git import LineRange
from witness.models import Finding, ReviewResult, Status, SuggestedFix

MARKER = "<!-- witness:review -->"
LANGUAGE_FENCE = {"python": "python", "typescript": "ts"}
MAX_OUTPUT_CHARS = 3000

ReadFile = Callable[[str], str | None]


@dataclass(frozen=True)
class CheckOutput:
    conclusion: str
    title: str
    summary: str
    text: str


def check_run_output(result: ReviewResult) -> CheckOutput:
    """Verified bugs fail the check (it only blocks merges if the repo makes it required)."""
    verified = result.with_status(Status.VERIFIED)
    counts = {status: len(result.with_status(status)) for status in Status}
    table = "\n".join(
        [
            "| Result | Count |",
            "| --- | ---: |",
            f"| Verified bugs (reported) | {counts[Status.VERIFIED]} |",
            f"| Pre-existing bugs | {counts[Status.PRE_EXISTING]} |",
            f"| Rejected hypotheses (repro passed) | {counts[Status.REJECTED]} |",
            f"| Unverified hypotheses | {counts[Status.UNVERIFIED]} |",
            f"| **Hypotheses tested** | **{len(result.findings)}** |",
        ]
    )
    summary = (
        f"{table}\n\nWitness reports only bugs it reproduced with a failing test in a sandbox. "
        "Hypotheses it could not reproduce are not posted."
    )
    lines = [
        f"- **{f.candidate.id}** `{f.candidate.file}:{f.candidate.line_start}`: {f.candidate.claim}"
        for f in verified
    ]
    others = [
        f"- {f.candidate.id} ({f.status.value}): {f.candidate.claim} *{f.reason}*"
        for f in result.findings
        if f.status is not Status.VERIFIED
    ]
    text = ""
    if lines:
        text += "### Verified\n" + "\n".join(lines) + "\n\n"
    if others:
        text += "### Not reported\n" + "\n".join(others) + "\n"
    if verified:
        noun = "bug" if len(verified) == 1 else "bugs"
        return CheckOutput("failure", f"{len(verified)} verified {noun}", summary, text)
    return CheckOutput("success", "No verified bugs", summary, text)


def review_payload(
    result: ReviewResult,
    *,
    commentable: Mapping[str, Sequence[LineRange]],
    read_file: ReadFile,
) -> dict[str, Any] | None:
    """The body of ``POST /repos/{owner}/{repo}/pulls/{n}/reviews``, or None if nothing to say.

    ``commentable`` maps each file to the HEAD line ranges visible in the PR diff.
    """
    verified = result.with_status(Status.VERIFIED)
    if not verified:
        return None
    language = LANGUAGE_FENCE.get(result.language, "")
    comments: list[dict[str, Any]] = []
    unanchored: list[str] = []
    for finding in verified:
        anchor, suggestion = _anchor(
            finding, commentable.get(finding.candidate.file, ()), read_file
        )
        body = comment_body(finding, language, suggestion)
        if anchor is None:
            unanchored.append(
                f"#### `{finding.candidate.file}:{finding.candidate.line_start}`\n\n{body}"
            )
            continue
        comment: dict[str, Any] = {
            "path": finding.candidate.file,
            "line": anchor.end,
            "side": "RIGHT",
            "body": body,
        }
        if anchor.start < anchor.end:
            comment |= {"start_line": anchor.start, "start_side": "RIGHT"}
        comments.append(comment)

    noun = "bug" if len(verified) == 1 else "bugs"
    body = (
        f"{MARKER}\n**Witness verified {len(verified)} {noun}** in `{result.head[:8]}`: each one "
        f"comes with a test that fails on this PR and passes on the base branch."
    )
    if unanchored:
        body += "\n\n### Findings outside the diff\n\n" + "\n\n".join(unanchored)
    return {"commit_id": result.head, "event": "COMMENT", "body": body, "comments": comments}


def comment_body(finding: Finding, language: str, suggestion: str | None) -> str:
    candidate, attempt = finding.candidate, finding.evidence
    assert attempt is not None
    output = (attempt.head.details or attempt.head.output)[-MAX_OUTPUT_CHARS:]
    parts = [
        f"**Witness: verified bug** ({candidate.severity.value}): {candidate.claim}",
        f"**Trigger:** {candidate.trigger}\n**Evidence:** {finding.reason}. "
        f"On this PR it fails with `{_inline(attempt.head.summary)}`.",
        _details("Reproducing test", fenced(attempt.test_code, language)),
        _details("Sandbox output", fenced(output, "text")),
    ]
    if finding.fix is not None:
        parts.append(_fix_section(finding.fix, suggestion))
    return "\n\n".join(parts)


def _fix_section(fix: SuggestedFix, suggestion: str | None) -> str:
    status = (
        "verified: the reproducing test passes with it applied" if fix.verified else "not verified"
    )
    header = f"**Suggested fix** ({status}). {fix.explanation}"
    if suggestion is not None:
        return f"{header}\n\n{fenced(suggestion, 'suggestion')}"
    diff = "\n".join(
        line
        for edit in fix.edits
        for line in (
            f"--- {edit.file}",
            f"+++ {edit.file}",
            *(f"-{x}" for x in edit.find.splitlines()),
            *(f"+{x}" for x in edit.replace.splitlines()),
        )
    )
    return f"{header}\n\n{fenced(diff, 'diff')}"


def _anchor(
    finding: Finding, visible: Sequence[LineRange], read_file: ReadFile
) -> tuple[LineRange | None, str | None]:
    """Where to put the comment, and the suggestion text if the fix can be one.

    A verified single-edit fix inside the diff becomes a GitHub suggestion anchored on
    exactly the lines it replaces. Otherwise the comment goes on the claimed lines,
    clipped to what the diff shows.
    """
    fix = finding.fix
    if fix is not None and fix.verified and len(fix.edits) == 1:
        edit = fix.edits[0]
        source = read_file(edit.file) if edit.file == finding.candidate.file else None
        if source is not None and source.count(edit.find) == 1:
            lines, replacement = _line_replacement(source, edit.find, edit.replace)
            if any(r.start <= lines.start and lines.end <= r.end for r in visible):
                return lines, replacement
    claimed = finding.candidate
    for r in visible:
        if r.overlaps(claimed.line_start, claimed.line_end):
            return LineRange(max(r.start, claimed.line_start), min(r.end, claimed.line_end)), None
    return None, None


def _line_replacement(source: str, find: str, replace: str) -> tuple[LineRange, str]:
    """The whole lines that contain ``find``, and what they become after the edit."""
    start = source.index(find)
    end = start + len(find)
    line_start = source.rfind("\n", 0, start) + 1
    if find.endswith("\n"):
        end -= 1  # the snippet's own trailing newline ends its last line
    line_end = source.find("\n", end)
    line_end = len(source) if line_end == -1 else line_end
    first = source.count("\n", 0, line_start) + 1
    last = source.count("\n", 0, line_end) + 1
    edited = source[line_start:start] + replace + source[start + len(find) : line_end]
    return LineRange(first, last), edited.removesuffix("\n")


def fenced(text: str, info: str) -> str:
    """A code fence longer than any backtick run inside ``text``."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text.rstrip()}\n{fence}"


def _details(summary: str, content: str) -> str:
    return f"<details><summary>{summary}</summary>\n\n{content}\n\n</details>"


def _inline(text: str) -> str:
    return text.replace("`", "'").splitlines()[0] if text else ""


SUPERSEDED_BODY = "{marker}\n~~Earlier Witness review.~~ Superseded by the review of `{sha}`."


def superseded_body(new_head: str) -> str:
    return SUPERSEDED_BODY.format(marker=MARKER, sha=new_head[:8])
