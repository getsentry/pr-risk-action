"""Per-repository SDK fact sheets and path tagging.

Fact sheets are TOML files shipped with the action (never read from the
target repository). They carry only path patterns, family names, kind names
and ``shared_core``.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk"

FAMILIES = ("server", "mobile-native", "native", "desktop", "game-engine", "browser", "hybrid", "tooling")
KINDS = ("tests", "samples", "docs", "dev-tooling", "public-api-record", "dependencies", "build-release")
NON_SHIPPING_KINDS = ("tests", "samples", "docs", "dev-tooling")
BLOCKING_KINDS = ("public-api-record", "dependencies", "build-release")

_REPO_SLUG = re.compile(r"^(?!\.+/)[A-Za-z0-9_.-]+/(?!\.+$)[A-Za-z0-9_.-]+$")


class FactSheetError(ValueError):
    pass


@functools.lru_cache(maxsize=None)
def _compile(pattern: str) -> "re.Pattern[str]":
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.DOTALL)


def matches(pattern: str, path: str) -> bool:
    return _compile(pattern).fullmatch(path) is not None


def _read_toml(path: Path) -> dict:
    import tomllib

    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise FactSheetError(f"{path.name}: invalid TOML: {error}") from error


def _check_keys(table: dict, allowed: set, where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise FactSheetError(f"{where}: unknown key(s) {unknown}")


def _paths(table: dict, where: str) -> list:
    paths = table.get("paths")
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(p, str) and p for p in paths)
    ):
        raise FactSheetError(f"{where}: paths must be a non-empty list of non-empty strings")
    return list(paths)


def _tables(data: dict, key: str, where: str) -> list:
    tables = data.get(key, [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise FactSheetError(f"{where}: [[{key}]] must be an array of tables")
    return tables


def _parse_kinds(data: dict, where: str) -> list:
    kinds = []
    for index, table in enumerate(_tables(data, "kind", where)):
        label = f"{where} kind[{index}]"
        _check_keys(table, {"paths", "kind"}, label)
        kind = table.get("kind")
        if kind not in KINDS:
            raise FactSheetError(f"{label}: unknown kind {kind!r}")
        kinds.append({"paths": _paths(table, label), "kind": kind})
    return kinds


def _parse_areas(data: dict, where: str) -> list:
    areas = []
    for index, table in enumerate(_tables(data, "area", where)):
        label = f"{where} area[{index}]"
        _check_keys(table, {"paths", "families", "shared_core"}, label)
        families = table.get("families")
        if (
            not isinstance(families, list)
            or not families
            or any(f not in FAMILIES for f in families)
        ):
            raise FactSheetError(f"{label}: families must be a non-empty list from {FAMILIES}")
        shared_core = table.get("shared_core", False)
        if not isinstance(shared_core, bool):
            raise FactSheetError(f"{label}: shared_core must be a boolean")
        areas.append({"paths": _paths(table, label), "families": list(families), "shared_core": shared_core})
    return areas


def load_fact_sheet(repo: str, root: Path | None = None) -> dict | None:
    root = SDK_ROOT if root is None else root
    # A string that is not an owner/name slug can never name a fact sheet; no
    # file is read for it, so it cannot reach outside ``repos/``.
    if not _REPO_SLUG.match(repo):
        return None
    sheet_path = root / "repos" / (repo.lower().replace("/", "__") + ".toml")
    if not sheet_path.is_file():
        return None

    sheet = _read_toml(sheet_path)
    _check_keys(sheet, {"area", "kind"}, sheet_path.name)
    areas = _parse_areas(sheet, sheet_path.name)
    kinds = _parse_kinds(sheet, sheet_path.name)

    defaults_path = root / "defaults.toml"
    if not defaults_path.is_file():
        raise FactSheetError("defaults.toml is missing")
    defaults = _read_toml(defaults_path)
    _check_keys(defaults, {"kind"}, defaults_path.name)
    kinds += _parse_kinds(defaults, defaults_path.name)

    canonical = json.dumps({"areas": areas, "kinds": kinds}, sort_keys=True, separators=(",", ":"))
    return {
        "repo": repo,
        "areas": areas,
        "kinds": kinds,
        "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _any_match(patterns: list, path: str) -> bool:
    return any(matches(pattern, path) for pattern in patterns)


def tag_path(sheet: dict, path: str) -> dict:
    areas = [a for a in sheet["areas"] if _any_match(a["paths"], path)]
    kinds = sorted({k["kind"] for k in sheet["kinds"] if _any_match(k["paths"], path)})
    families = sorted({f for a in areas for f in a["families"]})
    shared_core = any(a["shared_core"] for a in areas)
    if kinds and all(kind in NON_SHIPPING_KINDS for kind in kinds):
        families, shared_core = [], False
    return {
        "families": families,
        "shared_core": shared_core,
        "kinds": kinds,
        "unmapped": not areas and not kinds,
    }
