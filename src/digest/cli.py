"""CLI entrypoint: `digest run-all | run | export | summarize | eval`."""

from __future__ import annotations

import functools
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import click
import yaml
from dotenv import load_dotenv

from . import batch as batching
from .assets import fetch_images, index_attachments
from .export import DCE_VERSION, export_channel, install_native, latest_version
from .parse import bucket_by_week, is_image, is_relevant, lead_in_context, load_export
from .render import (
    WORD_BUDGET,
    current_week,
    digest_path,
    expanded_topics,
    read_checkpoint,
    render_within_budget,
    week_monday,
    word_count,
    write_checkpoint,
    write_digest,
)
from .summarize import (
    DEFAULT_EFFORT,
    DEFAULT_INPUT_FORMAT,
    DEFAULT_MODEL,
    EFFORTS,
    INPUT_FORMATS,
    Result,
    WeekTooLargeError,
    build_request,
    check_size,
    summarize_week,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORTS_DIR = REPO_ROOT / "exports"
DIGESTS_DIR = REPO_ROOT / "digests"
LOGS_DIR = REPO_ROOT / "logs"
CHANNELS_FILE = REPO_ROOT / "channels.yaml"
BATCH_STATE = REPO_ROOT / ".batch_pending.json"
LOCK_FILE = REPO_ROOT / ".digest.lock"
MAX_WORKERS = 2
# Export this many days before the first week to digest so the summarizer can see
# a conversation that was already running when the week started.
CONTEXT_LOOKBACK_DAYS = 7


# --- channel / week bookkeeping ---------------------------------------------


@functools.cache
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


def _this_monday() -> str:
    return week_monday(current_week()).isoformat()


def _pending_weeks(cfg: dict, buckets: dict, include_current: bool) -> list[str]:
    """Weeks after the checkpoint plus earlier failures; never the still-running week."""
    cp = read_checkpoint(DIGESTS_DIR, cfg["server"], cfg["channel_name"])
    weeks = {w for w in buckets if not cp or w > cp["last_week"]}
    if cp:
        weeks |= {w for w in cp["failed_weeks"] if w in buckets}
    if not include_current:
        weeks = {w for w in weeks if w < current_week()}
    return sorted(weeks)


def _update_checkpoint(cfg: dict, ok: dict[str, str], failed: set[str]) -> None:
    """Advance to the newest digested week (never backwards) and remember failures for retry."""
    cp = read_checkpoint(DIGESTS_DIR, cfg["server"], cfg["channel_name"]) or {
        "last_week": "", "last_message_id": "", "failed_weeks": [],
    }
    # A still-running week (--include-current) must not move the checkpoint: its later messages would be skipped.
    done = {w: m for w, m in ok.items() if w < current_week()}
    if done and max(done) > cp["last_week"]:
        cp["last_week"] = max(done)
        cp["last_message_id"] = done[max(done)]
    cp["failed_weeks"] = sorted((set(cp["failed_weeks"]) - set(ok)) | failed)
    write_checkpoint(DIGESTS_DIR, cfg["server"], cfg["channel_name"], cp)


def _log_usage(record: dict) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    with (LOGS_DIR / "usage.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- one channel-week job ---------------------------------------------------


@dataclass
class WeekJob:
    channel_id: str
    cfg: dict
    meta: dict
    week: str
    msgs: list[dict]
    context: list[dict]
    attachments: dict[str, dict] = field(default_factory=dict)


def _load_jobs(channel_id: str, weeks: list[str] | None, include_current: bool) -> list[WeekJob]:
    cfg = _channel(channel_id)
    export_path = _export_path(channel_id)
    if not export_path.exists():
        raise click.ClickException(f"No export at {export_path}. Run `digest export {channel_id}` first.")
    meta, messages = load_export(export_path)
    relevant = sorted((m for m in messages if is_relevant(m) and m.get("timestamp")), key=lambda m: m["timestamp"])
    buckets = bucket_by_week(relevant)
    attachments = index_attachments(relevant)
    weeks = weeks if weeks is not None else _pending_weeks(cfg, buckets, include_current)
    jobs = []
    for w in weeks:
        msgs = buckets.get(w, [])
        if not msgs:
            continue  # empty week: no file
        ids = {m["id"] for m in msgs}
        jobs.append(WeekJob(
            channel_id, cfg, meta, w, msgs, lead_in_context(relevant, w),
            {k: dict(v) for k, v in attachments.items() if v["msg_id"] in ids},
        ))
    return jobs


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


def _prune_assets(out: Path, week: str, markdown: str) -> None:
    """A re-run may pick different images; drop the ones the digest no longer references."""
    week_assets = out.parent / "assets" / week
    if not week_assets.is_dir():
        return
    for f in week_assets.iterdir():
        if f"assets/{week}/{f.name}" not in markdown:
            f.unlink()
    if not any(week_assets.iterdir()):
        week_assets.rmdir()


def _finish(job: WeekJob, result: Result, *, out_root: Path, mode: str, effort: str, input_format: str) -> int:
    """Validate IDs, fetch images, render and write the digest; returns its word count."""
    summary = result.summary
    # Keep the raw model output so format changes can be re-rendered without new API calls.
    raw = LOGS_DIR / "summaries" / out_root.name / job.channel_id / f"{job.week}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    week_ids = {m["id"] for m in job.msgs}
    urls = {link.url for link in summary.links}
    for t in summary.top_topics:
        # Drop IDs the model invented or took from lead-in context.
        t.anchor_msg_ids = [i for i in t.anchor_msg_ids if i in week_ids]
        t.attachment_ids = [i for i in t.attachment_ids if i in job.attachments]
        t.link_urls = [u for u in t.link_urls if u in urls]
    _attach_orphan_files(summary, job.attachments)

    out = digest_path(out_root, job.cfg["server"], job.cfg["channel_name"], job.week)
    if not (summary.top_topics or summary.links):
        # Only chit-chat this week: like an empty week, no file.
        out.unlink(missing_ok=True)
        _prune_assets(out, job.week, "")
        _log_usage(_usage_record(job, result, out_root, mode, effort, input_format, words=0))
        return 0
    for t in expanded_topics(summary):
        fetch_images(t.attachment_ids, job.attachments, out.parent, job.week)
    markdown = render_within_budget(
        summary, job.cfg["channel_name"], job.week, len(job.msgs),
        guild_id=job.meta["guild_id"], channel_id=job.meta["channel_id"], attachments=job.attachments,
    )
    write_digest(markdown, out)
    _prune_assets(out, job.week, markdown)

    words = word_count(markdown)
    _log_usage(_usage_record(job, result, out_root, mode, effort, input_format, words))
    return words


def _usage_record(job: WeekJob, result: Result, out_root: Path, mode: str, effort: str, input_format: str, words: int) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel": f"{job.cfg['server']}/{job.cfg['channel_name']}", "week": job.week,
        "messages": len(job.msgs), "lead_in": len(job.context), "mode": mode,
        "model": result.model, "effort": effort, "input_format": input_format,
        "stop_reason": result.stop_reason, "words": words, "usage": result.usage,
        "out_root": str(out_root.relative_to(REPO_ROOT)) if out_root.is_relative_to(REPO_ROOT) else str(out_root),
    }


def _report(job: WeekJob, words: int) -> None:
    if not words:
        click.echo(f"    {job.week}: no substantive content, no file")
        return
    flag = "" if words <= WORD_BUDGET else f"  (over {WORD_BUDGET}-word budget)"
    click.echo(f"    {job.week}: wrote digest, {words} words{flag}")


def _finish_job(job: WeekJob, result, ok: dict, failed: set, **kw) -> None:
    """Write one job's digest and record success/failure under (channel_id, week)."""
    key = (job.channel_id, job.week)
    try:
        _report(job, _finish(job, result, **kw))
        ok[key] = job.msgs[-1]["id"]
    except Exception as e:  # noqa: BLE001
        failed.add(key)
        click.echo(f"    {job.week}: FAILED — {e}", err=True)


def _run_sync(jobs: list[WeekJob], client, *, out_root: Path, effort: str, input_format: str, model: str) -> tuple[dict, set]:
    """Summarize jobs one request each (with refusal fallback). Returns ({(cid, week): last_msg_id}, failed)."""
    ok: dict[tuple[str, str], str] = {}
    failed: set[tuple[str, str]] = set()

    def work(job: WeekJob):
        return summarize_week(job.msgs, context=job.context, client=client, model=model, effort=effort, input_format=input_format)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(work, j): j for j in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                result = fut.result()
            except Exception as e:  # noqa: BLE001
                failed.add((job.channel_id, job.week))
                click.echo(f"    {job.week}: FAILED — {e}", err=True)
                continue
            _finish_job(job, result, ok, failed, out_root=out_root, mode="sync", effort=effort, input_format=input_format)
    return ok, failed


def _run_batch(jobs: list[WeekJob], client, *, out_root: Path, effort: str, input_format: str, model: str) -> tuple[dict, set]:
    """Submit all jobs as one Message Batch (50% off); retry refusals/errors synchronously."""
    ok: dict[tuple[str, str], str] = {}
    failed: set[tuple[str, str]] = set()
    by_cid = {batching.custom_id(j.channel_id, j.week): j for j in jobs}

    requests = {}
    for cid, job in by_cid.items():
        params = build_request(job.msgs, context=job.context, model=model, effort=effort, input_format=input_format)
        try:
            check_size(client, params)
        except WeekTooLargeError as e:
            failed.add((job.channel_id, job.week))
            click.echo(f"    {job.week}: SKIP — {e}", err=True)
            continue
        requests[cid] = params
    if not requests:
        return ok, failed

    meta = {"effort": effort, "input_format": input_format, "model": model, "custom_ids": sorted(requests)}
    pending = batching.load_pending(BATCH_STATE)
    if pending and pending["meta"] == meta:
        # An earlier run was interrupted while polling: resume instead of paying twice.
        batch_id = pending["batch_id"]
        click.echo(f"  resuming batch {batch_id}")
    else:
        batch_id = batching.submit(client, requests, BATCH_STATE, meta)
        click.echo(f"  submitted batch {batch_id} ({len(requests)} requests); polling every {batching.POLL_SECONDS}s")
    batching.wait(client, batch_id, echo=click.echo)
    results = batching.collect(client, batch_id)

    retry = []
    for cid, res in results.items():
        job = by_cid.get(cid)
        if job is None:
            continue
        if isinstance(res, Exception):
            click.echo(f"    {job.week}: batch failed ({res}); retrying synchronously", err=True)
            retry.append(job)
            continue
        _finish_job(job, res, ok, failed, out_root=out_root, mode="batch", effort=effort, input_format=input_format)
    if retry:
        ok2, failed2 = _run_sync(retry, client, out_root=out_root, effort=effort, input_format=input_format, model=model)
        ok |= ok2
        failed |= failed2
    BATCH_STATE.unlink(missing_ok=True)
    return ok, failed


def _summarize_jobs(jobs: list[WeekJob], *, use_batch: bool, out_root: Path, effort: str, input_format: str, model: str) -> tuple[dict, set]:
    for j in jobs:
        click.echo(f"  {j.cfg['server']}/{j.cfg['channel_name']} {j.week}: {len(j.msgs)} messages" + (f", +{len(j.context)} lead-in" if j.context else ""))
    if not jobs:
        click.echo("  nothing to digest")
        return {}, set()
    client = anthropic.Anthropic(max_retries=8)
    run = _run_batch if use_batch and len(jobs) > 1 else _run_sync
    return run(jobs, client, out_root=out_root, effort=effort, input_format=input_format, model=model)


def _apply_checkpoints(jobs: list[WeekJob], ok: dict, failed: set) -> None:
    for cid in {j.channel_id for j in jobs}:
        cfg = _channel(cid)
        _update_checkpoint(cfg, {w: m for (c, w), m in ok.items() if c == cid}, {w for (c, w) in failed if c == cid})


# --- commands ---------------------------------------------------------------

_effort_opt = click.option("--effort", type=click.Choice(EFFORTS), default=DEFAULT_EFFORT, show_default=True)
_format_opt = click.option("--input-format", type=click.Choice(INPUT_FORMATS), default=DEFAULT_INPUT_FORMAT, show_default=True)
_model_opt = click.option("--model", default=DEFAULT_MODEL, show_default=True)


@click.group()
def cli() -> None:
    load_dotenv(REPO_ROOT / ".env")


@cli.command("install-exporter")
def install_exporter() -> None:
    """Download the pinned native DiscordChatExporter CLI into tools/dce/."""
    click.echo(f"Installed {install_native()}")
    _warn_if_exporter_outdated()


def _warn_if_exporter_outdated() -> None:
    latest = latest_version()
    if latest and latest != DCE_VERSION:
        click.echo(f"  note: DiscordChatExporter {latest} is out (pinned {DCE_VERSION}); bump DCE_VERSION in export.py and re-run install-exporter", err=True)


@cli.command()
@click.argument("channel_id")
@click.option("--since", default=None, help="ISO date (YYYY-MM-DD) to export from. Defaults to the checkpoint minus a week of lead-in.")
@click.option("--include-current", is_flag=True, help="Also export the still-running ISO week.")
def export(channel_id: str, since: str | None, include_current: bool) -> None:
    """Export channel messages to JSON via DiscordChatExporter."""
    cfg = _channel(channel_id)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise click.ClickException("DISCORD_TOKEN not set in .env")

    if since is None:
        cp = read_checkpoint(DIGESTS_DIR, cfg["server"], cfg["channel_name"])
        marks = [w for w in [cp["last_week"], *cp["failed_weeks"]] if w] if cp else []
        if marks:
            since = (week_monday(min(marks)) - timedelta(days=CONTEXT_LOOKBACK_DAYS)).isoformat()
        elif cfg.get("since"):
            since = str(cfg["since"])
    before = None if include_current else _this_monday()

    out = _export_path(channel_id)
    click.echo(f"Exporting {cfg['server']}/{cfg['channel_name']} → {out} ({since or 'start'} … {before or 'now'})")
    export_channel(channel_id, out, token=token, after=since, before=before)
    click.echo(f"Wrote {out} ({out.stat().st_size} bytes)")


@cli.command()
@click.argument("channel_id")
@click.option("--week", "weeks", multiple=True, help="ISO week (e.g. 2026-W20); repeatable. Defaults to weeks pending since the checkpoint.")
@click.option("--include-current", is_flag=True, help="Also digest the still-running ISO week.")
@click.option("--batch/--sync", "use_batch", default=False, show_default=True, help="Use the Message Batches API (50% off, slower).")
@click.option("--out-root", type=click.Path(path_type=Path), default=None, help="Write digests here instead of digests/ (checkpoint untouched). Used by evals.")
@_effort_opt
@_format_opt
@_model_opt
def summarize(channel_id, weeks, include_current, use_batch, out_root, effort, input_format, model) -> None:
    """Summarize already-exported messages into weekly digests."""
    jobs = _load_jobs(channel_id, list(weeks) or None, include_current)
    ok, failed = _summarize_jobs(jobs, use_batch=use_batch, out_root=out_root or DIGESTS_DIR, effort=effort, input_format=input_format, model=model)
    click.echo(f"Done: {len(ok)} ok, {len(failed)} failed")
    if out_root is None:
        _apply_checkpoints(jobs, ok, failed)
    if failed:
        raise SystemExit(1)


@cli.command()
@click.argument("channel_id")
@click.pass_context
def run(ctx: click.Context, channel_id: str) -> None:
    """Export then summarize all pending weeks for one channel."""
    ctx.invoke(export, channel_id=channel_id, since=None, include_current=False)
    ctx.invoke(summarize, channel_id=channel_id)


class _Lock:
    """Single-run lock so an overlapping scheduled run exits instead of double-spending."""

    def __enter__(self):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(LOCK_FILE.read_text() or 0)
            except ValueError:
                pid = 0  # corrupt lock file: treat as stale
            try:
                if pid <= 0:
                    raise ProcessLookupError
                os.kill(pid, 0)
                raise click.ClickException(f"Another digest run (pid {pid}) is in progress.") from None
            except PermissionError:
                raise click.ClickException(f"Another digest run (pid {pid}) is in progress.") from None
            except ProcessLookupError:
                LOCK_FILE.unlink(missing_ok=True)  # stale lock from a crashed run
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        LOCK_FILE.unlink(missing_ok=True)


@cli.command("run-all")
@click.option("--batch/--sync", "use_batch", default=True, show_default=True, help="Use the Message Batches API (50% off).")
@click.option("--skip-export", is_flag=True, help="Summarize existing exports only.")
@click.option("--channel", "only", multiple=True, help="Limit to these channel IDs (repeatable).")
@_effort_opt
@_format_opt
@_model_opt
@click.pass_context
def run_all(ctx, use_batch, skip_export, only, effort, input_format, model) -> None:
    """The weekly job: export every channel, digest all pending weeks in one batch, build indexes."""
    with _Lock():
        _warn_if_exporter_outdated()
        jobs: list[WeekJob] = []
        export_failed = []
        channels = [(cid, cfg) for cid, cfg in _load_channels().items() if not only or cid in only]
        for i, (cid, cfg) in enumerate(channels, 1):
            name = f"{cfg['server']}/{cfg['channel_name']}"
            click.echo(f"[{i}/{len(channels)}] {name}")
            try:
                if not skip_export:
                    ctx.invoke(export, channel_id=cid, since=None, include_current=False)
                jobs += _load_jobs(cid, None, include_current=False)
            except Exception as e:  # noqa: BLE001 — one bad channel must not block the rest
                export_failed.append(name)
                click.echo(f"  {name}: FAILED — {e}", err=True)

        ok, failed = _summarize_jobs(jobs, use_batch=use_batch, out_root=DIGESTS_DIR, effort=effort, input_format=input_format, model=model)
        _apply_checkpoints(jobs, ok, failed)
        click.echo(f"Done: {len(ok)} ok, {len(failed)} failed, {len(export_failed)} channel(s) failed to export")
        if failed or export_failed:
            raise SystemExit(1)


def _register_eval() -> None:
    from .evals import eval_cmd

    cli.add_command(eval_cmd)


_register_eval()


if __name__ == "__main__":
    cli()
