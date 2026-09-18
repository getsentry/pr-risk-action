"""Run risk_tagger inference on the agent-labeled open-PR test set and compare.

We do NOT rebuild features: we read the already-extracted open-PR feature rows and run
``assign_labels`` (inference), ranking each repo's open PRs among themselves
(``percentile_mode='global'``) plus the global hard-signal floor. Then we join to the
ground-truth agent labels and report the confusion, with emphasis on the asymmetric cost
cell: agent=high but ours=low.

    PYTHONPATH=. python -m risk_tagger.compare
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List, Sequence

from .labeling import LabelConfig, assign_labels

LABELS = ("low", "medium", "high")
RANK = {l: i for i, l in enumerate(LABELS)}

AGENT_LABELS = "data/processed/open-pr-agent-labels-2026-06-24/agent_labels.jsonl"
FEATURE_FILES = {
    "getsentry/sentry": "data/processed/getsentry__sentry/open-pr-risk/open_pr_risk.jsonl",
    "getsentry/snuba": "data/processed/getsentry__snuba/open-pr-risk-open-only/open_pr_risk.jsonl",
}


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def infer(repo: str, feature_path: str, config: LabelConfig) -> Dict[int, Dict[str, Any]]:
    rows = [r for r in load_jsonl(feature_path) if r.get("repo") == repo or "repo" not in r]
    for r in rows:
        r.setdefault("repo", repo)
    labeled = assign_labels(rows, config=config, percentile_mode="global")
    return {int(r["number"]): r for r in labeled}


def confusion(pairs: Sequence[tuple]) -> Dict[str, Any]:
    """pairs of (agent_label, our_label)."""
    matrix = {a: Counter() for a in LABELS}
    exact = more = less = 0
    agent_high_our_low = 0
    agent_low_our_high = 0
    for agent, ours in pairs:
        if agent not in RANK or ours not in RANK:
            continue
        matrix[agent][ours] += 1
        if ours == agent:
            exact += 1
        elif RANK[ours] > RANK[agent]:
            more += 1
        else:
            less += 1
        if agent == "high" and ours == "low":
            agent_high_our_low += 1
        if agent == "low" and ours == "high":
            agent_low_our_high += 1
    n = sum(1 for a, o in pairs if a in RANK and o in RANK)
    agent_counts = Counter(a for a, _ in pairs)
    return {
        "n": n,
        "matrix": {a: dict(matrix[a]) for a in LABELS},
        "exact_agreement": round(exact / n, 4) if n else 0,
        "we_call_more_severe": more,
        "we_call_less_severe": less,
        "we_more_rate": round(more / n, 4) if n else 0,
        "we_less_rate": round(less / n, 4) if n else 0,
        "agent_high_our_low": agent_high_our_low,
        "agent_high_our_low_rate": round(agent_high_our_low / agent_counts.get("high", 1), 4),
        "agent_low_our_high": agent_low_our_high,
    }


def render(name: str, conf: Dict[str, Any]) -> str:
    lines = [f"## {name}  (n={conf['n']})", ""]
    lines.append("Confusion — rows = agent (ground truth), cols = risk_tagger:")
    lines.append("")
    lines.append(f"| agent \\ ours | low | medium | high | total |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for a in LABELS:
        row = conf["matrix"][a]
        tot = sum(row.values())
        lines.append(f"| **{a}** | {row.get('low',0)} | {row.get('medium',0)} | {row.get('high',0)} | {tot} |")
    lines += [
        "",
        f"- Exact agreement: **{conf['exact_agreement']:.1%}**",
        f"- We call *more* severe than agent: {conf['we_call_more_severe']} ({conf['we_more_rate']:.1%})",
        f"- We call *less* severe than agent: {conf['we_call_less_severe']} ({conf['we_less_rate']:.1%})",
        f"- **Dangerous cell — agent=high & ours=low: {conf['agent_high_our_low']} "
        f"({conf['agent_high_our_low_rate']:.1%} of agent-high)**",
        f"- agent=low & ours=high (we over-flag): {conf['agent_low_our_high']}",
        "",
    ]
    return "\n".join(lines)


# Open-PR feature rows per repo (already extracted; we only run inference).
# "open_from_raw": restrict to PR numbers that are state==open in this raw snapshot
#   (needed for cli, whose features.jsonl mixes merged + closed-unmerged + open).
# "all": the file is already open-only (sentry/snuba), so use every row.
OPEN_SETS = {
    "getsentry/sentry": {"features": "data/processed/getsentry__sentry/open-pr-risk/open_pr_risk.jsonl",
                         "mode": "all"},
    "getsentry/cli": {"features": "data/processed/getsentry__cli/features.jsonl",
                      "mode": "open_from_raw", "raw": "data/raw/getsentry__cli/prs.jsonl"},
    "getsentry/snuba": {"features": "data/processed/getsentry__snuba/open-pr-risk-open-only/open_pr_risk.jsonl",
                        "mode": "all"},
}


def _open_numbers(raw_path: str) -> set:
    return {int(json.loads(l)["number"]) for l in open(raw_path, encoding="utf-8")
            if l.strip() and json.loads(l).get("state") == "open"}


def _load_open_rows(spec: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = load_jsonl(spec["features"])
    if spec["mode"] == "open_from_raw":
        keep = _open_numbers(spec["raw"])
        rows = [r for r in rows if int(r.get("number") or -1) in keep]
    return rows


def by_repo(high_cut: float | None = None, low_cut: float | None = None) -> None:
    agent = {(d["repo"], int(d["number"])): d["agent_label"] for d in load_jsonl(AGENT_LABELS)}
    cfg = LabelConfig()
    if high_cut is not None:
        cfg.high_percentile_cut = high_cut
    if low_cut is not None:
        cfg.low_percentile_cut = low_cut

    out = ["# risk_tagger on open PRs — by repo", ""]
    out.append("| repo | open PRs | low | medium | high |")
    out.append("| --- | ---: | ---: | ---: | ---: |")
    detail: List[str] = []
    for repo, spec in OPEN_SETS.items():
        rows = _load_open_rows(spec)
        for r in rows:
            r.setdefault("repo", repo)
        labeled = assign_labels(rows, config=cfg, percentile_mode="global")
        dist = Counter(r["risk"]["label"] for r in labeled)
        n = len(labeled)
        out.append(f"| {repo} | {n} | {dist.get('low',0)} | {dist.get('medium',0)} | {dist.get('high',0)} |")

        pairs = [(agent[(repo, int(r['number']))], r["risk"]["label"])
                 for r in labeled if (repo, int(r["number"])) in agent]
        if pairs:
            conf = confusion(pairs)
            detail.append(render(f"{repo} vs agent labels", conf))
    print("\n".join(out))
    if detail:
        print("\n" + "\n".join(detail))


def main(high_cut: float | None = None, low_cut: float | None = None) -> None:
    agent = {(d["repo"], int(d["number"])): d["agent_label"] for d in load_jsonl(AGENT_LABELS)}
    cfg = LabelConfig()
    if high_cut is not None:
        cfg.high_percentile_cut = high_cut
    if low_cut is not None:
        cfg.low_percentile_cut = low_cut

    all_pairs: List[tuple] = []
    dangerous: List[Dict[str, Any]] = []
    out = ["# risk_tagger vs agent labels (open-PR test set)", ""]
    for repo, fpath in FEATURE_FILES.items():
        labeled = infer(repo, fpath, cfg)
        pairs = []
        our_dist = Counter()
        for (r, num), agent_label in agent.items():
            if r != repo or num not in labeled:
                continue
            ours = labeled[num]["risk"]["label"]
            our_dist[ours] += 1
            pairs.append((agent_label, ours))
            all_pairs.append((agent_label, ours))
            if agent_label == "high" and ours == "low":
                dangerous.append({"repo": repo, "number": num,
                                  "title": labeled[num].get("title"),
                                  "pct": labeled[num]["risk"]["risk_percentile_repo"]})
        out.append(render(repo, confusion(pairs)))
        out.append(f"_risk_tagger label mix on {repo}: {dict(our_dist)}_\n")

    out.append(render("COMBINED", confusion(all_pairs)))
    if dangerous:
        out.append("## Dangerous disagreements (agent=high, ours=low)\n")
        for d in dangerous:
            out.append(f"- {d['repo']}#{d['number']} (p{d['pct']:.0f}): {d['title']}")
    else:
        out.append("## Dangerous disagreements (agent=high, ours=low)\n\nNone.")
    report = "\n".join(out)
    print(report)


if __name__ == "__main__":
    main()
