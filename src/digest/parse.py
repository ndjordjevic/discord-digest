"""Parse DiscordChatExporter JSON and bucket messages by ISO week.

Top-level export shape (DiscordChatExporter JSON):
    {"guild": {...}, "channel": {...}, "messages": [...]}

Each message includes: id, type, timestamp (ISO 8601), content, author{name,isBot},
attachments[{url,...}], embeds[{url,title,description,...}].
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

URL_RE = re.compile(r"https?://[^\s<>\"')]+")

# Discord message types we want to keep. "Default" = normal user message.
# "ChannelPinnedMessage" = "X pinned a message" — signal for noteworthy content.
# "Reply" is what DCE labels threaded replies (we keep them).
KEEP_TYPES = {"Default", "Reply", "ChannelPinnedMessage"}

# Discord CDN hosts serve signed, expiring attachment URLs. They're noise in a
# digest: the link rots within hours and the model can't tell from the URL what
# the file is. Filter at extraction so they never reach the summarizer.
_CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")


def load_export(export_path: Path) -> tuple[dict, list[dict]]:
    """Load a DiscordChatExporter JSON file → ({guild_id, channel_id}, messages)."""
    with export_path.open(encoding="utf-8") as f:
        data = json.load(f)
    meta = {
        "guild_id": (data.get("guild") or {}).get("id"),
        "channel_id": (data.get("channel") or {}).get("id"),
    }
    return meta, data.get("messages", [])


def load_messages(export_path: Path) -> list[dict]:
    """Load the `messages` array from a DiscordChatExporter JSON file."""
    return load_export(export_path)[1]


def is_relevant(msg: dict) -> bool:
    """Filter out system noise and bot chatter, keep human messages + pins."""
    if msg.get("type") not in KEEP_TYPES:
        return False
    if msg.get("author", {}).get("isBot") and msg.get("type") != "ChannelPinnedMessage":
        return False
    return True


def iso_week(timestamp: str) -> str:
    """ISO-8601 timestamp → 'YYYY-Www' (e.g., '2026-W20')."""
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def bucket_by_week(messages: list[dict]) -> dict[str, list[dict]]:
    """Group relevant messages by ISO week. Returns {'YYYY-Www': [...]}."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        if not is_relevant(msg):
            continue
        ts = msg.get("timestamp")
        if not ts:
            continue
        buckets[iso_week(ts)].append(msg)
    return dict(buckets)


def _parse_ts(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def lead_in_context(
    messages: list[dict],
    week: str,
    *,
    max_messages: int = 30,
    lookback_hours: float = 48.0,
) -> list[dict]:
    """Messages from before `week` that the week's opening conversation may depend on.

    Takes up to `max_messages` of the latest messages posted within
    `lookback_hours` before the week's first message (the summarizer decides
    which are actually related), plus any older message that an in-week reply
    points at. `messages` must be relevant messages sorted by timestamp.
    """
    week_msgs = [m for m in messages if iso_week(m["timestamp"]) == week]
    if not week_msgs:
        return []
    first_ts = _parse_ts(week_msgs[0]["timestamp"])
    before = [m for m in messages if _parse_ts(m["timestamp"]) < first_ts]

    picked = [
        m for m in before[-max_messages:]
        if (first_ts - _parse_ts(m["timestamp"])).total_seconds() <= lookback_hours * 3600
    ]
    picked_ids = {m["id"] for m in picked}

    by_id = {m["id"]: m for m in before}
    for m in week_msgs:
        ref = (m.get("reference") or {}).get("messageId")
        if ref in by_id and ref not in picked_ids:
            picked.append(by_id[ref])
            picked_ids.add(ref)

    return sorted(picked, key=lambda m: m["timestamp"])


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def is_image(file_name: str) -> bool:
    return file_name.lower().endswith(IMAGE_EXTS)


def extract_links(message: dict) -> list[str]:
    """Pull URLs from message content, embeds, and attachments. De-duplicated.

    Skips Discord CDN attachment URLs — they're signed/expiring and add no signal.
    """
    urls: list[str] = []
    urls += URL_RE.findall(message.get("content") or "")
    for emb in message.get("embeds") or []:
        if url := emb.get("url"):
            urls.append(url)
    for att in message.get("attachments") or []:
        if url := att.get("url"):
            urls.append(url)
    seen: set[str] = set()
    return [
        u for u in urls
        if not any(host in u for host in _CDN_HOSTS)
        and not (u in seen or seen.add(u))
    ]
