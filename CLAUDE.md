# discord-digest — agent context

This repo contains weekly markdown digests of Discord channels and the raw JSON exports they're built from.

## Data layout

```
digests/<server>/<channel>/YYYY-Www.md   # one file per ISO week
digests/<server>/<channel>/latest.json   # checkpoint: last digested week + message ID
exports/<channel_id>.json                # full DiscordChatExporter dump (all messages)
channels.yaml                            # channel registry: server, channel_id, channel_name, since
```

Current channels: **rumbledethumps / chat** (`exports/1036721593763778641.json`, digests under `digests/rumbledethumps/chat/`).

## How to answer questions

**"What was discussed in week X / month Y / around topic Z?"**
→ Search digests first — they're fast, human-readable, and grep-friendly.
```bash
grep -ril "telnet" digests/
grep -l "2026-W1" digests/rumbledethumps/chat/
```
Then read the matching `.md` files.

**"Show me the exact message / quote / who said what"**
→ Go to the raw export JSON. Each message has `id`, `timestamp`, `author.name`, `content`, `attachments`, `embeds`, `reactions`.
```bash
# find messages mentioning a keyword
python3 -c "
import json
msgs = json.load(open('exports/1036721593763778641.json'))['messages']
for m in msgs:
    if 'telnet' in (m.get('content') or '').lower():
        print(m['timestamp'][:10], m['author']['name'], m['content'][:120])
"
```

**"Who talks most about X / who are the active participants?"**
→ Grep digest participant lines for names, or scan the export for author frequency on a filtered set.

**"What links were shared about topic X?"**
→ Search digest `## Links` sections, or scan `embeds[].url` / `content` in the export JSON.

## Digest format

Each `.md` file has up to three sections (omitted if empty for that week):
- `## Top topics` — substantive discussion threads with participants
- `## Announcements` — releases, decisions, news
- `## Links` — every non-CDN URL shared, with one-sentence context

Footer: `_Generated YYYY-MM-DD from N messages._`

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
