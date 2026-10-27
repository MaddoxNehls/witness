from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest
from helpers import DEMO


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_demo_repo", DEMO / "build_demo_repo.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def demo_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The demo repository, built fresh: branch ``main`` is BASE, ``float-discounts`` HEAD."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    builder = _load_builder()
    out: Path = builder.build(tmp_path_factory.mktemp("demo") / "repo")
    return out


@pytest.fixture(scope="session")
def docker_client() -> object:
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker is not available: {exc}")
    return client
