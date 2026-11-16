"""Shared SDK risk rubric and the Jev questions built from it.

The rubric text lives in ``sdk/rubric.toml`` in the action's own checkout and is
the same for every SDK repository. Per PR, only the family example blocks and
the shared-core sentence are selected; the level criteria never vary.
"""

from __future__ import annotations

from pathlib import Path

from . import sdk_facts

MECHANISMS = ("m_stability", "m_performance", "m_contract", "m_data", "m_inherited", "m_reach", "m_ships")
_LEVELS = ("low", "medium", "high")
# The rubric always comes from the action's own checkout, independently of the
# fact-sheet root, so the shared text cannot vary with where fact sheets load from.
RUBRIC_ROOT = Path(__file__).resolve().parents[2] / "sdk"


def _load(root: Path | None) -> dict:
    import tomllib

    root = RUBRIC_ROOT if root is None else root
    with (root / "rubric.toml").open("rb") as handle:
        data = tomllib.load(handle)
    if set(data.get("criteria", {})) != set(_LEVELS):
        raise ValueError(f"rubric.toml: [criteria] must define exactly {_LEVELS}")
    if set(data.get("mechanisms", {})) != set(MECHANISMS):
        raise ValueError(f"rubric.toml: [mechanisms] must define exactly {MECHANISMS}")
    if set(data.get("families", {})) != set(sdk_facts.FAMILIES):
        raise ValueError(f"rubric.toml: [families] must define exactly {sdk_facts.FAMILIES}")
    for key in ("instructions", "shared_core", "mechanism_preamble"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"rubric.toml: {key} must be a non-empty string")
    return data


def rubric_version(root: Path | None = None) -> str:
    return str(_load(root)["version"])


def build_questions(families: list[str], shared_core: bool, root: Path | None = None) -> dict:
    unknown = sorted(set(families) - set(sdk_facts.FAMILIES))
    if unknown:
        raise ValueError(f"unknown SDK families: {unknown}")
    data = _load(root)
    parts = [data["instructions"]]
    parts += [data["families"][family] for family in sorted(set(families))]
    if shared_core:
        parts.append(data["shared_core"])
    questions = {
        "risk": {
            "type": "choice",
            "instructions": " ".join(parts),
            "criteria": {level: data["criteria"][level] for level in _LEVELS},
        }
    }
    for name in MECHANISMS:
        questions[name] = {
            "type": "boolean",
            "instructions": f"{data['mechanism_preamble']} {data['mechanisms'][name]}",
        }
    return questions
