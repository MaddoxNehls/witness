"""Run a repro test against a source tree inside a locked-down Docker container.

Isolation, per run:

* ``--network none``; CPU, memory (no extra swap) and PID limits; a wall-clock
  timeout after which the container is killed.
* The source tree is bind-mounted read-only at /repo and copied into a size-capped
  tmpfs at /work, so tests can write freely without touching the host. The repro file
  is a separate read-only mount. Nothing on the host is writable from inside.
* Read-only root filesystem, all capabilities dropped, no-new-privileges, and an
  unprivileged uid.
* Output is capped in the container and again on the host.

Dependencies are installed once per (runner, dependency manifests) into a cached
image. The build context holds only the manifests, never repo code, so no repo code
runs while the network is available.
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
import docker.errors
import requests

from witness import tracing
from witness.classify import ExecResult
from witness.sandbox.runners import JUNIT_PATH, SANDBOX_UID, Runner

IMAGE_REPO = "witness-sandbox"
LABEL = "dev.witness.sandbox"


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 120.0
    cpus: float = 1.0
    memory: str = "1g"
    pids: int = 256
    work_size: str = "512m"
    output_bytes: int = 200_000


class SandboxError(RuntimeError):
    pass


class DockerSandbox:
    """Runs one repro file against a source tree. Implements ``prove.Sandbox``."""

    def __init__(
        self,
        runner: Runner,
        image: str,
        *,
        client: docker.DockerClient | None = None,
        limits: Limits | None = None,
    ) -> None:
        self.runner = runner
        self.image = image
        self._client = client or connect()
        self._limits = limits or Limits()

    @classmethod
    def create(
        cls,
        runner: Runner,
        head_tree: Path,
        *,
        client: docker.DockerClient | None = None,
        limits: Limits | None = None,
    ) -> DockerSandbox:
        client = client or connect()
        image = ensure_image(client, runner, head_tree)
        return cls(runner, image, client=client, limits=limits)

    @property
    def repro_path(self) -> str:
        return self.runner.repro_path

    def run(self, tree: Path, test_code: str, *, label: str = "run") -> ExecResult:
        with (
            tracing.span(
                f"sandbox exec {label}",
                kind="tool",
                input={"tree": label, "test_code": test_code},
                attributes={"gen_ai.tool.name": "sandbox", "witness.image": self.image},
            ) as span,
            tempfile.TemporaryDirectory(prefix="witness-test-") as test_dir,
        ):
            (Path(test_dir) / "repro").write_text(test_code, encoding="utf-8")
            result = self._run(tree, Path(test_dir))
            span.set_attributes(
                {"witness.exit_code": result.exit_code, "witness.timed_out": result.timed_out}
            )
            span.set_output(result.output[-2000:])
            return result

    def _run(self, tree: Path, test_dir: Path) -> ExecResult:
        nonce = secrets.token_hex(8)
        started = time.monotonic()
        try:
            container = self._client.containers.create(
                self.image,
                command=["sh", "-c", self._script(nonce)],
                user=f"{SANDBOX_UID}:{SANDBOX_UID}",
                working_dir="/work",
                network_mode="none",
                nano_cpus=int(self._limits.cpus * 1e9),
                mem_limit=self._limits.memory,
                memswap_limit=self._limits.memory,
                pids_limit=self._limits.pids,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                tmpfs={
                    "/work": f"rw,noexec,nosuid,nodev,size={self._limits.work_size},mode=1777",
                    "/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777",
                },
                volumes={
                    str(tree.resolve()): {"bind": "/repo", "mode": "ro"},
                    str(test_dir.resolve()): {"bind": "/witness", "mode": "ro"},
                },
                labels={LABEL: "run"},
            )
        except docker.errors.DockerException as exc:
            return ExecResult(None, "", None, error=f"could not create container: {exc}")

        timed_out = False
        try:
            container.start()
            try:
                status = container.wait(timeout=self._limits.timeout_s)
                exit_code: int | None = int(status.get("StatusCode", -1))
            except requests.exceptions.RequestException:  # docker-py surfaces timeouts this way
                timed_out, exit_code = True, None
                container.kill()
            raw = container.logs(stdout=True, stderr=True)[: self._limits.output_bytes * 2]
        except docker.errors.DockerException as exc:
            return ExecResult(None, "", None, error=f"sandbox failed: {exc}")
        finally:
            container.remove(force=True)

        output, junit = _split_output(raw.decode("utf-8", errors="replace"), nonce)
        return ExecResult(
            exit_code=exit_code,
            output=output,
            junit_xml=junit,
            timed_out=timed_out,
            duration_s=round(time.monotonic() - started, 2),
        )

    def _script(self, nonce: str) -> str:
        repro = self.runner.repro_path
        return "\n".join(
            [
                "set -u",
                "cp -R /repo/. /work/",
                f"mkdir -p \"$(dirname '/work/{repro}')\"",
                f"cp /witness/repro '/work/{repro}'",
                "cd /work",
                self.runner.setup_command(),
                f"{{ {self.runner.test_command()}; echo $? > /tmp/witness-rc; }} 2>&1"
                f" | head -c {self._limits.output_bytes}",
                # A per-run random marker separates runner output from the report, so
                # test output can't collide with the boundary by accident.
                f"echo; echo '{nonce}'",
                f"cat {JUNIT_PATH} 2>/dev/null",
                'exit "$(cat /tmp/witness-rc 2>/dev/null || echo 125)"',
            ]
        )


def connect() -> docker.DockerClient:
    try:
        return docker.from_env()
    except docker.errors.DockerException as exc:
        raise SandboxError(f"cannot reach the Docker daemon: {exc}") from exc


def _split_output(logs: str, nonce: str) -> tuple[str, str | None]:
    output, marker, report = logs.rpartition(f"\n{nonce}\n")
    if not marker:
        return logs, None
    return output.rstrip("\n"), report.strip() or None


def image_tag(runner: Runner, context: dict[str, bytes]) -> str:
    digest = hashlib.sha256(runner.name.encode())
    for name in sorted(context):
        digest.update(b"\0" + name.encode() + b"\0" + context[name])
    return f"{IMAGE_REPO}:{runner.name}-{digest.hexdigest()[:16]}"


def ensure_image(client: docker.DockerClient, runner: Runner, head_tree: Path) -> str:
    """Return a tag for an image with the project's dependencies, building it if needed.

    The tag hashes the Dockerfile and every dependency manifest/lockfile, so a change
    to any of them builds a fresh image and repeat runs reuse the cached one.
    """
    context = runner.build_context(head_tree)
    tag = image_tag(runner, context)
    try:
        client.images.get(tag)
        return tag
    except docker.errors.ImageNotFound:
        pass
    with tracing.span("sandbox image build", kind="tool", attributes={"witness.image": tag}):
        build_dir = Path(tempfile.mkdtemp(prefix="witness-build-"))
        try:
            for name, data in context.items():
                (build_dir / name).write_bytes(data)
            client.images.build(
                path=str(build_dir), tag=tag, rm=True, forcerm=True, labels={LABEL: "image"}
            )
        except docker.errors.BuildError as exc:
            log = "".join(str(chunk.get("stream", "")) for chunk in _iter(exc.build_log))
            raise SandboxError(f"dependency image build failed: {exc}\n{log[-2000:]}") from exc
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)
    return tag


def _iter(log: Any) -> list[dict[str, Any]]:
    return [chunk for chunk in log if isinstance(chunk, dict)]
