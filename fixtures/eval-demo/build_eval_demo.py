"""An offline mini-benchmark: an "upstream" repo with two real fixes and one refactor.

    uv run python fixtures/eval-demo/build_eval_demo.py     # needs Docker
    uv run witness eval --suite .demo/eval-demo/manifest.json --backend replay \
        --fixtures fixtures/eval-demo/replay

History (fixed author and dates, so commit hashes and case ids are stable):

1. the invoicing package with two bugs (float rounding, inclusive end date dropped)
2. "Fix prorate dropping the last day of the billing period" + a regression test
3. "Fix half-cent rounding in apply_discount" + a regression test
4. "Refactor Invoice.subtotal to use sum()" + a test (clean)

The real miner (witness.eval.mine) turns this into 2 bug cases and 1 clean case,
validating each one in the sandbox exactly as for the public benchmark.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

from witness.eval.manifest import Manifest
from witness.eval.mine import RepoSpec, mine_repo

HERE = Path(__file__).resolve().parent
DEMO = HERE.parent / "demo-repo"
DEFAULT_OUT = HERE.parents[1] / ".demo" / "eval-demo"

FULL_MONTH_TEST = """

def test_full_month_is_charged_in_full() -> None:
    assert prorate(Decimal("31.00"), date(2026, 1, 1), date(2026, 1, 31)) == Decimal("31.00")
"""
HALF_CENT_TEST = """

def test_half_cent_rounds_half_up() -> None:
    assert apply_discount(Decimal("1.15"), 50) == Decimal("0.58")
"""
EMPTY_INVOICE_TEST = """

def test_empty_invoice_subtotal() -> None:
    assert Invoice(customer="ACME").subtotal() == Decimal("0.00")
"""


def _demo_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_demo_repo", DEMO / "build_demo_repo.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def build_upstream(out: Path) -> Path:
    demo = _demo_builder()
    repo = out / "upstream"
    if repo.exists():
        import shutil

        shutil.rmtree(repo, onexc=demo._make_writable_and_retry)
    repo.mkdir(parents=True)
    demo.git(repo, "init", "--quiet", "--initial-branch=main")
    demo.git(repo, "config", "core.autocrlf", "false")

    def commit(files: dict[str, str], message: str, day: int) -> None:
        for name, text in files.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        demo.git(repo, "add", "--all")
        demo.git(repo, "commit", "--quiet", "-m", message, date=f"2026-08-{day:02d}T12:00:00+00:00")

    demo.copy_tree(DEMO / "base", repo)
    head = DEMO / "head" / "src" / "invoicing"
    buggy_dates, buggy_money = _read(head / "dates.py"), _read(head / "money.py")
    commit(
        {"src/invoicing/dates.py": buggy_dates, "src/invoicing/money.py": buggy_money},
        "Add invoicing package",
        1,
    )
    tests = DEMO / "base" / "tests"
    commit(
        {
            "src/invoicing/dates.py": buggy_dates.replace("while day < end:", "while day <= end:"),
            "tests/test_dates.py": _read(tests / "test_dates.py") + FULL_MONTH_TEST,
        },
        "Fix prorate dropping the last day of the billing period",
        2,
    )
    commit(
        {
            "src/invoicing/money.py": buggy_money.replace(
                "    discounted = float(amount) * (1 - float(percent) / 100)\n"
                "    return Decimal(str(round(discounted, 2)))\n",
                "    factor = (Decimal(100) - Decimal(str(percent))) / Decimal(100)\n"
                "    return to_money(amount * factor)\n",
            ),
            "tests/test_money.py": _read(tests / "test_money.py") + HALF_CENT_TEST,
        },
        "Fix half-cent rounding in apply_discount",
        3,
    )
    commit(
        {
            "src/invoicing/invoice.py": _read(head / "invoice.py"),
            "tests/test_invoice.py": _read(tests / "test_invoice.py") + EMPTY_INVOICE_TEST,
        },
        "Refactor Invoice.subtotal to use sum()",
        4,
    )
    return repo


def build(out: Path = DEFAULT_OUT) -> Path:
    """Build the upstream repo and mine it; returns the manifest path."""
    os.environ.setdefault("GIT_CONFIG_GLOBAL", os.devnull)
    upstream = build_upstream(out)
    spec = RepoSpec("eval-demo", str(upstream), "MIT", "python", ("src/",), max_bug=5, max_clean=5)
    cases, _ = mine_repo(spec, out / "cache", progress=print)
    manifest = out / "manifest.json"
    Manifest(description="Offline mini-benchmark", cases=sorted(cases, key=lambda c: c.id)).save(
        manifest
    )
    subprocess.run(["git", "-C", str(upstream), "log", "--oneline"], check=True)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    manifest = build(parser.parse_args().out)
    print(f"wrote {manifest}")


if __name__ == "__main__":
    main()
