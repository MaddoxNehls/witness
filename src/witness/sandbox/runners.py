"""Per-framework knowledge: where the repro file goes, how to run it, how to build deps.

A runner never executes anything itself; it only describes the image and the shell
command that the Docker sandbox runs.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from witness.project import Project, ProjectError, python_requirements

JUNIT_PATH = "/tmp/witness-junit.xml"  # inside the container, on a tmpfs

# hatch-vcs and setuptools-scm generate a version module at build time; the project is
# never built here, so write a stub wherever pyproject.toml says that module lives.
STUB_VERSION_FILES = """\
python - <<'PY'
import pathlib, tomllib
p = pathlib.Path("pyproject.toml")
tool = tomllib.loads(p.read_text()).get("tool", {}) if p.exists() else {}
scm = tool.get("setuptools_scm", {})
vcs = tool.get("hatch", {}).get("build", {}).get("hooks", {}).get("vcs", {})
for name in filter(None, (vcs.get("version-file"), scm.get("version_file"), scm.get("write_to"))):
    path = pathlib.Path(name)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('__version__ = version = "0.0.0"\\n'
                        '__version_tuple__ = version_tuple = (0, 0, 0)\\n')
PY"""
SANDBOX_UID = 10001  # no passwd entry needed; HOME points at the tmpfs


class Runner(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def repro_path(self) -> str:
        """Repo-relative path the repro file is written to."""
        ...

    def build_context(self, head_tree: Path) -> dict[str, bytes]:
        """Files for the image build: a Dockerfile plus dependency manifests only."""
        ...

    def setup_command(self) -> str: ...

    def test_command(self, targets: Sequence[str]) -> str:
        """Shell command running the given repo-relative test files, writing JUnit XML."""
        ...


@dataclass(frozen=True)
class PytestRunner:
    base_command: str = "python -m pytest"
    name: str = "pytest"
    repro_path: str = "__witness__/test_witness_repro.py"
    python_image: str = "python:3.13-slim"

    def build_context(self, head_tree: Path) -> dict[str, bytes]:
        requirements = "\n".join([*python_requirements(head_tree), "pytest>=8"]) + "\n"
        dockerfile = f"""\
FROM {self.python_image}
COPY requirements.txt /deps/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check --prefer-binary \\
        -r /deps/requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/work/src:/work HOME=/tmp NO_COLOR=1
"""
        return {"Dockerfile": dockerfile.encode(), "requirements.txt": requirements.encode()}

    def setup_command(self) -> str:
        return STUB_VERSION_FILES

    def test_command(self, targets: Sequence[str]) -> str:
        flags = f"-p no:cacheprovider -q --color=no --tb=long --junitxml={JUNIT_PATH}"
        return f"{self.base_command} {flags} {_quote(targets)}"


@dataclass(frozen=True)
class VitestRunner:
    base_command: str = "node_modules/.bin/vitest run"
    name: str = "vitest"
    repro_path: str = "__witness__/repro.test.ts"
    node_image: str = "node:24-slim"

    def build_context(self, head_tree: Path) -> dict[str, bytes]:
        files = {"package.json": (head_tree / "package.json").read_bytes()}
        lock = head_tree / "package-lock.json"
        if lock.is_file():
            files["package-lock.json"] = lock.read_bytes()
        if "vitest" not in json.loads(files["package.json"]).get("devDependencies", {}):
            raise ProjectError("vitest must be listed in devDependencies")
        # --ignore-scripts: dependency install hooks never run, even at build time.
        dockerfile = f"""\
FROM {self.node_image}
WORKDIR /deps
COPY package*.json /deps/
RUN if [ -f package-lock.json ]; then npm ci --ignore-scripts --no-audit --no-fund; \\
    else npm install --ignore-scripts --no-audit --no-fund; fi
ENV HOME=/tmp CI=1 NO_COLOR=1
"""
        return {"Dockerfile": dockerfile.encode(), **files}

    def setup_command(self) -> str:
        return "[ -e node_modules ] || ln -s /deps/node_modules node_modules"

    def test_command(self, targets: Sequence[str]) -> str:
        flags = f"--reporter=default --reporter=junit --outputFile.junit={JUNIT_PATH}"
        return f"{self.base_command} {_quote(targets)} {flags}"


def _quote(targets: Sequence[str]) -> str:
    return " ".join(shlex.quote(target) for target in targets)


def runner_for(project: Project) -> Runner:
    if project.runner == "pytest":
        return PytestRunner(base_command=project.test_command or PytestRunner.base_command)
    if project.runner == "vitest":
        return VitestRunner(base_command=project.test_command or VitestRunner.base_command)
    raise ProjectError(f"the {project.runner} runner is not supported yet (pytest, vitest)")
