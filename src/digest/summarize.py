"""Summarize a week of Discord messages via the Anthropic Messages API.

Single API call per week. The system prompt is frozen and cached so re-running
across weeks reuses the cache. Output is constrained to a Pydantic schema via
`messages.parse()`.

If a week is too large for one call, raises `WeekTooLargeError` — the caller
should split by day and call this once per bucket, then merge. Map-reduce
merging is not implemented yet; this gate fails loud rather than silently
truncating.
"""

from __future__ import annotations

import json

import anthropic
from anthropic.types import MessageParam
from pydantic import BaseModel, Field

from .parse import extract_links

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 16000
# Leave headroom for system prompt (~2K) + output (16K) under Opus 4.7's 1M window.
# A single call comfortably handles ~150K input tokens; above that, chunk upstream.
INPUT_TOKEN_BUDGET = 150_000


class Topic(BaseModel):
    title: str = Field(description="Short, specific title for the discussion thread.")
    summary: str = Field(description="One or two sentences capturing what was discussed and any conclusion reached.")
    key_participants: list[str] = Field(default_factory=list, description="Display names of the most active or notable contributors.")
    anchor_msg_ids: list[str] = Field(default_factory=list, description="IDs of 1-3 representative messages from the thread.")


class Link(BaseModel):
    url: str
    shared_by: str = Field(description="Display name of the user who shared the link.")
    context: str = Field(description="One sentence on why this was shared / what it is.")


class Announcement(BaseModel):
    headline: str = Field(description="Short headline as it would appear in a news digest.")
    detail: str = Field(description="One or two sentences of detail.")
    msg_id: str = Field(description="ID of the announcement message.")


class WeekSummary(BaseModel):
    top_topics: list[Topic]
    links: list[Link]
    announcements: list[Announcement]


SYSTEM_PROMPT = """You are a Discord channel digest writer. Given one week of messages from a single channel, produce a structured summary with three sections:

1. **top_topics** — the most substantive discussion threads of the week. Group related messages into coherent topics. Skip small talk, greetings, off-topic chatter, and routine Q&A unless it produced a notable answer. Aim for 3-8 topics; if the week was quiet, return fewer.

2. **links** — every URL that was shared, with one-sentence context on why it was shared (release announcement, tutorial, blog post, paper, demo, etc.). Deduplicate identical URLs. Skip URLs that are clearly auto-generated noise (image hosts for memes, internal Discord CDN links to attachments without context).

3. **announcements** — releases, launches, AMAs, decisions, deadlines, or other news-shaped messages someone would want to know about. Pinned messages and messages with many reactions are strong signals. Not every week has announcements; return an empty list if none.

Guidelines:
- Be concrete. "Discussion about X" is useless; say what was concluded or what the question was.
- Use the message author's display name (`author` field) **verbatim, character-for-character**. Do not normalize, correct, re-capitalize, or guess spellings — even if a name looks like a misspelled English word. Copy it exactly as it appears in the input.
- Reference messages by their `id` field for `anchor_msg_ids` and `msg_id`.
- Do not hallucinate links, participants, or messages that aren't in the input.
- If the week is essentially empty (no substantive content), return empty lists for all three fields.
"""


class WeekTooLargeError(RuntimeError):
    pass


def _compact_message(msg: dict) -> dict:
    """Strip a Discord message to the fields the summarizer needs."""
    return {
        "id": msg.get("id"),
        "timestamp": msg.get("timestamp"),
        "author": msg.get("author", {}).get("name"),
        "content": msg.get("content"),
        "reactions": [
            {"emoji": r.get("emoji", {}).get("name"), "count": r.get("count")}
            for r in (msg.get("reactions") or [])
        ],
        "is_pinned": msg.get("isPinned") or msg.get("type") == "ChannelPinnedMessage",
        "reference_msg_id": (msg.get("reference") or {}).get("messageId"),
        "links": extract_links(msg),
    }


def summarize_week(
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
) -> WeekSummary:
    """Summarize one week of messages into a WeekSummary.

    Raises WeekTooLargeError if the rendered input exceeds INPUT_TOKEN_BUDGET —
    caller should split by day and merge.
    """
    if not messages:
        return WeekSummary(top_topics=[], links=[], announcements=[])

    client = client or anthropic.Anthropic()
    payload = json.dumps([_compact_message(m) for m in messages], ensure_ascii=False)

    user_messages: list[MessageParam] = [
        {"role": "user", "content": f"Here are the messages from this week (JSON):\n\n{payload}"}
    ]

    token_count = client.messages.count_tokens(
        model=model,
        system=SYSTEM_PROMPT,
        messages=user_messages,
    )
    if token_count.input_tokens > INPUT_TOKEN_BUDGET:
        raise WeekTooLargeError(
            f"Week has {token_count.input_tokens} input tokens (budget {INPUT_TOKEN_BUDGET}). "
            "Split by day and merge."
        )

    response = client.messages.parse(
        model=model,
        max_tokens=DEFAULT_MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=[
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        messages=user_messages,
        output_format=WeekSummary,
    )
    return response.parsed_output
