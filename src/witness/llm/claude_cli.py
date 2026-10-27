"""Claude through the local ``claude -p`` CLI (dev only: it uses the Claude subscription).

The CLI has no structured-output hook, so the schema goes into the prompt and the
reply is validated like any other backend's. All prompt text travels over stdin.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from witness.llm.base import BackendError, Completion, CompletionRequest

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_SAFE_MODEL = re.compile(r"[A-Za-z0-9._\-\[\]]+")

PROTOCOL = """\
Reply with exactly one JSON object and nothing else. It must validate against this
JSON schema:
{schema}
Do not use any tools."""


class ClaudeCliBackend:
    name = "claude-cli"

    def __init__(
        self,
        model: str | None = None,
        *,
        executable: str = "claude",
        runner: Runner = subprocess.run,
        timeout: float = 600.0,
    ) -> None:
        if model is not None and not _SAFE_MODEL.fullmatch(model):
            raise ValueError(f"invalid model name {model!r}")
        self.model = model or "claude-cli-default"
        self._model_flag = model
        self._executable = executable
        self._runner = runner
        self._timeout = timeout

    def complete(self, request: CompletionRequest) -> Completion:
        payload = self._invoke(render_prompt(request))
        usage = payload.get("usage") or {}
        models = list((payload.get("modelUsage") or {}).keys())
        cost = payload.get("total_cost_usd")
        input_keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        return Completion(
            text=str(payload.get("result", "")),
            model=models[0] if models else self.model,
            input_tokens=sum(int(usage.get(key) or 0) for key in input_keys),
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        )

    def _invoke(self, prompt: str) -> dict[str, Any]:
        executable = shutil.which(self._executable)  # resolves claude.cmd on Windows
        if executable is None:
            raise BackendError(f"{self._executable!r} not found on PATH")
        # Only fixed flags go on the command line: on Windows `claude` is a .cmd shim,
        # so arguments pass through cmd.exe parsing.
        command = [executable, "-p", "--output-format", "json", "--tools", ""]
        command += ["--no-session-persistence"]
        if self._model_flag:
            command += ["--model", self._model_flag]
        proc = self._runner(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=self._timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise BackendError(f"claude exited with {proc.returncode}: {proc.stderr.strip()[:500]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError("claude returned non-JSON output") from exc
        if not isinstance(payload, dict):
            raise BackendError("claude returned an unexpected JSON payload")
        if payload.get("is_error"):
            raise BackendError(f"claude reported an error: {payload.get('result')}")
        return payload


def render_prompt(request: CompletionRequest) -> str:
    lines = [request.system, "", PROTOCOL.format(schema=json.dumps(request.schema)), ""]
    for message in request.messages:
        lines += [f"{message.role.upper()}:", message.content, ""]
    lines.append("Respond with the JSON object now.")
    return "\n".join(lines)
