"""Render a weekly WeekSummary to markdown and write it to disk."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .assets import MAX_IMAGES_PER_TOPIC
from .summarize import WeekSummary

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("-", s.lower()).strip("-") or "unnamed"


def week_monday(week: str) -> date:
    """'2026-W20' → the Monday date of that ISO week."""
    m = _WEEK_RE.match(week)
    if not m:
        raise ValueError(f"Invalid ISO week: {week!r} (expected YYYY-Www).")
    return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)


def _week_dates(week: str) -> tuple[date, date]:
    """'2026-W20' → (Mon, Sun) dates for that ISO week."""
    monday = week_monday(week)
    return monday, monday + timedelta(days=6)


_LINK_SECTIONS = (("code", "Code & tools"), ("project", "Projects & releases"), ("reading", "Articles & videos"), ("other", "Other"))


def _size(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{max(1, round(n / 1024))} KB"


def jump_url(guild_id: str, channel_id: str, msg_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{msg_id}"


def render_digest(
    summary: WeekSummary,
    channel_name: str,
    week: str,
    message_count: int,
    *,
    guild_id: str | None = None,
    channel_id: str | None = None,
    attachments: dict[str, dict] | None = None,
) -> str:
    """Render to markdown. `attachments` is {id: {file_name, size_bytes, msg_id, local?}}
    where `local` is an image path relative to the digest file (see assets.fetch_images)."""
    monday, sunday = _week_dates(week)
    year, wk = monday.isocalendar()[:2]
    header_dates = f"{monday:%b %d} – {sunday:%b %d, %Y}"
    attachments = attachments or {}

    def jump(msg_id: str) -> str | None:
        return jump_url(guild_id, channel_id, msg_id) if guild_id and channel_id and msg_id else None

    out: list[str] = [f"# #{channel_name} — Week {wk}, {year} ({header_dates})", ""]

    if not (summary.top_topics or summary.links):
        out += ["_No notable activity this week._", ""]
    else:
        # With a single topic the TL;DR would just repeat it.
        if summary.tldr and len(summary.top_topics) > 1:
            out += [f"> **TL;DR** — {summary.tldr}", ""]

        if summary.top_topics:
            out.append("## Top topics")
            out.append("")
            for t in summary.top_topics:
                out.append(f"### {'📦 ' if t.is_release else ''}{t.title}")
                out.append(t.summary)
                if t.key_points:
                    out += [""] + [f"- {p}" for p in t.key_points]
                if t.open_question:
                    out += ["", f"❓ **Open:** {t.open_question}"]

                atts = [attachments[a] for a in t.attachment_ids if a in attachments]
                files = []
                for a in atts:
                    if a.get("local"):
                        continue
                    label = f"`{a['file_name']}` ({_size(a['size_bytes'])})"
                    url = jump(a["msg_id"])
                    files.append(f"[{label}]({url})" if url else label)
                if files:
                    out += ["", "📎 " + " · ".join(files)]
                images = [a for a in atts if a.get("local")][: MAX_IMAGES_PER_TOPIC]
                if images:
                    out += ["", " ".join(f'<img src="{a["local"]}" width="240" alt="{a["file_name"]}">' for a in images)]

                meta = []
                if t.key_participants:
                    meta.append(", ".join(t.key_participants))
                if t.anchor_msg_ids and (url := jump(t.anchor_msg_ids[0])):
                    meta.append(f"[jump to thread →]({url})")
                if meta:
                    out += ["", f"_{' · '.join(meta)}_"]
                out.append("")

        if summary.links:
            out.append("## Links")
            for key, heading in _LINK_SECTIONS:
                group = [link for link in summary.links if link.category == key]
                if not group:
                    continue
                out += ["", f"**{heading}**"]
                for link in group:
                    out.append(f"- [{link.title}]({link.url}) — {link.context} _({link.shared_by})_")
            out.append("")

    out += ["---", f"_Generated {datetime.now():%Y-%m-%d} from {message_count} message{'s' if message_count != 1 else ''}._", ""]
    return "\n".join(out)


def digest_path(root: Path, server: str, channel: str, week: str) -> Path:
    return root / _slug(server) / _slug(channel) / f"{week}.md"


def write_digest(markdown: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def write_checkpoint(root: Path, server: str, channel: str, last_week: str, last_message_id: str) -> Path:
    path = root / _slug(server) / _slug(channel) / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_week": last_week, "last_message_id": last_message_id}, indent=2),
        encoding="utf-8",
    )
    return path


def read_checkpoint(root: Path, server: str, channel: str) -> dict | None:
    path = root / _slug(server) / _slug(channel) / "latest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
