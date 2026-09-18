"""Self-contained percentile helpers (stdlib only).

We re-implement ``as_of`` per-repo percentiles here rather than importing from
``risk_pr_agent.scoring`` so this package stays decoupled from a concurrently-edited file.

``as_of`` means each PR is ranked only against *prior* PRs in the *same repo* (by
``created_at``), so we never use future distribution data when simulating thresholds.
PRs sharing an identical timestamp do not rank against each other (no peek at peers).
"""

from __future__ import annotations

from bisect import bisect_right, insort
from typing import Any, Callable, Dict, List, Sequence


def percentile_rank(value: float, sorted_values: Sequence[float]) -> float:
    if not sorted_values:
        return 0.0
    below_or_equal = bisect_right(sorted_values, value)
    return 100.0 * below_or_equal / len(sorted_values)


def as_of_percentiles(
    rows: Sequence[Dict[str, Any]], values: Sequence[float]
) -> List[float]:
    """Per-repo percentile of each value against prior same-repo values only."""

    result = [0.0] * len(rows)
    order = sorted(
        range(len(rows)),
        key=lambda i: (
            rows[i].get("created_at") or "",
            rows[i].get("repo") or "",
            int(rows[i].get("number") or 0),
        ),
    )
    prior_by_repo: Dict[str, List[float]] = {}
    i = 0
    while i < len(order):
        repo = rows[order[i]].get("repo") or ""
        created_at = rows[order[i]].get("created_at") or ""
        group: List[int] = []
        while i < len(order):
            idx = order[i]
            if (rows[idx].get("repo") or "", rows[idx].get("created_at") or "") != (
                repo,
                created_at,
            ):
                break
            group.append(idx)
            i += 1
        prior = prior_by_repo.setdefault(repo, [])
        for idx in group:
            result[idx] = percentile_rank(float(values[idx]), prior)
        for idx in group:
            insort(prior, float(values[idx]))
    return result


def global_percentiles(
    rows: Sequence[Dict[str, Any]], values: Sequence[float]
) -> List[float]:
    """Per-repo percentile using the full repo distribution (for live one-shot scoring).

    Used by ``run_open`` where the "current" PR is ranked against the whole recent
    historical distribution of its repo rather than only chronologically-prior rows.
    """

    by_repo: Dict[str, List[float]] = {}
    for row, value in zip(rows, values):
        by_repo.setdefault(row.get("repo") or "", []).append(float(value))
    sorted_by_repo = {repo: sorted(vals) for repo, vals in by_repo.items()}
    return [
        percentile_rank(float(value), sorted_by_repo[row.get("repo") or ""])
        for row, value in zip(rows, values)
    ]


def feature_percentiles(
    rows: Sequence[Dict[str, Any]],
    feature_name: str,
    mode: str = "as_of",
) -> List[float]:
    values = [_safe(row.get("prediction_features", {}).get(feature_name)) for row in rows]
    if mode == "as_of":
        return as_of_percentiles(rows, values)
    if mode == "global":
        return global_percentiles(rows, values)
    raise ValueError(f"unknown percentile mode: {mode}")


def _safe(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
