"""Render the offline demo's terminal output to docs/demo.svg for the README.

Runs the real pipeline (replay backend, real Docker sandbox), so the image always matches
what `witness review` prints. Needs Docker and the demo repo:

    uv run python fixtures/demo-repo/build_demo_repo.py
    uv run python scripts/render_demo_svg.py
"""

import contextlib
import io
import sys
from pathlib import Path

from rich.console import Console

from witness.cli import _review, build_parser

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(
            encoding="utf-8"
        )  # as in witness.cli: cp1252 can't print the spinner
    args = build_parser().parse_args(
        [
            "review",
            "--repo", str(ROOT / ".demo" / "demo-repo"),
            "--base", "main",
            "--head", "float-discounts",
            "--backend", "replay",
            "--fixtures", str(ROOT / "fixtures" / "demo-repo" / "replay.json"),
            "--show-unverified",
        ]
    )  # fmt: skip
    console = Console(record=True, width=100, force_terminal=True)
    # The live spinner's frames would be recorded into the image.
    console.status = lambda *_, **__: contextlib.nullcontext()  # type: ignore[method-assign,assignment,return-value]
    _review(args, console)
    out = ROOT / "docs" / "demo.svg"
    out.parent.mkdir(exist_ok=True)
    console.save_svg(str(out), title="witness review")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
