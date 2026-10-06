"""Summarize a week of Discord messages via the Anthropic Messages API.

One request per channel-week. `build_request` produces the request params so the
same request can be sent synchronously (`summarize_week`) or through the Message
Batches API (see `batch.py`, 50% cheaper). Output is constrained to the
`WeekSummary` JSON schema via `output_config.format` and validated client-side.

If a week is too large for one call, raises `WeekTooLargeError`. Map-reduce
merging is not implemented; Sonnet's 1M window makes it unnecessary at the
budget below.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from .parse import extract_links, is_image

DEFAULT_MODEL = "claude-sonnet-5-5"
# Eval sweep (evals/results/lines-*): low matched high on recall and faithfulness at ~5% lower cost.
DEFAULT_EFFORT = "low"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Thinking counts toward max_tokens; too low a cap truncates the JSON and wastes the call.
DEFAULT_MAX_TOKENS = 32000
# Sonnet 5.5 has a 1M window with no long-context premium.
INPUT_TOKEN_BUDGET = 500_000
# Non-streaming requests with a large max_tokens need an explicit timeout in the SDK.
REQUEST_TIMEOUT_S = 900
INPUT_FORMATS = ("lines", "json")
DEFAULT_INPUT_FORMAT = "lines"


class Topic(BaseModel):
    title: str = Field(description="Headline of at most 12 words that says what happened, most informative words first.")
    summary: str = Field(description="One sentence of at most 30 words: what happened and the outcome.")
    key_points: list[str] = Field(default_factory=list, description="At most 3 bullets of at most 20 words each, adding facts beyond the summary: problems, causes, fixes, decisions, tips, versions. Empty for a simple topic.")
    is_release: bool = Field(default=False, description="True if this topic is a release, launch or announcement of something people can download/use.")
    open_question: str = Field(default="", description="If the thread ended with something unresolved or promised for later, one short sentence. Empty otherwise.")
    key_participants: list[str] = Field(default_factory=list, description="Display names of the most active or notable contributors (max 4).")
    anchor_msg_ids: list[str] = Field(default_factory=list, description="IDs of 1-3 representative in-week messages; the first is where a reader should jump in.")
    attachment_ids: list[str] = Field(default_factory=list, description="IDs of this topic's downloadable files (releases, source archives) and of the single best screenshot/photo, if any.")
    link_urls: list[str] = Field(default_factory=list, description="URLs (exactly as in the input) of links that belong to this topic.")


class Link(BaseModel):
    url: str
    title: str = Field(description="Human-readable title, taken from the link preview when available, otherwise a short accurate label.")
    category: Literal["code", "project", "reading", "other"] = Field(
        description="code = repos, commits, PRs, issues, gists, dev tools, SDKs, compilers, libraries; project = demos, releases, products, scene productions; reading = articles, docs, videos, live streams, forum threads; other = anything else."
    )
    shared_by: str = Field(description="Display name of the user who shared the link.")
    context: str = Field(description="At most 15 words on what it is and why it was shared.")


class WeekSummary(BaseModel):
    tldr: str = Field(description="At most 45 words: the most important things that happened this week. Empty only if the week had nothing substantive.")
    top_topics: list[Topic] = Field(description="All substantive topics, most important first. Only the first 3 are shown in full; the rest appear as one-line headlines.")
    links: list[Link]


SYSTEM_PROMPT = """You are a Discord channel digest writer. A reader must be able to read the digest in 1-2 minutes (about 450 words) and know what's new in the channel this week. Given one week of messages from a single channel, produce:

1. **tldr** — at most 45 words on the most important things that happened (releases, decisions, problems found/solved). Always write it when there is at least one topic.

2. **top_topics** — every substantive discussion thread, MOST IMPORTANT FIRST: releases and decisions before questions and chatter-adjacent shares. Only the first 3 are shown in full; the rest appear as a one-line headline, so their `title` must stand on its own. Group related messages (replies, follow-ups) into ONE topic — never split a conversation, and never repeat a topic as a separate release entry; mark it with `is_release`.
   - `title`: at most 12 words, most informative words first.
   - `summary`: one sentence, at most 30 words.
   - `key_points`: at most 3 bullets of at most 20 words, each adding a fact beyond the summary (bug report, cause, fix, decision, tip, version). State outcomes, not who-said-what. Never restate the summary; never list files or links there (they are rendered separately). Empty for a simple topic.
   - `open_question`: something left unresolved or promised for later this week.
   - `attachment_ids`: every downloadable file of the topic plus at most one image that best shows it (for a release, a screenshot/photo of the release itself, not of a bug report).
   - `link_urls`: links that belong to the topic.

3. **links** — every URL shared this week (not Discord CDN attachments), deduplicated, with title, category and a short context. Skip obvious noise (tenor gifs, meme hosts).

Exclude entirely: greetings, banter, jokes, off-topic small talk, one-word reactions ("awesome", "nice"). They must not appear as topics and do not make someone a key participant.

Input format: a `<this_week>` block, optionally preceded by a `<background_already_digested>` block. One message per line:
`[<id>] <MM-DD HH:MM> <author> (re <id>) [pinned]: <text> {<emoji>×<count> ...}`
`(re <id>)` marks a reply, `{...}` lists reactions. Indented lines under a message are its link previews (`link: <url> | <title> | <description>`) and attachments (`file <attachment_id>: <file_name> (<size>, image|file)`). Times are UTC.
Background messages are from BEFORE this week and were already covered by last week's digest; they exist only so you understand a conversation that was already running. Never make a topic from background alone, never list background links, and only reference this week's message and attachment IDs. `summary`, `key_points` and `open_question` report only what happened this week; use background only to identify things (e.g. "the demo that won last week's Wild Compo") in at most a short clause.

Rules:
- Be concrete. "Discussion about X" is useless; say what was concluded or what the question was.
- Describe a link ONLY from its preview or what people said about it. Never guess — never write "likely", "probably", "appears to be". If you cannot tell what it is, say "shared without comment".
- Keep technical terms, commands, paths, file names and version numbers verbatim; put commands and paths in `backticks`. Never paraphrase a command (if someone asks another person to run `version libs:foo.library`, they asked them to run that command). Name a project the way its creator wrote it in a message (if the creator wrote "here comes Foo Bar" and uploaded `Fo_Bar.zip`, it is "Foo Bar") — not how others misspelled it, not from archive file names.
- Every downloadable file (archives, executables, disk images) posted this week must be in some topic's `attachment_ids`.
- Write every person's display name exactly as in the input, character-for-character, everywhere (titles, summaries, bullets) — `pellicus.pelella.dario` stays `pellicus.pelella.dario`, never "Pellicus Pelella Dario".
- Refer to people by name. Do not use gendered pronouns (he/she); rephrase or use "they".
- Channels are often demoscene / retro-computing ones: "msx" usually means music, "gfx" graphics, "prod" a production, "compo" a competition, "wild" the wild compo.
- A shared link or release that drew many reactions (roughly 5+) deserves its own topic even without discussion.
- Reactions signal what the community found notable — use them to rank, but never narrate reaction counts or emoji.
- Don't link two things just because the same person mentioned both (an experiment and a separate plan are not "X for Y" unless someone said so).
- No filler. Report what was said, not more: never generalize a statement beyond what it covered, never present a suspicion as a confirmed cause, never infer facts the messages don't state. Attribute uncertainty ("tjomp suspects…") instead of hedging.
- Do not hallucinate links, participants, messages or attachments that aren't in the input.
- If the week has no substantive content, return an empty tldr and empty lists.
"""


class WeekTooLargeError(RuntimeError):
    pass


class SummaryError(RuntimeError):
    """The model returned no usable summary (refusal, truncation, invalid JSON)."""


@dataclass
class Result:
    summary: WeekSummary
    usage: dict[str, Any]
    stop_reason: str | None
    model: str


# --- input formatting -------------------------------------------------------


def _hhmm(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M")


def _line_message(msg: dict) -> str:
    """One message in the compact line format described in SYSTEM_PROMPT."""
    head = f"[{msg.get('id')}] {_hhmm(msg['timestamp'])} {msg.get('author', {}).get('name')}"
    ref = (msg.get("reference") or {}).get("messageId")
    if ref:
        head += f" (re {ref})"
    if msg.get("isPinned") or msg.get("type") == "ChannelPinnedMessage":
        head += " [pinned]"
    text = (msg.get("content") or "").replace("\r", "").replace("\n", " / ")
    line = f"{head}: {text}"
    reactions = [f"{r.get('emoji', {}).get('name')}×{r.get('count')}" for r in (msg.get("reactions") or [])]
    if reactions:
        line += " {" + " ".join(reactions) + "}"

    out = [line]
    previewed = set()
    for e in msg.get("embeds") or []:
        if not (e.get("url") or e.get("title")):
            continue
        previewed.add(e.get("url"))
        desc = (e.get("description") or "").replace("\n", " ")[:200]
        out.append(f"    link: {e.get('url') or ''} | {e.get('title') or ''} | {desc}".rstrip(" |"))
    for url in extract_links(msg):
        if url not in previewed:
            out.append(f"    link: {url}")
    for a in msg.get("attachments") or []:
        name = a.get("fileName") or "file"
        kind = "image" if is_image(name) else "file"
        out.append(f"    file {a.get('id')}: {name} ({round((a.get('fileSizeBytes') or 0) / 1024)} KB, {kind})")
    return "\n".join(out)


def _json_message(msg: dict) -> dict:
    """Legacy JSON shape, kept for A/B token comparisons (`--input-format json`)."""
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
    }


def _block(messages: list[dict], input_format: str) -> str:
    if input_format == "json":
        return json.dumps([_json_message(m) for m in messages], ensure_ascii=False)
    return "\n".join(_line_message(m) for m in messages)


def build_user_content(messages: list[dict], context: list[dict] | None, input_format: str = DEFAULT_INPUT_FORMAT) -> str:
    content = f"<this_week>\n{_block(messages, input_format)}\n</this_week>"
    if context:
        content = f"<background_already_digested>\n{_block(context, input_format)}\n</background_already_digested>\n\n" + content
    return content


# --- request / response -----------------------------------------------------


@functools.cache
def _week_schema() -> dict:
    return anthropic.transform_schema(WeekSummary)


def build_request(
    messages: list[dict],
    *,
    context: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    input_format: str = DEFAULT_INPUT_FORMAT,
) -> dict[str, Any]:
    """Messages API params for one week; usable with `messages.create` or as a batch request."""
    return {
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": _week_schema()},
        },
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": build_user_content(messages, context, input_format)}],
    }


def check_size(client: anthropic.Anthropic, params: dict[str, Any]) -> int:
    """Count input tokens (free endpoint); raise WeekTooLargeError over budget."""
    n = client.messages.count_tokens(model=params["model"], system=params["system"], messages=params["messages"]).input_tokens
    if n > INPUT_TOKEN_BUDGET:
        raise WeekTooLargeError(f"Week has {n} input tokens (budget {INPUT_TOKEN_BUDGET}).")
    return n


def parse_message(message: Any) -> Result:
    """Turn an API Message into a Result; raise SummaryError when unusable."""
    stop = getattr(message, "stop_reason", None)
    usage = message.usage.model_dump() if getattr(message, "usage", None) else {}
    if stop == "refusal":
        raise SummaryError(f"refusal: {getattr(message, 'stop_details', None)}")
    if stop == "max_tokens":
        raise SummaryError(f"output truncated at max_tokens={DEFAULT_MAX_TOKENS}")
    text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise SummaryError(f"no text block (stop_reason={stop})")
    try:
        summary = WeekSummary.model_validate_json(text)
    except ValidationError as e:
        raise SummaryError(f"invalid JSON from model: {e}") from e
    return Result(summary=summary, usage=usage, stop_reason=stop, model=getattr(message, "model", ""))


def summarize_week(
    messages: list[dict],
    *,
    context: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    input_format: str = DEFAULT_INPUT_FORMAT,
    client: anthropic.Anthropic | None = None,
) -> Result:
    """Summarize one week synchronously, with server-side refusal fallback."""
    if not messages:
        return Result(WeekSummary(tldr="", top_topics=[], links=[]), {}, None, model)
    client = client or anthropic.Anthropic()
    params = build_request(messages, context=context, model=model, effort=effort, input_format=input_format)
    check_size(client, params)
    message = client.with_options(timeout=REQUEST_TIMEOUT_S).beta.messages.create(
        **params,
        # On a safety-classifier decline, re-run on Anthropic's recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},  # not a typed kwarg in this SDK version
    )
    return parse_message(message)
