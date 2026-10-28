from __future__ import annotations

import importlib.util
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from helpers import DEMO
from psycopg.conninfo import make_conninfo


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


POSTGRES_URL = os.environ.get(
    "WITNESS_TEST_DATABASE_URL", "postgresql://witness:witness@localhost:5449/witness"
)


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    """A DSN whose search_path is a fresh schema, dropped afterwards (docker compose up)."""
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(POSTGRES_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available (docker compose up -d): {exc}")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin.execute(f"CREATE SCHEMA {schema}")
    try:
        yield make_conninfo(POSTGRES_URL, options=f"-c search_path={schema}")
    finally:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.fixture(scope="session")
def rsa_private_key() -> str:
    """A throwaway App private key: githubkit signs real JWTs with it."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
