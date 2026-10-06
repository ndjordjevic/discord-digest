# discord-digest

Weekly markdown digests of Discord channels.

**[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter)** (Docker) → parse → **Claude** (`claude-sonnet-5-5`) → one markdown file per channel per ISO week.

Each digest: a TL;DR, top topics (releases marked 📦, open questions, attached files, images, a "jump to thread" Discord link), and links grouped into code & tools / projects / articles & videos / other. Chit-chat is excluded.

## Setup

```bash
cp .env.example .env   # DISCORD_TOKEN, ANTHROPIC_API_KEY
# Docker Desktop must be running for exports
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

Run via `uv run digest …` (or `.venv/bin/digest`). A bare `digest` may hit an unrelated system binary (e.g. Homebrew's NSS `digest`).

```bash
digest run <channel_id>           # export + summarize new weeks (the usual command)
digest export <channel_id>        # export only
digest summarize <channel_id>     # summarize already-exported weeks
   [--week 2026-W19]              #   …or just one week (handy when iterating on the prompt)
digest export <channel_id> --since 2026-01-01    # backfill, ignoring checkpoint
```

Re-runs resume from `digests/<server>/<channel>/latest.json` — no flags needed. Exports start 7 days before the checkpoint week, so a conversation that runs across the week boundary is given to the model as background.

Output: `digests/<server>/<channel>/YYYY-Www.md` (folder names are lowercased slugs of the yaml labels). Images are downloaded (downscaled to ≤1200px) into `digests/<server>/<channel>/assets/<week>/` because Discord attachment URLs expire. Raw JSON under `exports/` (gitignored).

## Notes

- Uses a **user token**; Discord TOS forbids this — keep volume low.
- Weeks over ~150K input tokens raise `WeekTooLargeError` and are skipped.
- Default model is `claude-sonnet-5-5` at effort `high`, with server-side refusal fallback; edit `DEFAULT_MODEL` / `DEFAULT_EFFORT` in `src/digest/summarize.py` to switch.
