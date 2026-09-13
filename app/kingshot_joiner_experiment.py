#!/usr/bin/env python3
"""
Joiner-combination Kingshot experiment.

Normal use is configured through kingshot_gui.py / kingshot_config.json. The
Python configuration block below remains only as a fallback. Full workflow notes
are in README.txt. Results are written to results/joiner_experiment.
"""

from __future__ import annotations

import copy
import csv
import itertools
import json
import tempfile
from functools import lru_cache
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

from kingshot_config import apply_config_to_globals
from kingshot_paths import ROOT_DIR, JSON_DIR, RESULTS_DIR, LAST_RUN_STATS_FILE

from kingshot_simulator_client import (
    KingshotSimulatorSession,
    SimulatorError,
    run_spyder_compatible,
    validate_profile_options,
    write_experiment_profile_metadata,
    prepare_profile_json,
    write_last_run_stats,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

SCRIPT_FOLDER = ROOT_DIR

# Player A is attacker and Player B is defender; source JSON internal names are ignored.
ATTACKER_PROFILE = "A"
DEFENDER_PROFILE = "B"

PLAYER_FILES = {
    "A": JSON_DIR / "playerA_data.json",
    "B": JSON_DIR / "playerB_data.json",
}
ATTACKER_JSON = PLAYER_FILES[ATTACKER_PROFILE]
DEFENDER_JSON = PLAYER_FILES[DEFENDER_PROFILE]

HERO_STATS_LOOKUP = JSON_DIR / "kingshot_hero_data.json"
HERO_PROGRESSION_LOOKUP = JSON_DIR / "kingshot_hero_data.json"
HERO_GEAR_LOOKUP = JSON_DIR / "kingshot_hero_gear.json"

EXPERIMENT_FOLDER = RESULTS_DIR / "joiner_experiment"
OUTPUT_CSV = EXPERIMENT_FOLDER / "kingshot_winrates.csv"
SETTINGS_TXT = EXPERIMENT_FOLDER / "experiment_settings.txt"
FIRST_ATTACKER_JSON = EXPERIMENT_FOLDER / "attacker_data.json"
FIRST_DEFENDER_JSON = EXPERIMENT_FOLDER / "defender_data.json"

SIMULATIONS_PER_BATCH = 100
BATCHES_PER_CONDITION = 1
RESUME_FROM_CSV = False

HEADLESS = True
TIMEOUT_SECONDS = 30
DELAY_BETWEEN_BATCHES_SECONDS = 0.5

ATTACK_LEADS = {"inf": "Triton", "cav": "Thrud", "arch": "Marlin"}
DEFENSE_LEADS = {"inf": "Triton", "cav": "Sophia", "arch": "Vivian"}
ATTACK_WIDGET_BUFFS = {"inf": True, "cav": True, "arch": True}
DEFENSE_WIDGET_BUFFS = {"inf": True, "cav": True, "arch": True}

ATTACK_TOTAL_TROOPS = 1_600_000
DEFENSE_TOTAL_TROOPS = 1_600_000
ATTACK_TROOP_PERCENTAGES = (50, 50, 0)
DEFENSE_TROOP_PERCENTAGES = (60, 10, 30)

ATTACK_TROOP_QUALITY = {
    "inf": {"tier": 11, "tg_level": 8},
    "cav": {"tier": 11, "tg_level": 8},
    "arch": {"tier": 11, "tg_level": 8},
}
DEFENSE_TROOP_QUALITY = {
    "inf": {"tier": 11, "tg_level": 8},
    "cav": {"tier": 11, "tg_level": 8},
    "arch": {"tier": 11, "tg_level": 8},
}

ATTACK_BASE_STAT_OVERRIDE = None
DEFENSE_BASE_STAT_OVERRIDE = None

ADD_5STAR_HERO_STATS_ATK = True
ADD_HERO_GEAR_ATK = True
ADD_HERO_STATS_ATK = True

ADD_5STAR_HERO_STATS_DEF = False
ADD_HERO_GEAR_DEF = False
ADD_HERO_STATS_DEF = False

APPLY_SPECIAL_BONUSES = True
APPLY_WIDGET_BUFFS = True
APPLY_SPECIAL_BONUSES_ATK = APPLY_SPECIAL_BONUSES
APPLY_SPECIAL_BONUSES_DEF = APPLY_SPECIAL_BONUSES
APPLY_WIDGET_BUFFS_ATK = APPLY_WIDGET_BUFFS
APPLY_WIDGET_BUFFS_DEF = APPLY_WIDGET_BUFFS
HERO_PROGRESSION_CONFIG_ATK = {"enabled": True, "defaults": {"star_step": 30, "widget_level": 10}, "heroes": {}}
HERO_PROGRESSION_CONFIG_DEF = {"enabled": False, "defaults": {"star_step": 30, "widget_level": 0}, "heroes": {}}
HERO_GEAR_CONFIG_ATK = {}
HERO_GEAR_CONFIG_DEF = {}
JOINER_SKILL_LEVEL = 5

def c(*values: Any) -> list[Any]:
    return list(values)

joiner_pool_atk_max_4 = c()
joiner_pool_atk_max_3 = c()
joiner_pool_atk_max_2 = c()
joiner_pool_atk_max_1 = c()

joiner_pool_def_max_4 = c()
joiner_pool_def_max_3 = c()
joiner_pool_def_max_2 = c()
joiner_pool_def_max_1 = c(
    "Yang", "Thrud", "Saul", "Fahd", "Howard", "Chenko",
    "Amane", "Gordon", "Triton", "Hilde", "Vivian", "Petra"
)

joiner1_atk = c("Hilde")
joiner2_atk = c("Thrud")
joiner3_atk = c("Amadeus")
joiner4_atk = c("Triton")

joiner1_def = c()
joiner2_def = c()
joiner3_def = c()
joiner4_def = c()

# =============================================================================
# END CONFIGURATION
# =============================================================================

# If kingshot_config.json exists, it is the shared source of truth for this script.
apply_config_to_globals(globals(), "joiner")

def _special_bonus_overrides(side: str) -> dict[str, Any]:
    if side == "atk":
        return copy.deepcopy(SPECIAL_STATS_CONFIG_ATK)
    if side == "def":
        return copy.deepcopy(SPECIAL_STATS_CONFIG_DEF)
    raise ValueError(f"Unknown side: {side!r}")


def _pretty(value: Any) -> str:
    """Readable, deterministic representation for the human settings file."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def _save_first_prepared_profiles(
    conditions: list[tuple[str | None, ...]],
    attacker_baseline: Any,
    defender_baseline: Any,
    effective_stats_include_heroes_atk: bool | None,
    effective_stats_include_heroes_def: bool | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Save the exact final JSON pair for the first configured joiner condition."""
    first = conditions[0]
    attacker_profile = _profile_for_lineup(
        attacker_baseline,
        ATTACKER_JSON.name,
        first[:4],
        ATTACK_LEADS,
        ATTACK_TROOP_PERCENTAGES,
        ATTACK_TOTAL_TROOPS,
        ATTACK_TROOP_QUALITY,
        ATTACK_BASE_STAT_OVERRIDE,
        "attacker",
    )
    defender_profile = _profile_for_lineup(
        defender_baseline,
        DEFENDER_JSON.name,
        first[4:],
        DEFENSE_LEADS,
        DEFENSE_TROOP_PERCENTAGES,
        DEFENSE_TOTAL_TROOPS,
        DEFENSE_TROOP_QUALITY,
        DEFENSE_BASE_STAT_OVERRIDE,
        "defender",
    )

    EXPERIMENT_FOLDER.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kingshot_first_profiles_") as tmp:
        tmpdir = Path(tmp)
        raw_attacker = tmpdir / "attacker_condition.json"
        raw_defender = tmpdir / "defender_condition.json"
        _write_json(raw_attacker, attacker_profile)
        _write_json(raw_defender, defender_profile)

        atk_info = prepare_profile_json(
            raw_attacker,
            FIRST_ATTACKER_JSON,
            add_5star_hero_stats=ADD_5STAR_HERO_STATS_ATK,
            add_hero_gear=ADD_HERO_GEAR_ATK,
            stats_include_heroes=effective_stats_include_heroes_atk,
            hero_stats_lookup=HERO_STATS_LOOKUP,
            hero_gear_lookup=HERO_GEAR_LOOKUP,
            hero_progression_lookup=HERO_PROGRESSION_LOOKUP,
            hero_progression_config=HERO_PROGRESSION_CONFIG_ATK,
            hero_gear_config=HERO_GEAR_CONFIG_ATK,
            apply_special_bonuses=APPLY_SPECIAL_BONUSES_ATK,
            apply_widget_buffs=APPLY_WIDGET_BUFFS_ATK,
            active_widget_heroes=_fixed_widget_heroes(ATTACK_LEADS, ATTACK_WIDGET_BUFFS),
            special_bonuses=_special_bonus_overrides("atk"),
            side="attacker",
            label="attacker",
        )
        def_info = prepare_profile_json(
            raw_defender,
            FIRST_DEFENDER_JSON,
            add_5star_hero_stats=ADD_5STAR_HERO_STATS_DEF,
            add_hero_gear=ADD_HERO_GEAR_DEF,
            stats_include_heroes=effective_stats_include_heroes_def,
            hero_stats_lookup=HERO_STATS_LOOKUP,
            hero_gear_lookup=HERO_GEAR_LOOKUP,
            hero_progression_lookup=HERO_PROGRESSION_LOOKUP,
            hero_progression_config=HERO_PROGRESSION_CONFIG_DEF,
            hero_gear_config=HERO_GEAR_CONFIG_DEF,
            apply_special_bonuses=APPLY_SPECIAL_BONUSES_DEF,
            apply_widget_buffs=APPLY_WIDGET_BUFFS_DEF,
            active_widget_heroes=_fixed_widget_heroes(DEFENSE_LEADS, DEFENSE_WIDGET_BUFFS),
            special_bonuses=_special_bonus_overrides("def"),
            side="defender",
            label="defender",
        )
    return atk_info, def_info



def _augment_machine_settings(settings_path: Path) -> None:
    """Add joiner-design details used later by the analysis/plot titles."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulatorError(
            f"Could not update experiment settings file {settings_path}: {exc}"
        ) from exc

    attacker_profile = _load_json(FIRST_ATTACKER_JSON)
    defender_profile = _load_json(FIRST_DEFENDER_JSON)

    data["profile_progression"] = {
        "hero_progression_lookup": str(HERO_PROGRESSION_LOOKUP),
        "attacker": {
            "profile": ATTACKER_PROFILE,
            "configured_progression": copy.deepcopy(HERO_PROGRESSION_CONFIG_ATK),
            "hero_gear": copy.deepcopy(HERO_GEAR_CONFIG_ATK),
            "effective_stats_include_heroes": False if HERO_PROGRESSION_CONFIG_ATK.get("enabled", False) else bool(attacker_profile.get("stats_include_heroes", False)),
        },
        "defender": {
            "profile": DEFENDER_PROFILE,
            "configured_progression": copy.deepcopy(HERO_PROGRESSION_CONFIG_DEF),
            "hero_gear": copy.deepcopy(HERO_GEAR_CONFIG_DEF),
            "effective_stats_include_heroes": False if HERO_PROGRESSION_CONFIG_DEF.get("enabled", False) else bool(defender_profile.get("stats_include_heroes", False)),
        },
        "apply_special_bonuses": {
            "attacker": bool(APPLY_SPECIAL_BONUSES_ATK),
            "defender": bool(APPLY_SPECIAL_BONUSES_DEF),
        },
        "apply_widget_buffs": {
            "attacker": bool(APPLY_WIDGET_BUFFS_ATK),
            "defender": bool(APPLY_WIDGET_BUFFS_DEF),
        },
        "formation_widget_buffs": {
            "attacker": copy.deepcopy(ATTACK_WIDGET_BUFFS),
            "defender": copy.deepcopy(DEFENSE_WIDGET_BUFFS),
        },
    }

    data["joiner_experiment"] = {
        "simulations_per_batch": int(SIMULATIONS_PER_BATCH),
        "batches_per_condition_target": int(BATCHES_PER_CONDITION),
        "attacker_total_troops": int(ATTACK_TOTAL_TROOPS),
        "defender_total_troops": int(DEFENSE_TOTAL_TROOPS),
        "attacker_troop_percentages": {
            "infantry": float(ATTACK_TROOP_PERCENTAGES[0]),
            "cavalry": float(ATTACK_TROOP_PERCENTAGES[1]),
            "archers": float(ATTACK_TROOP_PERCENTAGES[2]),
        },
        "defender_troop_percentages": {
            "infantry": float(DEFENSE_TROOP_PERCENTAGES[0]),
            "cavalry": float(DEFENSE_TROOP_PERCENTAGES[1]),
            "archers": float(DEFENSE_TROOP_PERCENTAGES[2]),
        },
        "attacker_troop_quality": copy.deepcopy(ATTACK_TROOP_QUALITY),
        "defender_troop_quality": copy.deepcopy(DEFENSE_TROOP_QUALITY),
        "attacker_base_stat_override": ATTACK_BASE_STAT_OVERRIDE,
        "defender_base_stat_override": DEFENSE_BASE_STAT_OVERRIDE,
        "attacker_profile_source": ATTACKER_PROFILE,
        "defender_profile_source": DEFENDER_PROFILE,
        "attacker_player_source": ATTACKER_PROFILE,
        "defender_player_source": DEFENDER_PROFILE,
        "attacker_special_source": ATTACKER_PROFILE,
        "defender_special_source": DEFENDER_PROFILE,
        "attacker_lead_heroes": list(attacker_profile.get("selectedHeroes", [])),
        "defender_lead_heroes": list(defender_profile.get("selectedHeroes", [])),
    }

    try:
        settings_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        raise SimulatorError(
            f"Could not write experiment settings file {settings_path}: {exc}"
        ) from exc


def _write_readable_settings(
    conditions: list[tuple[str | None, ...]],
    effective_stats_include_heroes_atk: bool | None,
    effective_stats_include_heroes_def: bool | None,
    first_atk_info: dict[str, Any],
    first_def_info: dict[str, Any],
) -> Path:
    """Write a human-readable record of the joiner experiment configuration."""
    attacker_lineups, defender_lineups = build_side_lineups()
    lines = [
        "KINGSHOT JOINER EXPERIMENT SETTINGS",
        "=" * 35,
        "",
        "Files",
        "-----",
        f"Attacker baseline JSON: {ATTACKER_JSON} (profile {ATTACKER_PROFILE})",
        f"Defender baseline JSON: {DEFENDER_JSON} (profile {DEFENDER_PROFILE})",
        f"Output CSV: {OUTPUT_CSV}",
        f"First prepared attacker JSON: {FIRST_ATTACKER_JSON.name}",
        f"First prepared defender JSON: {FIRST_DEFENDER_JSON.name}",
        "",
        "Run settings",
        "------------",
        f"SIMULATIONS_PER_BATCH = {SIMULATIONS_PER_BATCH}",
        f"BATCHES_PER_CONDITION = {BATCHES_PER_CONDITION}",
        f"RESUME_FROM_CSV = {RESUME_FROM_CSV}",
        f"JOINER_SKILL_LEVEL = {JOINER_SKILL_LEVEL}",
        f"HEADLESS = {HEADLESS}",
        f"TIMEOUT_SECONDS = {TIMEOUT_SECONDS}",
        f"DELAY_BETWEEN_BATCHES_SECONDS = {DELAY_BETWEEN_BATCHES_SECONDS}",
        f"Unique attacker lineups = {len(attacker_lineups)}",
        f"Unique defender lineups = {len(defender_lineups)}",
        f"Combined unique conditions = {len(conditions)}",
        "",
        "Troop setup",
        "-----------",
        f"ATTACK_TOTAL_TROOPS = {ATTACK_TOTAL_TROOPS}",
        f"ATTACK_TROOP_PERCENTAGES (inf/cav/arch) = {_pretty(ATTACK_TROOP_PERCENTAGES)}",
        f"ATTACK computed quantities = {_pretty(_pct_to_quantities(ATTACK_TROOP_PERCENTAGES, ATTACK_TOTAL_TROOPS, 'attacker'))}",
        f"ATTACK_TROOP_QUALITY = {_pretty(ATTACK_TROOP_QUALITY)}",
        f"ATTACK_BASE_STAT_OVERRIDE = {ATTACK_BASE_STAT_OVERRIDE!r}",
        f"DEFENSE_TOTAL_TROOPS = {DEFENSE_TOTAL_TROOPS}",
        f"DEFENSE_TROOP_PERCENTAGES (inf/cav/arch) = {_pretty(DEFENSE_TROOP_PERCENTAGES)}",
        f"DEFENSE computed quantities = {_pretty(_pct_to_quantities(DEFENSE_TROOP_PERCENTAGES, DEFENSE_TOTAL_TROOPS, 'defender'))}",
        f"DEFENSE_TROOP_QUALITY = {_pretty(DEFENSE_TROOP_QUALITY)}",
        f"DEFENSE_BASE_STAT_OVERRIDE = {DEFENSE_BASE_STAT_OVERRIDE!r}",
        f"Attacker lead heroes = {_pretty(_load_json(FIRST_ATTACKER_JSON).get('selectedHeroes', []))}",
        f"Defender lead heroes = {_pretty(_load_json(FIRST_DEFENDER_JSON).get('selectedHeroes', []))}",
        f"Attacker final troop rows = {_pretty(_load_json(FIRST_ATTACKER_JSON).get('troops', []))}",
        f"Defender final troop rows = {_pretty(_load_json(FIRST_DEFENDER_JSON).get('troops', []))}",
        "",
        "Hero progression / gear",
        "-----------------------",
        f"HERO_PROGRESSION_LOOKUP = {HERO_PROGRESSION_LOOKUP}",
        f"Attacker configured progression enabled = {bool(HERO_PROGRESSION_CONFIG_ATK.get('enabled', False))}",
        f"Defender configured progression enabled = {bool(HERO_PROGRESSION_CONFIG_DEF.get('enabled', False))}",
        "Attacker star / passive widget configuration:",
        _pretty(HERO_PROGRESSION_CONFIG_ATK),
        "Defender star / passive widget configuration:",
        _pretty(HERO_PROGRESSION_CONFIG_DEF),
        "Attacker hero gear configuration:",
        _pretty(HERO_GEAR_CONFIG_ATK),
        "Defender hero gear configuration:",
        _pretty(HERO_GEAR_CONFIG_DEF),
        f"stats_include_heroes effective ATK = {effective_stats_include_heroes_atk!r}",
        f"stats_include_heroes effective DEF = {effective_stats_include_heroes_def!r}",
        f"Attacker effective troop stats = {first_atk_info.get('effective_troop_stats_text', '(unavailable)')}",
        f"Defender effective troop stats = {first_def_info.get('effective_troop_stats_text', '(unavailable)')}",
        "",
        "Special bonuses / active widget skills",
        "--------------------------------------",
        f"APPLY_SPECIAL_BONUSES_ATK = {APPLY_SPECIAL_BONUSES_ATK}",
        f"APPLY_SPECIAL_BONUSES_DEF = {APPLY_SPECIAL_BONUSES_DEF}",
        f"APPLY_WIDGET_BUFFS_ATK = {APPLY_WIDGET_BUFFS_ATK}",
        f"APPLY_WIDGET_BUFFS_DEF = {APPLY_WIDGET_BUFFS_DEF}",
        f"ATTACK_WIDGET_BUFFS = {_pretty(ATTACK_WIDGET_BUFFS)}",
        f"DEFENSE_WIDGET_BUFFS = {_pretty(DEFENSE_WIDGET_BUFFS)}",
        "Special stats are stored directly in kingshot_config.json; appointments are forced to zero.",
        "Attacker special stats:",
        _pretty(_special_bonus_overrides("atk")),
        "Defender special stats:",
        _pretty(_special_bonus_overrides("def")),
        "",
        "Joiner pools",
        "------------",
        f"joiner_pool_atk_max_4 = {_pretty(joiner_pool_atk_max_4)}",
        f"joiner_pool_atk_max_3 = {_pretty(joiner_pool_atk_max_3)}",
        f"joiner_pool_atk_max_2 = {_pretty(joiner_pool_atk_max_2)}",
        f"joiner_pool_atk_max_1 = {_pretty(joiner_pool_atk_max_1)}",
        f"joiner_pool_def_max_4 = {_pretty(joiner_pool_def_max_4)}",
        f"joiner_pool_def_max_3 = {_pretty(joiner_pool_def_max_3)}",
        f"joiner_pool_def_max_2 = {_pretty(joiner_pool_def_max_2)}",
        f"joiner_pool_def_max_1 = {_pretty(joiner_pool_def_max_1)}",
        "",
        "Manual joiner slots",
        "-------------------",
        f"joiner1_atk = {_pretty(joiner1_atk)}",
        f"joiner2_atk = {_pretty(joiner2_atk)}",
        f"joiner3_atk = {_pretty(joiner3_atk)}",
        f"joiner4_atk = {_pretty(joiner4_atk)}",
        f"joiner1_def = {_pretty(joiner1_def)}",
        f"joiner2_def = {_pretty(joiner2_def)}",
        f"joiner3_def = {_pretty(joiner3_def)}",
        f"joiner4_def = {_pretty(joiner4_def)}",
        "",
        "First generated condition",
        "-------------------------",
        f"Attacker joiners = {_pretty(list(conditions[0][:4]))}",
        f"Defender joiners = {_pretty(list(conditions[0][4:]))}",
        "",
        "First prepared JSON inspection",
        "------------------------------",
        "These are the exact final profiles saved as attacker_data.json and defender_data.json.",
        "Attacker:",
        f"  stats_include_heroes = {first_atk_info.get('stats_include_heroes')!r}",
        f"  active_widget_levels = {_pretty(first_atk_info.get('active_widget_levels', {}))}",
        f"  hero_stats_profiles_written = {len(first_atk_info.get('hero_stats_applied', []))}",
        "Defender:",
        f"  stats_include_heroes = {first_def_info.get('stats_include_heroes')!r}",
        f"  active_widget_levels = {_pretty(first_def_info.get('active_widget_levels', {}))}",
        f"  hero_stats_profiles_written = {len(first_def_info.get('hero_stats_applied', []))}",
        "",
    ]
    EXPERIMENT_FOLDER.mkdir(parents=True, exist_ok=True)
    SETTINGS_TXT.write_text("\n".join(lines), encoding="utf-8")
    return SETTINGS_TXT


CSV_COLUMNS = [
    "condition",
    "batch",
    "joiner1_atk",
    "joiner2_atk",
    "joiner3_atk",
    "joiner4_atk",
    "joiner1_def",
    "joiner2_def",
    "joiner3_def",
    "joiner4_def",
    "winrate",
]


def _clean_pool(pool: Sequence[str | None], label: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in pool:
        if raw_name is None or not str(raw_name).strip():
            raise SimulatorError(f"{label} cannot contain None or an empty hero name.")
        name = str(raw_name).strip()
        key = name.casefold()
        if key in seen:
            raise SimulatorError(f'{label} contains duplicate hero "{name}".')
        seen.add(key)
        names.append(name)
    return names


def _clean_limited_pools(
    pools: Sequence[tuple[int, Sequence[str | None]]],
    label: str,
) -> tuple[list[str], dict[str, int]]:
    """Merge maximum-count pools and validate that every hero occurs once."""

    names: list[str] = []
    maximum_by_hero: dict[str, int] = {}
    display_name_by_hero: dict[str, str] = {}

    for maximum, pool in pools:
        if not 1 <= maximum <= 4:
            raise SimulatorError(
                f"joiner_pool_{label}_max_{maximum} has an invalid maximum. "
                "Joiner maxima must be between 1 and 4."
            )
        pool_label = f"joiner_pool_{label}_max_{maximum}"
        for name in _clean_pool(pool, pool_label):
            key = name.casefold()
            if key in maximum_by_hero:
                previous = maximum_by_hero[key]
                display_name = display_name_by_hero[key]
                raise SimulatorError(
                    f'Hero "{display_name}" appears in both '
                    f"joiner_pool_{label}_max_{previous} and {pool_label}. "
                    "List each pooled hero exactly once."
                )
            names.append(name)
            maximum_by_hero[key] = maximum
            display_name_by_hero[key] = name

    return names, maximum_by_hero


def _lineup_limit_violations(
    lineup: Sequence[str | None],
    maximum_by_hero: dict[str, int],
) -> list[tuple[str, int, int]]:
    """Return (hero, observed count, maximum) for exceeded pool limits."""

    counts: dict[str, int] = {}
    display_names: dict[str, str] = {}
    for hero in lineup:
        if hero is None or not str(hero).strip():
            continue
        name = str(hero).strip()
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1
        display_names.setdefault(key, name)

    return [
        (display_names[key], count, maximum_by_hero[key])
        for key, count in counts.items()
        if key in maximum_by_hero and count > maximum_by_hero[key]
    ]


def _canonical_lineup_key(
    lineup: Sequence[str | None],
) -> tuple[int, tuple[str, ...]]:
    """Identify a lineup as an order-independent, case-insensitive multiset."""

    names = sorted(
        str(hero).strip().casefold()
        for hero in lineup
        if hero is not None and str(hero).strip()
    )
    empty_slots = len(lineup) - len(names)
    return empty_slots, tuple(names)


def _condition_key(
    attacker: Sequence[str | None],
    defender: Sequence[str | None],
) -> tuple[
    tuple[int, tuple[str, ...]],
    tuple[int, tuple[str, ...]],
]:
    """Identify a condition independently of slot and condition-number order."""

    return _canonical_lineup_key(attacker), _canonical_lineup_key(defender)


def _manual_slot_values(
    values: Sequence[str | None], label: str
) -> list[str] | None:
    """Return manual alternatives, or None when the pool owns this slot."""

    raw_values = list(values) if values else [None]
    empty = [value is None or not str(value).strip() for value in raw_values]
    if all(empty):
        return None
    if any(empty):
        raise SimulatorError(
            f"{label} mixes None/empty entries with named heroes. Use only "
            "c(None), or use only hero names."
        )
    return [str(value).strip() for value in raw_values]


def _hybrid_lineups(
    pools: Sequence[tuple[int, Sequence[str | None]]],
    slots: Sequence[Sequence[str | None]],
    label: str,
) -> list[tuple[str | None, ...]]:
    """Combine index-paired manual variants with pool-filled empty slots."""

    pool_names, maximum_by_hero = _clean_limited_pools(pools, label)
    manual = [
        _manual_slot_values(values, f"joiner{index}_{label}")
        for index, values in enumerate(slots, start=1)
    ]
    varying_lengths = [len(values) for values in manual if values and len(values) > 1]
    variant_count = max(varying_lengths, default=1)

    incompatible = sorted(
        {length for length in varying_lengths if length != variant_count}
    )
    if incompatible:
        lengths = sorted(set(varying_lengths))
        raise SimulatorError(
            f"Multi-value manual slots for {label} must have the same length; "
            f"found lengths {lengths}. Single-value slots are repeated automatically."
        )

    lineups: list[tuple[str | None, ...]] = []
    seen_lineups: set[tuple[int, tuple[str, ...]]] = set()
    for variant_index in range(variant_count):
        base: list[str | None] = []
        empty_indices: list[int] = []
        for slot_index, values in enumerate(manual):
            if values is None:
                base.append(None)
                empty_indices.append(slot_index)
            elif len(values) == 1:
                base.append(values[0])
            else:
                base.append(values[variant_index])

        manual_violations = _lineup_limit_violations(base, maximum_by_hero)
        if manual_violations:
            details = ", ".join(
                f'{hero} appears {count} times (maximum {maximum})'
                for hero, count, maximum in manual_violations
            )
            raise SimulatorError(
                f"Manual {label} variant {variant_index + 1} exceeds its "
                f"joiner-pool limit: {details}."
            )

        empty_count = len(empty_indices)
        if empty_count == 0:
            pool_fills: Iterable[tuple[str | None, ...]] = [()]
        elif pool_names:
            pool_fills = itertools.combinations_with_replacement(
                pool_names, empty_count
            )
        else:
            pool_fills = [(None,) * empty_count]

        for pool_fill in pool_fills:
            lineup = list(base)
            for slot_index, hero in zip(empty_indices, pool_fill):
                lineup[slot_index] = hero
            result = tuple(lineup)
            if _lineup_limit_violations(result, maximum_by_hero):
                continue
            lineup_key = _canonical_lineup_key(result)
            if lineup_key not in seen_lineups:
                seen_lineups.add(lineup_key)
                lineups.append(result)

    if not lineups:
        raise SimulatorError(
            f"No {label} joiner lineup satisfies the configured maximum "
            "appearance limits. Increase a maximum, add another pooled hero, "
            "or define more manual slots."
        )

    return lineups

def build_side_lineups() -> tuple[
    list[tuple[str | None, ...]], list[tuple[str | None, ...]]
]:
    attacker = _hybrid_lineups(
        (
            (4, joiner_pool_atk_max_4),
            (3, joiner_pool_atk_max_3),
            (2, joiner_pool_atk_max_2),
            (1, joiner_pool_atk_max_1),
        ),
        (joiner1_atk, joiner2_atk, joiner3_atk, joiner4_atk),
        "atk",
    )
    defender = _hybrid_lineups(
        (
            (4, joiner_pool_def_max_4),
            (3, joiner_pool_def_max_3),
            (2, joiner_pool_def_max_2),
            (1, joiner_pool_def_max_1),
        ),
        (joiner1_def, joiner2_def, joiner3_def, joiner4_def),
        "def",
    )
    return attacker, defender


def build_conditions() -> list[tuple[str | None, ...]]:
    """Return each unique attacker/defender multiset pair exactly once."""

    attacker_lineups, defender_lineups = build_side_lineups()
    conditions: list[tuple[str | None, ...]] = []
    seen_conditions: set[
        tuple[tuple[int, tuple[str, ...]], tuple[int, tuple[str, ...]]]
    ] = set()

    for attacker, defender in itertools.product(
        attacker_lineups, defender_lineups
    ):
        condition_key = _condition_key(attacker, defender)
        if condition_key in seen_conditions:
            continue
        seen_conditions.add(condition_key)
        conditions.append((*attacker, *defender))

    return conditions


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulatorError(f"Not a readable JSON file: {path} ({exc})") from exc


_TROOP_TYPE_ALIASES = {
    "inf": "inf",
    "infantry": "inf",
    "lanc": "lanc",
    "cav": "lanc",
    "cavalry": "lanc",
    "mark": "mark",
    "arch": "mark",
    "archer": "mark",
    "archers": "mark",
    "marksman": "mark",
    "marksmen": "mark",
}

ROLE_TO_JSON_TYPE = {"inf": "inf", "cav": "lanc", "arch": "mark"}
ROLE_ORDER = ("inf", "cav", "arch")
STAT_NAMES = ("attack", "defense", "lethality", "health")


def _fixed_widget_heroes(leads: dict[str, str], toggles: dict[str, bool]) -> list[str]:
    """Return fixed lead heroes whose page-3 active-widget switch is enabled."""
    return [
        str(leads[role]).strip()
        for role in ROLE_ORDER
        if bool(toggles.get(role, True)) and str(leads.get(role, "")).strip()
    ]


def _normalise_leads(leads: Any, label: str) -> dict[str, str]:
    if not isinstance(leads, dict):
        raise SimulatorError(f"{label}: leads must be a dictionary with inf/cav/arch.")
    missing = [role for role in ROLE_ORDER if role not in leads]
    extra = [role for role in leads if role not in ROLE_ORDER]
    if missing or extra:
        raise SimulatorError(
            f"{label}: leads must contain exactly {ROLE_ORDER}; "
            f"missing={missing}, extra={extra}."
        )
    result: dict[str, str] = {}
    for role in ROLE_ORDER:
        name = str(leads[role]).strip()
        if not name:
            raise SimulatorError(f"{label}: lead for {role} is blank.")
        result[role] = name
    if len(set(result.values())) != 3:
        raise SimulatorError(f"{label}: the three lead heroes must be different heroes.")
    return result


def _default_hero(name: str, hero_type: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": hero_type,
        "stats": {stat: 0.0 for stat in STAT_NAMES},
        "skill_levels": {"1": 5, "2": 5, "3": 5},
        "widget_level": 0,
    }


def _set_leads(profile: dict[str, Any], leads: Any, label: str) -> dict[str, str]:
    leads = _normalise_leads(leads, label)
    heroes = profile.get("heroes")
    if not isinstance(heroes, dict):
        raise SimulatorError(f"{label}: JSON has no 'heroes' dictionary.")

    selected: list[str] = []
    requested_widgets: dict[str, int] = {}
    for role in ROLE_ORDER:
        hero_name = leads[role]
        expected_type = ROLE_TO_JSON_TYPE[role]
        if hero_name not in heroes:
            heroes[hero_name] = _default_hero(hero_name, expected_type)
        hero = heroes[hero_name]
        if not isinstance(hero, dict):
            raise SimulatorError(f"{label}: malformed hero entry for {hero_name!r}.")
        existing_type = hero.get("type")
        if existing_type is not None and existing_type != expected_type:
            raise SimulatorError(
                f"{label}: {hero_name!r} is assigned to {role} ({expected_type}), "
                f"but the source JSON says type={existing_type!r}."
            )
        hero["name"] = hero_name
        hero["type"] = expected_type
        skill_levels = hero.get("skill_levels")
        if isinstance(skill_levels, dict) and skill_levels:
            hero["skill_levels"] = {str(k): 5 for k in skill_levels}
        else:
            hero["skill_levels"] = {"1": 5, "2": 5, "3": 5}
        selected.append(hero_name)
        # Request max widget skill. The shared client removes it automatically if
        # the hero has no widget or its widget role does not match this battle side.
        requested_widgets[hero_name] = 5

    profile["selectedHeroes"] = selected
    special = profile.setdefault("special_bonuses", {})
    if not isinstance(special, dict):
        raise SimulatorError(f"{label}: special_bonuses is not an object.")
    special["widgetLevels"] = requested_widgets
    return leads


def _normalise_troop_quality(quality: Any, label: str) -> dict[str, dict[str, int]]:
    if not isinstance(quality, dict):
        raise SimulatorError(f"{label}: troop quality must be a dictionary.")
    aliases = {
        "inf": ("inf", "infantry"),
        "lanc": ("lanc", "cav", "cavalry"),
        "mark": ("mark", "arch", "archer", "archers"),
    }
    result: dict[str, dict[str, int]] = {}
    for unit_type, keys in aliases.items():
        value = next((quality[key] for key in keys if key in quality), None)
        if not isinstance(value, dict):
            raise SimulatorError(f"{label}: missing quality for {unit_type}.")
        tier = value.get("tier")
        tg = value.get("tg_level", value.get("fc_level"))
        if isinstance(tier, bool) or not isinstance(tier, int) or tier < 1:
            raise SimulatorError(f"{label}: {unit_type} tier must be a positive integer.")
        if isinstance(tg, bool) or not isinstance(tg, int) or tg < 0:
            raise SimulatorError(f"{label}: {unit_type} tg_level must be a non-negative integer.")
        result[unit_type] = {"tier": int(tier), "fc_level": int(tg)}
    return result


def _set_base_stats(profile: dict[str, Any], value: Any, label: str) -> None:
    if value is None:
        return
    stats = profile.get("stats")
    if not isinstance(stats, dict):
        raise SimulatorError(f"{label}: JSON has no root-level 'stats' dictionary.")
    for troop_type in ("inf", "lanc", "mark"):
        block = stats.get(troop_type)
        if not isinstance(block, dict):
            raise SimulatorError(f"{label}: root stats missing {troop_type!r}.")
        for stat_name in STAT_NAMES:
            if stat_name not in block:
                raise SimulatorError(
                    f"{label}: stats[{troop_type!r}] missing {stat_name!r}."
                )
            if isinstance(value, dict):
                row = value.get(troop_type, {})
                if not isinstance(row, dict) or stat_name not in row:
                    raise SimulatorError(f"{label}: configured base stats missing {troop_type}.{stat_name}.")
                block[stat_name] = float(row[stat_name])
            else:
                block[stat_name] = value


def _validate_troop_percentages(
    percentages: Sequence[float],
    label: str,
) -> tuple[float, float, float]:
    """Validate an (infantry, cavalry, archers) percentage split."""
    if isinstance(percentages, (str, bytes)) or not isinstance(percentages, Sequence):
        raise SimulatorError(
            f"{label} must be a 3-value sequence: (infantry, cavalry, archers)."
        )
    if len(percentages) != 3:
        raise SimulatorError(
            f"{label} must contain exactly 3 percentages; received {percentages!r}."
        )

    try:
        values = tuple(float(x) for x in percentages)
    except (TypeError, ValueError) as exc:
        raise SimulatorError(f"{label} contains a non-numeric percentage.") from exc

    if any((not math.isfinite(x)) or x < 0 for x in values):
        raise SimulatorError(f"{label} percentages must be finite and non-negative.")
    if abs(sum(values) - 100.0) > 1e-9:
        raise SimulatorError(
            f"{label} percentages must sum to 100; received {values} "
            f"(sum={sum(values):g})."
        )
    return values  # type: ignore[return-value]


def _pct_to_quantities(
    percentages: Sequence[float],
    total_troops: int,
    label: str,
) -> dict[str, int]:
    """Convert percentages to exact whole-troop quantities."""
    if isinstance(total_troops, bool) or not isinstance(total_troops, int):
        raise SimulatorError(f"{label} total troops must be a whole number.")
    if total_troops < 1:
        raise SimulatorError(f"{label} total troops must be >= 1.")

    values = _validate_troop_percentages(percentages, label)
    exact = [total_troops * p / 100.0 for p in values]
    quantities = [int(round(x)) for x in exact]
    diff = int(total_troops - sum(quantities))
    if diff:
        largest = max(range(3), key=lambda i: values[i])
        quantities[largest] += diff

    return {
        "inf": quantities[0],
        "lanc": quantities[1],
        "mark": quantities[2],
    }


def _apply_troop_percentages(
    profile: Any,
    percentages: Sequence[float],
    total_troops: int,
    quality: Any,
    label: str,
) -> dict[str, int]:
    """Set quantities, tier and TG/fc level for all three troop types."""
    quantities = _pct_to_quantities(percentages, total_troops, label)
    normalized_quality = _normalise_troop_quality(quality, label)
    parent = _troop_parent(profile, label)
    troops = parent.get("troops")
    if not isinstance(troops, list):
        raise SimulatorError(f'{label} has no valid "troops" list.')

    rows_by_type: dict[str, list[dict[str, Any]]] = {"inf": [], "lanc": [], "mark": []}
    for row in troops:
        if not isinstance(row, dict):
            continue
        raw_type = row.get("type")
        if not isinstance(raw_type, str):
            continue
        key = _TROOP_TYPE_ALIASES.get(raw_type.strip().casefold())
        if key in rows_by_type:
            rows_by_type[key].append(row)

    for unit_type in ("inf", "lanc", "mark"):
        rows = rows_by_type[unit_type]
        if len(rows) != 1:
            raise SimulatorError(
                f"{label}: expected exactly one {unit_type!r} troop row in the "
                f"baseline JSON, found {len(rows)}."
            )
        rows[0]["quantity"] = int(quantities[unit_type])
        rows[0]["tier"] = normalized_quality[unit_type]["tier"]
        rows[0]["fc_level"] = normalized_quality[unit_type]["fc_level"]

    return quantities

def _array_parent(
    profile: Any,
    source_name: str,
    field_name: str,
) -> dict[str, Any]:
    matches: list[tuple[str, dict[str, Any]]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if field_name in value:
                if not isinstance(value[field_name], list):
                    raise SimulatorError(
                        f'{source_name} has a non-list "{field_name}" value at {path}.'
                    )
                matches.append((path, value))
            for key, child in value.items():
                if key != field_name:
                    visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(profile, "$")
    if not matches:
        raise SimulatorError(
            f'{source_name} contains no "{field_name}" array, so it cannot be replaced.'
        )
    if len(matches) > 1:
        paths = ", ".join(path for path, _ in matches)
        raise SimulatorError(
            f'{source_name} contains multiple "{field_name}" arrays ({paths}). '
            "Use a single-side exported JSON file."
        )
    return matches[0][1]


def _joiner_parent(profile: Any, source_name: str) -> dict[str, Any]:
    return _array_parent(profile, source_name, "joiners")


def _troop_parent(profile: Any, source_name: str) -> dict[str, Any]:
    return _array_parent(profile, source_name, "troops")



def _validate_lineup(
    lineup: Sequence[str | None],
    label: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Validate one four-slot joiner lineup without changing its meaning."""

    if isinstance(lineup, (str, bytes)) or not isinstance(lineup, Sequence):
        raise SimulatorError(
            f"{label} must be a four-slot sequence of hero names/None."
        )

    if len(lineup) != 4:
        raise SimulatorError(
            f"{label} must contain exactly 4 slots; received {len(lineup)}."
        )

    cleaned: list[str | None] = []
    for index, raw_name in enumerate(lineup, start=1):
        if raw_name is None:
            cleaned.append(None)
            continue

        if not isinstance(raw_name, str):
            raise SimulatorError(
                f"{label} slot {index} must be a hero name or None; "
                f"received {raw_name!r}."
            )

        name = raw_name.strip()
        if not name:
            raise SimulatorError(
                f"{label} slot {index} is blank. Use None for an empty slot."
            )
        cleaned.append(name)

    return tuple(cleaned)  # type: ignore[return-value]


def _profile_for_lineup(
    baseline: Any,
    source_name: str,
    lineup: Sequence[str | None],
    leads: Any,
    troop_percentages: Sequence[float],
    total_troops: int,
    troop_quality: Any,
    base_stat_override: Any,
    role: str,
) -> Any:
    """Build one deterministic attacker/defender condition profile."""
    if role not in {"attacker", "defender"}:
        raise SimulatorError(f"Unknown profile role: {role!r}.")
    lineup = _validate_lineup(lineup, f"{source_name} joiners")
    profile = copy.deepcopy(baseline)
    if not isinstance(profile, dict):
        raise SimulatorError(f"{source_name}: baseline JSON must be an object.")

    # Role assignment is authoritative; the source JSON's internal name is ignored.
    profile["name"] = role
    _set_leads(profile, leads, role)

    parent = _joiner_parent(profile, source_name)
    parent["joiners"] = [
        {
            "id": index,
            "name": str(raw_name).strip(),
            "skill_levels": {"1": JOINER_SKILL_LEVEL},
        }
        for index, raw_name in enumerate(lineup)
        if raw_name is not None and str(raw_name).strip()
    ]

    _apply_troop_percentages(
        profile, troop_percentages, total_troops, troop_quality, f"{role} troop setup"
    )
    _set_base_stats(profile, base_stat_override, role)
    return profile

def _write_json(path: Path, value: Any) -> None:
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise SimulatorError(f"Could not write temporary condition JSON: {path}") from exc


def _validate_settings() -> tuple[bool | None, bool | None]:
    """Validate run inputs for the v2 profile/progression model."""
    if SIMULATIONS_PER_BATCH < 1 or BATCHES_PER_CONDITION < 1:
        raise SimulatorError("Simulation and batch counts must both be at least 1.")
    if TIMEOUT_SECONDS < 1 or DELAY_BETWEEN_BATCHES_SECONDS < 0:
        raise SimulatorError("Timeout must be positive and delay cannot be negative.")
    if ATTACK_TOTAL_TROOPS < 1 or DEFENSE_TOTAL_TROOPS < 1:
        raise SimulatorError("Total troop counts must be positive.")
    if not 1 <= JOINER_SKILL_LEVEL <= 5:
        raise SimulatorError("JOINER_SKILL_LEVEL must be between 1 and 5.")
    for value, label in ((ATTACKER_PROFILE, "ATTACKER_PROFILE"), (DEFENDER_PROFILE, "DEFENDER_PROFILE")):
        if value not in {"A", "B"}:
            raise SimulatorError(f"{label} must be 'A' or 'B', got {value!r}.")
    for label, path in (
        ("attacker player", ATTACKER_JSON), ("defender player", DEFENDER_JSON),
        ("hero schema lookup", HERO_STATS_LOOKUP), ("hero progression lookup", HERO_PROGRESSION_LOOKUP),
    ):
        if not Path(path).is_file():
            raise SimulatorError(f"{label} file not found: {path}")
        _load_json(Path(path))
    _normalise_troop_quality(ATTACK_TROOP_QUALITY, "attacker")
    _normalise_troop_quality(DEFENSE_TROOP_QUALITY, "defender")
    _normalise_leads(ATTACK_LEADS, "attacker")
    _normalise_leads(DEFENSE_LEADS, "defender")
    for label, toggles in (("attacker", ATTACK_WIDGET_BUFFS), ("defender", DEFENSE_WIDGET_BUFFS)):
        if not isinstance(toggles, dict) or any(role not in toggles or not isinstance(toggles[role], bool) for role in ROLE_ORDER):
            raise SimulatorError(f"{label} widget buffs must contain boolean inf/cav/arch switches.")
    _pct_to_quantities(ATTACK_TROOP_PERCENTAGES, ATTACK_TOTAL_TROOPS, "attacker troop setup")
    _pct_to_quantities(DEFENSE_TROOP_PERCENTAGES, DEFENSE_TOTAL_TROOPS, "defender troop setup")
    if not build_conditions():
        raise SimulatorError("No joiner conditions were configured.")
    atk_source = _load_json(ATTACKER_JSON)
    def_source = _load_json(DEFENDER_JSON)
    atk_effective = False if HERO_PROGRESSION_CONFIG_ATK.get("enabled", False) else bool(atk_source.get("stats_include_heroes", False))
    def_effective = False if HERO_PROGRESSION_CONFIG_DEF.get("enabled", False) else bool(def_source.get("stats_include_heroes", False))
    print(
        "Profile progression: "
        f"ATK[configured={bool(HERO_PROGRESSION_CONFIG_ATK.get('enabled', False))}]; "
        f"DEF[configured={bool(HERO_PROGRESSION_CONFIG_DEF.get('enabled', False))}]; "
        f"special bonuses ATK/DEF={APPLY_SPECIAL_BONUSES_ATK}/{APPLY_SPECIAL_BONUSES_DEF}; "
        f"active widget heroes ATK={_fixed_widget_heroes(ATTACK_LEADS, ATTACK_WIDGET_BUFFS)} / DEF={_fixed_widget_heroes(DEFENSE_LEADS, DEFENSE_WIDGET_BUFFS)}.",
        flush=True,
    )
    return atk_effective, def_effective
def _completed_row_key(
    row: dict[str, str | None],
) -> tuple[
    tuple[
        tuple[int, tuple[str, ...]],
        tuple[int, tuple[str, ...]],
    ],
    int,
]:
    """Parse one existing CSV result into its condition key and batch number."""

    joiner_columns = [
        "joiner1_atk",
        "joiner2_atk",
        "joiner3_atk",
        "joiner4_atk",
        "joiner1_def",
        "joiner2_def",
        "joiner3_def",
        "joiner4_def",
    ]
    missing_values = [column for column in joiner_columns if row.get(column) is None]
    if missing_values:
        raise ValueError(f"missing values for {', '.join(missing_values)}")

    try:
        batch = int(str(row.get("batch", "")).strip())
    except ValueError as exc:
        raise ValueError("batch is not an integer") from exc
    if batch < 1:
        raise ValueError("batch must be at least 1")

    try:
        winrate = float(str(row.get("winrate", "")).strip())
    except ValueError as exc:
        raise ValueError("winrate is not numeric") from exc
    if not 0 <= winrate <= 100:
        raise ValueError("winrate is outside 0..100")

    heroes: list[str | None] = []
    for column in joiner_columns:
        value = str(row[column]).strip()
        heroes.append(value or None)

    return _condition_key(heroes[:4], heroes[4:]), batch


def _prepare_csv() -> dict[
    tuple[
        tuple[int, tuple[str, ...]],
        tuple[int, tuple[str, ...]],
    ],
    set[int],
]:
    """Initialize a new CSV or index completed batches for resume mode."""

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if (
        not RESUME_FROM_CSV
        or not OUTPUT_CSV.exists()
        or OUTPUT_CSV.stat().st_size == 0
    ):
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=CSV_COLUMNS).writeheader()
        return {}

    completed: dict[
        tuple[
            tuple[int, tuple[str, ...]],
            tuple[int, tuple[str, ...]],
        ],
        set[int],
    ] = {}

    try:
        with OUTPUT_CSV.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise SimulatorError(f"Resume CSV has no header: {OUTPUT_CSV}")
            missing_columns = [
                column for column in CSV_COLUMNS if column not in reader.fieldnames
            ]
            if missing_columns:
                raise SimulatorError(
                    f"Resume CSV is missing column(s) {missing_columns}: {OUTPUT_CSV}"
                )
            rows = list(reader)
    except OSError as exc:
        raise SimulatorError(f"Could not read resume CSV: {OUTPUT_CSV}") from exc

    duplicate_rows = 0
    ignored_trailing_row = False
    valid_rows: list[dict[str, str | None]] = []
    for index, row in enumerate(rows, start=2):
        if not any(value and str(value).strip() for value in row.values()):
            continue
        try:
            key, batch = _completed_row_key(row)
        except ValueError as exc:
            if index == len(rows) + 1:
                ignored_trailing_row = True
                continue
            raise SimulatorError(
                f"Invalid resume CSV row {index}: {exc}. File: {OUTPUT_CSV}"
            ) from exc

        valid_rows.append(row)
        batches = completed.setdefault(key, set())
        if batch in batches:
            duplicate_rows += 1
        batches.add(batch)

    if ignored_trailing_row:
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=CSV_COLUMNS, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(valid_rows)
        print(
            "Resume warning: removed an incomplete final CSV row from a prior "
            "interruption.",
            flush=True,
        )
    if duplicate_rows:
        print(
            f"Resume warning: found {duplicate_rows} duplicate completed batch "
            "row(s); each batch is counted only once.",
            flush=True,
        )

    return completed


def _append_csv(row: dict[str, Any]) -> None:
    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writerow(row)



async def run_experiment() -> None:
    """Run every joiner condition and append each batch win rate to the CSV."""
    effective_stats_include_heroes_atk, effective_stats_include_heroes_def = _validate_settings()

    EXPERIMENT_FOLDER.mkdir(parents=True, exist_ok=True)

    settings_path = write_experiment_profile_metadata(
        OUTPUT_CSV,
        resume_from_csv=RESUME_FROM_CSV,
        add_5star_hero_stats_attacker=ADD_5STAR_HERO_STATS_ATK,
        add_5star_hero_stats_defender=ADD_5STAR_HERO_STATS_DEF,
        add_hero_gear_attacker=ADD_HERO_GEAR_ATK,
        add_hero_gear_defender=ADD_HERO_GEAR_DEF,
        stats_include_heroes_requested_attacker=(not ADD_HERO_STATS_ATK),
        stats_include_heroes_requested_defender=(not ADD_HERO_STATS_DEF),
        stats_include_heroes_effective_attacker=effective_stats_include_heroes_atk,
        stats_include_heroes_effective_defender=effective_stats_include_heroes_def,
        hero_stats_lookup=HERO_STATS_LOOKUP,
        hero_gear_lookup=HERO_GEAR_LOOKUP,
        apply_special_bonuses=bool(APPLY_SPECIAL_BONUSES_ATK or APPLY_SPECIAL_BONUSES_DEF),
        special_bonuses_attacker=_special_bonus_overrides("atk"),
        special_bonuses_defender=_special_bonus_overrides("def"),
    )
    print(f"Machine-readable experiment settings saved to: {settings_path}", flush=True)

    conditions = build_conditions()
    attacker_baseline = _load_json(ATTACKER_JSON)
    defender_baseline = _load_json(DEFENDER_JSON)

    print(
        f"Profile mapping: attacker <- profile {ATTACKER_PROFILE} "
        f"({ATTACKER_JSON.name}); "
        f"defender <- profile {DEFENDER_PROFILE} "
        f"({DEFENDER_JSON.name})",
        flush=True,
    )

    first_atk_info, first_def_info = _save_first_prepared_profiles(
        conditions,
        attacker_baseline,
        defender_baseline,
        effective_stats_include_heroes_atk,
        effective_stats_include_heroes_def,
    )
    _augment_machine_settings(settings_path)
    readable_settings = _write_readable_settings(
        conditions,
        effective_stats_include_heroes_atk,
        effective_stats_include_heroes_def,
        first_atk_info,
        first_def_info,
    )
    print(f"Readable experiment settings saved to: {readable_settings}", flush=True)
    print(f"First prepared attacker JSON saved to: {FIRST_ATTACKER_JSON}", flush=True)
    print(f"First prepared defender JSON saved to: {FIRST_DEFENDER_JSON}", flush=True)
    print("Effective troop stats (root + selected lead hero stats; active widget skill excluded):", flush=True)
    print(f"  Attacker: {first_atk_info.get('effective_troop_stats_text', '(unavailable)')}", flush=True)
    print(f"  Defender: {first_def_info.get('effective_troop_stats_text', '(unavailable)')}", flush=True)

    # Validate every requested condition before launching Chromium.
    for condition in conditions:
        _profile_for_lineup(
            attacker_baseline, ATTACKER_JSON.name, condition[:4], ATTACK_LEADS,
            ATTACK_TROOP_PERCENTAGES, ATTACK_TOTAL_TROOPS, ATTACK_TROOP_QUALITY,
            ATTACK_BASE_STAT_OVERRIDE, "attacker",
        )
        _profile_for_lineup(
            defender_baseline, DEFENDER_JSON.name, condition[4:], DEFENSE_LEADS,
            DEFENSE_TROOP_PERCENTAGES, DEFENSE_TOTAL_TROOPS, DEFENSE_TROOP_QUALITY,
            DEFENSE_BASE_STAT_OVERRIDE, "defender",
        )

    attacker_lineups, defender_lineups = build_side_lineups()
    print(
        f"Unique order-independent joiner lineups: attacker={len(attacker_lineups)}, "
        f"defender={len(defender_lineups)}; combined conditions={len(conditions)}.",
        flush=True,
    )
    print(
        f"Preparing {len(conditions)} condition(s), "
        f"{BATCHES_PER_CONDITION} batch(es) per condition, "
        f"{SIMULATIONS_PER_BATCH} simulations per batch.",
        flush=True,
    )

    completed_batches = _prepare_csv()
    target_batches = set(range(1, BATCHES_PER_CONDITION + 1))
    batch_plan = []
    already_completed = 0

    for condition_number, condition in enumerate(conditions, start=1):
        key = _condition_key(condition[:4], condition[4:])
        completed_for_condition = completed_batches.get(key, set())
        completed_target = target_batches.intersection(completed_for_condition)
        missing_batches = sorted(target_batches - completed_target)
        already_completed += len(completed_target)
        if missing_batches:
            batch_plan.append((condition_number, condition, missing_batches))

    total_target = len(conditions) * BATCHES_PER_CONDITION
    pending = total_target - already_completed
    if RESUME_FROM_CSV:
        print(
            f"Resume scan: {already_completed}/{total_target} target batch(es) "
            f"already complete; {pending} remain across {len(batch_plan)} condition(s).",
            flush=True,
        )

    if not batch_plan:
        print(f"Nothing to run. Results already complete: {OUTPUT_CSV}", flush=True)
        return

    with tempfile.TemporaryDirectory(prefix="kingshot_joiner_conditions_") as tmp:
        condition_folder = Path(tmp)

        async with KingshotSimulatorSession(
            headless=HEADLESS,
            timeout_seconds=TIMEOUT_SECONDS,
        ) as simulator:
            for condition_number, condition, missing_batches in batch_plan:
                atk = condition[:4]
                defender = condition[4:]
                completed_count = BATCHES_PER_CONDITION - len(missing_batches)

                print(
                    f"Condition {condition_number}/{len(conditions)}: "
                    f"attacker={atk}, defender={defender}; "
                    f"completed={completed_count}/{BATCHES_PER_CONDITION}",
                    flush=True,
                )

                attacker_condition = _profile_for_lineup(
                    attacker_baseline, ATTACKER_JSON.name, atk, ATTACK_LEADS,
                    ATTACK_TROOP_PERCENTAGES, ATTACK_TOTAL_TROOPS, ATTACK_TROOP_QUALITY,
                    ATTACK_BASE_STAT_OVERRIDE, "attacker",
                )
                defender_condition = _profile_for_lineup(
                    defender_baseline, DEFENDER_JSON.name, defender, DEFENSE_LEADS,
                    DEFENSE_TROOP_PERCENTAGES, DEFENSE_TOTAL_TROOPS, DEFENSE_TROOP_QUALITY,
                    DEFENSE_BASE_STAT_OVERRIDE, "defender",
                )

                attacker_path = condition_folder / "attacker_condition.json"
                defender_path = condition_folder / "defender_condition.json"
                _write_json(attacker_path, attacker_condition)
                _write_json(defender_path, defender_condition)

                atk_runtime_info, def_runtime_info = await simulator.load_condition(
                    attacker_path,
                    defender_path,
                    SIMULATIONS_PER_BATCH,
                    add_5star_hero_stats_attacker=ADD_5STAR_HERO_STATS_ATK,
                    add_5star_hero_stats_defender=ADD_5STAR_HERO_STATS_DEF,
                    add_hero_gear_attacker=ADD_HERO_GEAR_ATK,
                    add_hero_gear_defender=ADD_HERO_GEAR_DEF,
                    stats_include_heroes_attacker=effective_stats_include_heroes_atk,
                    stats_include_heroes_defender=effective_stats_include_heroes_def,
                    hero_stats_lookup=HERO_STATS_LOOKUP,
                    hero_gear_lookup=HERO_GEAR_LOOKUP,
                    hero_progression_lookup=HERO_PROGRESSION_LOOKUP,
                    hero_progression_config_attacker=HERO_PROGRESSION_CONFIG_ATK,
                    hero_progression_config_defender=HERO_PROGRESSION_CONFIG_DEF,
                    hero_gear_config_attacker=HERO_GEAR_CONFIG_ATK,
                    hero_gear_config_defender=HERO_GEAR_CONFIG_DEF,
                    apply_special_bonuses_attacker=APPLY_SPECIAL_BONUSES_ATK,
                    apply_special_bonuses_defender=APPLY_SPECIAL_BONUSES_DEF,
                    apply_widget_buffs_attacker=APPLY_WIDGET_BUFFS_ATK,
                    apply_widget_buffs_defender=APPLY_WIDGET_BUFFS_DEF,
                    active_widget_heroes_attacker=_fixed_widget_heroes(ATTACK_LEADS, ATTACK_WIDGET_BUFFS),
                    active_widget_heroes_defender=_fixed_widget_heroes(DEFENSE_LEADS, DEFENSE_WIDGET_BUFFS),
                    special_bonuses_attacker=_special_bonus_overrides("atk"),
                    special_bonuses_defender=_special_bonus_overrides("def"),
                )
                print("  Final stats (enabled Pet/City + formation widget buffs included):", flush=True)
                print(f"    ATK {atk_runtime_info.get('final_stats_text', '(unavailable)')}", flush=True)
                print(f"    DEF {def_runtime_info.get('final_stats_text', '(unavailable)')}", flush=True)

                for batch_position, batch in enumerate(missing_batches, start=1):
                    print(
                        f"  Running batch {batch}/{BATCHES_PER_CONDITION}...",
                        flush=True,
                    )
                    winrate = await simulator.run_batch()
                    write_last_run_stats(
                        LAST_RUN_STATS_FILE,
                        experiment="joiners",
                        attacker_info=atk_runtime_info,
                        defender_info=def_runtime_info,
                        context={
                            "condition": condition_number,
                            "batch": batch,
                            "attacker_joiners": [x or "" for x in atk],
                            "defender_joiners": [x or "" for x in defender],
                        },
                    )

                    row = {
                        "condition": condition_number,
                        "batch": batch,
                        "joiner1_atk": atk[0] or "",
                        "joiner2_atk": atk[1] or "",
                        "joiner3_atk": atk[2] or "",
                        "joiner4_atk": atk[3] or "",
                        "joiner1_def": defender[0] or "",
                        "joiner2_def": defender[1] or "",
                        "joiner3_def": defender[2] or "",
                        "joiner4_def": defender[3] or "",
                        "winrate": winrate,
                    }
                    _append_csv(row)

                    print(
                        f"  Batch {batch}/{BATCHES_PER_CONDITION}: {winrate}%",
                        flush=True,
                    )

                    if (
                        batch_position < len(missing_batches)
                        and DELAY_BETWEEN_BATCHES_SECONDS
                    ):
                        await simulator.page.wait_for_timeout(
                            int(DELAY_BETWEEN_BATCHES_SECONDS * 1000)
                        )

    print(f"Finished. Results saved to: {OUTPUT_CSV}", flush=True)


def preview_experiment() -> None:
    _validate_settings()
    attacker_lineups, defender_lineups = build_side_lineups()
    conditions = build_conditions()
    print("Configuration is valid.", flush=True)
    print(f"Attacker profile: {ATTACKER_PROFILE} -> {ATTACKER_JSON.name}", flush=True)
    print(f"Defender profile: {DEFENDER_PROFILE} -> {DEFENDER_JSON.name}", flush=True)
    print(f"Unique attacker joiner lineups: {len(attacker_lineups):,}", flush=True)
    print(f"Unique defender joiner lineups: {len(defender_lineups):,}", flush=True)
    print(f"Combined unique conditions: {len(conditions):,}", flush=True)
    print(f"Target simulator batches: {len(conditions) * BATCHES_PER_CONDITION:,}", flush=True)
    print(f"Simulations per batch: {SIMULATIONS_PER_BATCH:,}", flush=True)


if __name__ == "__main__":
    import sys
    if "--preview" in sys.argv:
        print("Checking Kingshot joiner configuration...", flush=True)
        preview_experiment()
    else:
        print("Running Kingshot joiner experiment...", flush=True)
        run_spyder_compatible(run_experiment)
