"""Wrap DiscordChatExporter CLI (Docker) to fetch channel messages as JSON.

Runs the `tyrrrz/discordchatexporter:stable` image in a one-shot container,
mounting the output directory so the resulting JSON lands on the host.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

IMAGE = "tyrrrz/discordchatexporter:stable"


def export_channel(
    channel_id: str,
    out_path: Path,
    token: str,
    after: str | None = None,
) -> Path:
    """Export one channel's messages as JSON to `out_path`.

    Args:
        channel_id: Discord channel snowflake.
        out_path: Absolute host path where the JSON file should be written.
        token: Discord user or bot token.
        after: Optional ISO date (YYYY-MM-DD) — only export messages after this.

    Returns:
        The path to the exported JSON file.
    """
    if shutil.which("docker") is None:
        raise RuntimeError("docker not found on PATH; install Docker Desktop or set up the .NET CLI.")
    if not token:
        raise ValueError("Discord token is empty.")

    out_path = out_path.resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{out_dir}:/out",
        IMAGE,
        "export",
        "-t", token,
        "-c", channel_id,
        "-f", "Json",
        "-o", f"/out/{out_path.name}",
    ]
    if after:
        cmd += ["--after", after]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Don't leak the token in error output.
        scrubbed = result.stderr.replace(token, "***")
        raise RuntimeError(f"DiscordChatExporter failed (exit {result.returncode}):\n{scrubbed}")

    if not out_path.exists():
        raise RuntimeError(f"Exporter reported success but {out_path} is missing.\nstdout:\n{result.stdout}")

    return out_path
