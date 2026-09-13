#!/usr/bin/env python3
"""Kingshot Special Bonus helpers used by the GUI preview.

The simulator itself receives pet/city/widget levels and resolves them online.
This module mirrors the visible Special Bonus math for the desktop preview:
positive percentages on the same stat add together, then multiply the current
stat factor once; enemy-down percentages are applied as the opposing divisor.
Active lead-widget skills are never inferred here because their activation depends
on the selected formation and battle side; callers may pass already-resolved active
widget percentages through ``own_extra_positive``.
"""
from __future__ import annotations

from typing import Any

STAT_NAMES = ("attack", "defense", "lethality", "health")

# Level -> active skill percent. Values beyond a pet's real maximum are clamped
# so older config files that allowed 0..10 for every pet remain readable.
TEN_LEVEL = (0.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0)
GRIZZLY = (0.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
MOOSE = (0.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0)

# key -> (target, stat, level table), target is "self" or "enemy".
PET_EFFECTS = {
    "alpha-black-panther": ("self", "lethality", TEN_LEVEL),
    "giant-rhino": ("self", "attack", TEN_LEVEL),
    "regal-white-lion": ("self", "defense", TEN_LEVEL),
    "ironclad-war-elephant": ("self", "health", TEN_LEVEL),
    "ironclad-war-bear": ("enemy", "defense", TEN_LEVEL),
    "grizzly-bear": ("enemy", "lethality", GRIZZLY),
    "moose": ("enemy", "health", MOOSE),
}

CITY_SELF = {"attack": "attack", "defense": "defense", "lethality": "lethality", "health": "health"}
CITY_ENEMY = {"enemyAttack": "attack", "enemyDefense": "defense"}


def _level_percent(level: Any, table: tuple[float, ...]) -> float:
    try:
        idx = int(float(level or 0))
    except Exception:
        return 0.0
    idx = max(0, min(idx, len(table) - 1))
    return float(table[idx])


def _city_percent(level: Any) -> float:
    """City tier I/II correspond to 10%/20%."""
    try:
        tier = int(float(level or 0))
    except Exception:
        return 0.0
    return float(max(0, min(tier, 2)) * 10)


def special_bonus_vectors(special: dict[str, Any] | None) -> tuple[dict[str, float], dict[str, float]]:
    """Return (positive self buffs, enemy-down buffs), in percentage points."""
    own = {name: 0.0 for name in STAT_NAMES}
    enemy = {name: 0.0 for name in STAT_NAMES}
    special = special if isinstance(special, dict) else {}

    pets = special.get("petLevels", {})
    if isinstance(pets, dict):
        for key, (target, stat, table) in PET_EFFECTS.items():
            value = _level_percent(pets.get(key, 0), table)
            (own if target == "self" else enemy)[stat] += value

    city = special.get("city", {})
    if isinstance(city, dict):
        for key, stat in CITY_SELF.items():
            own[stat] += _city_percent(city.get(key, 0))
        for key, stat in CITY_ENEMY.items():
            enemy[stat] += _city_percent(city.get(key, 0))

    return own, enemy


def apply_special_bonus_preview(
    stats: dict[str, dict[str, float]],
    *,
    own_special: dict[str, Any] | None,
    opponent_special: dict[str, Any] | None,
    own_enabled: bool,
    opponent_enabled: bool,
    own_extra_positive: dict[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """Apply pet/city Special Bonuses to additive percentage stats.

    If A is the additive displayed bonus, x is the sum of own positive Special
    Bonuses and y is the sum of opponent enemy-down bonuses for that stat:

        final = ((1 + A/100) * (1 + x/100) / (1 + y/100) - 1) * 100

    Equivalently, when ``x`` and ``y`` are expressed as percentage points:

        final = (A * (1 + x/100) + x - y) / (1 + y/100)

    ``own_extra_positive`` is used for formation-scoped active widget skills.
    Those percentages join the same positive multiplicative layer as Pet/City
    bonuses, matching the simulator's Special Bonus treatment.
    """
    own_pos, _ = special_bonus_vectors(own_special if own_enabled else None)
    if own_extra_positive:
        for stat in STAT_NAMES:
            own_pos[stat] += float(own_extra_positive.get(stat, 0.0) or 0.0)
    _, incoming_down = special_bonus_vectors(opponent_special if opponent_enabled else None)
    out: dict[str, dict[str, float]] = {}
    for troop_type, row in stats.items():
        out[troop_type] = {}
        for stat in STAT_NAMES:
            base = float(row.get(stat, 0.0))
            x = own_pos[stat] / 100.0
            y = incoming_down[stat] / 100.0
            out[troop_type][stat] = round(((1.0 + base / 100.0) * (1.0 + x) / (1.0 + y) - 1.0) * 100.0, 4)
    return out
