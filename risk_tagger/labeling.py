"""From-zero, no-LLM, recall-first PR risk labeling.

Produces `low | medium | high` for each PR feature row. Output is attached under a
dedicated ``row["risk"]`` key so it never collides with the existing ``row["prediction"]``.

Policy (two tails on one continuous score, with a hard floor):

    composite_pct = per-repo percentile of a weighted, explainable risk score
    severe        = count of severe hard-high signals
    elevated      = count of elevated hard-high signals

    high   if  severe >= 1  or  elevated >= 2  or  composite_pct >= high_cut
    low    if  not high  and  severe == 0  and  elevated == 0
                          and  composite_pct <= low_cut  and  is_safe_scope(...)
    medium otherwise

`low` means "bypass-eligible" (safe enough to skip extra review), so the costly error is a
risky PR landing in `low`. Any hard-high signal therefore *forbids* `low` — the recall
guarantee against high->low. All thresholds are global (no per-repo rules); only the
percentile calibration is per-repo and data-driven.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from .percentiles import feature_percentiles

# Raw features whose per-repo percentile feeds the continuous score / signals.
_PERCENTILE_FEATURES = (
    "changed_lines",
    "file_count",
    "directory_count",
    "max_file_churn_ratio",
    "sum_churn_over_base_sloc",
    "max_file_prior_bad_outcomes",
    "max_dir_prior_bad_outcomes",
)


@dataclass
class LabelConfig:
    # Band cut points on the composite per-repo percentile (recall-first / conservative).
    high_percentile_cut: float = 90.0   # top ~10% by score -> high
    low_percentile_cut: float = 40.0    # must be in the safe bottom ~40% to be bypass-eligible
    pileup_high_count: int = 5          # this many gating signals at once -> high (rank carries the rest)

    # Signal thresholds (global constants, not per-repo).
    history_bad_percentile: float = 90.0   # "historically unstable area" = top decile of prior bad outcomes
    churn_percentile: float = 90.0         # unusually large relative churn
    broad_dir_count: int = 3               # crosses several directories
    broad_blast_file_pct: float = 90.0     # public-API change with broad file blast radius
    low_familiarity_min_files: int = 3     # min files for a low-familiarity change to count
    low_familiarity_min_size_pct: float = 50.0
    dependency_bot_max_files: int = 5      # narrow bot patch eligible for bypass

    # Continuous-score weights for percentile families (each component is 0..100).
    w_changed_lines: float = 0.40
    w_file_count: float = 0.20
    w_directory_count: float = 0.10
    w_file_churn: float = 0.15
    w_churn_sloc: float = 0.15
    history_weight: float = 0.50           # weight on max(prior-bad-outcome percentile)

    # Additive change-type points (raw, re-ranked to percentile afterwards).
    # Weights reflect measured outcome lift on Sentry history (high-lift -> higher points;
    # auth/code-without-test measured below base rate, so kept small).
    change_type_points: Dict[str, float] = field(
        default_factory=lambda: {
            "ci_or_deploy_changed": 20.0,        # lift ~3.45
            "public_api_changed": 16.0,          # broad-blast variant lift ~3.15
            "migration_changed": 16.0,           # +app code lift ~1.91
            "touches_data_deletion": 14.0,       # destructive, rare
            "lockfile_changed": 6.0,
            "touches_error_handling": 5.0,
            "touches_async_or_concurrency": 5.0,
            "touches_serialization": 4.0,
            "auth_or_permission_changed": 4.0,   # lift ~0.67 for reverts; impact-only
            "code_changed_without_test_signal": 3.0,  # lift ~0.71
        }
    )
    # Safe-lowering points subtracted from the raw score (ranking only).
    safe_lowering_points: Dict[str, float] = field(
        default_factory=lambda: {
            "docs_only": 45.0,
            "generated_only": 45.0,
            "comment_only": 30.0,
            "whitespace_only": 30.0,
            "tests_only": 25.0,
        }
    )


def assign_labels(
    rows: Sequence[Dict[str, Any]],
    config: LabelConfig | None = None,
    percentile_mode: str = "as_of",
) -> List[Dict[str, Any]]:
    """Label a batch of feature rows. Returns new row dicts with a ``risk`` field.

    ``percentile_mode='as_of'`` for backtests (rank vs prior same-repo PRs);
    ``'global'`` for live scoring of currently-open PRs against repo history.
    """

    cfg = config or LabelConfig()
    enriched = [dict(row) for row in rows]

    # 1. Per-repo percentile for each raw feature we need.
    # For count/ratio features a raw value of 0 means "no risk"; force its percentile to 0
    # so a zero never ranks high just because many peers are also zero (bisect_right tie).
    pct_by_feature: Dict[str, List[float]] = {}
    for name in _PERCENTILE_FEATURES:
        pcts = feature_percentiles(enriched, name, mode=percentile_mode)
        for j, row in enumerate(enriched):
            if _feature_value(row, name) <= 0:
                pcts[j] = 0.0
        pct_by_feature[name] = pcts

    # 2. Continuous risk score from those percentiles + change-type points.
    raw_scores: List[float] = []
    per_row_pcts: List[Dict[str, float]] = []
    for i, row in enumerate(enriched):
        pcts = {name: pct_by_feature[name][i] for name in _PERCENTILE_FEATURES}
        per_row_pcts.append(pcts)
        raw_scores.append(_composite_raw_score(row["prediction_features"], pcts, cfg))

    # 3. Re-rank the composite raw score into a per-repo percentile for banding.
    composite_pcts = _percentiles_of_values(enriched, raw_scores, percentile_mode)

    # 4. Signals + label per row.
    for i, row in enumerate(enriched):
        features = row["prediction_features"]
        pcts = per_row_pcts[i]
        gating = _gating_signals(features, pcts, cfg)
        composite_pct = composite_pcts[i]
        label = _decide_label(gating, composite_pct, features, cfg)
        informational = _informational_signals(features, pcts, cfg)
        forcing = [s for s in gating if s.get("force_high")]
        row["risk"] = {
            "label": label,
            "risk_score_raw": round(raw_scores[i], 3),
            "risk_percentile_repo": round(composite_pct, 3),
            "percentile_mode": percentile_mode,
            "bypass_eligible": label == "low",
            "gating_signal_count": len(gating),
            "high_forcing_signal_count": len(forcing),
            "signals": gating + informational,
            "feature_percentiles": {k: round(v, 1) for k, v in pcts.items()},
        }
    return enriched


def label_row(
    rows: Sequence[Dict[str, Any]],
    number: int,
    repo: str,
    config: LabelConfig | None = None,
    percentile_mode: str = "global",
) -> Dict[str, Any]:
    """Label a batch and return the single row for ``repo``/``number`` (live scoring)."""

    labeled = assign_labels(rows, config=config, percentile_mode=percentile_mode)
    for row in labeled:
        if row.get("repo") == repo and int(row.get("number") or 0) == number:
            return row
    raise ValueError(f"no labeled row for {repo}#{number}")


# --- scoring -------------------------------------------------------------------------


def _composite_raw_score(
    features: Dict[str, Any], pcts: Dict[str, float], cfg: LabelConfig
) -> float:
    score = (
        cfg.w_changed_lines * pcts["changed_lines"]
        + cfg.w_file_count * pcts["file_count"]
        + cfg.w_directory_count * pcts["directory_count"]
        + cfg.w_file_churn * pcts["max_file_churn_ratio"]
        + cfg.w_churn_sloc * pcts["sum_churn_over_base_sloc"]
    )
    score += cfg.history_weight * max(
        pcts["max_file_prior_bad_outcomes"], pcts["max_dir_prior_bad_outcomes"]
    )
    for name, points in cfg.change_type_points.items():
        if features.get(name):
            score += points
    for name, points in cfg.safe_lowering_points.items():
        if features.get(name):
            score -= points
    if _truthy(features.get("source_type") == "dependency_bot"):
        score -= 6.0
    return max(score, 0.0)


def _percentiles_of_values(rows, values, mode):
    # Local shim: build a temporary feature on a copy so we can reuse feature_percentiles.
    tmp = []
    for row, value in zip(rows, values):
        clone = dict(row)
        clone_features = dict(clone.get("prediction_features") or {})
        clone_features["__composite__"] = value
        clone["prediction_features"] = clone_features
        tmp.append(clone)
    return feature_percentiles(tmp, "__composite__", mode=mode)


# --- signals -------------------------------------------------------------------------


def _gating_signals(
    features: Dict[str, Any], pcts: Dict[str, float], cfg: LabelConfig
) -> List[Dict[str, Any]]:
    """Risk signals that forbid the bypass (`low`) label.

    ``force_high=True`` signals are rare and empirically high-lift (calibrated on Sentry
    history), so any one of them forces `high`. The rest only block `low` and contribute to
    the pile-up count (>=3 gating signals also forces `high`).
    """

    out: List[Dict[str, Any]] = []
    file_count = _num(features.get("file_count"))
    migration_files = _num(features.get("migration_file_count"))

    # --- force_high: rare + high outcome lift ---
    if features.get("ci_or_deploy_changed"):
        out.append(_sig("ci_or_deploy_changed", "high",
                        "CI, deploy, or release workflow changed.", force_high=True))
    if features.get("public_api_changed") and (
        pcts["file_count"] >= cfg.broad_blast_file_pct
        or _num(features.get("directory_count")) >= cfg.broad_dir_count
    ):
        out.append(_sig("public_api_broad_blast", "high",
                        "Public API/schema change with a broad blast radius.", force_high=True))
    if features.get("migration_changed") and (file_count - migration_files) > 0:
        out.append(_sig("migration_with_app_code", "high",
                        "Database migration changed together with application code.", force_high=True))

    # --- block_low only: common or moderate-lift; feed pile-up + the score ---
    # data_deletion is patch-text heuristic and was unvalidated on the training slice
    # (it over-fires on live diffs), so it blocks bypass but does not force high.
    if features.get("touches_data_deletion"):
        out.append(_sig("data_deletion", "medium",
                        "Patch text mentions data deletion or destructive change."))
    history_pct = max(pcts["max_file_prior_bad_outcomes"], pcts["max_dir_prior_bad_outcomes"])
    if history_pct >= cfg.history_bad_percentile:
        out.append(_sig("historically_unstable_area", "medium",
                        "Touched files/folders are among the more revert- or fix-prone in this repo.",
                        percentile=round(history_pct, 1)))
    if features.get("large_relative_churn") or pcts["max_file_churn_ratio"] >= cfg.churn_percentile:
        out.append(_sig("large_relative_churn", "medium",
                        "File churn relative to prior file size is unusually large for this repo.",
                        percentile=round(pcts["max_file_churn_ratio"], 1)))
    if _num(features.get("directory_count")) >= cfg.broad_dir_count \
            or _num(features.get("top_level_directory_count")) >= cfg.broad_dir_count:
        out.append(_sig("broad_diffusion", "medium",
                        "Change spans several directories/areas."))
    if features.get("lockfile_changed"):
        out.append(_sig("dependency_change", "medium", "Dependency lockfile changed."))
    if (features.get("auth_or_permission_changed") or features.get("security_or_privacy_changed")) \
            and not features.get("tests_changed"):
        out.append(_sig("auth_change_without_tests", "medium",
                        "Auth/permission/security code changed without an accompanying test change."))
    if features.get("code_changed_without_test_signal"):
        out.append(_sig("code_changed_without_test_signal", "medium",
                        "Code changed in a tested area without a test update."))
    if _num(features.get("author_touched_file_ratio")) == 0 \
            and file_is_nontrivial(features, pcts, cfg):
        out.append(_sig("low_area_familiarity", "medium",
                        "Change touches files this PR's author has not modified before.",
                        sensitive=True))
    return out


def file_is_nontrivial(features: Dict[str, Any], pcts: Dict[str, float], cfg: LabelConfig) -> bool:
    return (
        _num(features.get("file_count")) >= cfg.low_familiarity_min_files
        and pcts["changed_lines"] >= cfg.low_familiarity_min_size_pct
    )


def _informational_signals(
    features: Dict[str, Any], pcts: Dict[str, float], cfg: LabelConfig
) -> List[Dict[str, Any]]:
    """Non-gating context signals used for explanation only."""

    out: List[Dict[str, Any]] = []
    if pcts["changed_lines"] >= 90:
        out.append(_sig("unusual_change_size", "info",
                        "Change size is large for this repo.", percentile=round(pcts["changed_lines"], 1)))
    if features.get("docs_only") or features.get("generated_only") \
            or features.get("comment_only") or features.get("whitespace_only") \
            or features.get("tests_only"):
        out.append(_sig("safe_scope", "info",
                        "Change appears limited to docs, generated files, comments, or tests."))
    return out


def is_safe_scope(features: Dict[str, Any], cfg: LabelConfig) -> bool:
    """Positive check that a change is safe enough to bypass review (gate for `low`)."""

    if features.get("docs_only") or features.get("generated_only") \
            or features.get("comment_only") or features.get("whitespace_only"):
        return True
    if features.get("tests_only") and not features.get("sensitive_area_changed") \
            and not features.get("ci_or_deploy_changed"):
        return True
    if features.get("source_type") == "dependency_bot" \
            and _num(features.get("file_count")) <= cfg.dependency_bot_max_files \
            and not features.get("migration_changed"):
        return True
    return False


# --- decision ------------------------------------------------------------------------


def _decide_label(
    gating: List[Dict[str, Any]],
    composite_pct: float,
    features: Dict[str, Any],
    cfg: LabelConfig,
) -> str:
    force_high = any(s.get("force_high") for s in gating)
    if force_high or len(gating) >= cfg.pileup_high_count or composite_pct >= cfg.high_percentile_cut:
        return "high"
    if (
        not gating
        and composite_pct <= cfg.low_percentile_cut
        and is_safe_scope(features, cfg)
    ):
        return "low"
    return "medium"


# --- helpers -------------------------------------------------------------------------


def _sig(name: str, severity: str, reason: str, **extra: Any) -> Dict[str, Any]:
    sig = {"name": name, "severity": severity, "reason": reason}
    sig.update(extra)
    return sig


def _feature_value(row: Dict[str, Any], name: str) -> float:
    return _num((row.get("prediction_features") or {}).get(name))


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _truthy(value: Any) -> bool:
    return bool(value)
