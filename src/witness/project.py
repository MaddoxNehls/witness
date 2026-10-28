"""Detect the language and test runner of a repo, with ``.witness.yml`` overrides.

Detection reads the HEAD snapshot only. ``.witness.yml`` comes from the repo under
review and is therefore untrusted, but everything it can set only changes what
runs inside the sandbox.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

Language = Literal["python", "typescript"]
RunnerName = Literal["pytest", "vitest", "jest"]

CONFIG_FILE = ".witness.yml"


class ProjectError(ValueError):
    pass


class WitnessConfig(BaseModel):
    """Schema of ``.witness.yml``. Every key is optional."""

    model_config = ConfigDict(extra="forbid")

    language: Language | None = None
    runner: RunnerName | None = None
    test_command: str | None = None


@dataclass(frozen=True)
class Project:
    language: Language
    runner: RunnerName
    test_command: str | None  # base command override; the runner appends repro path + flags
    source: str  # "detected" or ".witness.yml"


def load_config(root: Path) -> WitnessConfig:
    path = root / CONFIG_FILE
    if not path.is_file():
        return WitnessConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return WitnessConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ProjectError(f"invalid {CONFIG_FILE}: {exc}") from exc


def detect_project(root: Path) -> Project:
    config = load_config(root)
    language = config.language or _detect_language(root)
    runner = config.runner or _detect_runner(root, language)
    if (language == "python") != (runner == "pytest"):
        raise ProjectError(f"runner {runner!r} does not match language {language!r}")
    explicit = config.language or config.runner or config.test_command
    return Project(language, runner, config.test_command, CONFIG_FILE if explicit else "detected")


def _detect_language(root: Path) -> Language:
    if (root / "package.json").is_file() and any(root.glob("**/*.ts")):
        return "typescript"
    python_markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini")
    if any((root / marker).is_file() for marker in python_markers):
        return "python"
    raise ProjectError(f"could not detect a Python or TypeScript project; add {CONFIG_FILE}")


def _detect_runner(root: Path, language: Language) -> RunnerName:
    if language == "python":
        return "pytest"
    deps = package_dependencies(root)
    if "vitest" in deps:
        return "vitest"
    if "jest" in deps:
        return "jest"
    raise ProjectError("package.json declares neither vitest nor jest")


def package_dependencies(root: Path) -> dict[str, str]:
    data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies"):
        section = data.get(key) or {}
        if isinstance(section, dict):
            deps.update({str(k): str(v) for k, v in section.items()})
    return deps


def python_requirements(root: Path) -> list[str]:
    """Requirement specifiers for the sandbox image: project deps plus dev/test groups.

    The project itself is never installed; tests import it from the mounted tree so the
    same image serves both BASE and HEAD.
    """
    requirements: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        requirements += data.get("project", {}).get("dependencies", [])
        groups = data.get("dependency-groups", {})
        extras = data.get("project", {}).get("optional-dependencies", {})
        for name in ("test", "tests", "testing", "dev"):
            requirements += [r for r in groups.get(name, []) if isinstance(r, str)]
            requirements += [r for r in extras.get(name, []) if isinstance(r, str)]
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
        path = root / name
        if path.is_file():
            requirements += [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                # Options like "-r other.txt" or "-e ." could reach outside the build context.
                if line.strip() and not line.lstrip().startswith(("#", "-"))
            ]
    return sorted(set(requirements))
