"""Run many channel-week summaries through the Message Batches API (50% cheaper).

The pending batch is saved to disk so an interrupted run resumes polling instead
of paying twice. Batches don't accept the `fallbacks` parameter, so refused or
failed items are reported back for the caller to retry synchronously.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic

from .render import atomic_write
from .summarize import Result, SummaryError, parse_message

POLL_SECONDS = 60
_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def custom_id(channel_id: str, week: str) -> str:
    """Batch custom_id must match ^[a-zA-Z0-9_-]{1,64}$."""
    return _ID_RE.sub("_", f"{channel_id}_{week}")[:64]


def load_pending(state_file: Path) -> dict | None:
    return json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else None


def submit(
    client: anthropic.Anthropic,
    requests: dict[str, dict[str, Any]],
    state_file: Path,
    meta: dict[str, Any],
) -> str:
    """Submit {custom_id: params}; persist {batch_id, meta} so a crash can resume."""
    batch = client.messages.batches.create(
        requests=[{"custom_id": cid, "params": params} for cid, params in requests.items()]
    )
    atomic_write(state_file, json.dumps({"batch_id": batch.id, "meta": meta}, indent=2))
    return batch.id


def wait(client: anthropic.Anthropic, batch_id: str, echo: Callable[[str], None] = print) -> None:
    start = time.monotonic()
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return
        c = batch.request_counts
        echo(f"  batch {batch_id}: {c.processing} processing, {c.succeeded} done, {c.errored} errored ({int(time.monotonic() - start)}s elapsed)")
        time.sleep(POLL_SECONDS)


def collect(client: anthropic.Anthropic, batch_id: str) -> dict[str, Result | Exception]:
    """{custom_id: Result} for usable items, {custom_id: Exception} for the rest."""
    out: dict[str, Result | Exception] = {}
    for item in client.messages.batches.results(batch_id):
        r = item.result
        if r.type == "succeeded":
            try:
                out[item.custom_id] = parse_message(r.message)
            except SummaryError as e:
                out[item.custom_id] = e
        elif r.type == "errored":
            out[item.custom_id] = SummaryError(f"batch item errored: {r.error}")
        else:
            out[item.custom_id] = SummaryError(f"batch item {r.type}")
    return out
