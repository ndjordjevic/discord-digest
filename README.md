# discord-digest

Weekly markdown digests of Discord channels.

**[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter)** (Docker) → parse → **Claude** (`claude-opus-4-7`) → one markdown file per channel per ISO week.

Sections per digest: top topics, announcements, links.

## Setup

```bash
cp .env.example .env   # DISCORD_TOKEN, ANTHROPIC_API_KEY
docker pull tyrrrz/discordchatexporter:stable
uv sync
```

Add channels to `channels.yaml`:

```yaml
channels:
  - server: Rumbledethumps           # folder label
    channel_id: "1036721593763778641"
    channel_name: chat               # folder label
    since: 2026-05-01                # only for first export
```

Get the channel ID by right-clicking a channel → **Copy Channel Link** — second number in the URL.

## Usage

```bash
digest run <channel_id>           # export + summarize new weeks (the usual command)
digest export <channel_id>        # export only
digest summarize <channel_id>     # summarize already-exported weeks
   [--week 2026-W19]              #   …or just one week (handy when iterating on the prompt)
digest export <channel_id> --since 2026-01-01    # backfill, ignoring checkpoint
```

Re-runs resume from `digests/<server>/<channel>/latest.json` — no flags needed.

Output: `digests/<server>/<channel>/YYYY-Www.md`. Raw JSON under `exports/` (gitignored).

## Notes

- Uses a **user token**; Discord TOS forbids this — keep volume low.
- Weeks over ~150K input tokens raise `WeekTooLargeError` and are skipped.
- Default model is `claude-opus-4-7`; edit `DEFAULT_MODEL` in `src/digest/summarize.py` to switch.
