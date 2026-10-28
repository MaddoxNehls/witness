"""Build benchmark/manifest.json by mining and validating real bug-fix and clean commits.

    uv run python benchmark/build_benchmark.py [--only humanize,ufo] [--workers 4]

Needs network (to mirror the repositories into .cache/benchmark) and Docker (every
case is validated by running its ground-truth tests; see witness/eval/mine.py). Only
commit references go into the manifest; `witness eval` re-creates each PR locally.
All repositories are MIT or BSD licensed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from witness.eval.manifest import Manifest
from witness.eval.materialize import DEFAULT_CACHE
from witness.eval.mine import RepoSpec, mine_repo

HERE = Path(__file__).resolve().parent

# (slug, url, license, language, source roots, max bug cases, max clean cases)
REPOS = [
    RepoSpec("more-itertools", "https://github.com/more-itertools/more-itertools", "MIT",
             "python", ("more_itertools/",), max_bug=6, max_clean=6),
    RepoSpec("sqlparse", "https://github.com/andialbrecht/sqlparse", "BSD-3-Clause",
             "python", ("sqlparse/",), max_bug=5, max_clean=4),
    RepoSpec("humanize", "https://github.com/python-humanize/humanize", "MIT",
             "python", ("src/",), max_bug=5, max_clean=5),
    RepoSpec("marshmallow", "https://github.com/marshmallow-code/marshmallow", "MIT",
             "python", ("src/",), max_bug=5, max_clean=6),
    RepoSpec("tabulate", "https://github.com/astanin/python-tabulate", "MIT",
             "python", ("tabulate",), max_bug=3, max_clean=0),
    RepoSpec("ufo", "https://github.com/unjs/ufo", "MIT",
             "typescript", ("src/",), max_bug=4, max_clean=0),
    RepoSpec("pathe", "https://github.com/unjs/pathe", "MIT",
             "typescript", ("src/",), max_bug=2, max_clean=1),
    RepoSpec("defu", "https://github.com/unjs/defu", "MIT",
             "typescript", ("src/",), max_bug=0, max_clean=3),
    RepoSpec("std-env", "https://github.com/unjs/std-env", "MIT",
             "typescript", ("src/",), max_bug=0, max_clean=5),
]  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "manifest.json")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--only", help="comma-separated repo slugs; merges into --out")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    wanted = set(args.only.split(",")) if args.only else None
    existing = Manifest.load(args.out).cases if args.out.exists() and wanted else []
    cases = [c for c in existing if wanted and c.id.rsplit("-", 2)[0] not in wanted]
    report: dict[str, object] = {}
    for spec in REPOS:
        if wanted and spec.slug not in wanted:
            continue
        print(f"{spec.slug}: mining", flush=True)
        kept, stats = mine_repo(spec, args.cache, workers=args.workers, progress=print)
        cases += kept
        report[spec.slug] = {
            "scanned_candidates": stats.candidates,
            "kept": stats.kept,
            "rejected": stats.rejected,
        }
        print(f"{spec.slug}: kept {stats.kept}", flush=True)

    cases.sort(key=lambda c: (c.kind, c.id))
    Manifest(
        description="Real bug fixes reverted into PRs (bug) and refactor/feature commits (clean).",
        cases=cases,
    ).save(args.out)
    (args.out.with_name("mining-report.json")).write_text(json.dumps(report, indent=2) + "\n")
    bugs = sum(c.kind == "bug" for c in cases)
    print(f"wrote {args.out}: {bugs} bug + {len(cases) - bugs} clean cases")


if __name__ == "__main__":
    main()
