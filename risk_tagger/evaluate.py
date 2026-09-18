"""Backtest metrics for the risk tagger, framed around the asymmetric cost.

Primary (safety) metric: **leakage** — the fraction of bad-outcome PRs that got labeled
``low`` (bypass-eligible). We want this near zero. Secondary: ``high``-label capture and
Recall@Top-k of the continuous score, plus label distribution (to confirm ``low``/``high``
are genuinely small tails).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Sequence

TOP_KS = (5, 10, 30)


def _is_positive(row: Dict[str, Any], outcome_name: str) -> bool:
    return bool((row.get("outcomes") or {}).get(outcome_name))


def label_distribution(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {"low": 0, "medium": 0, "high": 0}
    for row in rows:
        label = (row.get("risk") or {}).get("label")
        if label in counts:
            counts[label] += 1
    total = sum(counts.values()) or 1
    return {
        "counts": counts,
        "share": {k: round(100 * v / total, 2) for k, v in counts.items()},
        "total": sum(counts.values()),
    }


def safety_metrics(rows: Sequence[Dict[str, Any]], outcome_name: str) -> Dict[str, Any]:
    """Where do the bad-outcome PRs land? Leakage into `low` is the dangerous miss."""

    by_label = {"low": 0, "medium": 0, "high": 0}
    positives = 0
    low_total = 0
    for row in rows:
        label = (row.get("risk") or {}).get("label")
        if label == "low":
            low_total += 1
        if _is_positive(row, outcome_name):
            positives += 1
            if label in by_label:
                by_label[label] += 1
    leaked = by_label["low"]
    return {
        "outcome_name": outcome_name,
        "positive_outcomes": positives,
        "positives_by_label": by_label,
        "leakage_into_low": leaked,
        "leakage_rate": round(leaked / positives, 4) if positives else 0.0,
        "non_low_recall": round((positives - leaked) / positives, 4) if positives else 0.0,
        "high_label_recall": round(by_label["high"] / positives, 4) if positives else 0.0,
        "low_bucket_size": low_total,
        "low_bucket_positive_rate": round(leaked / low_total, 4) if low_total else 0.0,
        "base_rate": round(positives / (len(rows) or 1), 4),
    }


def recall_at_topk(rows: Sequence[Dict[str, Any]], outcome_name: str) -> Dict[str, Any]:
    """Rank by continuous risk percentile and report Recall@Top-k vs random."""

    ranked = sorted(
        rows,
        key=lambda r: (r.get("risk") or {}).get("risk_percentile_repo") or 0.0,
        reverse=True,
    )
    positives = sum(1 for r in rows if _is_positive(r, outcome_name))
    total = len(ranked)
    out: Dict[str, Any] = {}
    for k in TOP_KS:
        size = max(1, round(total * k / 100))
        bucket = ranked[:size]
        tp = sum(1 for r in bucket if _is_positive(r, outcome_name))
        recall = tp / positives if positives else 0.0
        out[f"top_{k}"] = {
            "bucket_size": size,
            "true_positives": tp,
            "recall": round(recall, 4),
            "precision": round(tp / size, 4) if size else 0.0,
            "lift_over_random": round(recall / (k / 100), 3) if positives else 0.0,
        }
    return out


def maturity_filter(rows: Sequence[Dict[str, Any]], maturity_days: int) -> List[Dict[str, Any]]:
    """Drop the newest rows that have not had time to be reverted/fixed yet."""

    if maturity_days <= 0:
        return list(rows)
    times = [_parse_time(r.get("created_at")) for r in rows]
    times = [t for t in times if t]
    if not times:
        return list(rows)
    cutoff = max(times) - timedelta(days=maturity_days)
    return [r for r, t in zip(rows, times) if t and t <= cutoff]


def chronological_tail(rows: Sequence[Dict[str, Any]], fraction: float) -> List[Dict[str, Any]]:
    ordered = sorted(rows, key=lambda r: (r.get("created_at") or "", int(r.get("number") or 0)))
    cut = int(len(ordered) * (1 - fraction))
    return ordered[cut:]


def evaluate(
    labeled_rows: Sequence[Dict[str, Any]],
    outcome_names: Sequence[str],
    merged_only: bool = True,
) -> Dict[str, Any]:
    rows = [r for r in labeled_rows if not merged_only or (r.get("outcomes") or {}).get("is_merged")]
    report: Dict[str, Any] = {
        "evaluated_rows": len(rows),
        "label_distribution": label_distribution(rows),
        "outcomes": {},
    }
    for outcome_name in outcome_names:
        report["outcomes"][outcome_name] = {
            "safety": safety_metrics(rows, outcome_name),
            "recall_at_topk": recall_at_topk(rows, outcome_name),
        }
    return report


def markdown_report(report: Dict[str, Any], title: str = "Risk Tagger Backtest") -> str:
    dist = report["label_distribution"]
    lines = [
        f"# {title}",
        "",
        f"- Evaluated rows: {report['evaluated_rows']}",
        f"- Label distribution: "
        f"low {dist['counts']['low']} ({dist['share']['low']}%), "
        f"medium {dist['counts']['medium']} ({dist['share']['medium']}%), "
        f"high {dist['counts']['high']} ({dist['share']['high']}%)",
        "",
    ]
    for outcome_name, block in report["outcomes"].items():
        s = block["safety"]
        lines += [
            f"## {outcome_name}",
            "",
            f"- Positives: {s['positive_outcomes']} (base rate {s['base_rate']})",
            f"- **Leakage into low (the dangerous miss): {s['leakage_into_low']} "
            f"({s['leakage_rate']:.4f})**",
            f"- Non-low recall: {s['non_low_recall']:.4f}",
            f"- High-label recall: {s['high_label_recall']:.4f}",
            f"- Positives by label: {s['positives_by_label']}",
            f"- Low bucket size: {s['low_bucket_size']}, positive rate inside low: "
            f"{s['low_bucket_positive_rate']:.4f} (vs base {s['base_rate']})",
            "",
            "| Bucket | Size | TP | Recall | Precision | Lift |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for bucket, v in block["recall_at_topk"].items():
            lines.append(
                f"| {bucket} | {v['bucket_size']} | {v['true_positives']} | "
                f"{v['recall']:.4f} | {v['precision']:.4f} | {v['lift_over_random']:.3f} |"
            )
        lines.append("")
    return "\n".join(lines)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
