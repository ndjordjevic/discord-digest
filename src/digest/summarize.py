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
from typing import Literal

import anthropic
from anthropic.types import MessageParam
from pydantic import BaseModel, Field

from .parse import extract_links, is_image

DEFAULT_MODEL = "claude-sonnet-5-5"
# Faithfulness to the chat log is the whole point here; keep thinking depth up.
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 16000
# Leave headroom for system prompt (~2K) + output (16K) under the model's 1M window.
# A single call comfortably handles ~150K input tokens; above that, chunk upstream.
INPUT_TOKEN_BUDGET = 150_000


class Topic(BaseModel):
    title: str = Field(description="Short, specific headline that says what happened, not just the subject.")
    summary: str = Field(description="One sentence: what this thread is about and its outcome.")
    key_points: list[str] = Field(default_factory=list, description="0-5 short bullet facts a reader needs: problems reported, causes found or suspected, fixes, decisions, technical tips, versions/numbers. One fact each, attributed where useful. Empty for a simple one-message topic.")
    is_release: bool = Field(default=False, description="True if this topic is a release, launch or announcement of something people can download/use.")
    open_question: str = Field(default="", description="If the thread ended with something unresolved or promised for later, one short sentence on it. Empty otherwise.")
    key_participants: list[str] = Field(default_factory=list, description="Display names of the most active or notable contributors.")
    anchor_msg_ids: list[str] = Field(default_factory=list, description="IDs of 1-3 representative in-week messages; the first is where a reader should jump in.")
    attachment_ids: list[str] = Field(default_factory=list, description="IDs of attachments worth showing for this topic: screenshots/photos that illustrate it and downloadable files (releases, source). Skip memes and irrelevant images.")


class Link(BaseModel):
    url: str
    title: str = Field(description="Human-readable title, taken from the link preview (`embeds[].title`) when available, otherwise a short accurate label.")
    category: Literal["code", "project", "reading", "other"] = Field(
        description="code = repos, commits, PRs, issues, gists, dev tools, SDKs, compilers, libraries; project = demos, releases, products, scene productions; reading = articles, docs, videos, live streams, forum threads; other = anything else."
    )
    shared_by: str = Field(description="Display name of the user who shared the link.")
    context: str = Field(description="One sentence on what it is and why it was shared.")


class WeekSummary(BaseModel):
    tldr: str = Field(description="1-3 sentences: the most important things that happened this week. Empty if the week had nothing substantive.")
    top_topics: list[Topic]
    links: list[Link]


SYSTEM_PROMPT = """You are a Discord channel digest writer. A reader should be able to skim your digest in under a minute and know what mattered in this channel this week. Given one week of messages from a single channel, produce:

1. **tldr** — 1-3 sentences on the most important things that happened (releases, decisions, problems found/solved). No filler.

2. **top_topics** — the substantive discussion threads, most important first. Group related messages (including replies and follow-ups) into ONE coherent topic — do not split a single conversation into several topics, and do not repeat a topic as a separate release entry; mark it with `is_release` instead. Aim for 1-8 topics; a quiet week gets fewer. Every substantive point of a thread (a bug report, a diagnosis, a fix, a technical tip, a decision) must appear in its `key_points` — keep the digest short by cutting filler, never by dropping facts. `key_points` must add information beyond `summary` — never restate the summary, and never list attachments or links there (files and links are rendered separately). A single-message topic usually needs no key_points. If something was left unresolved or promised for later, put it in `open_question`. Pick `attachment_ids` for screenshots/photos that show what the topic is about and for downloadable files (releases, source archives).

3. **links** — every URL that was shared (excluding Discord CDN attachments), deduplicated, with a title and category. Skip obvious noise (meme image hosts, tenor gifs).

Exclude entirely: greetings, banter, jokes, off-topic small talk, one-word reactions ("awesome", "nice"). They must not appear as topics, and they do not make someone a key participant.

Input format: a `<this_week>` JSON list of messages, optionally preceded by a `<background_already_digested>` list. Each has `id`, `timestamp`, `author`, `content`, `reactions`, `reference_msg_id` (the message it replies to), `links`, `embeds` (link previews with `url`/`title`/`description`), `attachments` (`id`, `file_name`, `size_kb`, `kind` image|file) and `context`. Background messages (`context: true`) are from BEFORE this week and were already covered by last week's digest; they exist only so you understand a conversation that was already running when the week started. Never make a topic out of context messages alone, never list their links, and only reference in-week (`context: false`) message and attachment IDs. Context facts were already covered in last week's digest: `summary`, `key_points` and `open_question` must report only what happened this week. Use background only to identify things (e.g. "the demo that won last week's Wild Compo"), in at most a short clause — never as a bullet of its own, and never phrased as "earlier…".

Rules:
- Be concrete. "Discussion about X" is useless; say what was concluded or what the question was.
- Describe a link ONLY from its preview (`embeds`) or what people said about it. Never guess — never write "likely", "probably", "appears to be". If you cannot tell what it is, say "shared without comment".
- Keep technical terms, commands, paths, file names and version numbers verbatim; put commands and paths in `backticks`. Never paraphrase a command (if someone asks another person to run `version libs:foo.library`, they asked them to run that command). Name projects the way their authors wrote them in messages, not from archive file names.
- Every downloadable file (archives, executables, disk images) posted this week must be in some topic's `attachment_ids`.
- Use the author's display name (`author` field) **verbatim, character-for-character**. Do not normalize, correct, or re-capitalize names.
- Refer to people by name. Do not use gendered pronouns (he/she) for anyone; rephrase or use "they".
- Channels are often demoscene / retro-computing ones: "msx" usually means music, "gfx" graphics, "prod" a production, "compo" a competition, "wild" the wild compo.
- A shared link or release that drew many reactions (roughly 5+) deserves its own short topic even without discussion.
- Reactions are a strong signal of what the community found notable — use them to rank topics, but do not narrate reaction counts or emoji ("received 5 thumbs-up") in the text.
- No filler: skip incidental details that don't help a reader decide whether to look closer.
- Report what was said, not more. Never generalize a statement beyond what it covered (if someone says "the source is in the package" about one download, don't claim every download has source). Don't present a suspicion as a confirmed cause, and don't infer facts the messages don't state (e.g. that two files are two different projects). If something is uncertain, attribute it ("tjomp suspects…") rather than hedging with "likely".
- Do not hallucinate links, participants, messages or attachments that aren't in the input.
- If the week is essentially empty (no substantive content), return an empty tldr and empty lists.
"""


class WeekTooLargeError(RuntimeError):
    pass


def _compact_message(msg: dict, *, context: bool = False) -> dict:
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
        "embeds": [
            {"url": e.get("url"), "title": e.get("title"), "description": (e.get("description") or "")[:300]}
            for e in (msg.get("embeds") or [])
            if e.get("url") or e.get("title")
        ],
        "attachments": [
            {
                "id": a.get("id"),
                "file_name": a.get("fileName"),
                "size_kb": round((a.get("fileSizeBytes") or 0) / 1024),
                "kind": "image" if is_image(a.get("fileName") or "") else "file",
            }
            for a in (msg.get("attachments") or [])
        ],
        "context": context,
    }


def summarize_week(
    messages: list[dict],
    *,
    context: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
) -> WeekSummary:
    """Summarize one week of messages into a WeekSummary.

    `context` holds earlier messages (see `parse.lead_in_context`) so a
    conversation that started last week can be understood; they are marked as
    background and not summarized themselves.

    Raises WeekTooLargeError if the rendered input exceeds INPUT_TOKEN_BUDGET —
    caller should split by day and merge.
    """
    if not messages:
        return WeekSummary(tldr="", top_topics=[], links=[])

    client = client or anthropic.Anthropic()
    week_json = json.dumps([_compact_message(m) for m in messages], ensure_ascii=False)
    content = f"<this_week>\n{week_json}\n</this_week>"
    if context:
        context_json = json.dumps([_compact_message(m, context=True) for m in context], ensure_ascii=False)
        content = (
            "<background_already_digested>\n"
            f"{context_json}\n"
            "</background_already_digested>\n\n" + content
        )

    user_messages: list[MessageParam] = [
        {"role": "user", "content": content}
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

    response = client.beta.messages.parse(
        model=model,
        max_tokens=DEFAULT_MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": DEFAULT_EFFORT},
        # On a safety-classifier decline, re-run on Anthropic's recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},  # not a typed kwarg in this SDK version
        system=[
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        messages=user_messages,
        output_format=WeekSummary,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model refused to summarize this week: {response.stop_details}")
    return response.parsed_output
