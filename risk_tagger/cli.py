"""Legacy offline backtest CLI, retained only for benchmark reproduction.

Live inference uses ``risk-pr score-pr`` and the shared Jev engine.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

from .evaluate import (
    chronological_tail,
    evaluate,
    markdown_report,
    maturity_filter,
)
from .labeling import LabelConfig, assign_labels

DEFAULT_OUTCOMES = ("strong_outcome", "medium_outcome_strict")


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    opener = gzip.open if str(path).endswith(".gz") else open
    rows: List[Dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _config_from_args(args: argparse.Namespace) -> LabelConfig:
    cfg = LabelConfig()
    if args.high_cut is not None:
        cfg.high_percentile_cut = args.high_cut
    if args.low_cut is not None:
        cfg.low_percentile_cut = args.low_cut
    return cfg


def cmd_backtest(args: argparse.Namespace) -> None:
    rows = load_jsonl(args.input)
    matured = maturity_filter(rows, args.maturity_days)
    print(
        f"loaded {len(rows)} rows; {len(matured)} after {args.maturity_days}d maturity window",
        file=sys.stderr,
    )
    cfg = _config_from_args(args)
    labeled = assign_labels(matured, config=cfg, percentile_mode="as_of")
    outcomes = list(args.outcome or DEFAULT_OUTCOMES)

    report = evaluate(labeled, outcomes, merged_only=not args.include_unmerged)
    full_md = markdown_report(report, title="Risk Tagger Backtest — all matured rows")

    # Held-out chronological test slice (last fraction).
    test_rows = chronological_tail(labeled, args.test_fraction)
    test_report = evaluate(test_rows, outcomes, merged_only=not args.include_unmerged)
    test_md = markdown_report(
        test_report, title=f"Risk Tagger Backtest — last {int(args.test_fraction*100)}% (chronological test)"
    )

    out = full_md + "\n\n" + test_md
    print(out)
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "backtest.md").write_text(out, encoding="utf-8")
        (out_dir / "backtest.json").write_text(
            json.dumps({"all": report, "test": test_report}, indent=2), encoding="utf-8"
        )
        if args.write_labeled:
            with (out_dir / "labeled.jsonl").open("w", encoding="utf-8") as handle:
                for row in labeled:
                    slim = {
                        "repo": row.get("repo"),
                        "number": row.get("number"),
                        "title": row.get("title"),
                        "created_at": row.get("created_at"),
                        "risk": row.get("risk"),
                        "outcomes": row.get("outcomes"),
                    }
                    handle.write(json.dumps(slim) + "\n")
        print(f"wrote reports to {out_dir}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="risk-tagger")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="Backtest the labeler on a feature JSONL")
    bt.add_argument("--input", required=True, help="features.jsonl (or .gz)")
    bt.add_argument("--out", help="Output directory for reports")
    bt.add_argument("--outcome", action="append", help="Outcome name(s) to evaluate")
    bt.add_argument("--maturity-days", type=int, default=30)
    bt.add_argument("--test-fraction", type=float, default=0.1)
    bt.add_argument("--high-cut", type=float, default=None, help="High percentile cut override")
    bt.add_argument("--low-cut", type=float, default=None, help="Low (bypass) percentile cut override")
    bt.add_argument("--include-unmerged", action="store_true")
    bt.add_argument("--write-labeled", action="store_true", help="Write labeled.jsonl")
    bt.set_defaults(func=cmd_backtest)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
