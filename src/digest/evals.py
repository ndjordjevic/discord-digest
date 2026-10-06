"""`digest eval`: score digests against a hand-written golden set.

Per golden week, a judge model (different from the generator) checks binary
items against the digest and the source messages:
  - key-fact recall: is each golden fact stated in the digest?
  - traps: does the digest make any of the claims it must not make?
  - faithfulness: claims in the digest the source messages don't support.
Plus free deterministic checks: word budget, every URL and jump-link ID exists
in the export, no duplicate links.

Ship rule for a cheaper variant: recall within 5 points of the baseline, no
trap hits or unsupported claims beyond the baseline, all deterministic checks pass.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import anthropic
import click
from pydantic import BaseModel

from .render import WORD_BUDGET, digest_path, word_count
from .summarize import (
    DEFAULT_EFFORT,
    DEFAULT_INPUT_FORMAT,
    DEFAULT_MODEL,
    EFFORTS,
    INPUT_FORMATS,
    build_user_content,
)

JUDGE_MODEL = "claude-opus-5-5"
# $/MTok (input, output); batch halves both. Cache writes 1.25x input, reads 0.1x.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0), "claude-haiku-4-5": (1.0, 5.0)}

JUDGE_PROMPT = """You grade a weekly Discord channel digest against the channel's messages.

For each FACT, answer `present: true` only if the digest states it (paraphrase is fine; partial or vaguer statements are false).
For each TRAP, answer `claimed: true` if the digest makes that claim or clearly implies it.
Then list every claim in the digest that the messages in <this_week> do NOT support (wrong, invented, overstated, or generalized beyond what was said). Background messages may be used to identify something in a short clause (e.g. "the demo that won last week's compo") — that is allowed; flag only claims that present background events as this week's news. Ignore headings, participant lists and link titles that match previews. An empty list is normal.
Be strict and literal."""


class FactVerdict(BaseModel):
    id: int
    reasoning: str
    present: bool


class TrapVerdict(BaseModel):
    id: int
    reasoning: str
    claimed: bool


class Judgement(BaseModel):
    facts: list[FactVerdict]
    traps: list[TrapVerdict]
    unsupported_claims: list[str]


def _judge(client: anthropic.Anthropic, digest_md: str, source: str, facts: list[str], traps: list[str]) -> Judgement:
    items = "\n".join(f"FACT {i}: {f}" for i, f in enumerate(facts)) or "(no facts)"
    items += "\n" + ("\n".join(f"TRAP {i}: {t}" for i, t in enumerate(traps)) or "(no traps)")
    msg = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": anthropic.transform_schema(Judgement)}},
        system=JUDGE_PROMPT,
        messages=[{"role": "user", "content": f"{source}\n\n<digest>\n{digest_md}\n</digest>\n\n{items}"}],
    )
    text = next(b.text for b in msg.content if b.type == "text")
    return Judgement.model_validate_json(text)


_URL_RE = re.compile(r"\]\((https?://[^)\s]+)\)")
_JUMP_RE = re.compile(r"discord\.com/channels/\d+/\d+/(\d+)")


def _checks(md: str, export_text: str, export_ids: set[str]) -> dict:
    urls = [u for u in _URL_RE.findall(md) if "discord.com/channels/" not in u]
    jumps = _JUMP_RE.findall(md)
    head, _, links_section = md.partition("## Links")
    listed = _URL_RE.findall(links_section)
    topic_urls = set(_URL_RE.findall(head))
    words = word_count(md)
    return {
        "words": words,
        "over_budget": words > WORD_BUDGET,
        "unknown_urls": [u for u in urls if u not in export_text],
        "unknown_jump_ids": [j for j in jumps if j not in export_ids],
        "duplicate_links": sorted({u for u, n in Counter(listed).items() if n > 1} | (set(listed) & topic_urls)),
    }


def _cost(root_label: str, logs: Path) -> tuple[float, int]:
    """Estimated generation cost of a run, from logs/usage.jsonl rows with that out_root."""
    total, n = 0.0, 0
    if not logs.exists():
        return 0.0, 0
    for line in logs.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("out_root") != root_label:
            continue
        model = r.get("model") or ""
        pin, pout = next((v for k, v in PRICES.items() if model.startswith(k)), PRICES["claude-sonnet-5-5"])
        u = r.get("usage") or {}
        cost = (
            (u.get("input_tokens") or 0) * pin
            + (u.get("cache_creation_input_tokens") or 0) * pin * 1.25
            + (u.get("cache_read_input_tokens") or 0) * pin * 0.1
            + (u.get("output_tokens") or 0) * pout
        ) / 1e6
        total += cost / 2 if r.get("mode") == "batch" else cost
        n += 1
    return total, n


@click.command("eval")
@click.option("--root", type=click.Path(path_type=Path), default=None, help="Digest tree to score (default: digests/, or evals/runs/<label> with --generate).")
@click.option("--generate", "label", default=None, help="First generate the golden weeks into evals/runs/<label>/.")
@click.option("--batch/--sync", "use_batch", default=False, show_default=True)
@click.option("--effort", type=click.Choice(EFFORTS), default=DEFAULT_EFFORT, show_default=True)
@click.option("--input-format", type=click.Choice(INPUT_FORMATS), default=DEFAULT_INPUT_FORMAT, show_default=True)
@click.option("--model", default=DEFAULT_MODEL, show_default=True)
@click.option("--week", "only_weeks", multiple=True, help="Limit to these golden weeks.")
def eval_cmd(root, label, use_batch, effort, input_format, model, only_weeks) -> None:
    """Score digests against evals/golden.json (judge: a different, stronger model)."""
    from .cli import DIGESTS_DIR, REPO_ROOT, _load_jobs, _summarize_jobs

    golden = json.loads((REPO_ROOT / "evals" / "golden.json").read_text(encoding="utf-8"))
    cid = golden["channel_id"]
    weeks = [w for w in sorted(golden["weeks"]) if not only_weeks or w in only_weeks]

    jobs = {j.week: j for j in _load_jobs(cid, weeks, include_current=True)}
    if label:
        root = REPO_ROOT / "evals" / "runs" / label
        _summarize_jobs(list(jobs.values()), use_batch=use_batch, out_root=root, effort=effort, input_format=input_format, model=model)
    root = root or DIGESTS_DIR

    export_text = (REPO_ROOT / "exports" / f"{cid}.json").read_text(encoding="utf-8")
    export_ids = set(re.findall(r'"id": "(\d+)"', export_text))
    client = anthropic.Anthropic(max_retries=8)
    rows, n_facts, n_hit = [], 0, 0
    for w in weeks:
        g = golden["weeks"][w]
        job = jobs.get(w)
        md_path = digest_path(root, job.cfg["server"], job.cfg["channel_name"], w) if job else None
        md = md_path.read_text(encoding="utf-8") if md_path and md_path.exists() else ""
        if not md:
            judgement = Judgement(facts=[FactVerdict(id=i, reasoning="no digest", present=False) for i in range(len(g["facts"]))], traps=[], unsupported_claims=[])
        else:
            source = build_user_content(job.msgs, job.context, "lines") if job else "<this_week>\n</this_week>"
            judgement = _judge(client, md, source, g["facts"], g["traps"])
        # One verdict per id (a judge may repeat an id); unanswered facts count as missed.
        present = {v.id: v.present for v in judgement.facts if 0 <= v.id < len(g["facts"])}
        claimed = {v.id: v.claimed for v in judgement.traps if 0 <= v.id < len(g["traps"])}
        hits = sum(present.values())
        traps = [g["traps"][i] for i, c in claimed.items() if c]
        checks = _checks(md, export_text, export_ids) if md else {"words": 0, "over_budget": False, "unknown_urls": [], "unknown_jump_ids": [], "duplicate_links": []}
        n_facts += len(g["facts"])
        n_hit += hits
        rows.append({
            "week": w, "recall": f"{hits}/{len(g['facts'])}",
            "missed": [f for i, f in enumerate(g["facts"]) if not present.get(i)],
            "traps": traps, "unsupported": judgement.unsupported_claims, **checks,
        })

    label_path = str(root.relative_to(REPO_ROOT)) if root.is_relative_to(REPO_ROOT) else str(root)
    cost, n_calls = _cost(label_path, REPO_ROOT / "logs" / "usage.jsonl")
    summary = {
        "root": label_path,
        "recall": round(100 * n_hit / max(n_facts, 1), 1),
        "trap_hits": sum(len(r["traps"]) for r in rows),
        "unsupported": sum(len(r["unsupported"]) for r in rows),
        "words_total": sum(r["words"] for r in rows),
        "over_budget_weeks": [r["week"] for r in rows if r["over_budget"]],
        "check_failures": sum(len(r["unknown_urls"]) + len(r["unknown_jump_ids"]) + len(r["duplicate_links"]) for r in rows),
        "generation_cost_usd": round(cost, 4) if n_calls else None,
    }
    out = REPO_ROOT / "evals" / "results" / f"{label or root.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, json.dumps({"summary": summary, "weeks": rows}, indent=2, ensure_ascii=False))

    for r in rows:
        flags = []
        if r["missed"]:
            flags.append("missed: " + "; ".join(r["missed"]))
        if r["traps"]:
            flags.append("TRAP: " + "; ".join(r["traps"]))
        if r["unsupported"]:
            flags.append("unsupported: " + "; ".join(r["unsupported"]))
        for k in ("unknown_urls", "unknown_jump_ids", "duplicate_links"):
            if r[k]:
                flags.append(f"{k}: {r[k]}")
        click.echo(f"{r['week']}  recall {r['recall']:>5}  {r['words']:>4} words" + ("".join(f"\n    - {f}" for f in flags)))
    click.echo(json.dumps(summary, indent=2))
    click.echo(f"saved {out.relative_to(REPO_ROOT)}")
