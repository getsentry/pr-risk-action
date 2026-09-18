"""Risk tagger MVP (from-zero, no-LLM, recall-first).

This package is intentionally self-contained and isolated from ``src/risk_pr_agent``
so it can be developed alongside a concurrently-edited codebase without conflict.

It is retained for frozen offline baseline comparisons. Live PR inference now
uses Jev through ``risk-pr score-pr``. The labeling/scoring/evaluation logic here
documents the earlier experiment driven by the papers in ``papers/``.

Design (see ../README in this folder):
- One continuous, explainable risk score per PR, ranked to a per-repo ``as_of`` percentile.
- Two operating tails on that score: a small ``low`` (bypass-eligible) slice and a small
  ``high`` (needs-attention) slice; ``medium`` is the residual default.
- A global hard-signal floor that can never be overridden downward: any hard-high signal
  forbids the ``low`` (bypass) label. This is the recall guarantee against high->low.
- No per-repo custom rules. Only the percentile calibration is per-repo (data-driven).
"""

from .labeling import LabelConfig, assign_labels, label_row

__all__ = ["LabelConfig", "assign_labels", "label_row"]
