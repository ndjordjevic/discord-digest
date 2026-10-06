"""CLI entrypoint: `digest export | summarize | run <channel_id>`."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import anthropic
import click
import yaml
from dotenv import load_dotenv

from .assets import fetch_images, index_attachments
from .export import export_channel
from .parse import bucket_by_week, is_image, is_relevant, lead_in_context, load_export
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
# Export this many days before the checkpoint week so the summarizer can see
# a conversation that was already running when the week started.
CONTEXT_LOOKBACK_DAYS = 7


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


def _attach_orphan_files(summary, week_atts: dict[str, dict]) -> None:
    """Give every downloadable file the model didn't pick to the topic whose anchor
    message is closest in time (snowflake IDs are time-ordered), so no release is lost."""
    topics = [t for t in summary.top_topics if t.anchor_msg_ids]
    if not topics:
        return
    picked = {a for t in summary.top_topics for a in t.attachment_ids}
    for att_id, att in week_atts.items():
        if att_id in picked or is_image(att["file_name"]):
            continue
        msg = int(att["msg_id"])
        nearest = min(topics, key=lambda t: min(abs(int(i) - msg) for i in t.anchor_msg_ids))
        nearest.attachment_ids.append(att_id)


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
            since = (week_monday(checkpoint["last_week"]) - timedelta(days=CONTEXT_LOOKBACK_DAYS)).isoformat()
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

    meta, messages = load_export(export_path)
    relevant = sorted((m for m in messages if is_relevant(m) and m.get("timestamp")), key=lambda m: m["timestamp"])
    buckets = bucket_by_week(relevant)
    attachments = index_attachments(relevant)
    if not buckets:
        click.echo("No relevant messages found.")
        return

    if week:
        weeks = [week]
    else:
        # The export reaches back before the checkpoint week for lead-in context;
        # those earlier weeks are already digested and must not be redone.
        checkpoint = read_checkpoint(DIGESTS_DIR, cfg["server"], cfg["channel_name"])
        weeks = [w for w in sorted(buckets) if not checkpoint or w >= checkpoint["last_week"]]
    client = anthropic.Anthropic(max_retries=8)

    def process_week(w: str) -> tuple[str, str, str | None]:
        msgs = buckets.get(w, [])
        if not msgs:
            return w, "empty", None
        context = lead_in_context(relevant, w)
        try:
            summary = summarize_week(msgs, context=context, client=client)
        except WeekTooLargeError as e:
            return w, "skipped", str(e)
        out = digest_path(DIGESTS_DIR, cfg["server"], cfg["channel_name"], w)
        # Only attachments from this week's messages may be shown.
        week_ids = {m["id"] for m in msgs}
        week_atts = {k: dict(v) for k, v in attachments.items() if v["msg_id"] in week_ids}
        for t in summary.top_topics:
            # Drop IDs the model invented or took from lead-in context.
            t.anchor_msg_ids = [i for i in t.anchor_msg_ids if i in week_ids]
            t.attachment_ids = [i for i in t.attachment_ids if i in week_atts]
            fetch_images(t.attachment_ids, week_atts, out.parent, w)
        _attach_orphan_files(summary, week_atts)
        markdown = render_digest(
            summary, cfg["channel_name"], w, len(msgs),
            guild_id=meta["guild_id"], channel_id=meta["channel_id"], attachments=week_atts,
        )
        write_digest(markdown, out)
        # A re-run may pick different images; drop the ones no longer referenced.
        week_assets = out.parent / "assets" / w
        if week_assets.is_dir():
            for f in week_assets.iterdir():
                if f"assets/{w}/{f.name}" not in markdown:
                    f.unlink()
        return w, "ok", msgs[-1].get("id", "")

    results: dict[str, tuple[str, str | None]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_week, w): w for w in weeks}
        for w, msgs in ((w, buckets.get(w, [])) for w in weeks):
            if msgs:
                n_ctx = len(lead_in_context(relevant, w))
                click.echo(f"  {w}: queued ({len(msgs)} messages" + (f", +{n_ctx} lead-in" if n_ctx else "") + ")")
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
    previous = read_checkpoint(DIGESTS_DIR, cfg["server"], cfg["channel_name"])
    # Re-doing an older week (e.g. `--week`) must not move the checkpoint backwards.
    if ok_weeks and (not previous or max(ok_weeks) >= previous["last_week"]):
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
