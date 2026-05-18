"""Render a weekly WeekSummary to markdown and write it to disk."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

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


def render_digest(summary: WeekSummary, channel_name: str, week: str, message_count: int) -> str:
    monday, sunday = _week_dates(week)
    year, wk = monday.isocalendar()[:2]
    header_dates = f"{monday:%b %d} – {sunday:%b %d, %Y}"

    out: list[str] = [f"# #{channel_name} — Week {wk}, {year} ({header_dates})", ""]

    if not (summary.top_topics or summary.links or summary.announcements):
        out += ["_No notable activity this week._", ""]
    else:
        if summary.top_topics:
            out.append("## Top topics")
            for t in summary.top_topics:
                line = f"- **{t.title}** — {t.summary}"
                if t.key_participants:
                    line += f" _(participants: {', '.join(t.key_participants)})_"
                out.append(line)
            out.append("")

        if summary.announcements:
            out.append("## Announcements")
            for a in summary.announcements:
                out.append(f"- **{a.headline}** — {a.detail}")
            out.append("")

        if summary.links:
            out.append("## Links")
            for link in summary.links:
                out.append(f"- [{link.url}]({link.url}) — shared by {link.shared_by}; {link.context}")
            out.append("")

    out += ["---", f"_Generated {datetime.now():%Y-%m-%d} from {message_count} messages._", ""]
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
