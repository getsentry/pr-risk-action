"""Deterministic, no-LLM explanations for a risk label.

Follows the explainability paper's taxonomy (descriptive -> contextual -> actionable) and
its key empirical lesson: phrase risk *relative to this repo* ("unusual for this repo"),
not as absolute prose. Sensitive-signal rule: never attribute risk to a person; describe
the code/coverage instead (e.g. "consider an owning-area reviewer", never "author is
inexperienced").
"""

from __future__ import annotations

from typing import Any, Dict, List

# Per-signal action guidance (the "actionable" leg of the taxonomy).
_ACTIONS: Dict[str, str] = {
    "migration_with_app_code": "Confirm a rollout/rollback plan; consider splitting the migration from app code.",
    "data_deletion": "Confirm the deletion is intended and reversible; add a backup/rollback note.",
    "auth_change_without_tests": "Add tests for the auth/permission change and request an owning-area reviewer.",
    "historically_unstable_area": "Extra scrutiny suggested; verify regression coverage for this area.",
    "public_api_broad_blast": "Confirm downstream consumers; document the API/schema change.",
    "ci_or_deploy_changed": "Have someone who owns CI/release review this.",
    "dependency_change": "Confirm the dependency bump is intended and check for breaking changes.",
    "large_relative_churn": "Large rewrite relative to file size — consider focused review of the riskiest file.",
    "broad_diffusion": "Confirm the cross-area changes belong together; consider splitting.",
    "code_changed_without_test_signal": "Add or point to tests covering the changed behavior.",
    "low_area_familiarity": "Consider a reviewer who owns this area (see CODEOWNERS).",
    "unusual_change_size": "Consider splitting, or call out the riskiest file for focused review.",
}

_LABEL_HEADLINE = {
    "high": "PR Risk: High — needs extra review",
    "medium": "PR Risk: Medium — normal review",
    "low": "PR Risk: Low — bypass-eligible",
}


def render_signal_line(signal: Dict[str, Any], repo: str) -> str:
    """One descriptive→contextual→actionable line for a signal."""

    reason = signal.get("reason", "")
    pct = signal.get("percentile")
    contextual = ""
    if pct is not None:
        contextual = f" (p{pct:.0f} for `{repo}`)"
    action = _ACTIONS.get(signal.get("name", ""))
    line = f"- {reason}{contextual}"
    if action:
        line += f" {action}"
    return line


def render_markdown(row: Dict[str, Any]) -> str:
    """Reviewer-facing markdown for a single labeled row."""

    risk = row.get("risk") or {}
    label = risk.get("label", "unknown")
    repo = row.get("repo", "")
    pct = risk.get("risk_percentile_repo")
    signals: List[Dict[str, Any]] = risk.get("signals") or []

    headline = _LABEL_HEADLINE.get(label, f"PR Risk: {label}")
    lines = [f"## {headline}", ""]
    if pct is not None:
        if label == "high":
            lines.append(f"This PR ranks in the top {max(0, 100 - pct):.0f}% by risk for `{repo}`.")
        elif label == "low":
            lines.append(f"This PR ranks in the safest {pct:.0f}% for `{repo}` and matches a known-safe scope.")
        else:
            lines.append(f"This PR is around p{pct:.0f} by risk for `{repo}`.")
        lines.append("")

    gating = [s for s in signals if s.get("severity") in ("severe", "elevated")]
    if gating:
        lines.append("Main signals:")
        for s in gating:
            lines.append(render_signal_line(s, repo))
        lines.append("")

    if label == "high":
        lines.append("Suggested action: request an owner/area reviewer, and add rollout/rollback "
                     "or test evidence before merge.")
    elif label == "low":
        lines.append("No extra action: change is limited to a known-safe scope with no risk signals.")
    return "\n".join(lines).rstrip() + "\n"
