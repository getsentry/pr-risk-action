"""Reproducible Jev evaluation, with human labels kept separate from outcomes.

One report covers one frozen model/context configuration. Development evaluation
never reads holdout labels; ``evaluate_holdout_once`` reserves the benchmark in
the dataset's shared evaluation directory before publishing its final report.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence


RISK_LABELS = ("low", "medium", "high")
MIN_CLASS_SUPPORT = 5


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _index(rows: Iterable[Dict[str, Any]], name: str) -> Dict[str, Dict[str, Any]]:
    result = {}
    for row in rows:
        identifier = row.get("example_id")
        if not identifier:
            raise ValueError(f"{name} row is missing example_id")
        if identifier in result:
            raise ValueError(f"duplicate {name} example_id: {identifier}")
        result[identifier] = row
    return result


def _human_label(row: Dict[str, Any]) -> str | None:
    label = row.get("risk_label", row.get("label"))
    if (
        row.get("source") == "human"
        and label in RISK_LABELS
        and str(row.get("rationale") or "").strip()
        and row.get("reviewer")
        and row.get("reviewed_at")
        and row.get("rubric_version")
        and _revision(row) is not None
    ):
        return label
    return None


def _assisted_label(row: Dict[str, Any]) -> str | None:
    label = row.get("proposed_risk_label")
    if (
        row.get("source") == "agent"
        and row.get("review_status") == "proposed"
        and label in RISK_LABELS
        and all(str(row.get(field) or "").strip() for field in ("rationale", "reviewer", "reviewed_at", "rubric_version"))
        and _revision(row) is not None
    ):
        return label
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _valid_prediction(row: Dict[str, Any]) -> bool:
    probabilities = row.get("probabilities")
    if row.get("status") != "ok" or row.get("risk_label") not in RISK_LABELS or not isinstance(probabilities, dict):
        return False
    values = [_number(probabilities.get(label)) for label in RISK_LABELS]
    # Jev returns rounded probabilities. Preserve them rather than silently
    # renormalizing the provider's result; three 2-decimal values can sum to .99.
    return all(value is not None and value <= 1 for value in values) and abs(sum(values) - 1) <= 0.015001


def _configuration(predictions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    configurations = {}
    for row in predictions:
        config = row.get("configuration") or {
            "model": row.get("model"),
            "variant": row.get("variant"),
            "max_bytes": row.get("max_bytes"),
            "versions": row.get("versions") or {},
        }
        configurations[_digest(config)] = config
    if len(configurations) > 1:
        raise ValueError("evaluate one model/context configuration at a time")
    return next(iter(configurations.values()), {})


def _wilson(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    scale = 1 + z * z / total
    center = (p + z * z / (2 * total)) / scale
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return [max(0, center - radius), min(1, center + radius)]


def _classification(pairs: Sequence[tuple[str, str | None]]) -> Dict[str, Any]:
    confusion = {actual: {predicted: 0 for predicted in (*RISK_LABELS, "missing")} for actual in RISK_LABELS}
    for actual, predicted in pairs:
        confusion[actual][predicted if predicted in RISK_LABELS else "missing"] += 1
    per_class = {}
    for label in RISK_LABELS:
        true_positive = confusion[label][label]
        support = sum(confusion[label].values())
        predicted_count = sum(confusion[actual][label] for actual in RISK_LABELS)
        precision = true_positive / predicted_count if predicted_count else 0.0
        recall = true_positive / support if support else 0.0
        per_class[label] = {
            "support": support,
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        }
    return {
        "total": len(pairs),
        "predicted": sum(predicted in RISK_LABELS for _, predicted in pairs),
        "macro_f1": sum(item["f1"] for item in per_class.values()) / 3 if pairs else None,
        "per_class": per_class,
        "confusion": confusion,
        "false_high": confusion["low"]["high"] + confusion["medium"]["high"],
        "low_to_high": confusion["low"]["high"],
        "high_to_low": confusion["high"]["low"],
        "low_to_high_rate_ci95": _wilson(confusion["low"]["high"], per_class["low"]["support"]),
        "high_to_low_rate_ci95": _wilson(confusion["high"]["low"], per_class["high"]["support"]),
    }


def _low_review(predictions: Dict[str, Dict[str, Any]], reviewed: Dict[str, str], identifiers: Sequence[str], revision_matched: set[str]) -> Dict[str, Any]:
    """Describe fixed low-review thresholds after revision and reference validation.

    Unknown references count toward eligibility coverage but never precision;
    recall keeps every reviewed low case, including missing predictions.
    """
    valid = {identifier: predictions[identifier] for identifier in identifiers
             if identifier in revision_matched and _valid_prediction(predictions.get(identifier, {}))}
    low = {identifier: row for identifier, row in valid.items() if row["risk_label"] == "low"}
    support = {label: sum(reviewed.get(identifier) == label for identifier in identifiers) for label in RISK_LABELS}
    states = Counter()
    for identifier in identifiers:
        row = predictions.get(identifier, {})
        if _valid_prediction(row) and identifier not in revision_matched:
            states["snapshot_unverified"] += 1
        else:
            states["ok" if identifier in valid else ("invalid_prediction" if row.get("status") == "ok" else row.get("status", "missing"))] += 1
    scores = {
        "probability_low": {identifier: row["probabilities"]["low"] for identifier, row in low.items()},
        "provider_confidence": {},
    }
    for identifier, row in low.items():
        raw_confidence = row.get("provider_confidence")
        confidence = _number(raw_confidence) if isinstance(raw_confidence, (int, float)) and 0 <= raw_confidence <= 1 else None
        if confidence is not None:
            scores["provider_confidence"][identifier] = confidence
    curves = {}
    for metric, values in scores.items():
        curve = []
        for threshold in (0, 0.5, 0.7, 0.8, 0.9, 0.95, 0.99):
            eligible = sorted(identifier for identifier, score in values.items() if score >= threshold)
            known = [identifier for identifier in eligible if identifier in reviewed]
            correct = sum(reviewed[identifier] == "low" for identifier in known)
            errors = {label: [identifier for identifier in known if reviewed[identifier] == label]
                      for label in ("medium", "high")}
            curve.append({
                "threshold": threshold,
                "eligible_count": len(eligible),
                "all_cases_fraction": len(eligible) / len(identifiers) if identifiers else None,
                "known_label_count": len(known), "unknown_label_count": len(eligible) - len(known),
                "correct_low": correct, "false_low_medium": len(errors["medium"]), "false_low_high": len(errors["high"]),
                "precision_known": correct / len(known) if known else None,
                "precision_known_ci95": _wilson(correct, len(known)),
                "recall_expected_low": correct / support["low"] if support["low"] else None,
                "eligible_example_ids": eligible, "error_example_ids": errors,
            })
        curves[metric] = curve
    return {
        "expected": len(identifiers), "class_support": support,
        "unknown_reference_count": len(identifiers) - sum(support.values()),
        "prediction_coverage": {"valid": len(valid), "missing_or_invalid": len(identifiers) - len(valid), "by_status": dict(states)},
        "predicted_low": len(low),
        "provider_confidence_coverage": {"available": len(scores["provider_confidence"]),
                                         "missing_or_invalid": len(low) - len(scores["provider_confidence"])},
        "curves": curves,
        "interpretation": (
            "Descriptive thresholds require a valid low prediction and a score greater than or equal to the threshold. "
            "Predictions must match a known expected snapshot; an unverifiable revision is ineligible. "
            "Probability of the low class and provider confidence are separate signals, not incident probabilities. "
            "Eligibility fractions use all expected cases; precision and Wilson 95% intervals use only valid reviewed labels. "
            "Unknown references receive no precision credit. Recall includes all reviewed expected-low cases, even without predictions. "
            "Missing or invalid confidence is unknown, not zero. No threshold is automatically selected, "
            "and this curve does not authorize merging or bypassing review."
        ),
    }


def _calibration(rows: Sequence[tuple[str, Dict[str, Any]]], expected: int, *, assisted: bool = False) -> Dict[str, Any]:
    squared_errors = []
    bins = [{"lower": i / 10, "upper": (i + 1) / 10, "count": 0, "confidence_sum": 0.0, "correct": 0} for i in range(10)]
    for actual, prediction in rows:
        probabilities = prediction["probabilities"]
        squared_errors.append(sum((probabilities[label] - int(actual == label)) ** 2 for label in RISK_LABELS))
        confidence = probabilities[prediction["risk_label"]]
        bucket = bins[min(9, int(confidence * 10))]
        bucket["count"] += 1
        bucket["confidence_sum"] += confidence
        bucket["correct"] += int(prediction["risk_label"] == actual)
    calibration_bins = []
    ece = 0.0
    for bucket in bins:
        count = bucket["count"]
        mean = bucket["confidence_sum"] / count if count else None
        accuracy = bucket["correct"] / count if count else None
        calibration_bins.append({"lower": bucket["lower"], "upper": bucket["upper"], "count": count, "mean_confidence": mean, "accuracy": accuracy})
        if count:
            ece += count / len(rows) * abs(mean - accuracy)
    return {
        "evaluated": len(rows), "expected": expected, "missing": expected - len(rows),
        "brier_score": sum(squared_errors) / len(rows) if rows else None,
        "brier_definition": f"sum of squared errors over the three {'assistant-reviewed' if assisted else 'human'} risk classes; range 0 to 2",
        "expected_calibration_error": ece if rows else None,
        "bins": calibration_bins,
        "interpretation": "Class membership, not incident probability. Missing predictions are counted in coverage; probability metrics use available distributions only.",
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _distribution(values: Sequence[float], expected: int) -> Dict[str, Any]:
    known_total = sum(values)
    complete = len(values) == expected
    mean = known_total / len(values) if values else None
    return {
        "known_count": len(values), "unknown_count": expected - len(values),
        "known_subtotal": known_total, "total": known_total if complete else None,
        "mean_known": mean, "mean": mean if complete else None,
        "p50_known": _percentile(values, 0.5), "p95_known": _percentile(values, 0.95),
        "max_known": max(values) if values else None,
        "projected_1000": mean * 1000 if complete and mean is not None else None,
        "projected_10000": mean * 10000 if complete and mean is not None else None,
    }


def _operations(predictions: Dict[str, Dict[str, Any]], identifiers: Sequence[str]) -> Dict[str, Any]:
    per_pr = {"reported_usd": [], "estimated_usd": [], "effective_usd": []}
    attempts = []
    latencies = []
    price_snapshots = {}
    retries = 0
    for identifier in identifiers:
        row = predictions.get(identifier)
        if not row:
            continue
        row_attempts = row.get("attempts")
        if row_attempts is None:
            row_attempts = [row]  # Imported single-attempt records.
        per_request = Counter(attempt.get("request_hash") for attempt in row_attempts)
        retries += sum(max(0, count - 1) for count in per_request.values())
        attempts.extend(row_attempts)
        if row.get("price_snapshot"):
            price_snapshots[_digest(row["price_snapshot"])] = row["price_snapshot"]
        for name, field in (("reported_usd", "reported_cost_usd"), ("estimated_usd", "estimated_cost_usd"), ("effective_usd", None)):
            amounts = []
            for attempt in row_attempts:
                reported = _number(attempt.get("reported_cost_usd"))
                estimated = _number(attempt.get("estimated_cost_usd"))
                value = reported if reported is not None else estimated
                amounts.append(value if field is None else _number(attempt.get(field)))
            if all(value is not None for value in amounts):
                per_pr[name].append(sum(amounts))
        latency = _number(row.get("latency_ms"))
        if latency is not None:
            latencies.append(latency)
    costs = {name: _distribution(values, len(identifiers)) for name, values in per_pr.items()}
    # Partial attempts must remain visible even when the PR total is unknown.
    for name, field in (("reported_usd", "reported_cost_usd"), ("estimated_usd", "estimated_cost_usd")):
        values = [_number(attempt.get(field)) for attempt in attempts]
        costs[name]["known_attempt_subtotal"] = sum(value for value in values if value is not None)
        costs[name]["unknown_attempts"] = sum(value is None for value in values)
    usage = {}
    for field in ("input_tokens", "output_tokens"):
        values = [_number((attempt.get("usage") or {}).get(field)) for attempt in attempts]
        usage[field] = {"known_subtotal": sum(value for value in values if value is not None), "unknown_attempts": sum(value is None for value in values), "total": sum(values) if all(value is not None for value in values) else None}
    return {
        "expected_prs": len(identifiers), "recorded_prs": sum(identifier in predictions for identifier in identifiers),
        "attempts": len(attempts), "retries": retries,
        "context_fallbacks": sum(predictions.get(identifier, {}).get("context_fallback_reason") == "provider_context_rejected" for identifier in identifiers),
        "attempt_statuses": dict(Counter(attempt.get("status") or "unknown" for attempt in attempts)),
        "costs": costs, "usage": usage, "latency_ms": _distribution(latencies, len(identifiers)),
        "latency_definition": "Per PR: sum of recorded model-attempt durations, including retries. Excludes local preparation, retry backoff and cache retrieval time; cached predictions retain the original attempt durations.",
        "price_snapshots": list(price_snapshots.values()),
        "effective_cost_policy": "Per attempt: reported Gateway cost when available, otherwise estimate. Any unknown attempt leaves the PR total unknown.",
    }


def _historical(predictions: Dict[str, Dict[str, Any]], outcomes: Dict[str, Dict[str, Any]], identifiers: Sequence[str]) -> Dict[str, Any]:
    eligible = []
    excluded = Counter()
    for identifier in identifiers:
        outcome = outcomes.get(identifier)
        if not outcome:
            excluded["missing"] += 1
        elif outcome.get("valid") is not True or outcome.get("mature") is not True or (outcome.get("observation_days") or 0) < 30:
            excluded["immature_or_invalid"] += 1
        else:
            eligible.append(identifier)
    ranked = sorted(
        [identifier for identifier in eligible if _valid_prediction(predictions.get(identifier, {}))],
        key=lambda identifier: (-(predictions[identifier]["probabilities"]["high"] + 0.5 * predictions[identifier]["probabilities"]["medium"]), identifier),
    )
    metrics = {}
    for field in ("strong_outcome", "medium_outcome"):
        measured = [identifier for identifier in eligible if isinstance(outcomes[identifier].get(field), bool)]
        measured_set = set(measured)
        positive_count = sum(outcomes[identifier][field] for identifier in measured)
        field_ranked = [identifier for identifier in ranked if identifier in measured_set]
        buckets = {}
        for percentile in (5, 10, 30):
            quota = math.ceil(len(measured) * percentile / 100)
            chosen = field_ranked[:quota]
            positives = sum(outcomes[identifier][field] for identifier in chosen)
            buckets[f"top_{percentile}"] = {"quota": quota, "ranked_prs": len(chosen), "true_positives": positives, "precision": positives / quota if quota else None, "recall": positives / positive_count if positive_count else None}
        metrics[field] = {"eligible": len(measured), "missing_outcome": len(eligible) - len(measured), "positive_outcomes": positive_count, "ranked": len(field_ranked), "top_k": buckets}
    return {"expected": len(identifiers), "eligible": len(eligible), "ranked": len(ranked), "excluded": dict(excluded), "metrics": metrics, "score": "P(high) + 0.5 * P(medium)", "interpretation": "Observed follow-up proxies, evaluated separately from human risk classes; absent observed events do not establish low risk."}


def _revision(row: Dict[str, Any]) -> tuple[Any, Any] | None:
    snapshot = row.get("snapshot") or {}
    head = snapshot.get("head_sha")
    base = snapshot.get("base_sha")
    return (base, head) if base and head else None


def _baseline_reports(baselines: Sequence[Dict[str, Any]], predictions: Dict[str, Dict[str, Any]], labels: Dict[str, str]) -> Dict[str, Any]:
    grouped = {}
    for row in baselines:
        name = row.get("baseline") or row.get("name")
        if not name:
            raise ValueError("baseline name is required")
        grouped.setdefault(name, []).append(row)
    result = {}
    for name, rows in grouped.items():
        indexed = _index(rows, f"baseline {name}")
        paired = []
        exclusions = Counter()
        for identifier, actual in sorted(labels.items()):
            candidate = predictions.get(identifier, {})
            baseline = indexed.get(identifier, {})
            if not _valid_prediction(candidate):
                exclusions["candidate_missing"] += 1
            elif baseline.get("risk_label") not in RISK_LABELS:
                exclusions["baseline_missing"] += 1
            elif _revision(candidate) is None or _revision(candidate) != _revision(baseline):
                exclusions["snapshot_mismatch_or_missing"] += 1
            else:
                paired.append((identifier, actual, candidate["risk_label"], baseline["risk_label"]))
        candidate_metrics = _classification([(actual, candidate) for _, actual, candidate, _ in paired])
        baseline_metrics = _classification([(actual, baseline) for _, actual, _, baseline in paired])
        interval = None
        if paired:
            rng = random.Random(0)
            deltas = []
            for _ in range(1000):
                sample = [paired[rng.randrange(len(paired))] for _ in paired]
                deltas.append(_classification([(actual, candidate) for _, actual, candidate, _ in sample])["macro_f1"] - _classification([(actual, baseline) for _, actual, _, baseline in sample])["macro_f1"])
            interval = [_percentile(deltas, 0.025), _percentile(deltas, 0.975)]
        result[name] = {"expected": len(labels), "paired": len(paired), "paired_example_ids": [row[0] for row in paired], "excluded": dict(exclusions), "candidate": candidate_metrics, "baseline": baseline_metrics, "macro_f1_difference_ci95": interval, "uncertainty_method": "1000 paired bootstrap resamples, seed 0; Wilson 95% intervals for directional error rates. Descriptive uncertainty, not a significance guarantee."}
    return result


def _evaluate(predictions, labels, outcomes, baselines, examples, split, *, assisted=False):
    predictions_by_id = _index(predictions, "prediction")
    labels_by_id = _index(labels, "label")
    examples_by_id = _index(examples, "example")
    outcomes_by_id = _index(outcomes, "outcome")
    if examples:
        identifiers = sorted(identifier for identifier, row in examples_by_id.items() if row.get("split", split) == split)
    elif labels and split != "representative":
        identifiers = sorted(identifier for identifier, row in labels_by_id.items() if row.get("split") == split)
    else:
        identifiers = sorted(set(predictions_by_id) | set(outcomes_by_id))
        identifiers = [identifier for identifier in identifiers if labels_by_id.get(identifier, {}).get("split", split) == split]
    # IDs alone do not identify the reviewed change. Keep stale predictions in
    # operational accounting, but never give them classification credit.
    revision_matched = set()
    for identifier in identifiers:
        reference = examples_by_id.get(identifier) or labels_by_id.get(identifier, {})
        expected_revision = _revision(reference)
        prediction = predictions_by_id.get(identifier)
        if prediction and expected_revision is not None and _revision(prediction) != expected_revision:
            predictions_by_id[identifier] = {**prediction, "status": "snapshot_mismatch", "risk_label": None, "probabilities": None}
        elif prediction and expected_revision is not None:
            revision_matched.add(identifier)
    selected_predictions = [predictions_by_id[identifier] for identifier in identifiers if identifier in predictions_by_id]
    configuration = _configuration(selected_predictions)
    read_label = _assisted_label if assisted else _human_label
    reviewed = {identifier: read_label(labels_by_id.get(identifier, {})) for identifier in identifiers}
    reviewed = {
        identifier: label for identifier, label in reviewed.items()
        if label is not None and labels_by_id[identifier].get("split") == split
        and (identifier not in examples_by_id or _revision(labels_by_id[identifier]) == _revision(examples_by_id[identifier]))
    }
    states = Counter()
    for identifier in identifiers:
        prediction = predictions_by_id.get(identifier, {})
        states["ok" if _valid_prediction(prediction) else ("invalid_prediction" if prediction.get("status") == "ok" else prediction.get("status", "missing"))] += 1
    pairs = [(actual, predictions_by_id[identifier]["risk_label"] if _valid_prediction(predictions_by_id.get(identifier, {})) else None) for identifier, actual in reviewed.items()]
    evaluable_ids = sorted(identifier for identifier in reviewed if _valid_prediction(predictions_by_id.get(identifier, {})))
    slices = {"repo": {}, "stratum": {}}
    for identifier, actual in reviewed.items():
        row = examples_by_id.get(identifier) or labels_by_id.get(identifier) or predictions_by_id.get(identifier, {})
        prediction = predictions_by_id.get(identifier, {})
        pair = (actual, prediction["risk_label"] if _valid_prediction(prediction) else None)
        slices["repo"].setdefault(row.get("repo", "unknown"), []).append(pair)
        for stratum in row.get("strata") or ["unclassified"]:
            slices["stratum"].setdefault(stratum, []).append(pair)
    baseline_reports = _baseline_reports(baselines, predictions_by_id, reviewed)
    report = {
        "schema_version": 2, "split": split, "configuration": configuration, "configuration_hash": _digest(configuration),
        "example_ids": identifiers,
        "evaluable_example_ids": evaluable_ids,
        "coverage": {"expected": len(identifiers), "predicted": states["ok"], "missing_or_failed": len(identifiers) - states["ok"], "rate": states["ok"] / len(identifiers) if identifiers else None, "by_status": dict(states)},
        "classification": _classification(pairs),
        "classification_evaluable": _classification([(reviewed[identifier], predictions_by_id[identifier]["risk_label"]) for identifier in evaluable_ids]),
        "low_review": _low_review(predictions_by_id, reviewed, identifiers, revision_matched),
        "calibration": _calibration([(actual, predictions_by_id[identifier]) for identifier, actual in reviewed.items() if _valid_prediction(predictions_by_id.get(identifier, {}))], len(reviewed), assisted=assisted),
        "slices": {group: {name: _classification(rows) for name, rows in values.items()} for group, values in slices.items()},
        "historical": _historical(predictions_by_id, outcomes_by_id, identifiers) if split == "representative" else {"status": "not_evaluated", "reason": "Outcome ranking is reserved for the independent representative cohort."},
        "operations": _operations(predictions_by_id, identifiers),
        "baselines": baseline_reports,
        "acceptance": {"accepted": False, "reason": "A locked, one-time holdout evaluation is required for default promotion."},
    }
    if assisted:
        excluded = {}
        for identifier in identifiers:
            if identifier in reviewed:
                continue
            row = labels_by_id.get(identifier, {})
            if not row:
                reason = "missing_reference"
            elif row.get("split") != split:
                reason = "wrong_split"
            elif row.get("source") != "agent" or row.get("review_status") != "proposed":
                reason = "invalid_provenance"
            elif "proposed_risk_label" in row and row["proposed_risk_label"] is None:
                reason = "null_proposal"
            elif _assisted_label(row) is None:
                reason = "invalid_reference"
            else:
                reason = "snapshot_mismatch"
            excluded.setdefault(reason, []).append(identifier)
        report.update({
            "reference_type": "assisted",
            "assisted_references_hash": _digest([labels_by_id.get(identifier, {"example_id": identifier}) for identifier in identifiers]),
            "assisted_references": {
                "expected": len(identifiers), "reviewed": len(reviewed),
                "missing_or_invalid": len(identifiers) - len(reviewed),
                "excluded": {reason: len(ids) for reason, ids in excluded.items()},
                "excluded_example_ids": excluded,
            },
            "interpretation": "Development comparison against assistant proposals, not independent human ground truth. This report cannot select or promote a candidate.",
            "acceptance": {"accepted": False, "reason": "Assistant-reviewed development references cannot select a candidate or authorize promotion."},
        })
    else:
        report["human_labels_hash"] = _digest([{ "example_id": identifier, "risk_label": reviewed.get(identifier), "revision": _revision(labels_by_id.get(identifier, {})), "rubric_version": labels_by_id.get(identifier, {}).get("rubric_version")} for identifier in identifiers])
        report["human_labels"] = {"reviewed": len(reviewed), "pending_or_invalid": len(identifiers) - len(reviewed)}
    return report


def evaluate_run(predictions: Sequence[Dict[str, Any]], labels: Sequence[Dict[str, Any]] = (), outcomes: Sequence[Dict[str, Any]] = (), baselines: Sequence[Dict[str, Any]] = (), examples: Sequence[Dict[str, Any]] = (), *, split: str = "dev") -> Dict[str, Any]:
    """Evaluate dev or representative data; holdout is only available once."""
    if split not in ("dev", "representative"):
        raise ValueError("use evaluate_holdout_once for held-out labels")
    return _evaluate(predictions, labels, outcomes, baselines, examples, split)


def evaluate_assisted_run(predictions: Sequence[Dict[str, Any]], assisted_labels: Sequence[Dict[str, Any]], baselines: Sequence[Dict[str, Any]] = (), examples: Sequence[Dict[str, Any]] = (), *, split: str = "dev") -> Dict[str, Any]:
    """Compare development predictions with explicit, revision-matched proposals."""
    if split != "dev":
        raise ValueError("assisted evaluation is restricted to the dev split")
    return _evaluate(predictions, assisted_labels, (), baselines, examples, split, assisted=True)


def select_candidate(dev_reports: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Lock a complete dev configuration, preferring quality then known cost."""
    if not dev_reports:
        raise ValueError("development reports are required")
    if any(report.get("reference_type") == "assisted" or "assisted_references" in report for report in dev_reports):
        raise ValueError("assisted reports cannot select a candidate; reviewed human labels are required")
    cohort = dev_reports[0].get("example_ids")
    evaluable_cohort = dev_reports[0].get("evaluable_example_ids")
    labels_hash = dev_reports[0].get("human_labels_hash")
    eligible = []
    for report in dev_reports:
        if report.get("split") != "dev":
            raise ValueError("candidate selection may only use development reports")
        if report.get("example_ids") != cohort:
            raise ValueError("compare context variants on the same development cohort")
        if report.get("human_labels_hash") != labels_hash:
            raise ValueError("compare context variants against the same frozen human labels")
        if report.get("evaluable_example_ids") != evaluable_cohort:
            raise ValueError("compare context variants on exactly the same evaluable development examples")
        if report["human_labels"]["pending_or_invalid"]:
            continue
        quality = report["classification_evaluable"]
        if quality["macro_f1"] is None or any(quality["per_class"][label]["support"] == 0 for label in RISK_LABELS):
            continue
        config = report.get("configuration") or {}
        if not config.get("model") or not config.get("variant") or not config.get("max_bytes") or not all((config.get("versions") or {}).get(key) for key in ("rubric", "context", "sdk")):
            continue
        cost = report["operations"]["costs"]["effective_usd"]["mean"]
        eligible.append((-(quality["macro_f1"]), quality["low_to_high"], quality["high_to_low"], cost if cost is not None else math.inf, config.get("max_bytes"), str(config.get("variant")), report))
    if not eligible:
        return {"schema_version": 2, "status": "insufficient_evidence", "reason": "Selection needs reviewed human labels for every development example, all three classes in the common evaluable cohort and a versioned configuration."}
    chosen = sorted(eligible, key=lambda item: item[:-1])[0][-1]
    selection = {"schema_version": 2, "status": "locked", "configuration": chosen["configuration"], "configuration_hash": chosen["configuration_hash"], "development_example_ids": chosen["example_ids"], "development_evaluable_example_ids": chosen["evaluable_example_ids"], "development_coverage": chosen["coverage"], "development_report_hash": _digest(chosen), "selection_policy": "On identical evaluable examples: macro-F1, fewer low-to-high, fewer high-to-low, lower known all-attempt cost, lower byte budget, variant order. Full-cohort coverage remains reported."}
    selection["selection_hash"] = _digest(selection)
    return selection


def _acceptance(report: Dict[str, Any]) -> Dict[str, Any]:
    comparison = report["baselines"].get("score_pr") or report["baselines"].get("current_score_pr")
    reasons = []
    if report["human_labels"]["pending_or_invalid"]:
        reasons.append("pending_or_invalid_human_labels")
    if not comparison:
        reasons.append("missing_score_pr_baseline")
    else:
        if comparison["paired_example_ids"] != report["evaluable_example_ids"]:
            reasons.append("incomplete_revision_matched_baseline_coverage")
        candidate = comparison["candidate"]
        baseline = comparison["baseline"]
        if any(candidate["per_class"][label]["support"] < MIN_CLASS_SUPPORT for label in RISK_LABELS):
            reasons.append("insufficient_class_support")
        if candidate["macro_f1"] is None or baseline["macro_f1"] is None or candidate["macro_f1"] <= baseline["macro_f1"]:
            reasons.append("macro_f1_not_improved")
        if candidate["false_high"] >= baseline["false_high"]:
            reasons.append("false_high_not_reduced")
        if candidate["high_to_low"] > baseline["high_to_low"]:
            reasons.append("high_to_low_increased")
    return {"accepted": not reasons, "reasons": reasons,
            "evaluated": len(report["evaluable_example_ids"]), "expected": report["coverage"]["expected"],
            "coverage": report["coverage"]["rate"], "evaluable_example_ids": report["evaluable_example_ids"],
            "abstentions": report["coverage"]["missing_or_failed"],
            "abstention_statuses": {status: count for status, count in report["coverage"]["by_status"].items() if status != "ok"},
            "minimum_class_support": MIN_CLASS_SUPPORT, "configuration": report["configuration"], "configuration_hash": report["configuration_hash"],
            "interpretation": "Acceptance records observed criteria only on the common revision-matched evaluable holdout. Abstentions retain a null risk label and remain in full-cohort coverage and classification reports; no quality claim covers those examples. This is not statistical significance or incident-probability calibration."}


def validate_selection(selection: Dict[str, Any], holdout_ids: Iterable[str]) -> Dict[str, Any]:
    """Validate the frozen development decision shared by inference and scoring."""
    if selection.get("status") != "locked":
        raise ValueError("a locked development selection is required")
    signed = {key: value for key, value in selection.items() if key != "selection_hash"}
    if selection.get("selection_hash") != _digest(signed):
        raise ValueError("development selection integrity check failed")
    if set(holdout_ids) & set(selection.get("development_example_ids", [])):
        raise ValueError("development and holdout examples overlap")
    return selection["configuration"]


def evaluate_holdout_once(run_dir: str | Path, predictions: Sequence[Dict[str, Any]], labels: Sequence[Dict[str, Any]], outcomes: Sequence[Dict[str, Any]] = (), baselines: Sequence[Dict[str, Any]] = (), examples: Sequence[Dict[str, Any]] = (), *, selection: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate the frozen holdout once, using the dataset's shared run directory.

    Receipt identity uses example IDs and exact snapshots, never the output name,
    configuration or labels. Re-labeling or changing prompts cannot reopen it.
    Preflight failures do not consume the holdout; a reserved evaluation does.
    """
    indexed_examples = _index(examples, "example")
    indexed_labels = _index(labels, "label")
    cohort = sorted(identifier for identifier, row in (indexed_examples or indexed_labels).items() if row.get("split") == "holdout")
    configuration = validate_selection(selection, cohort)
    if not cohort or any(_human_label(indexed_labels.get(identifier, {})) is None for identifier in cohort):
        raise ValueError("complete human-reviewed holdout labels are required before evaluation")
    indexed_predictions = _index(predictions, "prediction")
    actual_configuration = _configuration([indexed_predictions[identifier] for identifier in cohort if identifier in indexed_predictions])
    if actual_configuration != configuration:
        raise ValueError("holdout predictions must use the locked development configuration")
    benchmark = []
    for identifier in cohort:
        example = indexed_examples.get(identifier, {})
        revision = _revision(example)
        if revision is None:
            # Freeze to dataset examples, not whatever a later model happened
            # to score. Callers without examples must persist the same labels.
            revision = _revision(indexed_labels.get(identifier, {}))
        if revision is None:
            raise ValueError("holdout examples must include frozen base_sha/head_sha snapshots")
        if _revision(indexed_labels[identifier]) != revision:
            raise ValueError("human label snapshot differs from frozen holdout benchmark")
        prediction = indexed_predictions.get(identifier)
        if prediction and _revision(prediction) != revision:
            raise ValueError("holdout prediction snapshot differs from frozen benchmark")
        benchmark.append({"example_id": identifier, "revision": revision})
    benchmark_hash = _digest(benchmark)
    # Validate all remaining inputs and calculate the report before reserving.
    # No result is returned or persisted unless exclusive reservation succeeds.
    # A malformed baseline therefore cannot consume the frozen benchmark.
    report = _evaluate(predictions, labels, outcomes, baselines, examples, "holdout")
    report["acceptance"] = _acceptance(report)
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path = directory / f"holdout-{benchmark_hash}.json"
    receipt = {"schema_version": 2, "status": "reserved", "benchmark_hash": benchmark_hash, "selection_hash": selection["selection_hash"], "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        with receipt_path.open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise ValueError(f"this frozen holdout has already been reserved or evaluated: {receipt_path}") from exc
    report["holdout_receipt"] = {**receipt, "status": "completed", "path": str(receipt_path.resolve())}
    receipt.update(status="completed", report=report)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=f".{receipt_path.name}.", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            json.dump(receipt, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, receipt_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return report
