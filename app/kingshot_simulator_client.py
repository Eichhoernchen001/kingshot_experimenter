#!/usr/bin/env python3
"""
Shared browser client for https://kingshotsimulator.com/battle/

Experiment scripts should handle:
- how conditions are generated
- condition-specific joiners/leads/troops
- which columns are written to CSV
- resume logic

This module handles the online simulator plus centralized, optional final profile
processing immediately before import:
- optional 5-star hero-stat injection from the internal kingshot_hero_data.json
- optional hero-gear stat addition from kingshot_hero_gear.json
- intuitive control over whether simulator counts hero.stats
- decisive special_bonuses enable/disable handling
- automatic widget-role filtering (offensive attacker / defensive defender)
- deterministic full-catalogue hero stat/gear preparation without repeated stacking
- launch Chromium via Playwright
- import attacker/defender JSON files
- set simulations per batch
- run a battle batch
- wait for queue completion
- parse attacker win rate

One-time installation in the Python environment used by Spyder:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import json
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from kingshot_progression import (
    hero_progression_stats,
    layered_hero_stats,
    load_progression_lookup,
    profile_hero_config,
    progression_enabled,
    widget_skill_rank,
)

from kingshot_special_bonuses import apply_special_bonus_preview

SITE_URL = "https://kingshotsimulator.com/battle/"
PERCENT_RE = re.compile(r"(?<![\d.])(100(?:\.0+)?|\d{1,2}(?:\.\d+)?)\s*%")
ATTACKER_WINRATE_RE = re.compile(
    r"\battacker\s*\(\s*(100(?:\.0+)?|\d{1,2}(?:\.\d+)?)\s*%\s*win\s*rate\s*\)",
    re.I,
)
QUEUE_STATUS_RE = re.compile(r"queue\s+status\s*:\s*([^\r\n]+)", re.I)
RESULT_RENDER_GRACE_SECONDS = 5.0

# Import enum names are not always the display names used by hero guides.
# Keep this fallback for existing/custom lookup files without simulator_name.
# The live importer rejects heroes["Wee & Woo"] even when that hero is unselected.
SIMULATOR_HERO_NAME_FALLBACKS = {"weewoo": "Weewoo"}


class SimulatorError(RuntimeError):
    """Raised when the current simulator page cannot be driven or parsed."""


def _chromium_launch_options(headless: bool) -> dict[str, Any]:
    """Use regular Chromium's reliable new-headless path when headless is enabled."""
    options: dict[str, Any] = {"headless": bool(headless)}
    if headless:
        # Without an explicit channel Playwright launches its separate legacy
        # headless shell. The regular Chromium channel uses the modern headless
        # implementation and behaves much closer to the headed browser.
        options["channel"] = "chromium"
    return options


def _looks_like_closed_target(error: BaseException) -> bool:
    """Recognize Playwright errors caused by a closed/crashed page or browser."""
    message = f"{type(error).__name__}: {error}".casefold()
    return any(
        marker in message
        for marker in (
            "targetclosederror",
            "target page, context or browser has been closed",
            "page has been closed",
            "browser has been closed",
            "browser closed",
            "page crashed",
            "browser disconnected",
        )
    )


def _hero_key(name: str) -> str:
    """Case/punctuation-insensitive key for hero-name lookup."""
    return re.sub(r"[^a-z0-9]+", "", str(name).casefold())


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulatorError(f"Could not read {label} JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SimulatorError(f"{label} JSON must contain one object: {path}")
    return value


def load_5star_hero_lookup(path: Path) -> dict[str, dict[str, Any]]:
    """Load and normalize the maxed 5-star hero-stat/widget-role lookup.

    ``stats`` is the complete maxed Expedition hero-stat package. For Mythic
    heroes this already includes the maximum passive Lethality/Health granted
    by maxed Exclusive Gear/widget. Widget activation controls only the
    separate widget skill/buff under ``special_bonuses.widgetLevels``.
    """
    data = _load_json_object(Path(path), "hero-stat lookup")
    heroes = data.get("heroes")
    if not isinstance(heroes, dict) or not heroes:
        raise SimulatorError(f"Hero-stat lookup has no non-empty 'heroes' object: {path}")

    by_key: dict[str, dict[str, Any]] = {}
    for canonical_name, entry in heroes.items():
        if not isinstance(entry, dict):
            raise SimulatorError(f"Invalid lookup entry for {canonical_name!r} in {path}")
        stats = entry.get("stats")
        if not isinstance(stats, dict) and isinstance(entry.get("max_5star_base_stats"), dict):
            base = entry["max_5star_base_stats"]
            passive = entry.get("exclusive_gear", {}).get("max_passive_expedition", {})
            stats = {
                "attack": float(base.get("attack", 0.0)),
                "defense": float(base.get("defense", base.get("attack", 0.0))),
                "lethality": float(passive.get("lethality", 0.0)),
                "health": float(passive.get("health", 0.0)),
            }
            entry = copy.deepcopy(entry)
            entry["stats"] = stats
            widget = entry.get("exclusive_gear", {})
            entry["has_widget"] = bool(widget.get("has_widget", False))
            entry["widget_type"] = widget.get("widget_type")
        if not isinstance(stats, dict):
            raise SimulatorError(f"Lookup entry {canonical_name!r} has no 'stats' object")
        missing = [s for s in ("attack", "defense", "lethality", "health") if s not in stats]
        if missing:
            raise SimulatorError(
                f"Lookup entry {canonical_name!r} is missing simulator stat(s): {missing}"
            )
        for stat_name in ("attack", "defense", "lethality", "health"):
            try:
                float(stats[stat_name])
            except (TypeError, ValueError) as exc:
                raise SimulatorError(
                    f"Lookup entry {canonical_name!r} has non-numeric {stat_name}."
                ) from exc

        rarity = str(entry.get("rarity", "")).casefold()
        has_widget = bool(entry.get("has_widget", False))
        widget_type = entry.get("widget_type")
        if rarity == "mythic":
            if not has_widget or widget_type not in {"offensive", "defensive"}:
                raise SimulatorError(
                    f"Lookup entry {canonical_name!r} is Mythic but does not have a valid "
                    "widget_type ('offensive' or 'defensive')."
                )
        elif has_widget or widget_type is not None:
            raise SimulatorError(
                f"Lookup entry {canonical_name!r} is non-Mythic but is marked as having a widget."
            )

        skill_slots = entry.get("skill_slots", ["1", "2", "3"])
        if (
            not isinstance(skill_slots, list)
            or not skill_slots
            or any(str(slot) not in {"1", "2", "3"} for slot in skill_slots)
            or len({str(slot) for slot in skill_slots}) != len(skill_slots)
        ):
            raise SimulatorError(
                f"Lookup entry {canonical_name!r} has invalid skill_slots; "
                "expected a unique non-empty list using only '1', '2', '3'."
            )

        record = copy.deepcopy(entry)
        record["skill_slots"] = [str(slot) for slot in skill_slots]
        record["skill_slots_explicit"] = "skill_slots" in entry
        record["canonical_name"] = str(canonical_name)
        record["simulator_name"] = str(
            entry.get("simulator_name")
            or SIMULATOR_HERO_NAME_FALLBACKS.get(_hero_key(canonical_name), canonical_name)
        )
        names = [canonical_name, record["simulator_name"], *entry.get("aliases", [])]
        for name in names:
            key = _hero_key(name)
            if not key:
                continue
            if key in by_key and by_key[key]["canonical_name"] != canonical_name:
                raise SimulatorError(f"Ambiguous hero alias {name!r} in lookup {path}")
            by_key[key] = record
    return by_key

def load_hero_gear_lookup(path: Path) -> dict[str, dict[str, dict[str, float]]]:
    """Load hero-gear bonuses keyed by battle side and simulator hero/troop type.

    Preferred schema::

        {"gear_by_side": {"attacker": {...}, "defender": {...}}}

    Legacy ``gear_by_type`` files are still accepted and applied identically to
    both sides, so older lookup files do not suddenly stop working.
    """
    data = _load_json_object(Path(path), "hero-gear lookup")
    raw_by_side = data.get("gear_by_side")

    if raw_by_side is None:
        legacy = data.get("gear_by_type")
        if not isinstance(legacy, dict):
            raise SimulatorError(
                f"Hero-gear lookup has no 'gear_by_side' object: {path}"
            )
        raw_by_side = {"attacker": legacy, "defender": copy.deepcopy(legacy)}

    if not isinstance(raw_by_side, dict):
        raise SimulatorError(f"Hero-gear lookup 'gear_by_side' must be an object: {path}")

    required_sides = ("attacker", "defender")
    required_types = ("inf", "lanc", "mark")
    required_stats = ("attack", "defense", "lethality", "health")
    result: dict[str, dict[str, dict[str, float]]] = {}

    for side in required_sides:
        raw_side = raw_by_side.get(side)
        if not isinstance(raw_side, dict):
            raise SimulatorError(
                f"Hero-gear lookup is missing a '{side}' object under gear_by_side: {path}"
            )
        side_result: dict[str, dict[str, float]] = {}
        for hero_type in required_types:
            entry = raw_side.get(hero_type)
            if not isinstance(entry, dict):
                raise SimulatorError(
                    f"Hero-gear lookup '{side}' is missing a '{hero_type}' object: {path}"
                )
            missing = [name for name in required_stats if name not in entry]
            if missing:
                raise SimulatorError(
                    f"Hero-gear lookup '{side}/{hero_type}' is missing stat(s) {missing}: {path}"
                )
            try:
                side_result[hero_type] = {
                    name: float(entry[name]) for name in required_stats
                }
            except (TypeError, ValueError) as exc:
                raise SimulatorError(
                    f"Hero-gear lookup '{side}/{hero_type}' contains a non-numeric stat: {path}"
                ) from exc
        result[side] = side_result

    return result


def _normalise_profile_hero_names(
    profile: dict[str, Any],
    hero_stats_lookup: Path | None,
    label: str,
) -> None:
    """Rewrite hero references to the exact names accepted by the simulator.

    The human-facing lookup may use a prettier canonical name while the simulator
    import schema expects a different spelling.  ``simulator_name`` in the hero
    lookup is authoritative for generated JSON; aliases remain accepted as input.
    """
    if hero_stats_lookup is None:
        return

    lookup = load_5star_hero_lookup(Path(hero_stats_lookup))

    def simulator_name(raw_name: Any) -> str:
        text = str(raw_name).strip()
        entry = lookup.get(_hero_key(text))
        if entry is None:
            return text
        return str(entry.get("simulator_name") or entry["canonical_name"])

    heroes = profile.get("heroes")
    if isinstance(heroes, dict):
        rebuilt: dict[str, Any] = {}
        source_priority: dict[str, int] = {}
        for old_key, raw_hero in heroes.items():
            hero = copy.deepcopy(raw_hero)
            nested_name = hero.get("name", old_key) if isinstance(hero, dict) else old_key
            target = simulator_name(nested_name)
            if isinstance(hero, dict):
                hero["name"] = target

            # Prefer an entry that already used the simulator spelling.  This
            # matters when a GUI-selected alias added a temporary duplicate beside
            # the same hero already present in the baseline JSON.
            priority = int(_hero_key(old_key) == _hero_key(target)) + int(
                _hero_key(nested_name) == _hero_key(target)
            )
            if target not in rebuilt or priority > source_priority[target]:
                rebuilt[target] = hero
                source_priority[target] = priority
        profile["heroes"] = rebuilt

    selected = profile.get("selectedHeroes")
    if isinstance(selected, list):
        profile["selectedHeroes"] = [simulator_name(name) for name in selected]

    joiners = profile.get("joiners")
    if isinstance(joiners, list):
        for row in joiners:
            if isinstance(row, dict) and row.get("name"):
                row["name"] = simulator_name(row["name"])

    special = profile.get("special_bonuses")
    if isinstance(special, dict) and isinstance(special.get("widgetLevels"), dict):
        widget_levels = {}
        for name, level in special["widgetLevels"].items():
            widget_levels[simulator_name(name)] = level
        special["widgetLevels"] = widget_levels


def get_hero_widget_type(name: str, lookup_path: Path) -> str | None:
    """Return 'offensive', 'defensive', or None for a hero without a widget."""
    lookup = load_5star_hero_lookup(Path(lookup_path))
    entry = lookup.get(_hero_key(name))
    if entry is None:
        raise SimulatorError(f"Hero {name!r} was not found in {lookup_path}.")
    return entry.get("widget_type")


def _apply_selected_hero_stats(
    profile: dict[str, Any],
    *,
    add_5star_hero_stats: bool,
    hero_stats_lookup: Path | None,
    add_hero_gear: bool,
    hero_gear_lookup: Path | None,
    side: str,
    label: str,
) -> list[dict[str, Any]]:
    """Populate the verified hero catalogue and deterministically set stats.

    Every verified hero is retained in every prepared profile when a hero lookup
    is available. If 5-star stats and/or gear are enabled, stats are recomputed
    from lookup values from scratch; existing profile values are never used as an
    additive base, so repeated preparation cannot accumulate bonuses. If neither
    layer is enabled, existing hero stats are preserved and missing catalogue
    heroes are added with zero stats.
    """
    if side not in {"attacker", "defender"}:
        raise SimulatorError(f"{label}: side must be 'attacker' or 'defender', got {side!r}.")

    if hero_stats_lookup is None:
        if add_5star_hero_stats or add_hero_gear:
            raise SimulatorError(
                "hero_stats_lookup is required when 5-star hero stats or hero gear are enabled."
            )
        return []
    hero_lookup_by_key = load_5star_hero_lookup(Path(hero_stats_lookup))

    gear_lookup = None
    if add_hero_gear:
        if hero_gear_lookup is None:
            raise SimulatorError("hero_gear_lookup is required when ADD_HERO_GEAR=True.")
        gear_lookup = load_hero_gear_lookup(Path(hero_gear_lookup))

    heroes = profile.setdefault("heroes", {})
    if not isinstance(heroes, dict):
        raise SimulatorError(f"{label}: profile['heroes'] is not a dictionary.")

    catalogue: dict[str, dict[str, Any]] = {}
    for entry in hero_lookup_by_key.values():
        catalogue[str(entry["canonical_name"])] = entry

    existing_by_key = {_hero_key(name): name for name in heroes}
    stat_names = ("attack", "defense", "lethality", "health")
    applied: list[dict[str, Any]] = []

    for canonical_name in sorted(catalogue):
        entry = catalogue[canonical_name]
        simulator_name = str(entry.get("simulator_name") or canonical_name)
        simulator_key = _hero_key(simulator_name)
        hero_key = existing_by_key.get(simulator_key, simulator_name)
        created = hero_key not in heroes

        if created:
            skill_slots = [str(x) for x in entry.get("skill_slots", ("1", "2", "3"))]
            heroes[hero_key] = {
                "name": simulator_name,
                "type": entry.get("type"),
                "stats": {name: 0.0 for name in stat_names},
                "skill_levels": {slot: 5 for slot in skill_slots},
                "widget_level": 0,
            }
            existing_by_key[simulator_key] = hero_key

        hero = heroes[hero_key]
        if not isinstance(hero, dict):
            raise SimulatorError(f"{label}: malformed hero entry for {hero_key!r}.")

        hero_type = entry.get("type")
        if hero_type not in {"inf", "lanc", "mark"}:
            raise SimulatorError(
                f"{label}: lookup entry {canonical_name!r} has unsupported type {hero_type!r}."
            )

        hero["name"] = simulator_name
        hero["type"] = hero_type

        # Preserve each hero's simulator-exported skill-slot structure.  Most
        # heroes use 1/2/3, but at least Saul is exported with 1/3 only.  Adding
        # a nonexistent slot makes the simulator reject the whole heroes object.
        raw_skill_levels = hero.get("skill_levels")
        if entry.get("skill_slots_explicit"):
            # Hero-specific schema metadata is authoritative (Saul is currently
            # the known exception: slots 1 and 3, with no slot 2).
            skill_slots = [str(x) for x in entry["skill_slots"]]
            hero["skill_levels"] = {slot: 5 for slot in skill_slots}
        elif isinstance(raw_skill_levels, dict) and raw_skill_levels:
            hero["skill_levels"] = {str(slot): 5 for slot in raw_skill_levels}
        else:
            skill_slots = [str(x) for x in entry.get("skill_slots", ("1", "2", "3"))]
            hero["skill_levels"] = {slot: 5 for slot in skill_slots}

        if add_5star_hero_stats or add_hero_gear:
            # Maxed-profile generation: reset stats first, then add the selected
            # layers. The legacy hero.widget_level field is deliberately left
            # untouched; active widget skills are controlled only through
            # special_bonuses.widgetLevels, and passive max widget stats already
            # live in hero.stats.
            if add_5star_hero_stats:
                base_stats = {name: float(entry["stats"][name]) for name in stat_names}
                base_source = "maxed 5-star hero lookup"
            else:
                base_stats = {name: 0.0 for name in stat_names}
                base_source = "zero base (gear-only mode)"

            gear_stats = (
                dict(gear_lookup[side][hero_type])
                if gear_lookup is not None
                else {name: 0.0 for name in stat_names}
            )
            final_stats = {
                name: float(base_stats[name]) + float(gear_stats[name])
                for name in stat_names
            }
            hero["stats"] = final_stats
        else:
            # Catalogue-only mode: preserve source stats exactly; newly created
            # heroes already have a zero-stat block.
            raw_stats = hero.get("stats")
            if not isinstance(raw_stats, dict):
                raw_stats = {name: 0.0 for name in stat_names}
                hero["stats"] = raw_stats
            for name in stat_names:
                raw_stats.setdefault(name, 0.0)
            base_stats = {name: float(raw_stats[name]) for name in stat_names}
            gear_stats = {name: 0.0 for name in stat_names}
            final_stats = copy.deepcopy(base_stats)
            base_source = "preserved source stats" if not created else "new zero-stat catalogue entry"

        applied.append({
            "hero": canonical_name,
            "simulator_name": simulator_name,
            "type": hero_type,
            "generation": entry.get("generation"),
            "base_source": base_source,
            "base_hero_stats": copy.deepcopy(base_stats),
            "gear_stats_added": copy.deepcopy(gear_stats),
            "final_hero_stats": copy.deepcopy(final_stats),
        })

    # Unknown heroes are preserved rather than guessed or deleted.
    unverified = [
        str(name) for name in heroes if _hero_key(name) not in hero_lookup_by_key
    ]
    if unverified:
        print(
            f"{label}: leaving {len(unverified)} hero(s) outside the verified lookup "
            f"unchanged: {', '.join(sorted(unverified))}",
            flush=True,
        )

    return applied

def _validate_special_bonus_config(config: Any, label: str) -> dict[str, Any]:
    """Validate per-item pet/city/appointment overrides.

    widgetLevels is deliberately not stored in the external special-stat files.
    Experiment scripts request max widget level for selected leads; this client
    filters those requests by widget role for attacker vs defender.
    """
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise SimulatorError(f"{label}: special bonus overrides must be a dict or None.")

    allowed = {"petLevels", "city", "appointment"}
    extra = [key for key in config if key not in allowed]
    if extra:
        raise SimulatorError(
            f"{label}: unsupported special-bonus section(s) {extra}. "
            "widgetLevels is controlled automatically from the selected lead heroes."
        )

    for section, values in config.items():
        if values is not None and not isinstance(values, dict):
            raise SimulatorError(
                f"{label}: {section} overrides must be a dict or None."
            )
    try:
        json.dumps(config)
    except (TypeError, ValueError) as exc:
        raise SimulatorError(f"{label}: special bonus overrides are not JSON serializable.") from exc
    return copy.deepcopy(config)


def _merge_individual_special_overrides(
    special: dict[str, Any],
    config: dict[str, Any] | None,
    label: str,
) -> None:
    """Apply the external special-stat file as the authoritative side profile."""
    cfg = _validate_special_bonus_config(config, label)
    for section in ("petLevels", "city", "appointment"):
        values = cfg.get(section)
        if values is not None:
            special[section] = copy.deepcopy(values)


def _requested_widget_levels(profile: dict[str, Any], label: str) -> dict[str, int]:
    special = profile.get("special_bonuses")
    if not isinstance(special, dict):
        return {}
    raw = special.get("widgetLevels", {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SimulatorError(f"{label}: special_bonuses.widgetLevels is not an object.")

    result: dict[str, int] = {}
    for raw_name, raw_level in raw.items():
        name = str(raw_name).strip()
        if not name:
            continue
        try:
            level = int(raw_level)
        except Exception as exc:
            raise SimulatorError(
                f"{label}: widget level for {name!r} must be an integer from 0 to 5."
            ) from exc
        if not 0 <= level <= 5:
            raise SimulatorError(
                f"{label}: widget level for {name!r} must be between 0 and 5."
            )
        if level:
            result[name] = level
    return result


def _effective_widget_levels(
    profile: dict[str, Any],
    lookup_path: Path,
    side: str,
    label: str,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Keep only role-appropriate widgets for the current battle side."""
    if side not in {"attacker", "defender"}:
        raise SimulatorError(f"Unknown simulator side for widget filtering: {side!r}")
    required_type = "offensive" if side == "attacker" else "defensive"
    lookup = load_5star_hero_lookup(Path(lookup_path))

    selected_raw = profile.get("selectedHeroes", [])
    if not isinstance(selected_raw, list):
        selected_raw = []
    selected_keys = {_hero_key(name) for name in selected_raw if str(name).strip()}
    requested = _requested_widget_levels(profile, label)

    active: dict[str, int] = {}
    decisions: list[dict[str, Any]] = []
    for hero_name, level in requested.items():
        key = _hero_key(hero_name)
        if key not in selected_keys:
            decisions.append({
                "hero": hero_name,
                "requested_level": level,
                "widget_type": None,
                "active": False,
                "reason": "hero is not selected as a lead",
            })
            continue
        entry = lookup.get(key)
        if entry is None:
            raise SimulatorError(
                f"{label}: widget level requested for {hero_name!r}, but that hero is "
                f"not present in {lookup_path}."
            )
        widget_type = entry.get("widget_type")
        has_widget = bool(entry.get("has_widget", False))
        is_active = has_widget and widget_type == required_type
        if is_active:
            active[hero_name] = level
        decisions.append({
            "hero": hero_name,
            "canonical_name": entry.get("canonical_name"),
            "requested_level": level,
            "widget_type": widget_type,
            "active": is_active,
            "reason": (
                f"{required_type} widget matches {side} side"
                if is_active
                else (
                    "hero has no widget"
                    if not has_widget
                    else f"{widget_type} widget does not activate on {side} side"
                )
            ),
        })
    return active, decisions


def _apply_special_bonus_policy(
    profile: dict[str, Any],
    config: dict[str, Any] | None,
    lookup_path: Path | None,
    side: str,
    label: str,
    apply_special_bonuses: bool,
) -> list[dict[str, Any]]:
    """Make APPLY_SPECIAL_BONUSES the single switch for all special bonuses."""
    special = profile.setdefault("special_bonuses", {})
    if not isinstance(special, dict):
        raise SimulatorError(f"{label}: profile special_bonuses is not an object.")

    # The A/B special-stat file remains authoritative even while disabled, so
    # archived profiles always show which pet/city/appointment package belongs
    # to that profile. The switch below controls only whether the simulator adds it.
    _merge_individual_special_overrides(special, config, label)

    if not apply_special_bonuses:
        special["includedInStats"] = True
        special["widgetLevels"] = {}
        return []

    if lookup_path is None or not Path(lookup_path).is_file():
        raise SimulatorError(
            "APPLY_SPECIAL_BONUSES=True requires HERO_STATS_LOOKUP so widget roles "
            "can be determined safely."
        )

    special["includedInStats"] = False
    active_widgets, decisions = _effective_widget_levels(
        profile, Path(lookup_path), side, label
    )
    special["widgetLevels"] = active_widgets
    return decisions



def _zero_non_widget_specials(special: dict[str, Any]) -> None:
    """Disable pet/city/appointment bonuses while leaving widgetLevels independent."""
    for group in ("petLevels", "city", "appointment"):
        value = special.get(group)
        if isinstance(value, dict):
            for key in list(value):
                if isinstance(value[key], (int, float)) or value[key] is None:
                    value[key] = 0


def _widget_requests_from_progression(
    profile: dict[str, Any],
    progression_lookup: Path | None,
    progression_config: dict[str, Any] | None,
    active_widget_heroes: Iterable[str] | None = None,
) -> dict[str, int]:
    if progression_lookup is None or progression_config is None:
        return {}
    try:
        heroes = load_progression_lookup(Path(progression_lookup))
    except Exception as exc:
        raise SimulatorError(f"Could not read hero progression lookup {progression_lookup}: {exc}") from exc
    selected = profile.get("selectedHeroes", [])
    if not isinstance(selected, list):
        return {}
    by_key: dict[str, tuple[str, dict[str, Any]]] = {}
    for canonical, entry in heroes.items():
        names = [canonical, entry.get("simulator_name", canonical), *entry.get("aliases", [])]
        for name in names:
            by_key[_hero_key(name)] = (canonical, entry)
    allowed = None
    if active_widget_heroes is not None:
        allowed = {_hero_key(name) for name in active_widget_heroes if str(name).strip()}
    result: dict[str, int] = {}
    # Even when imported source stats are preserved, the configured widget level
    # controls the active widget skill. This makes the UI's widget setting useful
    # independently from whether star/gear stats are overwritten.
    for selected_name in selected:
        match = by_key.get(_hero_key(selected_name))
        if not match:
            continue
        canonical, entry = match
        if allowed is not None:
            candidate_keys = {
                _hero_key(selected_name),
                _hero_key(canonical),
                _hero_key(entry.get("simulator_name", canonical)),
            }
            if allowed.isdisjoint(candidate_keys):
                continue
        if not entry.get("exclusive_gear", {}).get("has_widget"):
            continue
        cfg = profile_hero_config({"hero_progression": progression_config}, canonical)
        rank = widget_skill_rank(int(cfg.get("widget_level", 0)))
        if rank:
            result[str(entry.get("simulator_name") or canonical)] = rank
    return result


def _apply_configured_progression(
    profile: dict[str, Any],
    *,
    hero_schema_lookup: Path | None,
    progression_lookup: Path,
    progression_config: dict[str, Any],
    gear_config: dict[str, Any] | None,
    side: str,
    label: str,
) -> list[dict[str, Any]]:
    """Overwrite hero.stats from explicit stars + widget passive + gear.

    The operation is deterministic and idempotent: source hero.stats are never
    used as an additive starting point when configured progression is enabled.
    """
    # Preserve the live-verified catalogue shape/name normalization path.
    _apply_selected_hero_stats(
        profile,
        add_5star_hero_stats=False,
        hero_stats_lookup=hero_schema_lookup,
        add_hero_gear=False,
        hero_gear_lookup=None,
        side=side,
        label=label,
    )
    if not progression_enabled({"hero_progression": progression_config}):
        return []

    try:
        lookup = load_progression_lookup(Path(progression_lookup))
    except Exception as exc:
        raise SimulatorError(f"Could not read hero progression lookup {progression_lookup}: {exc}") from exc
    schema = load_5star_hero_lookup(Path(hero_schema_lookup)) if hero_schema_lookup else {}
    heroes = profile.get("heroes")
    if not isinstance(heroes, dict):
        raise SimulatorError(f"{label}: profile['heroes'] is not a dictionary.")
    by_existing = {_hero_key(name): name for name in heroes}
    role_for_type = {"inf": "inf", "lanc": "cav", "mark": "arch"}
    applied: list[dict[str, Any]] = []

    for canonical, entry in lookup.items():
        simulator_name = str(entry.get("simulator_name") or canonical)
        # Prefer the exact simulator spelling resolved by the schema lookup.
        schema_entry = schema.get(_hero_key(canonical)) if schema else None
        if schema_entry:
            simulator_name = str(schema_entry.get("simulator_name") or simulator_name)
        existing_key = by_existing.get(_hero_key(simulator_name))
        if existing_key is None:
            # Normally impossible because _apply_selected_hero_stats populated
            # the catalogue; keep this defensive branch for custom lookups.
            heroes[simulator_name] = {
                "name": simulator_name,
                "type": entry.get("type"),
                "stats": {k: 0.0 for k in ("attack","defense","lethality","health")},
                "skill_levels": {"1": 5, "2": 5, "3": 5},
                "widget_level": 0,
            }
            existing_key = simulator_name
            by_existing[_hero_key(simulator_name)] = existing_key
        hero_cfg = profile_hero_config({"hero_progression": progression_config}, canonical)
        hero_type = entry.get("type")
        gear_role = role_for_type.get(hero_type)
        gear_set = (gear_config or {}).get(gear_role, {}) if gear_role else {}
        raw_stats = heroes[existing_key].get("stats", {})
        imported = raw_stats if progression_config.get("stars_source", "manual") == "json" else None
        calc = layered_hero_stats(
            entry,
            hero_cfg,
            gear_set,
            use_stars=bool(progression_config.get("stars_enabled", True)),
            use_passive_widget=bool(progression_config.get("widgets_enabled", True)),
            use_gear=bool(progression_config.get("gear_enabled", True)),
            imported_stats=imported if isinstance(imported, dict) else {},
        )
        heroes[existing_key]["stats"] = copy.deepcopy(calc["final_hero_stats"])
        applied.append({
            "hero": canonical,
            "simulator_name": simulator_name,
            "type": hero_type,
            **calc,
        })
    # Explicitly configured progression is meant to be counted in battle.
    profile["stats_include_heroes"] = False
    return applied


def calculate_effective_troop_stats(profile: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Root troop stats plus selected lead hero.stats (widget skill excluded)."""
    stat_names = ("attack", "defense", "lethality", "health")
    root = profile.get("stats", {})
    result: dict[str, dict[str, float]] = {}
    for troop_type in ("inf", "lanc", "mark"):
        row = root.get(troop_type, {}) if isinstance(root, dict) else {}
        result[troop_type] = {name: float(row.get(name, 0.0)) for name in stat_names}
    if profile.get("stats_include_heroes") is True:
        return result
    heroes = profile.get("heroes", {})
    selected = profile.get("selectedHeroes", [])
    if not isinstance(heroes, dict) or not isinstance(selected, list):
        return result
    for hero_name in selected:
        hero = heroes.get(hero_name)
        if not isinstance(hero, dict):
            # Exact name should already be normalized, but tolerate aliases.
            hero = next((v for k, v in heroes.items() if _hero_key(k) == _hero_key(hero_name)), None)
        if not isinstance(hero, dict):
            continue
        troop_type = hero.get("type")
        if troop_type not in result:
            continue
        stats = hero.get("stats", {})
        if not isinstance(stats, dict):
            continue
        for name in stat_names:
            result[troop_type][name] = round(result[troop_type][name] + float(stats.get(name, 0.0)), 4)
    return result


def active_widget_bonus_vector(
    profile: dict[str, Any],
    hero_stats_lookup: Path | None,
) -> dict[str, float]:
    """Return active formation widget Expedition percentages grouped by stat."""
    result = {name: 0.0 for name in ("attack", "defense", "lethality", "health")}
    if hero_stats_lookup is None:
        return result
    special = profile.get("special_bonuses", {})
    levels = special.get("widgetLevels", {}) if isinstance(special, dict) else {}
    if not isinstance(levels, dict) or not levels:
        return result
    lookup = load_5star_hero_lookup(Path(hero_stats_lookup))
    for raw_name, raw_rank in levels.items():
        entry = lookup.get(_hero_key(str(raw_name)))
        if not entry:
            continue
        gear = entry.get("exclusive_gear", {}) if isinstance(entry, dict) else {}
        stat = str(gear.get("active_expedition_stat", "")).strip()
        values = gear.get("active_expedition_values", [5.0, 7.5, 10.0, 12.5, 15.0])
        if stat not in result or not isinstance(values, list) or not values:
            continue
        try:
            rank = max(0, min(int(raw_rank), len(values)))
            if rank:
                result[stat] += float(values[rank - 1])
        except (TypeError, ValueError, IndexError):
            continue
    return result


def format_effective_troop_stats(stats: dict[str, dict[str, float]]) -> str:
    labels = {"inf": "Inf", "lanc": "Cav", "mark": "Arch"}
    parts = []
    for troop_type in ("inf", "lanc", "mark"):
        row = stats[troop_type]
        parts.append(
            f"{labels[troop_type]} A/D/L/H="
            f"{row['attack']:.2f}/{row['defense']:.2f}/{row['lethality']:.2f}/{row['health']:.2f}"
        )
    return "; ".join(parts)

def validate_profile_options(
    *,
    add_5star_hero_stats_attacker: bool,
    add_5star_hero_stats_defender: bool,
    add_hero_gear_attacker: bool,
    add_hero_gear_defender: bool,
    stats_include_heroes_attacker: bool | None,
    stats_include_heroes_defender: bool | None,
    hero_stats_lookup: Path | None,
    hero_gear_lookup: Path | None,
    apply_special_bonuses: bool,
    special_bonuses_attacker: dict[str, Any] | None = None,
    special_bonuses_defender: dict[str, Any] | None = None,
    interactive: bool = True,
) -> tuple[bool | None, bool | None]:
    """Validate side-specific profile options and return effective hero flags."""

    for side, value in (
        ("attacker", stats_include_heroes_attacker),
        ("defender", stats_include_heroes_defender),
    ):
        if value not in (None, True, False):
            raise SimulatorError(
                f"STATS_INCLUDE_HEROES_{'ATK' if side == 'attacker' else 'DEF'} "
                "must be True, False, or None."
            )

    hero_lookup_needed = (
        hero_stats_lookup is not None
        or add_5star_hero_stats_attacker
        or add_5star_hero_stats_defender
        or add_hero_gear_attacker
        or add_hero_gear_defender
        or apply_special_bonuses
    )
    if hero_lookup_needed:
        if hero_stats_lookup is None or not Path(hero_stats_lookup).is_file():
            raise SimulatorError(
                "The selected 5-star/widget options require a readable HERO_STATS_LOOKUP file."
            )
        load_5star_hero_lookup(Path(hero_stats_lookup))

    gear_lookup_needed = add_hero_gear_attacker or add_hero_gear_defender
    if gear_lookup_needed:
        if hero_gear_lookup is None or not Path(hero_gear_lookup).is_file():
            raise SimulatorError(
                "An enabled ADD_HERO_GEAR_ATK/DEF option requires a readable "
                "HERO_GEAR_LOOKUP file."
            )
        load_hero_gear_lookup(Path(hero_gear_lookup))

    def resolve_side(
        side: str,
        add_stats: bool,
        add_gear: bool,
        requested: bool | None,
    ) -> bool | None:
        # Hero-value injection and whether the simulator counts hero.stats are
        # deliberately independent. New scripts expose ADD_HERO_STATS and pass
        # its inverse as stats_include_heroes.
        return requested

    effective_attacker = resolve_side(
        "attacker",
        bool(add_5star_hero_stats_attacker),
        bool(add_hero_gear_attacker),
        stats_include_heroes_attacker,
    )
    effective_defender = resolve_side(
        "defender",
        bool(add_5star_hero_stats_defender),
        bool(add_hero_gear_defender),
        stats_include_heroes_defender,
    )

    _validate_special_bonus_config(special_bonuses_attacker, "attacker")
    _validate_special_bonus_config(special_bonuses_defender, "defender")

    print(
        "Profile options: "
        f"ATK[5STAR={bool(add_5star_hero_stats_attacker)}, "
        f"GEAR={bool(add_hero_gear_attacker)}, "
        f"ADD_HERO_STATS={effective_attacker is False}]; "
        f"DEF[5STAR={bool(add_5star_hero_stats_defender)}, "
        f"GEAR={bool(add_hero_gear_defender)}, "
        f"ADD_HERO_STATS={effective_defender is False}]; "
        f"APPLY_SPECIAL_BONUSES={apply_special_bonuses}. "
        "Widgets are role-filtered automatically (offensive=attacker, defensive=defender).",
        flush=True,
    )
    return effective_attacker, effective_defender


def prepare_profile_json(
    source_path: Path,
    destination_path: Path,
    *,
    add_5star_hero_stats: bool = False,
    add_hero_gear: bool = False,
    stats_include_heroes: bool | None = None,
    hero_stats_lookup: Path | None = None,
    hero_gear_lookup: Path | None = None,
    # v2 progression model. When supplied, these replace the coarse max-stat
    # and flat-gear layers above while retaining them for legacy callers/tests.
    hero_progression_lookup: Path | None = None,
    hero_progression_config: dict[str, Any] | None = None,
    hero_gear_config: dict[str, Any] | None = None,
    apply_special_bonuses: bool = False,
    apply_widget_buffs: bool | None = None,
    active_widget_heroes: Iterable[str] | None = None,
    special_bonuses: dict[str, Any] | None = None,
    side: str | None = None,
    label: str = "profile",
) -> dict[str, Any]:
    """Create one simulator-import JSON with centralized profile changes."""
    profile = _load_json_object(Path(source_path), label)
    effective_side = side or label.casefold()
    if effective_side not in {"attacker", "defender"}:
        raise SimulatorError(
            f"{label}: side must resolve to 'attacker' or 'defender', got {effective_side!r}."
        )
    profile["name"] = effective_side
    _normalise_profile_hero_names(profile, hero_stats_lookup, label)

    formation_ready = bool(profile.pop("_formation_stats_ready", False))
    using_progression = hero_progression_lookup is not None and hero_progression_config is not None
    if formation_ready:
        applied_hero_stats = [{"hero": name, "final_hero_stats": copy.deepcopy(profile["heroes"][name]["stats"])}
                              for name in profile.get("selectedHeroes", [])]
    elif using_progression:
        applied_hero_stats = _apply_configured_progression(
            profile,
            hero_schema_lookup=hero_stats_lookup,
            progression_lookup=Path(hero_progression_lookup),
            progression_config=hero_progression_config,
            gear_config=hero_gear_config,
            side=effective_side,
            label=label,
        )
        # Rebuild requested active widget ranks from the configured Exclusive
        # Gear level instead of hard-coded experiment lead values.
        special = profile.setdefault("special_bonuses", {})
        if not isinstance(special, dict):
            raise SimulatorError(f"{label}: profile special_bonuses is not an object.")
        special["widgetLevels"] = _widget_requests_from_progression(
            profile,
            hero_progression_lookup,
            hero_progression_config,
            active_widget_heroes=active_widget_heroes,
        )
    else:
        applied_hero_stats = _apply_selected_hero_stats(
            profile,
            add_5star_hero_stats=bool(add_5star_hero_stats),
            hero_stats_lookup=hero_stats_lookup,
            add_hero_gear=bool(add_hero_gear),
            hero_gear_lookup=hero_gear_lookup,
            side=effective_side,
            label=label,
        )
        if stats_include_heroes is not None:
            profile["stats_include_heroes"] = bool(stats_include_heroes)

    # Special bonuses and widget skill activation are now independent switches.
    special = profile.setdefault("special_bonuses", {})
    if not isinstance(special, dict):
        raise SimulatorError(f"{label}: profile special_bonuses is not an object.")
    _merge_individual_special_overrides(special, special_bonuses, label)
    if not apply_special_bonuses:
        _zero_non_widget_specials(special)
    widget_enabled = bool(apply_special_bonuses) if apply_widget_buffs is None else bool(apply_widget_buffs)
    special["includedInStats"] = False
    if widget_enabled:
        if hero_stats_lookup is None:
            raise SimulatorError("Widget buffs require the hero schema lookup for role filtering.")
        active_widgets, widget_decisions = _effective_widget_levels(
            profile, Path(hero_stats_lookup), effective_side, label
        )
        special["widgetLevels"] = active_widgets
    else:
        widget_decisions = []
        special["widgetLevels"] = {}
    active_widget_levels = copy.deepcopy(special.get("widgetLevels", {}))

    destination_path = Path(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise SimulatorError(f"Could not write prepared simulator JSON {destination_path}: {exc}") from exc

    effective_stats = calculate_effective_troop_stats(profile)
    return {
        "path": str(destination_path),
        "stats_include_heroes": profile.get("stats_include_heroes"),
        "hero_stats_applied": applied_hero_stats,
        "special_bonuses_included_in_stats": special.get("includedInStats"),
        "active_widget_levels": active_widget_levels,
        "widget_decisions": widget_decisions,
        "effective_troop_stats": effective_stats,
        "effective_troop_stats_text": format_effective_troop_stats(effective_stats),
    }

def write_last_run_stats(
    path: Path,
    *,
    experiment: str,
    attacker_info: dict[str, Any],
    defender_info: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> Path:
    """Persist the final stat snapshot from the most recently completed batch."""
    payload = {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": str(experiment),
        "context": copy.deepcopy(context or {}),
        "attacker": {
            "final_stats": copy.deepcopy(attacker_info.get("final_stats", attacker_info.get("effective_troop_stats", {}))),
            "active_widget_levels": copy.deepcopy(attacker_info.get("active_widget_levels", {})),
        },
        "defender": {
            "final_stats": copy.deepcopy(defender_info.get("final_stats", defender_info.get("effective_troop_stats", {}))),
            "active_widget_levels": copy.deepcopy(defender_info.get("active_widget_levels", {})),
        },
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_experiment_profile_metadata(
    output_csv: Path,
    *,
    resume_from_csv: bool,
    add_5star_hero_stats_attacker: bool,
    add_5star_hero_stats_defender: bool,
    add_hero_gear_attacker: bool,
    add_hero_gear_defender: bool,
    stats_include_heroes_requested_attacker: bool | None,
    stats_include_heroes_requested_defender: bool | None,
    stats_include_heroes_effective_attacker: bool | None,
    stats_include_heroes_effective_defender: bool | None,
    hero_stats_lookup: Path | None,
    hero_gear_lookup: Path | None,
    apply_special_bonuses: bool,
    special_bonuses_attacker: dict[str, Any] | None,
    special_bonuses_defender: dict[str, Any] | None,
) -> Path:
    """Persist side-specific profile-affecting settings beside the results CSV."""
    output_csv = Path(output_csv)
    metadata_path = output_csv.with_name(output_csv.stem + "_experiment_settings.json")

    lookup_info = None
    if (
        (add_5star_hero_stats_attacker or add_5star_hero_stats_defender
         or add_hero_gear_attacker or add_hero_gear_defender or apply_special_bonuses)
        and hero_stats_lookup is not None
        and Path(hero_stats_lookup).is_file()
    ):
        lookup_info = {
            "filename": Path(hero_stats_lookup).name,
            "sha256": _file_sha256(Path(hero_stats_lookup)),
        }

    gear_lookup_info = None
    if (
        (add_hero_gear_attacker or add_hero_gear_defender)
        and hero_gear_lookup is not None
        and Path(hero_gear_lookup).is_file()
    ):
        gear_lookup_info = {
            "filename": Path(hero_gear_lookup).name,
            "sha256": _file_sha256(Path(hero_gear_lookup)),
        }

    profile_options = {
        "add_5star_hero_stats_atk": bool(add_5star_hero_stats_attacker),
        "add_5star_hero_stats_def": bool(add_5star_hero_stats_defender),
        "add_hero_gear_atk": bool(add_hero_gear_attacker),
        "add_hero_gear_def": bool(add_hero_gear_defender),
        "stats_include_heroes_requested_atk": stats_include_heroes_requested_attacker,
        "stats_include_heroes_requested_def": stats_include_heroes_requested_defender,
        "stats_include_heroes_effective_atk": stats_include_heroes_effective_attacker,
        "stats_include_heroes_effective_def": stats_include_heroes_effective_defender,
        "add_hero_stats_atk": stats_include_heroes_effective_attacker is False,
        "add_hero_stats_def": stats_include_heroes_effective_defender is False,
        "hero_stats_lookup": lookup_info,
        "hero_gear_lookup": gear_lookup_info,
        "apply_special_bonuses": bool(apply_special_bonuses),
        "widget_policy": {
            "attacker": "offensive widgets only",
            "defender": "defensive widgets only",
            "levels_source": "selected lead heroes are requested at max widget level by the experiment scripts",
            "active_only_when_apply_special_bonuses": True,
            "max_passive_widget_stats": "already included in maxed 5-star lookup hero.stats; independent of widget activation",
        },
        "special_bonuses_attacker": copy.deepcopy(special_bonuses_attacker),
        "special_bonuses_defender": copy.deepcopy(special_bonuses_defender),
    }
    payload = {
        "schema_version": 7,
        "result_csv": output_csv.name,
        "profile_options": profile_options,
    }

    if resume_from_csv and output_csv.exists() and output_csv.stat().st_size > 0:
        if metadata_path.is_file():
            old = _load_json_object(metadata_path, "experiment settings")
            old_opts = old.get("profile_options")
            # Backward-compatible normalization of the previous global-option metadata.
            if isinstance(old_opts, dict) and "add_5star_hero_stats_atk" not in old_opts:
                global_stats = bool(old_opts.get("add_5star_hero_stats", False))
                global_gear = bool(old_opts.get("add_hero_gear", False))
                global_req = old_opts.get("stats_include_heroes_requested")
                global_eff = old_opts.get("stats_include_heroes_effective")
                old_opts = {
                    **old_opts,
                    "add_5star_hero_stats_atk": global_stats,
                    "add_5star_hero_stats_def": global_stats,
                    "add_hero_gear_atk": global_gear,
                    "add_hero_gear_def": global_gear,
                    "stats_include_heroes_requested_atk": global_req,
                    "stats_include_heroes_requested_def": global_req,
                    "stats_include_heroes_effective_atk": global_eff,
                    "stats_include_heroes_effective_def": global_eff,
                }
                for key in (
                    "add_5star_hero_stats", "add_hero_gear",
                    "stats_include_heroes_requested", "stats_include_heroes_effective",
                ):
                    old_opts.pop(key, None)
            if old_opts != profile_options:
                raise SimulatorError(
                    "RESUME_FROM_CSV=True but the hero-stat/gear/special-bonus settings differ "
                    f"from the existing run metadata: {metadata_path}. Use a new OUTPUT_CSV "
                    "or set RESUME_FROM_CSV=False to avoid mixing different battle setups."
                )
        else:
            nondefault = (
                add_5star_hero_stats_attacker
                or add_5star_hero_stats_defender
                or add_hero_gear_attacker
                or add_hero_gear_defender
                or stats_include_heroes_requested_attacker is not None
                or stats_include_heroes_requested_defender is not None
                or apply_special_bonuses
            )
            if nondefault:
                raise SimulatorError(
                    "RESUME_FROM_CSV=True and the existing CSV predates profile-option "
                    "metadata, while new hero-stat/gear/special-bonus overrides are enabled. "
                    "Use a new OUTPUT_CSV or set RESUME_FROM_CSV=False so incompatible "
                    "battles are not mixed."
                )
            print(
                "Resume warning: existing CSV has no experiment-settings sidecar. "
                "New profile options are all in preserve-original mode, so resume is allowed.",
                flush=True,
            )

    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metadata_path


def _percentages(text: str) -> list[float]:
    return [float(match.group(1)) for match in PERCENT_RE.finditer(text)]


def extract_attacker_winrate(text: str) -> float:
    """Read the attacker win rate without using unrelated troop percentages."""
    direct_match = ATTACKER_WINRATE_RE.search(text)
    if direct_match:
        return round(float(direct_match.group(1)), 6)

    attacker: float | None = None
    defender: float | None = None
    win_lines: list[tuple[str, list[float]]] = []

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        lower = line.lower()
        values = _percentages(line)
        if not values or not re.search(r"\b(win|wins|victor|victory|chance)\b", lower):
            continue
        win_lines.append((lower, values))

        if "attacker" in lower and attacker is None:
            attacker = values[0]
        if "defender" in lower and defender is None:
            defender = values[-1]

    if attacker is None:
        for line, values in win_lines:
            if "attacker" in line and "defender" in line and len(values) >= 2:
                attacker = (
                    values[0]
                    if line.find("attacker") < line.find("defender")
                    else values[-1]
                )
                break

    if attacker is None and defender is not None:
        attacker = 100.0 - defender

    if attacker is None:
        relevant = "\n".join(line for line, _ in win_lines[-10:]) or "<none>"
        raise SimulatorError(
            "The battle completed, but the attacker win rate could not be parsed. "
            f"Win-related lines found:\n{relevant}"
        )

    if not 0.0 <= attacker <= 100.0:
        raise SimulatorError(f"Invalid attacker win rate parsed: {attacker}")

    return round(attacker, 6)


def _queue_status(text: str) -> str | None:
    match = QUEUE_STATUS_RE.search(text)
    return match.group(1).strip() if match else None


async def _first_visible(locators: Iterable[Any]) -> Any | None:
    for locator in locators:
        try:
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
        except Exception:
            continue
    return None


async def _dismiss_cookie_dialog(page: Any, wait_seconds: float = 8.0) -> None:
    """Dismiss Google Funding Choices, including iframe and two-step variants."""
    reject_name = re.compile(
        r"(?:do not|don't)\s+consent|reject(?:\s+all)?|decline(?:\s+all)?|"
        r"continue\s+without\s+(?:accepting|consenting)|"
        r"(?:use\s+)?non[- ]personali[sz]ed\s+ads(?:\s+only)?|"
        r"(?:essential|necessary)\s+(?:cookies\s+)?only|"
        r"nicht\s+zustimmen|alle\s+ablehnen|ohne\s+zustimmung",
        re.I,
    )
    confirm_name = re.compile(
        r"^(?:confirm(?:\s+(?:choices?|selection))?|"
        r"save(?:\s+(?:choices?|selection))?(?:\s+and\s+(?:exit|continue))?|"
        r"submit(?:\s+choices?)?|continue|"
        r"auswahl\s+best[aä]tigen|speichern)$",
        re.I,
    )
    accept_name = re.compile(
        r"^(?:consent|accept(?:\s+all)?|agree|i\s+agree|allow\s+all|"
        r"zustimmen|alle\s+akzeptieren)"
        r"(?:\s+to\s+(?:personali[sz]ed\s+)?(?:ads|advertising))?$",
        re.I,
    )

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        scopes = [page, *page.frames]
        for scope in scopes:
            try:
                button = await _first_visible(
                    [
                        scope.get_by_role("button", name=reject_name),
                        scope.get_by_role("button", name=accept_name),
                    ]
                )
                if button is None:
                    continue

                await button.click(timeout=1_500, force=True)
                await page.wait_for_timeout(350)

                for confirmation_scope in [page, *page.frames]:
                    confirmation = await _first_visible(
                        [
                            confirmation_scope.get_by_role(
                                "button", name=confirm_name
                            )
                        ]
                    )
                    if confirmation is not None:
                        await confirmation.click(timeout=1_500, force=True)
                        await page.wait_for_timeout(250)
                        break

                print("  Advertising consent dialog dismissed.", flush=True)
                return
            except Exception:
                continue

        await page.wait_for_timeout(200)


async def _click_with_consent_retry(page: Any, locator: Any) -> None:
    try:
        await locator.click(timeout=2_500)
    except Exception as first_error:
        await _dismiss_cookie_dialog(page, wait_seconds=2.5)
        try:
            await locator.click(timeout=5_000)
        except Exception as retry_error:
            raise retry_error from first_error


async def _select_side(page: Any, side: str) -> None:
    tab = page.get_by_role("tab", name=re.compile(rf"^{re.escape(side)}$", re.I))
    if not await tab.count():
        raise SimulatorError(
            f"Could not find the {side} tab; the site layout may have changed."
        )
    await _click_with_consent_retry(page, tab.first)


async def _wait_for_import_confirmation(page: Any, side: str, path: Path) -> None:
    side_lower = side.lower()
    success_pattern = re.compile(
        rf"['\"]?{re.escape(side_lower)}['\"]?\s+data\s+successfully\s+"
        rf"imported\s+for\s+side:\s*{re.escape(side_lower)}",
        re.I,
    )
    error_pattern = re.compile(r"\b(invalid|unable|failed|error|rejected)\b", re.I)
    message_nodes = page.locator(
        '[data-sonner-toast], [role="alert"], [role="status"], [aria-live]'
    )
    exact_success_nodes = page.get_by_text(success_pattern, exact=True)

    deadline = time.monotonic() + 5.0
    short_messages: list[str] = []

    while time.monotonic() < deadline:
        try:
            for index in range(await exact_success_nodes.count()):
                if await exact_success_nodes.nth(index).is_visible():
                    return

            count = await message_nodes.count()
            for index in range(count):
                node = message_nodes.nth(index)
                if not await node.is_visible():
                    continue
                text = " ".join((await node.inner_text()).split())
                if not text or len(text) > 500:
                    continue
                if text not in short_messages:
                    short_messages.append(text)
                if success_pattern.search(text):
                    return
        except Exception as exc:
            if _looks_like_closed_target(exc) or page.is_closed():
                raise SimulatorError(
                    f"Chromium closed unexpectedly while {path.name} was being "
                    f"imported for {side_lower}."
                ) from exc

        try:
            await page.wait_for_timeout(100)
        except Exception as exc:
            if _looks_like_closed_target(exc) or page.is_closed():
                raise SimulatorError(
                    f"Chromium closed unexpectedly while {path.name} was being "
                    f"imported for {side_lower}."
                ) from exc
            raise

    errors = [m for m in short_messages if error_pattern.search(m)]
    if errors:
        raise SimulatorError(f"The site rejected {path.name}: {errors[-1]}")

    raise SimulatorError(
        f"The site did not show an import confirmation for {path.name}. "
        "No JSON rejection message was found."
    )


async def _import_profile(page: Any, side: str, path: Path) -> None:
    await _select_side(page, side)

    menu_button = page.get_by_role(
        "button", name=re.compile(r"import.*export", re.I)
    )
    if not await menu_button.count():
        raise SimulatorError("Could not find the Import/Export button.")
    await _click_with_consent_retry(page, menu_button.first)

    item = page.get_by_role(
        "menuitem", name=re.compile(rf"import\s+{re.escape(side)}\s+data", re.I)
    )
    if not await item.count():
        item = page.get_by_role("menuitem", name=re.compile(r"import", re.I))
    if not await item.count():
        raise SimulatorError(f"Could not find the {side} JSON import action.")

    try:
        async with page.expect_file_chooser(timeout=5_000) as chooser_info:
            await _click_with_consent_retry(page, item.first)
        chooser = await chooser_info.value
        await chooser.set_files(str(path))
    except Exception as chooser_error:
        file_inputs = page.locator('input[type="file"]')
        if not await file_inputs.count():
            raise SimulatorError(
                f"Could not open the {side} file chooser: {chooser_error}"
            ) from chooser_error
        await file_inputs.last.set_input_files(str(path))

    await _wait_for_import_confirmation(page, side, path)


async def _set_run_count(page: Any, runs: int) -> None:
    control = await _first_visible(
        [
            page.get_by_label(
                re.compile(r"(number of )?(runs|simulations|battles)", re.I)
            ),
            page.get_by_placeholder(re.compile(r"(runs|simulations|battles)", re.I)),
            page.locator(
                'input[name*="run" i], input[name*="simulation" i], '
                'input[name*="battle" i]'
            ),
            page.locator('input[type="number"]'),
        ]
    )
    if control is None:
        raise SimulatorError(
            "Could not find the number-of-simulations input on the Battle tab."
        )
    await control.fill(str(runs))
    await control.press("Tab")


async def _battle_button(page: Any, require_enabled: bool = True) -> Any:
    locators = [
        page.get_by_role("button", name=re.compile(r"^run\s+simulation$", re.I)),
        page.get_by_role("button", name=re.compile(r"run.*simulation", re.I)),
        page.get_by_role("button", name=re.compile(r"simulate", re.I)),
        page.get_by_role("button", name=re.compile(r"run.*battle|battle.*run", re.I)),
        page.get_by_role("button", name=re.compile(r"start.*battle|fight", re.I)),
    ]
    for locator in locators:
        try:
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if not await candidate.is_visible():
                    continue
                if require_enabled and not await candidate.is_enabled():
                    continue
                return candidate
        except Exception:
            continue

    raise SimulatorError("Could not find the battle simulation button.")


class KingshotSimulatorSession:
    """
    Reusable browser session.

    Use one session for the full experiment. For each condition call
    load_condition(...), then run_batch() as many times as needed.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_seconds: float = 30,
        locale: str = "en-US",
    ):
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self.locale = locale
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._condition_counter = 0
        self._closing_browser = False
        self._browser_events: list[str] = []
        self._site_errors: list[str] = []
        self._loaded_condition: tuple[bytes, bytes, int] | None = None

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:
            raise SimulatorError(
                "Playwright is not installed in Spyder's Python environment. "
                "Run: pip install playwright, then: playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        await self._launch_browser()
        return self

    def _record_browser_event(self, event: str) -> None:
        if self._closing_browser:
            return
        self._browser_events.append(event)
        print(f"  Browser diagnostic: {event}", flush=True)

    def _record_site_error(self, error: Any) -> None:
        message = str(error).strip()
        self._site_errors.append(message)
        print(f"  Site JavaScript diagnostic: {message}", flush=True)

    async def _launch_browser(self) -> None:
        if self._playwright is None:
            raise RuntimeError("Playwright has not been started.")

        self._browser_events = []
        self._site_errors = []
        mode = "new headless" if self.headless else "headed"
        print(f"Launching Chromium ({mode} mode)...", flush=True)
        self.browser = await self._playwright.chromium.launch(
            **_chromium_launch_options(self.headless)
        )
        self.browser.on(
            "disconnected",
            lambda _browser: self._record_browser_event(
                "the Chromium process disconnected or exited"
            ),
        )
        self.context = await self.browser.new_context(locale=self.locale)
        self.page = await self.context.new_page()
        self.page.on(
            "crash",
            lambda _page: self._record_browser_event("the simulator page crashed"),
        )
        self.page.on(
            "close",
            lambda _page: self._record_browser_event("the simulator page closed"),
        )
        self.page.on(
            "pageerror",
            self._record_site_error,
        )
        self.page.set_default_timeout(int(self.timeout_seconds * 1000))

    async def _close_browser(self) -> None:
        self._closing_browser = True
        try:
            if self.context is not None:
                try:
                    await self.context.close()
                except Exception:
                    pass
            if self.browser is not None:
                try:
                    await self.browser.close()
                except Exception:
                    pass
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self._closing_browser = False

    async def _restart_browser(self) -> None:
        await self._close_browser()
        await self._launch_browser()

    def _connection_was_lost(self, error: BaseException) -> bool:
        if self._browser_events or _looks_like_closed_target(error):
            return True
        if self.page is None or self.page.is_closed():
            return True
        return self.browser is None or not self.browser.is_connected()

    def _connection_error(self, action: str) -> str:
        details = "; ".join(self._browser_events[-3:])
        suffix = f" Browser events: {details}." if details else ""
        if self._site_errors:
            suffix += f" Last site error: {self._site_errors[-1]}."
        retry_note = (
            "The headless session was already retried once."
            if self.headless
            else "The headed browser was not restarted automatically."
        )
        return (
            f"Chromium closed unexpectedly while {action}.{suffix} "
            f"{retry_note} If this persists, "
            "turn off 'Headless browser' to confirm whether the local Chromium "
            "renderer or security software is terminating the process."
        )

    async def __aexit__(self, exc_type, exc, tb):
        await self._close_browser()
        if self._playwright is not None:
            await self._playwright.stop()

    async def load_condition(
        self,
        attacker_json: Path,
        defender_json: Path,
        simulations_per_batch: int,
        *,
        add_5star_hero_stats_attacker: bool = False,
        add_5star_hero_stats_defender: bool = False,
        add_hero_gear_attacker: bool = False,
        add_hero_gear_defender: bool = False,
        stats_include_heroes_attacker: bool | None = None,
        stats_include_heroes_defender: bool | None = None,
        hero_stats_lookup: Path | None = None,
        hero_gear_lookup: Path | None = None,
        hero_progression_lookup: Path | None = None,
        hero_progression_config_attacker: dict[str, Any] | None = None,
        hero_progression_config_defender: dict[str, Any] | None = None,
        hero_gear_config_attacker: dict[str, Any] | None = None,
        hero_gear_config_defender: dict[str, Any] | None = None,
        apply_special_bonuses: bool = False,
        apply_widget_buffs: bool | None = None,
        apply_special_bonuses_attacker: bool | None = None,
        apply_special_bonuses_defender: bool | None = None,
        apply_widget_buffs_attacker: bool | None = None,
        apply_widget_buffs_defender: bool | None = None,
        active_widget_heroes_attacker: Iterable[str] | None = None,
        active_widget_heroes_defender: Iterable[str] | None = None,
        special_bonuses_attacker: dict[str, Any] | None = None,
        special_bonuses_defender: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.page is None:
            raise RuntimeError("Simulator session is not open.")

        self._condition_counter += 1
        page = self.page

        atk_special = bool(apply_special_bonuses) if apply_special_bonuses_attacker is None else bool(apply_special_bonuses_attacker)
        def_special = bool(apply_special_bonuses) if apply_special_bonuses_defender is None else bool(apply_special_bonuses_defender)
        atk_widgets = apply_widget_buffs if apply_widget_buffs_attacker is None else apply_widget_buffs_attacker
        def_widgets = apply_widget_buffs if apply_widget_buffs_defender is None else apply_widget_buffs_defender

        # The client always prepares temporary profiles. Side-specific switches
        # override the legacy shared flags while preserving old callers.
        with tempfile.TemporaryDirectory(prefix="kingshot_client_profiles_") as tmp:
            tmpdir = Path(tmp)
            prepared_attacker = tmpdir / "attacker_import.json"
            prepared_defender = tmpdir / "defender_import.json"
            attacker_info = prepare_profile_json(
                Path(attacker_json),
                prepared_attacker,
                add_5star_hero_stats=add_5star_hero_stats_attacker,
                add_hero_gear=add_hero_gear_attacker,
                stats_include_heroes=stats_include_heroes_attacker,
                hero_stats_lookup=hero_stats_lookup,
                hero_gear_lookup=hero_gear_lookup,
                hero_progression_lookup=hero_progression_lookup,
                hero_progression_config=hero_progression_config_attacker,
                hero_gear_config=hero_gear_config_attacker,
                apply_special_bonuses=atk_special,
                apply_widget_buffs=atk_widgets,
                active_widget_heroes=active_widget_heroes_attacker,
                special_bonuses=special_bonuses_attacker,
                side="attacker",
                label="attacker",
            )
            defender_info = prepare_profile_json(
                Path(defender_json),
                prepared_defender,
                add_5star_hero_stats=add_5star_hero_stats_defender,
                add_hero_gear=add_hero_gear_defender,
                stats_include_heroes=stats_include_heroes_defender,
                hero_stats_lookup=hero_stats_lookup,
                hero_gear_lookup=hero_gear_lookup,
                hero_progression_lookup=hero_progression_lookup,
                hero_progression_config=hero_progression_config_defender,
                hero_gear_config=hero_gear_config_defender,
                apply_special_bonuses=def_special,
                apply_widget_buffs=def_widgets,
                active_widget_heroes=active_widget_heroes_defender,
                special_bonuses=special_bonuses_defender,
                side="defender",
                label="defender",
            )

            # Build the same human-readable final percentage totals shown by the GUI.
            # Prepared profiles already contain the exact enabled pet/city packages and
            # role-filtered active widget ranks used for this condition.
            prepared_atk_profile = _load_json_object(prepared_attacker, "prepared attacker")
            prepared_def_profile = _load_json_object(prepared_defender, "prepared defender")
            attacker_info["active_widget_bonus_vector"] = active_widget_bonus_vector(prepared_atk_profile, hero_stats_lookup)
            defender_info["active_widget_bonus_vector"] = active_widget_bonus_vector(prepared_def_profile, hero_stats_lookup)
            attacker_info["final_stats"] = apply_special_bonus_preview(
                attacker_info["effective_troop_stats"],
                own_special=prepared_atk_profile.get("special_bonuses"),
                opponent_special=prepared_def_profile.get("special_bonuses"),
                own_enabled=True,
                opponent_enabled=True,
                own_extra_positive=attacker_info["active_widget_bonus_vector"],
            )
            defender_info["final_stats"] = apply_special_bonus_preview(
                defender_info["effective_troop_stats"],
                own_special=prepared_def_profile.get("special_bonuses"),
                opponent_special=prepared_atk_profile.get("special_bonuses"),
                own_enabled=True,
                opponent_enabled=True,
                own_extra_positive=defender_info["active_widget_bonus_vector"],
            )
            attacker_info["final_stats_text"] = format_effective_troop_stats(attacker_info["final_stats"])
            defender_info["final_stats_text"] = format_effective_troop_stats(defender_info["final_stats"])

            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            attempt = 0
            display_retries = 0
            while True:
                if self.page is None:
                    raise RuntimeError("Simulator session is not open.")
                page = self.page
                started = time.monotonic()
                try:
                    print("  Opening simulator...", flush=True)
                    await page.goto(SITE_URL, wait_until="domcontentloaded")
                    await _dismiss_cookie_dialog(
                        page,
                        wait_seconds=2.0 if self._condition_counter == 1 else 0.25,
                    )

                    print("  Importing generated attacker JSON...", flush=True)
                    await _import_profile(page, "Attacker", prepared_attacker)

                    print("  Importing generated defender JSON...", flush=True)
                    await _import_profile(page, "Defender", prepared_defender)

                    print("  Opening Battle tab...", flush=True)
                    await _select_side(page, "Battle")
                    await _set_run_count(page, simulations_per_batch)
                    self._loaded_condition = (prepared_attacker.read_bytes(), prepared_defender.read_bytes(), simulations_per_batch)
                    return attacker_info, defender_info
                except Exception as exc:
                    connection_lost = self._connection_was_lost(exc)
                    if connection_lost and self.headless and attempt == 0:
                        print(
                            "  Chromium closed unexpectedly; restarting the "
                            "headless browser and retrying this condition once...",
                            flush=True,
                        )
                        await self._restart_browser()
                        attempt += 1
                        continue
                    if connection_lost:
                        raise SimulatorError(
                            self._connection_error("loading the simulator condition")
                        ) from exc
                    if isinstance(exc, (asyncio.TimeoutError, PlaywrightTimeoutError, SimulatorError)):
                        remaining = self.timeout_seconds - (time.monotonic() - started)
                        if remaining > 0:
                            await asyncio.sleep(remaining)
                        display_retries += 1
                        print(f"  Simulator page not ready; reloading and repeating this condition "
                              f"(retry {display_retries}, timeout {self.timeout_seconds:g}s). {exc}", flush=True)
                        continue
                    raise

    async def run_batch(self) -> float:
        """Retry a missing result without advancing the condition or CSV batch."""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        retry = 0
        while True:
            started = time.monotonic()
            try:
                if retry:
                    await asyncio.wait_for(self._reload_current_condition(), self.timeout_seconds)
                return await asyncio.wait_for(self._run_batch_once(), self.timeout_seconds)
            except (asyncio.TimeoutError, PlaywrightTimeoutError, SimulatorError) as exc:
                # An early missing-button/parser error must not cause a tight
                # retry loop. Cancellation is intentionally not caught.
                remaining = self.timeout_seconds - (time.monotonic() - started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                retry += 1
                print(
                    f"  No simulation result found within {self.timeout_seconds:g}s; "
                    f"reloading the same condition and repeating this batch "
                    f"(retry {retry}). {str(exc).strip()}", flush=True,
                )

    async def _reload_current_condition(self) -> None:
        """Reload exact prepared inputs, clearing any stale result or queue UI."""
        if self.page is None or self._loaded_condition is None:
            raise RuntimeError('No loaded simulator condition is available to retry.')
        attacker, defender, run_count = self._loaded_condition
        with tempfile.TemporaryDirectory(prefix='kingshot_retry_') as tmp:
            atk_path = Path(tmp) / 'attacker_import.json'
            def_path = Path(tmp) / 'defender_import.json'
            atk_path.write_bytes(attacker); def_path.write_bytes(defender)
            await self.page.goto(SITE_URL, wait_until='domcontentloaded')
            await _dismiss_cookie_dialog(self.page, wait_seconds=0.25)
            await _import_profile(self.page, 'Attacker', atk_path)
            await _import_profile(self.page, 'Defender', def_path)
            await _select_side(self.page, 'Battle')
            await _set_run_count(self.page, run_count)

    async def _run_batch_once(self) -> float:
        """Run one batch and return attacker win rate in percent."""
        if self.page is None:
            raise RuntimeError("Simulator session is not open.")

        page = self.page
        before = await page.locator("body").inner_text()
        button = await _battle_button(page)
        await button.click()

        deadline = time.monotonic() + self.timeout_seconds
        earliest_same_result = time.monotonic() + 1.0
        saw_activity = not await button.is_enabled()
        saw_noncompleted_status = False
        completed_at: float | None = None

        while time.monotonic() < max(
            deadline,
            (completed_at + RESULT_RENDER_GRACE_SECONDS)
            if completed_at
            else deadline,
        ):
            await page.wait_for_timeout(250)
            visible_text = await page.locator("body").inner_text()
            status = _queue_status(visible_text)
            status_is_completed = bool(
                status and status.casefold().startswith("completed")
            )

            if status and not status_is_completed:
                saw_activity = True
                saw_noncompleted_status = True
                completed_at = None

            current_button = await _battle_button(page, require_enabled=False)
            button_enabled = await current_button.is_enabled()
            if not button_enabled:
                saw_activity = True

            fresh_completion = status_is_completed and (
                saw_noncompleted_status or (saw_activity and button_enabled)
            )

            if fresh_completion:
                if completed_at is None:
                    completed_at = time.monotonic()
                try:
                    return extract_attacker_winrate(visible_text)
                except SimulatorError as parse_error:
                    if (
                        time.monotonic() - completed_at
                        >= RESULT_RENDER_GRACE_SECONDS
                    ):
                        raise SimulatorError(
                            'Queue status reached "Completed", but no attacker '
                            f"win rate appeared within "
                            f"{RESULT_RENDER_GRACE_SECONDS:g}s. "
                            f"Parser details: {parse_error}"
                        ) from parse_error
                    continue

            if saw_activity and not button_enabled:
                continue

            try:
                winrate = extract_attacker_winrate(visible_text)
            except SimulatorError:
                continue

            if saw_activity and button_enabled and status is None:
                return winrate
            if not saw_activity and visible_text != before:
                return winrate
            if not saw_activity and time.monotonic() >= earliest_same_result:
                return winrate

        last_status = (
            _queue_status(await page.locator("body").inner_text()) or "not shown"
        )
        raise SimulatorError(
            f"Timed out after {self.timeout_seconds:g}s waiting for the queue "
            f"to complete. Last queue status: {last_status}"
        )


def run_spyder_compatible(coro_factory) -> None:
    """
    Run an async experiment both from a normal terminal and from Spyder/IPython.
    Pass a zero-argument function returning the coroutine, e.g.
        run_spyder_compatible(run_experiment)
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro_factory())
        return

    print(
        "Spyder event loop detected. Running experiment to completion...",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="kingshot",
    ) as executor:
        future = executor.submit(lambda: asyncio.run(coro_factory()))
        future.result()
