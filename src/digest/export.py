"""Wrap the DiscordChatExporter CLI to fetch channel messages as JSON.

Prefers the native macOS/Linux build in `tools/dce/<version>/` (no Docker Desktop
needed); falls back to the `tyrrrz/discordchatexporter` Docker image. Timestamps
are always normalized to UTC so ISO-week bucketing matches regardless of runner.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

DCE_VERSION = "2.48"
IMAGE = f"tyrrrz/discordchatexporter:{DCE_VERSION}"
TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "dce"
_RELEASE_URL = "https://github.com/Tyrrrz/DiscordChatExporter/releases/download/{v}/DiscordChatExporter.Cli.{rid}.zip"


def _rid() -> str | None:
    system, machine = platform.system(), platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("x86_64", "amd64") else None
    if not arch:
        return None
    return {"Darwin": f"osx-{arch}", "Linux": f"linux-{arch}"}.get(system)


def latest_version() -> str | None:
    """Newest DiscordChatExporter release tag on GitHub, or None if offline."""
    req = urllib.request.Request(
        "https://api.github.com/repos/Tyrrrz/DiscordChatExporter/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "discord-digest"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp).get("tag_name")
    except (OSError, ValueError):
        return None


def native_binary() -> Path:
    return TOOLS_DIR / DCE_VERSION / "DiscordChatExporter.Cli"


def install_native() -> Path:
    """Download the pinned native CLI build into tools/dce/<version>/."""
    rid = _rid()
    if rid is None:
        raise RuntimeError(f"No native DiscordChatExporter build for {platform.system()} {platform.machine()}.")
    dest = TOOLS_DIR / DCE_VERSION
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest.with_suffix(".zip")
    urllib.request.urlretrieve(_RELEASE_URL.format(v=DCE_VERSION, rid=rid), zip_path)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    zip_path.unlink()
    binary = native_binary()
    binary.chmod(0o755)
    if platform.system() == "Darwin":
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest)], capture_output=True)
    return binary


def _utc_bound(date: str) -> str:
    return f"{date}T00:00:00Z" if len(date) == 10 else date


def export_channel(
    channel_id: str,
    out_path: Path,
    token: str,
    after: str | None = None,
    before: str | None = None,
) -> Path:
    """Export one channel's messages as JSON to `out_path`.

    Args:
        channel_id: Discord channel snowflake.
        out_path: Host path where the JSON file should be written.
        token: Discord user or bot token.
        after / before: Optional ISO dates (YYYY-MM-DD) bounding the export.
    """
    if not token:
        raise ValueError("Discord token is empty.")

    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file so a failed export never clobbers the previous one.
    tmp_path = out_path.with_name(out_path.stem + ".partial.json")

    args = ["export", "-c", channel_id, "-f", "Json", "--utc", "--parallel", "1"]
    # A bare date is parsed in the exporter's local timezone; pin bounds to UTC midnight so
    # they match the UTC week bucketing (otherwise edge messages fall between weeks).
    if after:
        args += ["--after", _utc_bound(after)]
    if before:
        args += ["--before", _utc_bound(before)]

    binary = native_binary()
    env = os.environ | {"DISCORD_TOKEN": token, "FUCK_RUSSIA": "true"}
    if binary.exists():
        cmd = [str(binary), *args, "-o", str(tmp_path)]
    elif shutil.which("docker"):
        cmd = [
            "docker", "run", "--rm", "-e", "DISCORD_TOKEN", "-e", "FUCK_RUSSIA",
            "-v", f"{out_path.parent}:/out", IMAGE, *args, "-o", f"/out/{tmp_path.name}",
        ]
    else:
        raise RuntimeError("DiscordChatExporter not found; run `digest install-exporter` or install Docker.")

    # The token goes via the environment (DISCORD_TOKEN), never on the command line.
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"DiscordChatExporter failed (exit {result.returncode}):\n{result.stderr.replace(token, '***')}")
    if not tmp_path.exists():
        raise RuntimeError(f"Exporter reported success but {tmp_path} is missing.\nstdout:\n{result.stdout}")
    os.replace(tmp_path, out_path)
    return out_path
