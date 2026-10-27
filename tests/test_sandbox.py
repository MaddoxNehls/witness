"""The Docker sandbox against a real daemon: isolation properties and both runners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from witness.classify import classify_run
from witness.models import OutcomeKind
from witness.sandbox import DockerSandbox, Limits, PytestRunner, VitestRunner
from witness.sandbox.docker_sandbox import ensure_image

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def python_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pyproj")
    (root / "pyproject.toml").write_text('[project]\nname = "calc"\ndependencies = []\n')
    (root / "src" / "calc").mkdir(parents=True)
    (root / "src" / "calc" / "__init__.py").write_text("def add(a, b):\n    return a - b\n")
    return root


@pytest.fixture(scope="module")
def python_sandbox(docker_client: Any, python_tree: Path) -> DockerSandbox:
    return DockerSandbox.create(
        PytestRunner(), python_tree, client=docker_client, limits=Limits(timeout_s=60)
    )


def run(sandbox: DockerSandbox, tree: Path, code: str) -> Any:
    return classify_run(sandbox.run(tree, code), sandbox.repro_path)


def test_failing_and_passing_tests(python_sandbox: DockerSandbox, python_tree: Path) -> None:
    failing = "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    passing = "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 0\n"
    outcome = run(python_sandbox, python_tree, failing)
    assert outcome.kind is OutcomeKind.ASSERTION
    assert "assert 0 == 4" in outcome.summary
    assert run(python_sandbox, python_tree, passing).kind is OutcomeKind.PASSED


def test_isolation(python_sandbox: DockerSandbox, python_tree: Path) -> None:
    probe = """\
import os, socket
from pathlib import Path

def test_isolation():
    assert os.getuid() == 10001                      # unprivileged
    Path("scratch.txt").write_text("ok")             # the /work copy is writable
    for target in ("/repo/pwned.txt", "/etc/pwned"):  # the mount and root fs are not
        try:
            Path(target).write_text("x")
        except OSError:
            pass
        else:
            raise AssertionError(f"wrote {target}")
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
    except OSError:
        pass
    else:
        raise AssertionError("network is reachable")
"""
    outcome = run(python_sandbox, python_tree, probe)
    assert outcome.kind is OutcomeKind.PASSED, outcome.details or outcome.output
    assert not (python_tree / "pwned.txt").exists()
    assert not (python_tree / "scratch.txt").exists()


def test_timeout_kills_the_container(docker_client: Any, python_tree: Path) -> None:
    sandbox = DockerSandbox.create(
        PytestRunner(), python_tree, client=docker_client, limits=Limits(timeout_s=4)
    )
    result = sandbox.run(python_tree, "import time\n\ndef test_hang():\n    time.sleep(60)\n")
    assert result.timed_out
    assert result.duration_s < 30
    assert classify_run(result, sandbox.repro_path).kind is OutcomeKind.TIMEOUT


def test_dependency_image_is_cached(
    docker_client: Any, python_sandbox: DockerSandbox, python_tree: Path
) -> None:
    assert ensure_image(docker_client, PytestRunner(), python_tree) == python_sandbox.image


def test_vitest_runner(docker_client: Any, tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "ts-demo", "type": "module", "devDependencies": {"vitest": "3.2.4"}})
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "range.ts").write_text(
        "export function span(lo: number, hi: number): number[] {\n"
        "  const out: number[] = [];\n"
        "  for (let i = lo; i < hi; i++) out.push(i);\n"
        "  return out;\n"
        "}\n"
    )
    sandbox = DockerSandbox.create(
        VitestRunner(), tmp_path, client=docker_client, limits=Limits(timeout_s=90)
    )
    test = (
        "import { expect, test } from 'vitest';\n"
        "import { span } from '../src/range';\n\n"
        "test('span is inclusive', () => {\n"
        "  expect(span(1, 3)).toEqual([1, 2, 3]);\n"
        "});\n"
    )
    outcome = run(sandbox, tmp_path, test)
    assert outcome.kind is OutcomeKind.ASSERTION, outcome.output
    broken = test.replace("../src/range", "../src/missing")
    assert run(sandbox, tmp_path, broken).kind is OutcomeKind.TEST_ERROR
