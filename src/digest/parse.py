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


def load_messages(export_path: Path) -> list[dict]:
    """Load the `messages` array from a DiscordChatExporter JSON file."""
    with export_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("messages", [])


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
