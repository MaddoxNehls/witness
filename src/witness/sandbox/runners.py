"""Per-framework knowledge: where the repro file goes, how to run it, how to build deps.

A runner never executes anything itself; it only describes the image and the shell
command that the Docker sandbox runs.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from witness.project import Project, ProjectError, python_requirements

JUNIT_PATH = "/tmp/witness-junit.xml"  # inside the container, on a tmpfs
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

    def test_command(self) -> str: ...


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
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/work/src:/work HOME=/tmp
"""
        return {"Dockerfile": dockerfile.encode(), "requirements.txt": requirements.encode()}

    def setup_command(self) -> str:
        return "true"

    def test_command(self) -> str:
        flags = f"-p no:cacheprovider -q --tb=long --junitxml={JUNIT_PATH}"
        return f"{self.base_command} {flags} {shlex.quote(self.repro_path)}"


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
ENV HOME=/tmp CI=1
"""
        return {"Dockerfile": dockerfile.encode(), **files}

    def setup_command(self) -> str:
        return "[ -e node_modules ] || ln -s /deps/node_modules node_modules"

    def test_command(self) -> str:
        flags = f"--reporter=default --reporter=junit --outputFile.junit={JUNIT_PATH}"
        return f"{self.base_command} {shlex.quote(self.repro_path)} {flags}"


def runner_for(project: Project) -> Runner:
    if project.runner == "pytest":
        return PytestRunner(base_command=project.test_command or PytestRunner.base_command)
    if project.runner == "vitest":
        return VitestRunner(base_command=project.test_command or VitestRunner.base_command)
    raise ProjectError(f"the {project.runner} runner is not supported yet (pytest, vitest)")
