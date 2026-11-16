"""Fixed rules that may raise a Jev ``low`` to ``medium``.

Code never grants low, never lowers a label and never produces high.
"""

from .sdk_rubric import MECHANISMS

LOW_PROBABILITY_THRESHOLD = 0.90

_KIND_RULES = (("R4", "public-api-record"), ("R5", "dependencies"), ("R6", "build-release"))


def _paths(row, *kinds):
    return sorted({item.get("path") for item in row.get("omitted") or [] if item.get("kind") in kinds and item.get("path")})


def evaluate_rules(row):
    """Return every rule that fires, in rule order, as ``{"rule", "files"}``."""
    fired = []
    omitted = _paths(row, "diff", "file_content")
    if row.get("diff_truncated") or row.get("context_stage") != "bounded_diff" or omitted:
        fired.append({"rule": "R1", "files": omitted})
    binary = _paths(row, "binary_content")
    if any(item.get("kind") == "binary_content" for item in row.get("omitted") or []):
        fired.append({"rule": "R2", "files": binary})
    files = (row.get("sdk") or {}).get("files") or []
    unmapped = sorted(item["path"] for item in files if item.get("unmapped"))
    if unmapped:
        fired.append({"rule": "R3", "files": unmapped})
    for rule, kind in _KIND_RULES:
        matched = sorted(item["path"] for item in files if kind in (item.get("kinds") or []))
        if matched:
            fired.append({"rule": rule, "files": matched})
    probabilities = row.get("probabilities") or {}
    low = probabilities.get("low")
    if row.get("risk_label") == "low" and not (isinstance(low, (int, float)) and low >= LOW_PROBABILITY_THRESHOLD):
        fired.append({"rule": "R7", "files": []})
    return fired


def apply_rules(row):
    """Apply the rules to an SDK row whose status is ``ok``."""
    if row.get("status") != "ok":
        return
    mechanisms = row.get("mechanisms")
    if not isinstance(mechanisms, dict) or set(mechanisms) != set(MECHANISMS):
        row.update(status="invalid_response", risk_label=None, probabilities=None, model_label=None,
                   mechanisms=None)
        if not row.get("error"):
            row["error"] = {"name": "invalid_mechanisms"}
        return
    row["model_label"] = row.get("risk_label")
    row["low_blocked_by"] = evaluate_rules(row)
    if row["model_label"] == "low" and row["low_blocked_by"]:
        row["risk_label"] = "medium"
