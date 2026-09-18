"""Diagnostic: per-signal frequency and outcome lift, to calibrate the policy.

    python -m risk_tagger.analyze data/processed/expanded-2020/features.jsonl

For each signal we report how often it fires and the outcome lift it carries
(P(positive | signal) / base rate). Rare + high-lift signals justify forcing `high`;
common or low-lift signals should only block the `low` (bypass) bucket.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from typing import Any, Dict, List

from .cli import load_jsonl
from .evaluate import maturity_filter
from .labeling import LabelConfig, assign_labels


def main(path: str, outcome: str = "medium_outcome_strict", maturity_days: int = 30) -> None:
    rows = maturity_filter(load_jsonl(path), maturity_days)
    labeled = assign_labels(rows, config=LabelConfig(), percentile_mode="as_of")
    merged = [r for r in labeled if (r.get("outcomes") or {}).get("is_merged")]
    total = len(merged)
    positives = sum(1 for r in merged if (r.get("outcomes") or {}).get(outcome))
    base = positives / total if total else 0.0

    fires: Dict[str, int] = defaultdict(int)
    pos_fires: Dict[str, int] = defaultdict(int)
    severity: Dict[str, str] = {}
    for r in merged:
        is_pos = bool((r.get("outcomes") or {}).get(outcome))
        seen = set()
        for sig in (r.get("risk") or {}).get("signals", []):
            name = sig["name"]
            severity[name] = sig.get("severity", "")
            if name in seen:
                continue
            seen.add(name)
            fires[name] += 1
            if is_pos:
                pos_fires[name] += 1

    print(f"rows={total} positives={positives} base_rate={base:.4f} outcome={outcome}\n")
    print(f"{'signal':32} {'sev':9} {'fires':>7} {'freq%':>7} {'pos':>5} {'P(pos|sig)':>11} {'lift':>6}")
    rows_out: List[tuple] = []
    for name, count in fires.items():
        p = pos_fires[name] / count if count else 0.0
        lift = p / base if base else 0.0
        rows_out.append((lift, name, count, severity.get(name, "")))
        print(f"{name:32} {severity.get(name,''):9} {count:7d} {100*count/total:7.2f} "
              f"{pos_fires[name]:5d} {p:11.4f} {lift:6.2f}")


if __name__ == "__main__":
    args = sys.argv[1:]
    path = args[0] if args else "data/processed/expanded-2020/features.jsonl"
    main(path)
