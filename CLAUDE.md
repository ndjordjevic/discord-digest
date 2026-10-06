# discord-digest — agent context

This repo contains weekly markdown digests of Discord channels and the raw JSON exports they're built from.

## Data layout

```
digests/<server>/<channel>/YYYY-Www.md   # one file per ISO week
digests/<server>/<channel>/latest.json   # checkpoint: last digested week + message ID + failed_weeks to retry
digests/<server>/<channel>/assets/<week>/ # images shown in that week's digest
exports/<channel_id>.json                # DiscordChatExporter dump from the LAST export run (overwritten each run; spans 7 days before the first pending week up to this Monday UTC; gitignored)
channels.yaml                            # channel registry: server, channel_id, channel_name, since
evals/golden.json, evals/results/        # hand-written key facts per week + eval scores
logs/usage.jsonl                         # token usage per generated digest (gitignored)
```

Current channels:
- **apolloteam** / democoding, announcements, amiga-events, english, cpu-design-talk, music, a6000, v2-series-legacy, v4-series-the-best, apolloboot, apolloos, games, compatibility-campaign, atari, maggie3d, developers, videoroom (digests under `digests/apolloteam/<channel>/`); the old **rumbledethumps / chat** digests stay but that channel is paused; raw exports are `exports/<channel_id>.json`

Server/channel folder names are lowercased slugs of the `channels.yaml` labels.

## How to answer questions

**"What was discussed in week X / month Y / around topic Z?"**
→ Search digests first — they're fast, human-readable, and grep-friendly.
```bash
grep -ril "telnet" digests/
grep -l "2026-W1" digests/rumbledethumps/chat/
```
Then read the matching `.md` files.

**"Show me the exact message / quote / who said what"**
→ Go to the raw export JSON (only covers messages since the last export's start date — older weeks may exist only as digests). Each message has `id`, `timestamp`, `author.name`, `content`, `attachments`, `embeds`, `reactions`.
```bash
# find messages mentioning a keyword
python3 -c "
import json
msgs = json.load(open('exports/1036721593763778641.json'))['messages']  # swap in the channel ID
for m in msgs:
    if 'telnet' in (m.get('content') or '').lower():
        print(m['timestamp'][:10], m['author']['name'], m['content'][:120])
"
```

**"Who talks most about X / who are the active participants?"**
→ Grep digest participant lines for names, or scan the export for author frequency on a filtered set.

**"What links were shared about topic X?"**
→ Search digests for `🔗` (links attached to a topic) and the `## Links` sections (remaining links, grouped; GitHub, tools etc. under **Code & tools**), or scan `embeds[].url` / `content` in the export JSON.

## Digest format

Each `.md` file is capped at ~450 words (1–2 minute read) and has (sections omitted if empty):
- `> **TL;DR** — …` — the week in ≤45 words
- `## Top topics` — up to 3 `###` topics, most important first: a one-sentence summary, ≤3 key-point bullets (bug reports, causes, fixes, tips, numbers), optional `❓ **Open:**` line, `📎` downloadable files and `🔗` links of that topic, one thumbnail, and a participants line with a Discord `jump →` link. `📦` marks releases.
- `## Also this week` — remaining topics as one-line headlines (with their files/links and jump link)
- `## Links` — links not already shown in a topic, grouped under **Code & tools / Projects & releases / Articles & videos / Other**

Footer: `_Generated YYYY-MM-DD from N messages._`

Images referenced by a digest live in `digests/<server>/<channel>/assets/<week>/` (≤800px). Discord CDN links expire, so downloads (zips etc.) are only listed by name with a jump link.

Older digests (rumbledethumps, generated before 2026-10-06) use an earlier format: bulleted topics, a separate `## Announcements` section, flat link list, no images or jump links.

When summarizing a week, the tool also feeds the model up to 30 messages from the 48h before the week started (plus anything an in-week reply points to) as background, so a conversation that spans the week boundary is understood. Those messages aren't summarized in that week.

## Raw export schema (key fields per message)

```json
{
  "id": "snowflake string",
  "timestamp": "ISO 8601",
  "author": { "name": "display name", "isBot": false },
  "content": "message text",
  "attachments": [{ "url": "...", "fileName": "...", "fileSizeBytes": 0 }],
  "embeds": [{ "url": "...", "title": "...", "description": "..." }],
  "reactions": [{ "emoji": { "name": "👍" }, "count": 3 }],
  "isPinned": false,
  "type": "Default | Reply | ChannelPinnedMessage"
}
```
