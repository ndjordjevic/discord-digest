# discord-digest

Weekly markdown digests of Discord channels, each readable in 1–2 minutes.

**[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter)** (native CLI, Docker fallback) → parse → **Claude** (`claude-sonnet-5-5`, Message Batches API) → one markdown file per channel per ISO week.

Each digest (≤450 words, trimmed in code): a TL;DR, up to 3 expanded topics (📦 releases, ❓ open questions, 📎 files, 🔗 links, one thumbnail, "jump →" Discord link), an "Also this week" list of one-liners, and the remaining links grouped into code & tools / projects / articles & videos / other. Chit-chat is excluded.

## Setup

```bash
cp .env.example .env               # DISCORD_TOKEN, ANTHROPIC_API_KEY
uv sync
uv run digest install-exporter     # native DiscordChatExporter into tools/dce/ (no Docker needed)
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
digest run-all                    # THE weekly job: export all channels, one batch for all pending weeks, indexes
digest run <channel_id>           # export + summarize one channel (sync)
digest export <channel_id>        # export only
digest summarize <channel_id>     # summarize already-exported weeks
   [--week 2026-W19 ...]          #   …or specific weeks   [--batch] [--effort low|medium|high]
digest eval --generate <label>    # generate the golden weeks with given options and score them
```

**Weekly schedule:** `.github/workflows/weekly-digest.yml` runs `digest run-all` Mondays 08:00 UTC on GitHub Actions (works with your laptop off) and commits the new digests. Needs repo secrets `DISCORD_TOKEN` and `ANTHROPIC_API_KEY`; run it by hand from the Actions tab.

**What a run digests:** completed ISO weeks after the checkpoint in `digests/<server>/<channel>/latest.json`, plus any week listed there under `failed_weeks`. The still-running week is skipped (`--include-current` to override); empty weeks produce no file. Exports start 7 days before the first pending week so a conversation that runs across the week boundary is given to the model as background, and end at this Monday (UTC).

**Output:** `digests/<server>/<channel>/YYYY-Www.md` (folder names are lowercased slugs of the yaml labels). Images are downloaded (≤800px, one per expanded topic) into `digests/<server>/<channel>/assets/<week>/` because Discord attachment URLs expire. Raw JSON under `exports/`, token usage per digest in `logs/usage.jsonl` (both gitignored).

## Cost & quality

- One request per channel-week, input as one line per message (−23% cost vs. JSON in our eval), effort `low` (matched `high` in the eval), sent through the Message Batches API (−50%). Refused or failed batch items are retried synchronously with server-side refusal fallback.
- A busy ~100-message week costs about a cent or two; quiet weeks well under a cent.
- `evals/golden.json` holds hand-written key facts and "must not claim" traps for 10 weeks. `digest eval` has `claude-opus-5-5` judge recall, traps and unsupported claims, plus free checks (word budget, URLs and jump IDs exist, no duplicate links). Results land in `evals/results/`. Ship a cheaper variant only if recall stays within 5 points and no new traps/unsupported claims appear.

## Notes

- Uses a **user token**; Discord TOS forbids this and DCE users report account penalties in 2026. Exports run with `--parallel 1`; prefer a bot token if a server admin will invite one.
- Weeks over 500K input tokens raise `WeekTooLargeError` and are recorded as failed.
- Defaults (`DEFAULT_MODEL`, `DEFAULT_EFFORT`, `DEFAULT_INPUT_FORMAT`) live in `src/digest/summarize.py`; the word budget (`WORD_BUDGET`) in `src/digest/render.py`.
