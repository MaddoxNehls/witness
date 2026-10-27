"""Build the demo repository: a BASE commit and a HEAD commit with two planted bugs.

    python fixtures/demo-repo/build_demo_repo.py [--out .demo/demo-repo] [--force]

``base/`` is the full BASE tree; ``head/`` holds only the files HEAD changes. HEAD:

* ``money.apply_discount`` switches to float arithmetic and ``round()``: half-cent
  amounts round the wrong way (bug).
* ``dates.iter_billing_days`` uses ``day < end`` where ``end`` is inclusive, so
  proration drops the last day of every period (bug).
* ``Invoice.subtotal`` becomes a ``sum()`` with a Decimal start value (harmless).

Author, committer and dates are fixed, so the commit hashes are the same on every
machine. BASE is branch ``main``; HEAD is branch ``float-discounts`` one commit
ahead, as in a pull request. (Tags named ``head`` would collide with ``HEAD`` on
case-insensitive filesystems.)
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parents[1] / ".demo" / "demo-repo"

BASE_BRANCH, HEAD_BRANCH = "main", "float-discounts"
COMMITS = [
    ("base", "Add invoicing package: money, proration, invoices", "2026-09-01T10:00:00+00:00"),
    (
        "head",
        "Accept float discounts, stream billing days, simplify subtotal",
        "2026-09-08T15:30:00+00:00",
    ),
]


def git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dana Developer",
        "GIT_AUTHOR_EMAIL": "dana@example.com",
        "GIT_COMMITTER_NAME": "Dana Developer",
        "GIT_COMMITTER_EMAIL": "dana@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,  # ignore the user's global config (autocrlf, hooks)
    }
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)


def copy_tree(src: Path, dest: Path) -> None:
    """Copy files with LF line endings, whatever the checkout of this repo uses."""
    for path in sorted(src.rglob("*")):
        if path.is_file():
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            target.write_bytes(text.encode("utf-8"))


def build(out: Path, *, force: bool = False) -> Path:
    if out.exists():
        if not force:
            raise SystemExit(f"{out} exists; pass --force to rebuild")
        shutil.rmtree(out, onexc=_make_writable_and_retry)
    out.mkdir(parents=True)
    git(out, "init", "--quiet", f"--initial-branch={BASE_BRANCH}")
    git(out, "config", "core.autocrlf", "false")
    for tree, message, date in COMMITS:
        if tree == "head":
            git(out, "switch", "--quiet", "--create", HEAD_BRANCH)
        copy_tree(HERE / tree, out)
        git(out, "add", "--all")
        git(out, "commit", "--quiet", "-m", message, date=date)
    return out


def _make_writable_and_retry(func: Callable[[str], object], path: str, _: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)  # git marks object files read-only, which Windows enforces
    func(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    out = build(args.out, force=args.force)
    print(f"demo repo ready at {out}: --base {BASE_BRANCH} --head {HEAD_BRANCH}")


if __name__ == "__main__":
    main()
