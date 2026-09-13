#!/usr/bin/env python3
"""Kingshot hero-progression and hero-gear stat calculations.

This module deliberately calculates only additive Expedition stat contributions
that can be written to the simulator's ``hero.stats`` block:
Attack, Defense, Lethality and Health. Hero skills/talents and the active
Exclusive-Gear/widget Expedition skill are handled separately.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

STAT_NAMES = ("attack", "defense", "lethality", "health")
ROLES = ("inf", "cav", "arch")
GEAR_SLOTS = ("helm", "gloves", "chest", "boots")
SLOT_PRIMARY_STAT = {
    "helm": "lethality",
    "boots": "lethality",
    "gloves": "health",
    "chest": "health",
}

# The game exposes 31 progression states from 0-star through 5-star max.
# These normalized values are based on the published Generation-6 Expedition
# star ladder (max 540.43%). Other published generations/rarities use the same
# normalized ladder; using ratios lets us preserve each hero's verified max value.
_PUBLISHED_GEN6 = [
    68.17, 74.52, 80.86, 87.21, 93.56, 99.90,
    111.32, 120.20, 129.09, 137.97, 146.85, 155.74,
    171.72, 184.14, 196.59, 209.03, 221.45, 233.90,
    256.28, 273.70, 291.09, 308.50, 325.92, 343.33,
    374.68, 399.03, 423.41, 447.80, 472.18, 496.56,
    540.43,
]
STAR_STEP_RATIOS = tuple(v / _PUBLISHED_GEN6[-1] for v in _PUBLISHED_GEN6)


def clamp_int(value: Any, low: int, high: int, label: str) -> int:
    try:
        result = int(value)
    except Exception as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if not low <= result <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return result


def star_step_label(step: int) -> str:
    """Human label for the 31-state star ladder (0..30)."""
    step = clamp_int(step, 0, 30, "Star progression")
    stars, substep = divmod(step, 6)
    label = f"{stars}.{substep}★"
    return f"{label} (max)" if step == 30 else label


def star_step_choices() -> list[str]:
    return [star_step_label(i) for i in range(31)]


def parse_star_step_label(text: str) -> int:
    choices = star_step_choices()
    if text in choices:
        return choices.index(text)
    # Also allow a raw index to keep hand-edited config friendly.
    return clamp_int(text, 0, 30, "Star progression")


def load_progression_lookup(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    heroes = data.get("heroes")
    if not isinstance(heroes, dict):
        raise ValueError(f"{path} has no 'heroes' object.")
    return heroes


def base_star_stats(hero: dict[str, Any], star_step: int) -> dict[str, float]:
    step = clamp_int(star_step, 0, 30, "Star progression")
    # The internal hero-data lookup stores the exact published 31-state ladder.
    # Keep the normalized-ratio fallback for custom/older lookup files.
    progression = hero.get("star_progression")
    if isinstance(progression, list) and len(progression) == 31:
        row = progression[step]
        if isinstance(row, dict):
            return {
                "attack": round(float(row.get("attack", 0.0)), 2),
                "defense": round(float(row.get("defense", row.get("attack", 0.0))), 2),
                "lethality": 0.0,
                "health": 0.0,
            }
    ratio = STAR_STEP_RATIOS[step]
    max_stats = hero.get("max_5star_base_stats", {})
    atk = float(max_stats.get("attack", 0.0))
    deff = float(max_stats.get("defense", atk))
    return {
        "attack": round(atk * ratio, 2),
        "defense": round(deff * ratio, 2),
        "lethality": 0.0,
        "health": 0.0,
    }


def widget_passive_stats(hero: dict[str, Any], widget_level: int) -> dict[str, float]:
    level = clamp_int(widget_level, 0, 10, "Exclusive Gear/widget level")
    widget = hero.get("exclusive_gear", {})
    if not widget.get("has_widget") or level == 0:
        return {name: 0.0 for name in STAT_NAMES}
    maximum = widget.get("max_passive_expedition", {})
    # Verified hero tables show the passive Expedition Lethality and Health
    # rising linearly at every Exclusive Gear level 1..10.
    return {
        "attack": 0.0,
        "defense": 0.0,
        "lethality": round(float(maximum.get("lethality", 0.0)) * level / 10.0, 4),
        "health": round(float(maximum.get("health", 0.0)) * level / 10.0, 4),
    }


def widget_skill_rank(widget_level: int) -> int:
    """Simulator active Expedition widget-skill rank (0..5) from gear level 0..10."""
    level = clamp_int(widget_level, 0, 10, "Exclusive Gear/widget level")
    # Expedition skill: locked at L1; ranks 1..5 at L2/4/6/8/10.
    return level // 2


def default_piece(*, maxed: bool = False) -> dict[str, Any]:
    if maxed:
        return {"quality": "red", "enhancement": 200, "mastery": 20}
    return {"quality": "none", "enhancement": 0, "mastery": 0}


def default_gear_set(*, maxed: bool = False) -> dict[str, dict[str, Any]]:
    return {slot: default_piece(maxed=maxed) for slot in GEAR_SLOTS}


def default_all_gear(*, maxed: bool = False) -> dict[str, dict[str, dict[str, Any]]]:
    return {role: default_gear_set(maxed=maxed) for role in ROLES}


def gear_xp_to_enhancement(quality: str, xp: Any) -> int:
    """Convert user-facing 0..100 gear XP to the internal effective level.

    Mythic uses XP directly (0..100). Red starts after Mythic 100, so a Red
    piece with 0..100 displayed XP maps to effective enhancement 100..200.
    """
    quality = str(quality).strip().casefold()
    if quality not in {"none", "mythic", "red"}:
        raise ValueError("Gear quality must be None, Mythic, or Red.")
    level = clamp_int(xp, 0, 100, "Gear XP")
    if quality == "none":
        return 0
    return level + (100 if quality == "red" else 0)


def gear_enhancement_to_xp(quality: str, enhancement: Any) -> int:
    """Convert the legacy/internal effective enhancement to displayed 0..100 XP."""
    quality = str(quality).strip().casefold()
    if quality == "none":
        return 0
    if quality == "mythic":
        return clamp_int(enhancement, 0, 100, "Mythic gear enhancement")
    if quality == "red":
        effective = clamp_int(enhancement, 100, 200, "Red gear enhancement")
        return effective - 100
    raise ValueError("Gear quality must be None, Mythic, or Red.")


def validate_gear_piece(piece: dict[str, Any], *, label: str = "Gear") -> dict[str, Any]:
    quality = str(piece.get("quality", "none")).strip().casefold()
    if quality not in {"none", "mythic", "red"}:
        raise ValueError(f"{label}: quality must be None, Mythic, or Red.")
    enhancement = clamp_int(piece.get("enhancement", 0), 0, 200, f"{label} enhancement")
    mastery = clamp_int(piece.get("mastery", 0), 0, 20, f"{label} mastery")
    if quality == "none":
        return {"quality": "none", "enhancement": 0, "mastery": 0}
    if quality == "mythic":
        if enhancement > 100:
            raise ValueError(f"{label}: Mythic gear enhancement cannot exceed 100.")
        return {"quality": quality, "enhancement": enhancement, "mastery": mastery}
    # Red quality continues from Mythic 100. Treat 100 as the just-ascended state.
    if enhancement < 100:
        raise ValueError(f"{label}: Red gear enhancement must be at least 100.")
    minimum_mastery = 10
    if enhancement >= 120:
        minimum_mastery = 11
    if enhancement >= 140:
        minimum_mastery = 12
    if enhancement >= 160:
        minimum_mastery = 13
    if enhancement >= 180:
        minimum_mastery = 14
    if enhancement >= 200:
        minimum_mastery = 15
    if mastery < minimum_mastery:
        raise ValueError(
            f"{label}: Red {enhancement} requires mastery {minimum_mastery} or higher."
        )
    return {"quality": quality, "enhancement": enhancement, "mastery": mastery}


def gear_enhanced_stat_percent(quality: str, enhancement: int) -> float:
    quality = quality.casefold()
    if quality == "none":
        return 0.0
    if quality == "mythic":
        # The ordinary Expedition primary stat rises from 15% at Mythic E0
        # to 50% at Mythic E100.
        return 15.0 + enhancement * 0.35
    if quality == "red":
        # Red gear inherits the 50% Mythic-100 Expedition primary stat. Red XP
        # advances the separate Red milestone/imbuement bonuses below; it does
        # NOT grow this ordinary Lethality/Health primary stat again. Treating
        # internal effective level 100..200 as another 50% -> 100% primary-stat
        # ladder double-counted Red progression and added +300 percentage points
        # too much Lethality/Health at Red 100 XP / Mastery 20.
        return 50.0
    raise ValueError(f"Unsupported gear quality {quality!r}.")


def gear_piece_primary_stat(piece: dict[str, Any]) -> float:
    clean = validate_gear_piece(piece)
    if clean["quality"] == "none":
        return 0.0
    enhanced = gear_enhanced_stat_percent(clean["quality"], clean["enhancement"])
    multiplier = 1.0 + 0.1 * clean["mastery"]
    return round(enhanced * multiplier, 4)


def gear_piece_imbuement_stats(slot: str, piece: dict[str, Any]) -> dict[str, float]:
    clean = validate_gear_piece(piece, label=slot.title())
    result = {"attack": 0.0, "defense": 0.0, "lethality": 0.0, "health": 0.0}
    if clean["quality"] != "red":
        return result
    level = clean["enhancement"]
    # Expedition red milestones. Helm/Chest use Attack -> Defense -> Attack;
    # Gloves/Boots use Defense -> Attack -> Defense. Conquest-only +40/+80
    # milestones are intentionally excluded from simulator Expedition stats.
    if slot in {"helm", "chest"}:
        if level >= 120:
            result["attack"] += 20.0
        if level >= 160:
            result["defense"] += 30.0
        if level >= 200:
            result["attack"] += 50.0
    else:
        if level >= 120:
            result["defense"] += 20.0
        if level >= 160:
            result["attack"] += 30.0
        if level >= 200:
            result["defense"] += 50.0
    return result


def gear_set_stats(gear_set: dict[str, Any] | None) -> dict[str, float]:
    gear_set = gear_set or {}
    total = {name: 0.0 for name in STAT_NAMES}
    for slot in GEAR_SLOTS:
        piece = gear_set.get(slot, default_piece())
        clean = validate_gear_piece(piece, label=slot.title())
        primary = SLOT_PRIMARY_STAT[slot]
        total[primary] += gear_piece_primary_stat(clean)
        extra = gear_piece_imbuement_stats(slot, clean)
        for stat in STAT_NAMES:
            total[stat] += extra[stat]
    return {k: round(v, 4) for k, v in total.items()}


def hero_progression_stats(
    hero: dict[str, Any],
    hero_cfg: dict[str, Any],
    gear_set: dict[str, Any] | None,
) -> dict[str, Any]:
    star_step = clamp_int(hero_cfg.get("star_step", 30), 0, 30, "Star progression")
    widget_level = clamp_int(hero_cfg.get("widget_level", 0), 0, 10, "Widget level")
    if not hero.get("exclusive_gear", {}).get("has_widget"):
        widget_level = 0
    base = base_star_stats(hero, star_step)
    widget = widget_passive_stats(hero, widget_level)
    gear = gear_set_stats(gear_set)
    final = {name: round(base[name] + widget[name] + gear[name], 4) for name in STAT_NAMES}
    return {
        "star_step": star_step,
        "star_label": star_step_label(star_step),
        "widget_level": widget_level,
        "widget_skill_rank": widget_skill_rank(widget_level),
        "base_star_stats": base,
        "widget_passive_stats": widget,
        "gear_stats": gear,
        "final_hero_stats": final,
    }


def layered_hero_stats(
    hero: dict[str, Any],
    hero_cfg: dict[str, Any],
    gear_set: dict[str, Any] | None,
    *,
    use_stars: bool = True,
    use_passive_widget: bool = True,
    use_gear: bool = True,
    imported_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate independently switchable additive hero-stat layers.

    ``imported_stats`` replaces the star layer when supplied. This is the only
    exact way to honour a player export because current exports contain combined
    ``hero.stats`` values rather than a recoverable star level.
    """
    calc = hero_progression_stats(hero, hero_cfg, gear_set)
    zero = {name: 0.0 for name in STAT_NAMES}
    if use_stars:
        star = (
            {name: float((imported_stats or {}).get(name, 0.0)) for name in STAT_NAMES}
            if imported_stats is not None
            else copy.deepcopy(calc["base_star_stats"])
        )
    else:
        star = copy.deepcopy(zero)
    widget = copy.deepcopy(calc["widget_passive_stats"] if use_passive_widget else zero)
    gear = copy.deepcopy(calc["gear_stats"] if use_gear else zero)
    final = {
        name: round(float(star[name]) + float(widget[name]) + float(gear[name]), 4)
        for name in STAT_NAMES
    }
    return {**calc, "base_star_stats": star, "widget_passive_stats": widget,
            "gear_stats": gear, "final_hero_stats": final}


def profile_hero_config(profile_cfg: dict[str, Any], hero_name: str) -> dict[str, Any]:
    progression = profile_cfg.get("hero_progression", {})
    defaults = progression.get("defaults", {}) if isinstance(progression, dict) else {}
    heroes = progression.get("heroes", {}) if isinstance(progression, dict) else {}
    item = heroes.get(hero_name, {}) if isinstance(heroes, dict) else {}
    result = copy.deepcopy(defaults) if isinstance(defaults, dict) else {}
    if isinstance(item, dict):
        result.update(copy.deepcopy(item))
    result.setdefault("star_step", 30)
    result.setdefault("widget_level", 0)
    return result


def progression_enabled(profile_cfg: dict[str, Any]) -> bool:
    progression = profile_cfg.get("hero_progression", {})
    return bool(progression.get("enabled", True)) if isinstance(progression, dict) else True
