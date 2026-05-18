"""CLI entrypoint: `digest export | summarize | run <channel_id>`."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
import click
import yaml
from dotenv import load_dotenv

from .export import export_channel
from .parse import bucket_by_week, load_messages
from .render import (
    digest_path,
    read_checkpoint,
    render_digest,
    week_monday,
    write_checkpoint,
    write_digest,
)
from .summarize import WeekTooLargeError, summarize_week

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORTS_DIR = REPO_ROOT / "exports"
DIGESTS_DIR = REPO_ROOT / "digests"
CHANNELS_FILE = REPO_ROOT / "channels.yaml"
MAX_WORKERS = 2


def _load_channels() -> dict[str, dict]:
    """Return {channel_id: {server, channel_name, since}}."""
    if not CHANNELS_FILE.exists():
        raise click.ClickException(f"channels.yaml not found at {CHANNELS_FILE}")
    data = yaml.safe_load(CHANNELS_FILE.read_text(encoding="utf-8")) or {}
    return {str(c["channel_id"]): c for c in data.get("channels", [])}


def _channel(channel_id: str) -> dict:
    channels = _load_channels()
    if channel_id not in channels:
        raise click.ClickException(f"Channel {channel_id} not in channels.yaml")
    return channels[channel_id]


def _export_path(channel_id: str) -> Path:
    return EXPORTS_DIR / f"{channel_id}.json"


@click.group()
def cli() -> None:
    load_dotenv(REPO_ROOT / ".env")


@cli.command()
@click.argument("channel_id")
@click.option("--since", default=None, help="ISO date for first export (YYYY-MM-DD). Defaults to channels.yaml `since` or checkpoint.")
def export(channel_id: str, since: str | None) -> None:
    """Export channel messages to JSON via DiscordChatExporter."""
    cfg = _channel(channel_id)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise click.ClickException("DISCORD_TOKEN not set in .env")

    if since is None:
        checkpoint = read_checkpoint(DIGESTS_DIR, cfg["server"], cfg["channel_name"])
        if checkpoint:
            since = week_monday(checkpoint["last_week"]).isoformat()
        if since is None:
            since = str(cfg.get("since")) if cfg.get("since") else None

    out = _export_path(channel_id)
    click.echo(f"Exporting channel {channel_id} → {out}" + (f" (since {since})" if since else ""))
    export_channel(channel_id, out, token=token, after=since)
    click.echo(f"Wrote {out} ({out.stat().st_size} bytes)")


@cli.command()
@click.argument("channel_id")
@click.option("--week", default=None, help="ISO week (e.g. 2026-W20). Defaults to all weeks present.")
def summarize(channel_id: str, week: str | None) -> None:
    """Summarize already-exported messages into weekly digests."""
    cfg = _channel(channel_id)
    export_path = _export_path(channel_id)
    if not export_path.exists():
        raise click.ClickException(f"No export at {export_path}. Run `digest export {channel_id}` first.")

    messages = load_messages(export_path)
    buckets = bucket_by_week(messages)
    if not buckets:
        click.echo("No relevant messages found.")
        return

    weeks = [week] if week else sorted(buckets)
    client = anthropic.Anthropic(max_retries=8)

    def process_week(w: str) -> tuple[str, str, str | None]:
        msgs = buckets.get(w, [])
        if not msgs:
            return w, "empty", None
        try:
            summary = summarize_week(msgs, client=client)
        except WeekTooLargeError as e:
            return w, "skipped", str(e)
        markdown = render_digest(summary, cfg["channel_name"], w, len(msgs))
        out = digest_path(DIGESTS_DIR, cfg["server"], cfg["channel_name"], w)
        write_digest(markdown, out)
        return w, "ok", msgs[-1].get("id", "")

    results: dict[str, tuple[str, str | None]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_week, w): w for w in weeks}
        for w, msgs in ((w, buckets.get(w, [])) for w in weeks):
            if msgs:
                click.echo(f"  {w}: queued ({len(msgs)} messages)")
        for fut in as_completed(futures):
            w = futures[fut]
            try:
                w_done, status, payload = fut.result()
            except Exception as e:  # noqa: BLE001
                results[w] = ("failed", str(e))
                click.echo(f"    {w}: FAILED — {e}", err=True)
                continue
            results[w_done] = (status, payload)
            if status == "ok":
                click.echo(f"    {w_done}: wrote digest")
            elif status == "skipped":
                click.echo(f"    {w_done}: SKIP — {payload}", err=True)
            elif status == "empty":
                click.echo(f"    {w_done}: no messages, skipping")

    n_ok = sum(1 for s, _ in results.values() if s == "ok")
    n_skipped = sum(1 for s, _ in results.values() if s == "skipped")
    n_failed = sum(1 for s, _ in results.values() if s == "failed")
    click.echo(f"Done: {n_ok} ok, {n_skipped} skipped, {n_failed} failed")

    ok_weeks = [w for w, (s, _) in results.items() if s == "ok"]
    if ok_weeks:
        last_week = max(ok_weeks)
        last_msg_id = results[last_week][1] or ""
        write_checkpoint(DIGESTS_DIR, cfg["server"], cfg["channel_name"], last_week, last_msg_id)


@cli.command()
@click.argument("channel_id")
@click.pass_context
def run(ctx: click.Context, channel_id: str) -> None:
    """Export then summarize all new weeks for a channel."""
    ctx.invoke(export, channel_id=channel_id, since=None)
    ctx.invoke(summarize, channel_id=channel_id, week=None)


if __name__ == "__main__":
    cli()
