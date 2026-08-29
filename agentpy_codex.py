"""Local, unrestricted Codex execution used by the Agentpy notebook."""

from __future__ import annotations

from pathlib import Path
import subprocess


def run_codex(prompt: str, *, workdir: Path, model: str | None = None) -> str:
    """Run local Codex and return its final text response."""
    command = ["codex", "exec"]
    if model:
        command.extend(["--model", model])
    command.extend(
        [
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(workdir),
            "--color",
            "never",
            prompt,
        ]
    )

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"codex exec failed (exit {completed.returncode}):\n{completed.stderr}"
        )
    return completed.stdout.strip()
