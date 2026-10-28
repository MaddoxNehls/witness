"""Context building: hunks, enclosing symbols, project detection, the token budget."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from witness import git
from witness.context import Section, build_context, estimate_tokens, fit_sections, split_diff
from witness.git import LineRange
from witness.project import Project, ProjectError, detect_project, python_requirements
from witness.sandbox import PytestRunner, VitestRunner, runner_for
from witness.symbols import enclosing_symbols

PY_SOURCE = """\
import math

RATE = 2


class Cart:
    def add(self, item):
        self.items.append(item)

    def total(self):
        return sum(i.price for i in self.items)


@cache
def area(r):
    return math.pi * r * r


def perimeter(r):
    return 2 * math.pi * r
"""

TS_SOURCE = """\
export function clamp(x: number, lo: number, hi: number): number {
  return Math.min(Math.max(x, lo), hi);
}

export class Account {
  balance = 0;

  deposit(amount: number): void {
    this.balance += amount;
  }
}

export const double = (x: number): number => x * 2;
"""


def names(path: str, source: str, *ranges: tuple[int, int]) -> list[str]:
    return [s.name for s in enclosing_symbols(path, source, [LineRange(*r) for r in ranges])]


class TestEnclosingSymbols:
    def test_innermost_function_wins(self) -> None:
        symbols = enclosing_symbols("cart.py", PY_SOURCE, [LineRange(11, 11)])
        assert [(s.name, s.kind, s.start_line, s.end_line) for s in symbols] == [
            ("total", "function", 10, 11)
        ]
        assert symbols[0].source.startswith("    def total(self):")

    def test_change_spanning_two_functions_returns_both(self) -> None:
        assert names("geo.py", PY_SOURCE, (16, 20)) == ["area", "perimeter"]

    def test_decorated_function_includes_decorator(self) -> None:
        [symbol] = enclosing_symbols("geo.py", PY_SOURCE, [LineRange(16, 16)])
        assert (symbol.name, symbol.start_line) == ("area", 14)

    def test_method_inside_selected_class_is_deduplicated(self) -> None:
        # Line 6 is the class header, line 8 sits inside add(): the class covers both.
        assert names("cart.py", PY_SOURCE, (6, 6), (8, 8)) == ["Cart"]

    def test_module_level_change_has_no_symbol(self) -> None:
        assert names("cart.py", PY_SOURCE, (3, 3)) == []

    def test_typescript_function_method_and_arrow(self) -> None:
        assert names("a.ts", TS_SOURCE, (2, 2)) == ["clamp"]
        assert names("a.ts", TS_SOURCE, (9, 9)) == ["deposit"]
        assert names("a.ts", TS_SOURCE, (13, 13)) == ["double"]

    def test_unknown_language_is_ignored(self) -> None:
        assert names("README.md", "# hi\n", (1, 1)) == []

    def test_render_numbers_lines(self) -> None:
        [symbol] = enclosing_symbols("cart.py", PY_SOURCE, [LineRange(11, 11)])
        assert symbol.render().splitlines()[1:] == [
            "10 |     def total(self):",
            "11 |         return sum(i.price for i in self.items)",
        ]


def test_parse_hunks_from_zero_context_diff() -> None:
    diff = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -3 +3 @@ def f():
-a
+b
@@ -10,0 +11,3 @@ def g():
+c
+d
+e
@@ -20,2 +22,0 @@
-gone
-gone
diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
"""
    assert git.parse_hunks(diff) == {
        "x.py": [LineRange(3, 3), LineRange(11, 13), LineRange(22, 22)],
    }


def test_split_diff_per_file() -> None:
    diff = "diff --git a/a.py b/a.py\n+1\ndiff --git a/b c.py b/b c.py\n+2\n"
    assert split_diff(diff) == {
        "a.py": "diff --git a/a.py b/a.py\n+1\n",
        "b c.py": "diff --git a/b c.py b/b c.py\n+2\n",
    }


class TestBudget:
    SECTIONS: ClassVar[list[Section]] = [
        Section(f"s{i}", "\n".join(f"line {i}.{n}" for n in range(40))) for i in range(4)
    ]

    def test_everything_fits(self) -> None:
        rendered, omitted = fit_sections(self.SECTIONS, budget_tokens=10_000)
        assert omitted == []
        assert all(f"## s{i}" in rendered for i in range(4))

    def test_overflowing_section_is_cut_and_later_ones_listed(self) -> None:
        one = estimate_tokens(self.SECTIONS[0].render())
        rendered, omitted = fit_sections(self.SECTIONS, budget_tokens=one + one // 2)
        assert omitted == ["s1 (truncated)", "s2", "s3"]
        assert "line 1.0" in rendered
        assert "line 1.39" not in rendered
        assert "[truncated:" in rendered
        assert rendered.rstrip().endswith("- s3")
        assert estimate_tokens(rendered) <= one + one // 2

    def test_tiny_budget_drops_rather_than_emitting_a_bare_heading(self) -> None:
        rendered, omitted = fit_sections(self.SECTIONS, budget_tokens=5)
        assert omitted == ["s0", "s1", "s2", "s3"]
        assert "## s0" not in rendered


class TestProjectDetection:
    def test_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert detect_project(tmp_path) == Project("python", "pytest", None, "detected")

    def test_typescript_with_vitest(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"devDependencies": {"vitest": "^3.2.0"}}')
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.ts").write_text("export const a = 1;\n")
        project = detect_project(tmp_path)
        assert (project.language, project.runner) == ("typescript", "vitest")
        assert isinstance(runner_for(project), VitestRunner)

    def test_jest_is_detected_but_not_yet_runnable(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"devDependencies": {"jest": "^29"}}')
        (tmp_path / "a.ts").write_text("export const a = 1;\n")
        project = detect_project(tmp_path)
        assert project.runner == "jest"
        with pytest.raises(ProjectError, match="not supported yet"):
            runner_for(project)

    def test_witness_yml_overrides_detection(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / ".witness.yml").write_text("test_command: python -m pytest -x\n")
        project = detect_project(tmp_path)
        assert project == Project("python", "pytest", "python -m pytest -x", ".witness.yml")
        runner = runner_for(project)
        assert isinstance(runner, PytestRunner)
        assert runner.test_command(["t.py"]).startswith("python -m pytest -x ")

    def test_invalid_witness_yml(self, tmp_path: Path) -> None:
        (tmp_path / ".witness.yml").write_text("runner: nose\n")
        with pytest.raises(ProjectError, match=r"invalid \.witness\.yml"):
            detect_project(tmp_path)

    def test_mismatched_language_and_runner(self, tmp_path: Path) -> None:
        (tmp_path / ".witness.yml").write_text("language: python\nrunner: vitest\n")
        with pytest.raises(ProjectError, match="does not match"):
            detect_project(tmp_path)

    def test_unknown_project(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectError, match="could not detect"):
            detect_project(tmp_path)

    def test_python_requirements_skip_options(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\ndependencies=["httpx>=0.27"]\n'
            '[dependency-groups]\ndev=["pytest>=8", {include-group="lint"}]\n'
        )
        (tmp_path / "requirements.txt").write_text("# pinned\nattrs==24.2\n-r other.txt\n-e .\n")
        assert python_requirements(tmp_path) == ["attrs==24.2", "httpx>=0.27", "pytest>=8"]


class TestBuildContext:
    @pytest.fixture
    def head_tree(self, demo_repo: Path, tmp_path: Path) -> Path:
        return git.export_tree(demo_repo, "float-discounts", tmp_path / "head")

    def test_demo_change(self, demo_repo: Path, head_tree: Path) -> None:
        project = detect_project(head_tree)
        context = build_context(demo_repo, "main", "float-discounts", head_tree, project)

        assert {(c.path, c.status) for c in context.changes} == {
            ("src/invoicing/dates.py", "M"),
            ("src/invoicing/invoice.py", "M"),
            ("src/invoicing/money.py", "M"),
        }
        assert {s.name for s in context.symbols} >= {
            "iter_billing_days",
            "apply_discount",
            "subtotal",
        }
        assert context.omitted == []
        assert "## Diff: src/invoicing/dates.py" in context.rendered
        assert "16 |     while day < end:" in context.rendered  # numbered HEAD lines
        assert context.tokens == estimate_tokens(context.rendered)

    def test_demo_change_under_a_tight_budget(self, demo_repo: Path, head_tree: Path) -> None:
        project = detect_project(head_tree)
        context = build_context(
            demo_repo, "main", "float-discounts", head_tree, project, budget_tokens=900
        )
        assert context.tokens <= 900
        assert context.omitted  # full files are the first thing to go
        assert all("Diff:" not in title for title in context.omitted[1:])
        assert "Omitted (over the context budget)" in context.rendered


def test_export_tree_contains_only_tracked_files(demo_repo: Path, tmp_path: Path) -> None:
    tree = git.export_tree(demo_repo, "main", tmp_path / "base")
    assert (tree / "src" / "invoicing" / "money.py").is_file()
    assert not (tree / ".git").exists()


def test_resolve_rejects_option_like_refs(demo_repo: Path) -> None:
    with pytest.raises(git.GitError, match="invalid ref"):
        git.resolve(demo_repo, "--output=/tmp/x")
    with pytest.raises(git.GitError, match="not a commit"):
        git.resolve(demo_repo, "no-such-branch")
