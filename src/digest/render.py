"""Render a weekly WeekSummary to markdown and write it to disk."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .parse import is_image
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


# Topics shown in full; the rest collapse into one-line "Also this week" items.
MAX_EXPANDED = 3
MAX_KEY_POINTS = 3
# ~1.9 minutes at 238 wpm (the 1-2 minute target). Over-budget digests are trimmed by render_within_budget.
WORD_BUDGET = 450


def expanded_topics(summary: WeekSummary, n: int = MAX_EXPANDED) -> list:
    return summary.top_topics[:n]


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG_RE = re.compile(r"<[^>]+>")


def word_count(markdown: str) -> int:
    """Words a reader actually reads: link targets, image tags and the footer excluded."""
    body = markdown.split("\n---\n")[0]
    body = _TAG_RE.sub(" ", _MD_LINK_RE.sub(r"\1", body))
    return len(re.findall(r"[\w'’.-]*\w", body))


def render_digest(
    summary: WeekSummary,
    channel_name: str,
    week: str,
    message_count: int,
    *,
    guild_id: str | None = None,
    channel_id: str | None = None,
    attachments: dict[str, dict] | None = None,
    max_expanded: int = MAX_EXPANDED,
) -> str:
    """Render to markdown. `attachments` is {id: {file_name, size_bytes, msg_id, local?}}
    where `local` is an image path relative to the digest file (see assets.fetch_images)."""
    monday, sunday = _week_dates(week)
    year, wk = monday.isocalendar()[:2]
    header_dates = f"{monday:%b %d} – {sunday:%b %d, %Y}"
    attachments = attachments or {}
    links_by_url = {link.url: link for link in summary.links}

    def jump(msg_id: str) -> str | None:
        return jump_url(guild_id, channel_id, msg_id) if guild_id and channel_id and msg_id else None

    def files_of(t) -> list[str]:
        out = []
        for a in (attachments[i] for i in t.attachment_ids if i in attachments):
            if is_image(a["file_name"]):
                continue  # images are shown as the topic thumbnail, never as downloads
            label = f"`{a['file_name']}` ({_size(a['size_bytes'])})"
            url = jump(a["msg_id"])
            out.append(f"[{label}]({url})" if url else label)
        return out

    out: list[str] = [f"# #{channel_name} — Week {wk}, {year} ({header_dates})", ""]

    if not (summary.top_topics or summary.links):
        out += ["_No notable activity this week._", ""]
    else:
        if summary.tldr:
            out += [f"> **TL;DR** — {summary.tldr}", ""]

        cited: set[str] = set()
        shown = expanded_topics(summary, max_expanded)
        if shown:
            out += ["## Top topics", ""]
        for t in shown:
            out.append(f"### {'📦 ' if t.is_release else ''}{t.title}")
            out.append(t.summary)
            if t.key_points:
                out += [""] + [f"- {p}" for p in t.key_points[:MAX_KEY_POINTS]]
            if t.open_question:
                out += ["", f"❓ **Open:** {t.open_question}"]
            topic_links = [links_by_url[u] for u in t.link_urls if u in links_by_url]
            cited.update(link.url for link in topic_links)
            extras = [f"📎 {f}" for f in files_of(t)] + [f"🔗 [{link.title}]({link.url})" for link in topic_links]
            if extras:
                out += ["", " · ".join(extras)]
            image = next((attachments[i] for i in t.attachment_ids if attachments.get(i, {}).get("local")), None)
            if image:
                out += ["", f'<img src="{image["local"]}" width="200" alt="{image["file_name"]}">']
            meta = [", ".join(t.key_participants[:4])] if t.key_participants else []
            if t.anchor_msg_ids and (url := jump(t.anchor_msg_ids[0])):
                meta.append(f"[jump →]({url})")
            if meta:
                out += ["", f"_{' · '.join(meta)}_"]
            out.append("")

        rest = summary.top_topics[max_expanded:]
        if rest:
            out += ["## Also this week", ""]
            for t in rest:
                line = f"- {'📦 ' if t.is_release else ''}{t.title}"
                line += "".join(f" · 📎 {f}" for f in files_of(t))
                topic_links = [links_by_url[u] for u in t.link_urls if u in links_by_url]
                cited.update(link.url for link in topic_links)
                line += "".join(f" · 🔗 [{link.title}]({link.url})" for link in topic_links)
                if t.anchor_msg_ids and (url := jump(t.anchor_msg_ids[0])):
                    line += f" · [jump →]({url})"
                out.append(line)
            out.append("")

        uncited = [link for link in summary.links if link.url not in cited]
        if uncited:
            out.append("## Links")
            for key, heading in _LINK_SECTIONS:
                group = [link for link in uncited if link.category == key]
                if not group:
                    continue
                out += ["", f"**{heading}**"]
                for link in group:
                    out.append(f"- [{link.title}]({link.url}) — {link.context} _({link.shared_by})_")
            out.append("")

    out += ["---", f"_Generated {datetime.now():%Y-%m-%d} from {message_count} message{'s' if message_count != 1 else ''}._", ""]
    return "\n".join(out)


def render_within_budget(summary: WeekSummary, *args, budget: int = WORD_BUDGET, **kwargs) -> str:
    """Render, then trim until the digest fits the reading budget: drop key points from the
    lowest-ranked expanded topic first (the top topic keeps one), then show fewer topics in full."""
    summary = summary.model_copy(deep=True)
    n = MAX_EXPANDED
    markdown = render_digest(summary, *args, max_expanded=n, **kwargs)
    while word_count(markdown) > budget:
        shown = summary.top_topics[:n]
        for t in shown:
            t.key_points = t.key_points[:MAX_KEY_POINTS]
        trimmable = [t for t in shown[1:] if t.key_points] or [t for t in shown[:1] if len(t.key_points) > 1]
        if trimmable:
            trimmable[-1].key_points.pop()
        elif n > 1:
            n -= 1
        else:
            break
        markdown = render_digest(summary, *args, max_expanded=n, **kwargs)
    return markdown


def digest_path(root: Path, server: str, channel: str, week: str) -> Path:
    return root / _slug(server) / _slug(channel) / f"{week}.md"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_digest(markdown: str, path: Path) -> Path:
    atomic_write(path, markdown)
    return path


def checkpoint_path(root: Path, server: str, channel: str) -> Path:
    return root / _slug(server) / _slug(channel) / "latest.json"


def write_checkpoint(root: Path, server: str, channel: str, checkpoint: dict) -> Path:
    """`checkpoint` = {last_week, last_message_id, failed_weeks: [...]}."""
    path = checkpoint_path(root, server, channel)
    atomic_write(path, json.dumps(checkpoint, indent=2))
    return path


def read_checkpoint(root: Path, server: str, channel: str) -> dict | None:
    path = checkpoint_path(root, server, channel)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("failed_weeks", [])
    return data


def current_week(now: datetime | None = None) -> str:
    year, wk, _ = (now or datetime.now(timezone.utc)).isocalendar()
    return f"{year}-W{wk:02d}"
