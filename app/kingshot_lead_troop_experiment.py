#!/usr/bin/env python3
"""
Lead-hero + troop-composition Kingshot experiment.

Normal use is configured through kingshot_gui.py / kingshot_config.json. The
Python configuration block below remains only as a fallback. Full workflow notes
are in README.txt. Results are written to results/lead_troop_experiment.
"""

from __future__ import annotations

import copy
import csv
import itertools
import json
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from kingshot_config import apply_config_to_globals
from kingshot_paths import APP_DIR, ROOT_DIR, JSON_DIR, RESULTS_DIR, LAST_RUN_STATS_FILE

from kingshot_simulator_client import (
    KingshotSimulatorSession,
    SimulatorError,
    run_spyder_compatible,
    validate_profile_options,
    write_experiment_profile_metadata,
    prepare_profile_json,
    get_hero_widget_type,
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

EXPERIMENT_FOLDER = RESULTS_DIR / "lead_troop_experiment"
OUTPUT_CSV = EXPERIMENT_FOLDER / "kingshot_lead_troop_winrates.csv"
SETTINGS_TXT = EXPERIMENT_FOLDER / "experiment_settings.txt"
FIRST_ATTACKER_JSON = EXPERIMENT_FOLDER / "attacker_data.json"
FIRST_DEFENDER_JSON = EXPERIMENT_FOLDER / "defender_data.json"

SIMULATIONS_PER_BATCH = 100
BATCHES_PER_CONDITION = 1
RESUME_FROM_CSV = False

HEADLESS = True
TIMEOUT_SECONDS = 30
DELAY_BETWEEN_BATCHES_SECONDS = 0.5

ATTACK_TOTAL_TROOPS = 1_600_000
DEFENSE_TOTAL_TROOPS = 1_600_000

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
DEFENSE_BASE_STAT_OVERRIDE = 2200

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

ATTACK_FORMATIONS = [
    {
        "joiners": ["Yang", "Triton", "Gordon", "Fahd"],
        "leads": {"inf": "Triton", "cav": "Thrud", "arch": "Marlin"},
        "troop_variants_pct": [
            (70, 30, 0),
            (60, 40, 0),
            (50, 50, 0),
            (40, 60, 0),
            (30, 70, 0),
        ],
    },
    {
        "joiners": ["Vivian", "Thrud", "Amane", "Chenko"],
        "leads": {"inf": "Triton", "cav": "Petra", "arch": "Yang"},
        "troop_variants_pct": [
            (20, 5, 75),
            (30, 5, 65),
            (40, 5, 55),
            (50, 5, 45),
            (60, 5, 35),
            (20, 10, 70),
            (30, 10, 60),
            (40, 10, 50),
            (50, 10, 40),
            (60, 10, 30),
            (20, 15, 65),
            (30, 15, 55),
            (40, 15, 45),
            (50, 15, 35),
            (60, 15, 25),
        ],
    },
]

DEFENSE_SCENARIOS = [
    {
        "joiners": None,
        "leads": {"inf": "Triton", "cav": "Petra", "arch": "Yang"},
        "troop_variants_pct": [
            (50, 10, 40),
        ],
    },
]

# None = all defense x attack formation matchups.
ONLY_MATCHUPS = None

# =============================================================================
# END CONFIGURATION
# =============================================================================

# If kingshot_config.json exists, it is the shared source of truth for this script.
apply_config_to_globals(globals(), "lead_troop")



def _special_bonus_overrides(side: str) -> dict[str, Any]:
    if side == "atk":
        return copy.deepcopy(SPECIAL_STATS_CONFIG_ATK)
    if side == "def":
        return copy.deepcopy(SPECIAL_STATS_CONFIG_DEF)
    raise ValueError(f"Unknown side: {side!r}")


def _pretty(value: Any) -> str:
    """Readable, deterministic representation for the human settings file."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def _copy_plotter_scripts() -> None:
    """Archive the plotter with the experiment so renamed folders remain standalone."""
    EXPERIMENT_FOLDER.mkdir(parents=True, exist_ok=True)
    source = APP_DIR / "kingshot_lead_troop_plotter.py"
    destination = EXPERIMENT_FOLDER / source.name
    if source.is_file() and source.resolve() != destination.resolve():
        shutil.copy2(source, destination)


def _save_first_prepared_profiles(
    conditions: list[dict[str, Any]],
    attacker_baseline: Any,
    defender_baseline: Any,
    effective_stats_include_heroes_atk: bool | None,
    effective_stats_include_heroes_def: bool | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Save the exact final JSON pair for the first configured condition."""
    first = conditions[0]
    attacker_profile, _, _ = _make_profile(
        attacker_baseline,
        first["atk_leads"],
        first["atk_joiners"],
        first["atk_pct"],
        ATTACK_TOTAL_TROOPS,
        ATTACK_TROOP_QUALITY,
        ATTACK_BASE_STAT_OVERRIDE,
        "attacker",
    )
    defender_profile, _, _ = _make_profile(
        defender_baseline,
        first["def_leads"],
        first["def_joiners"],
        first["def_pct"],
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
            active_widget_heroes=first.get("atk_widget_buff_heroes", []),
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
            active_widget_heroes=first.get("def_widget_buff_heroes", []),
            special_bonuses=_special_bonus_overrides("def"),
            side="defender",
            label="defender",
        )
    return atk_info, def_info


def _augment_machine_settings(
    settings_path: Path,
    first_atk_info: dict[str, Any],
    first_def_info: dict[str, Any],
) -> None:
    """Record v2 progression, shared troop setup, and effective first-condition stats."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulatorError(
            f"Could not update experiment settings file {settings_path}: {exc}"
        ) from exc

    data["profile_progression"] = {
        "hero_progression_lookup": str(HERO_PROGRESSION_LOOKUP),
        "attacker": {
            "profile": ATTACKER_PROFILE,
            "configured_progression": copy.deepcopy(HERO_PROGRESSION_CONFIG_ATK),
            "hero_gear": copy.deepcopy(HERO_GEAR_CONFIG_ATK),
            "effective_stats_include_heroes": first_atk_info.get("stats_include_heroes"),
            "effective_troop_stats": copy.deepcopy(first_atk_info.get("effective_troop_stats", {})),
        },
        "defender": {
            "profile": DEFENDER_PROFILE,
            "configured_progression": copy.deepcopy(HERO_PROGRESSION_CONFIG_DEF),
            "hero_gear": copy.deepcopy(HERO_GEAR_CONFIG_DEF),
            "effective_stats_include_heroes": first_def_info.get("stats_include_heroes"),
            "effective_troop_stats": copy.deepcopy(first_def_info.get("effective_troop_stats", {})),
        },
        "apply_special_bonuses": {
            "attacker": bool(APPLY_SPECIAL_BONUSES_ATK),
            "defender": bool(APPLY_SPECIAL_BONUSES_DEF),
        },
        "apply_widget_buffs": {
            "attacker": bool(APPLY_WIDGET_BUFFS_ATK),
            "defender": bool(APPLY_WIDGET_BUFFS_DEF),
        },
    }
    data["lead_troop_experiment"] = {
        "simulations_per_batch": int(SIMULATIONS_PER_BATCH),
        "batches_per_condition_target": int(BATCHES_PER_CONDITION),
        "attacker_total_troops": int(ATTACK_TOTAL_TROOPS),
        "defender_total_troops": int(DEFENSE_TOTAL_TROOPS),
        "attacker_troop_quality": copy.deepcopy(ATTACK_TROOP_QUALITY),
        "defender_troop_quality": copy.deepcopy(DEFENSE_TROOP_QUALITY),
        "attacker_base_stat_override": ATTACK_BASE_STAT_OVERRIDE,
        "defender_base_stat_override": DEFENSE_BASE_STAT_OVERRIDE,
        "attacker_profile_source": ATTACKER_PROFILE,
        "defender_profile_source": DEFENDER_PROFILE,
    }

    try:
        settings_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        raise SimulatorError(
            f"Could not write experiment settings file {settings_path}: {exc}"
        ) from exc


def _write_readable_settings(
    conditions: list[dict[str, Any]],
    effective_stats_include_heroes_atk: bool | None,
    effective_stats_include_heroes_def: bool | None,
    first_atk_info: dict[str, Any],
    first_def_info: dict[str, Any],
) -> Path:
    """Write a human-readable record of the configuration used for this run."""
    lines = [
        "KINGSHOT LEAD + TROOP EXPERIMENT SETTINGS",
        "=" * 44,
        "",
        "Files",
        "-----",
        f"Attacker baseline JSON: {ATTACKER_JSON}",
        f"Defender baseline JSON: {DEFENDER_JSON}",
        f"Output CSV: {OUTPUT_CSV}",
        f"First prepared attacker JSON: {FIRST_ATTACKER_JSON.name}",
        f"First prepared defender JSON: {FIRST_DEFENDER_JSON.name}",
        "",
        "Run settings",
        "------------",
        f"SIMULATIONS_PER_BATCH = {SIMULATIONS_PER_BATCH}",
        f"BATCHES_PER_CONDITION = {BATCHES_PER_CONDITION}",
        f"RESUME_FROM_CSV = {RESUME_FROM_CSV}",
        f"HEADLESS = {HEADLESS}",
        f"TIMEOUT_SECONDS = {TIMEOUT_SECONDS}",
        f"DELAY_BETWEEN_BATCHES_SECONDS = {DELAY_BETWEEN_BATCHES_SECONDS}",
        f"Number of unique conditions = {len(conditions)}",
        "",
        "Troops and root stats",
        "---------------------",
        f"ATTACK_TOTAL_TROOPS = {ATTACK_TOTAL_TROOPS}",
        f"DEFENSE_TOTAL_TROOPS = {DEFENSE_TOTAL_TROOPS}",
        f"ATTACK_TROOP_QUALITY = {_pretty(ATTACK_TROOP_QUALITY)}",
        f"DEFENSE_TROOP_QUALITY = {_pretty(DEFENSE_TROOP_QUALITY)}",
        f"ATTACK_BASE_STAT_OVERRIDE = {ATTACK_BASE_STAT_OVERRIDE!r}",
        f"DEFENSE_BASE_STAT_OVERRIDE = {DEFENSE_BASE_STAT_OVERRIDE!r}",
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
        "Special stats are stored directly in kingshot_config.json; appointments are forced to zero.",
        "Attacker special stats:",
        _pretty(_special_bonus_overrides("atk")),
        "Defender special stats:",
        _pretty(_special_bonus_overrides("def")),
        "",
        "Lead formations / troop variants",
        "--------------------------------",
        "ATTACK_FORMATIONS =",
        _pretty(ATTACK_FORMATIONS),
        "DEFENSE_SCENARIOS =",
        _pretty(DEFENSE_SCENARIOS),
        f"ONLY_MATCHUPS = {_pretty(ONLY_MATCHUPS)}",
        f"JOINER_SKILL_LEVEL = {JOINER_SKILL_LEVEL}",
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


ROLE_TO_JSON_TYPE = {
    "inf": "inf",
    "cav": "lanc",
    "arch": "mark",
}
ROLE_ORDER = ("inf", "cav", "arch")
TROOP_TYPES = ("inf", "lanc", "mark")
STAT_NAMES = ("attack", "defense", "lethality", "health")

# widget_kind_* columns are retained for CSV compatibility, but their values are
# now filled automatically from the hero lookup; users no longer specify them in lead().
CSV_COLUMNS = [
    "condition",
    "batch",
    "defense_scenario",
    "attack_formation",
    "defense_troop_variant",
    "attack_troop_variant",
    "defense_troop_range",
    "attack_troop_range",

    "joiner1_atk",
    "joiner2_atk",
    "joiner3_atk",
    "joiner4_atk",
    "joiner1_def",
    "joiner2_def",
    "joiner3_def",
    "joiner4_def",

    "lead_inf_atk",
    "lead_cav_atk",
    "lead_arch_atk",
    "lead_inf_def",
    "lead_cav_def",
    "lead_arch_def",

    "widget_inf_atk",
    "widget_cav_atk",
    "widget_arch_atk",
    "widget_inf_def",
    "widget_cav_def",
    "widget_arch_def",

    "widget_kind_inf_atk",
    "widget_kind_cav_atk",
    "widget_kind_arch_atk",
    "widget_kind_inf_def",
    "widget_kind_cav_def",
    "widget_kind_arch_def",

    "infantry_pct_atk",
    "cavalry_pct_atk",
    "archers_pct_atk",
    "infantry_pct_def",
    "cavalry_pct_def",
    "archers_pct_def",

    "infantry_atk",
    "cavalry_atk",
    "archers_atk",
    "infantry_def",
    "cavalry_def",
    "archers_def",

    "attacker_total_troops",
    "defender_total_troops",
    "base_stat_override_atk",
    "base_stat_override_def",
    "winrate",
]


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulatorError(f"Not a readable JSON file: {path} ({exc})") from exc


def _write_json(path: Path, value: Any) -> None:
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise SimulatorError(f"Could not write temporary JSON: {path}") from exc


def _validate_split(split, label: str) -> tuple[float, float, float]:
    if len(split) != 3:
        raise SimulatorError(f"{label}: troop split must contain 3 percentages.")
    vals = tuple(float(x) for x in split)
    if any(x < 0 for x in vals):
        raise SimulatorError(f"{label}: troop percentages cannot be negative.")
    if abs(sum(vals) - 100.0) > 1e-9:
        raise SimulatorError(
            f"{label}: troop percentages must sum to 100; got {vals}."
        )
    return vals


def _pct_to_raw(split, total: int) -> dict[str, int]:
    vals = _validate_split(split, "troop split")
    exact = [total * p / 100.0 for p in vals]
    raw = [int(round(x)) for x in exact]
    diff = total - sum(raw)
    if diff:
        idx = max(range(3), key=lambda i: vals[i])
        raw[idx] += diff
    return {"inf": raw[0], "lanc": raw[1], "mark": raw[2]}


def _normalise_leads(leads, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(leads, dict):
        raise SimulatorError(f"{label}: leads must be a dict with keys {ROLE_ORDER}.")
    missing = [r for r in ROLE_ORDER if r not in leads]
    extra = [r for r in leads if r not in ROLE_ORDER]
    if missing or extra:
        raise SimulatorError(
            f"{label}: leads must contain exactly {ROLE_ORDER}; missing={missing}, extra={extra}."
        )

    result = {}
    for role in ROLE_ORDER:
        value = leads[role]
        if isinstance(value, dict):
            name = str(value.get("name", "")).strip()
        else:
            name = str(value).strip()
        if not name:
            raise SimulatorError(f"{label}: role '{role}' has no hero name.")
        result[role] = {"name": name, "widget_level": 5}
    return result


def _formation_name(leads: dict[str, dict[str, Any]]) -> str:
    """Generate a formation name from infantry + cavalry + archer leads."""
    return " + ".join(str(leads[role]["name"]) for role in ROLE_ORDER)


def _formation_widget_heroes(
    formation: dict[str, Any],
    leads: dict[str, dict[str, Any]],
) -> list[str]:
    """Return lead heroes whose per-formation active-widget toggle is enabled."""
    toggles = formation.get("widget_buffs", {})
    if not isinstance(toggles, dict):
        toggles = {}
    return [
        str(leads[role]["name"])
        for role in ROLE_ORDER
        if bool(toggles.get(role, True))
    ]


def _default_hero(name: str, json_type: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": json_type,
        "stats": {
            "attack": 0,
            "defense": 0,
            "lethality": 0,
            "health": 0,
        },
        "skill_levels": {
            "1": 5,
            "2": 5,
            "3": 5,
        },
        "widget_level": 5,
    }


def _set_leads(profile: dict, leads, label: str) -> dict[str, dict[str, Any]]:
    """
    Set the selected lead heroes and their active widgets.

    The exported simulator JSON stores active widget levels under:
        profile["special_bonuses"]["widgetLevels"]

    It does NOT encode "offensive" vs "defensive" or the widget's exact buff.
    Those effects are inferred by the simulator from hero name + widget level.

    To prevent stale widgets from the baseline JSON leaking into another
    condition, widgetLevels is rebuilt from scratch for every condition.
    """
    leads = _normalise_leads(leads, label)

    if not isinstance(profile.get("heroes"), dict):
        raise SimulatorError(f"{label}: JSON has no 'heroes' dictionary.")

    selected = []
    active_widgets = {}

    for role in ROLE_ORDER:
        hero_info = leads[role]
        hero = hero_info["name"]
        expected_type = ROLE_TO_JSON_TYPE[role]

        if hero in profile["heroes"]:
            existing = profile["heroes"][hero]
            existing_type = existing.get("type")
            if existing_type is not None and existing_type != expected_type:
                raise SimulatorError(
                    f"{label}: '{hero}' is configured as {role} "
                    f"({expected_type}) but the JSON says '{existing_type}'."
                )

            skill_levels = existing.get("skill_levels")
            if isinstance(skill_levels, dict) and skill_levels:
                existing["skill_levels"] = {str(k): 5 for k in skill_levels}
            else:
                existing["skill_levels"] = {"1": 5, "2": 5, "3": 5}
        else:
            profile["heroes"][hero] = _default_hero(hero, expected_type)

        widget_level = hero_info["widget_level"]
        if widget_level:
            active_widgets[hero] = int(widget_level)

        selected.append(hero)

    profile["selectedHeroes"] = selected

    special = profile.setdefault("special_bonuses", {})
    if not isinstance(special, dict):
        raise SimulatorError(f"{label}: 'special_bonuses' is not a dictionary.")

    special["widgetLevels"] = active_widgets

    return leads

def _normalise_joiners(names, label: str) -> list[str]:
    """Validate one joiner setting. None means deliberately use no joiners."""
    if names is None:
        # Keep a four-slot representation internally so CSV output and resume
        # signatures line up naturally with the four joiner columns.
        return ["", "", "", ""]

    if not isinstance(names, (list, tuple)) or len(names) != 4:
        raise SimulatorError(
            f"{label}: 'joiners' must be None or contain exactly 4 hero names."
        )

    cleaned = [
        str(raw_name).strip() if raw_name is not None else ""
        for raw_name in names
    ]

    # The all-empty form is the internal/CSV representation of joiners=None.
    if all(not name for name in cleaned):
        return ["", "", "", ""]

    # Mixed empty/named slots are intentionally not supported: the experiment
    # uses either four joiners or no joiners at all.
    for index, name in enumerate(cleaned, start=1):
        if not name:
            raise SimulatorError(
                f"{label}: joiner slot {index} is empty. Use joiners=None to "
                "remove all joiners, or provide exactly 4 hero names."
            )

    return cleaned


def _set_joiners(profile: dict, names, label: str) -> None:
    names = _normalise_joiners(names, label)

    # A None configuration normalizes to four empty slots. Rebuild the JSON
    # array from scratch so baseline joiners cannot leak into the condition.
    profile["joiners"] = [
        {
            "id": index,
            "name": name,
            "skill_levels": {"1": JOINER_SKILL_LEVEL},
        }
        for index, name in enumerate(names)
        if name
    ]


def _normalise_troop_quality(quality, label: str) -> dict[str, dict[str, int]]:
    if not isinstance(quality, dict):
        raise SimulatorError(f"{label}: troop quality must be a dictionary.")
    aliases = {
        "inf": ("inf", "infantry"),
        "lanc": ("lanc", "cav", "cavalry"),
        "mark": ("mark", "arch", "archer", "archers"),
    }
    result = {}
    for unit_type, keys in aliases.items():
        value = next((quality[k] for k in keys if k in quality), None)
        if not isinstance(value, dict):
            raise SimulatorError(f"{label}: missing quality for {unit_type}.")
        tier = value.get("tier")
        tg = value.get("tg_level", value.get("fc_level"))
        if isinstance(tier, bool) or not isinstance(tier, int) or tier < 1:
            raise SimulatorError(f"{label}: {unit_type} tier must be a positive integer.")
        if isinstance(tg, bool) or not isinstance(tg, int) or tg < 0:
            raise SimulatorError(f"{label}: {unit_type} tg_level must be a non-negative integer.")
        result[unit_type] = {"tier": tier, "fc_level": tg}
    return result


def _set_troops(profile: dict, split, total: int, quality, label: str) -> dict[str, int]:
    quantities = _pct_to_raw(split, total)
    quality = _normalise_troop_quality(quality, label)

    troops = profile.get("troops")
    if not isinstance(troops, list):
        raise SimulatorError(f"{label}: JSON has no 'troops' list.")

    rows_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in troops:
        unit_type = row.get("type")
        if unit_type in TROOP_TYPES:
            rows_by_type.setdefault(unit_type, []).append(row)

    for unit_type in TROOP_TYPES:
        rows = rows_by_type.get(unit_type, [])
        if len(rows) != 1:
            raise SimulatorError(
                f"{label}: expected exactly one '{unit_type}' troop row in the baseline JSON, "
                f"found {len(rows)}."
            )
        rows[0]["quantity"] = int(quantities[unit_type])
        rows[0]["tier"] = quality[unit_type]["tier"]
        rows[0]["fc_level"] = quality[unit_type]["fc_level"]

    return quantities


def _set_base_stats(profile: dict, value, label: str) -> None:
    if value is None:
        return

    stats = profile.get("stats")
    if not isinstance(stats, dict):
        raise SimulatorError(f"{label}: JSON has no root-level 'stats' dict.")

    for troop_type in TROOP_TYPES:
        if troop_type not in stats:
            raise SimulatorError(
                f"{label}: root-level stats missing '{troop_type}'."
            )
        for stat_name in STAT_NAMES:
            if stat_name not in stats[troop_type]:
                raise SimulatorError(
                    f"{label}: stats['{troop_type}'] missing '{stat_name}'."
                )
            if isinstance(value, dict):
                row = value.get(troop_type, {})
                if not isinstance(row, dict) or stat_name not in row:
                    raise SimulatorError(f"{label}: configured base stats missing {troop_type}.{stat_name}.")
                stats[troop_type][stat_name] = float(row[stat_name])
            else:
                stats[troop_type][stat_name] = value


def _make_profile(
    baseline: Any,
    leads,
    joiners: list[str] | None,
    troop_split,
    total_troops: int,
    troop_quality,
    base_stat_override,
    label: str,
) -> tuple[Any, dict[str, str], dict[str, int]]:
    profile = copy.deepcopy(baseline)

    # The simulator uses the top-level JSON ``name`` field to determine which
    # battle side a profile belongs to.  File names alone are not sufficient.
    # Force the generated condition profile to the role assigned by this script
    # so that a baseline JSON exported/renamed from the opposite side cannot be
    # imported into the wrong slot.
    if label not in ("attacker", "defender"):
        raise SimulatorError(f"Unknown profile role: {label!r}")
    profile["name"] = label

    lead_config = _set_leads(profile, leads, label)
    _set_joiners(profile, joiners, label)
    quantities = _set_troops(profile, troop_split, total_troops, troop_quality, label)
    _set_base_stats(profile, base_stat_override, label)
    return profile, lead_config, quantities


def _normalise_troop_variants(item: dict[str, Any], label: str) -> list[tuple[tuple[float, float, float], int]]:
    """Validate the troop-variant list used by both attacker and defender.

    The preferred key is ``troop_variants_pct`` on both sides. For backward
    compatibility, a legacy single ``troops_pct`` tuple is also accepted and
    treated as a one-element list.
    """
    if "troop_variants_pct" in item:
        variants = item["troop_variants_pct"]
    elif "troops_pct" in item:
        variants = [item["troops_pct"]]
    else:
        raise SimulatorError(
            f"{label}: define 'troop_variants_pct' as a non-empty list of "
            "(infantry, cavalry, archers) percentage tuples."
        )

    if isinstance(variants, (str, bytes)) or not isinstance(variants, (list, tuple)):
        raise SimulatorError(
            f"{label}: 'troop_variants_pct' must be a non-empty list of troop splits."
        )
    if not variants:
        raise SimulatorError(
            f"{label}: 'troop_variants_pct' cannot be empty."
        )

    groups = item.get("troop_variant_groups", list(range(1, len(variants) + 1)))
    if not isinstance(groups, (list, tuple)) or len(groups) != len(variants):
        raise SimulatorError(f"{label}: troop_variant_groups must match troop_variants_pct.")
    result = []
    for variant_number, split in enumerate(variants, start=1):
        try: group = int(groups[variant_number - 1])
        except Exception as exc: raise SimulatorError(f"{label}: invalid range group for variant {variant_number}.") from exc
        result.append((_validate_split(split, f"{label} troop variant {variant_number}"), group))
    return result


def build_conditions() -> list[dict[str, Any]]:
    conditions = []
    condition_number = 0

    for defense_index, defense in enumerate(DEFENSE_SCENARIOS, start=1):
        if not isinstance(defense, dict):
            raise SimulatorError(
                f"Defense scenario {defense_index} must be a dictionary."
            )
        if "leads" not in defense:
            raise SimulatorError(
                f"Defense scenario {defense_index} is missing 'leads'."
            )

        def_leads = _normalise_leads(
            defense["leads"],
            f"defense scenario {defense_index}",
        )
        def_name = _formation_name(def_leads)
        def_joiners = _normalise_joiners(
            defense.get("joiners"),
            f"{def_name} defender joiners",
        )
        def_variants = _normalise_troop_variants(defense, def_name)
        def_widget_buff_heroes = _formation_widget_heroes(defense, def_leads)

        for attack_index, attack in enumerate(ATTACK_FORMATIONS, start=1):
            if not isinstance(attack, dict):
                raise SimulatorError(
                    f"Attack formation {attack_index} must be a dictionary."
                )
            if "leads" not in attack:
                raise SimulatorError(
                    f"Attack formation {attack_index} is missing 'leads'."
                )

            atk_leads = _normalise_leads(
                attack["leads"],
                f"attack formation {attack_index}",
            )
            atk_name = _formation_name(atk_leads)
            atk_joiners = _normalise_joiners(
                attack.get("joiners"),
                f"{atk_name} attacker joiners",
            )
            atk_variants = _normalise_troop_variants(attack, atk_name)
            atk_widget_buff_heroes = _formation_widget_heroes(attack, atk_leads)

            if ONLY_MATCHUPS is not None and (def_name, atk_name) not in ONLY_MATCHUPS:
                continue

            # Full Cartesian product of troop variants within this hero matchup.
            for def_variant, (def_pct, def_group) in enumerate(def_variants, start=1):
                for atk_variant, (atk_pct, atk_group) in enumerate(atk_variants, start=1):
                    condition_number += 1
                    conditions.append(
                        {
                            "condition": condition_number,
                            "defense_scenario": def_name,
                            "attack_formation": atk_name,
                            "defense_troop_variant": def_variant,
                            "attack_troop_variant": atk_variant,
                            "defense_troop_range": def_group,
                            "attack_troop_range": atk_group,
                            "def_leads": def_leads,
                            "atk_leads": atk_leads,
                            "def_joiners": list(def_joiners),
                            "atk_joiners": list(atk_joiners),
                            "def_widget_buff_heroes": list(def_widget_buff_heroes),
                            "atk_widget_buff_heroes": list(atk_widget_buff_heroes),
                            "def_pct": def_pct,
                            "atk_pct": atk_pct,
                        }
                    )

    return conditions

def _optional_numeric_signature(value: Any) -> str | float:
    """Normalize None/blank vs numeric configuration values for resume matching."""
    if value is None:
        return ""
    if isinstance(value, str) and not value.strip():
        return ""
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try: return json.dumps(json.loads(value), sort_keys=True, separators=(",", ":"))
        except Exception: return value
    return float(value)


def _widget_signature(value: Any) -> int:
    """Normalize None/blank widget levels to the simulator's effective level 0."""
    if value is None:
        return 0
    if isinstance(value, str) and not value.strip():
        return 0
    return int(float(value))


def _condition_signature(c: dict[str, Any]) -> tuple:
    """Return the simulator-relevant identity used by resume mode.

    Intentionally excluded:
    - generated display names (attack_formation / defense_scenario)
    - attack_troop_variant / defense_troop_variant numbers
    - condition number

    Those are labels/order metadata, not changes to the simulated battle.
    """
    return (
        tuple(c["def_joiners"]),
        tuple(c["atk_joiners"]),
        c["def_leads"]["inf"]["name"],
        c["def_leads"]["cav"]["name"],
        c["def_leads"]["arch"]["name"],
        c["atk_leads"]["inf"]["name"],
        c["atk_leads"]["cav"]["name"],
        c["atk_leads"]["arch"]["name"],
        _widget_signature(c["def_leads"]["inf"]["widget_level"]),
        _widget_signature(c["def_leads"]["cav"]["widget_level"]),
        _widget_signature(c["def_leads"]["arch"]["widget_level"]),
        _widget_signature(c["atk_leads"]["inf"]["widget_level"]),
        _widget_signature(c["atk_leads"]["cav"]["widget_level"]),
        _widget_signature(c["atk_leads"]["arch"]["widget_level"]),
        tuple(float(x) for x in c["def_pct"]),
        tuple(float(x) for x in c["atk_pct"]),
        int(DEFENSE_TOTAL_TROOPS),
        int(ATTACK_TOTAL_TROOPS),
        _optional_numeric_signature(DEFENSE_BASE_STAT_OVERRIDE),
        _optional_numeric_signature(ATTACK_BASE_STAT_OVERRIDE),
    )


def _row_signature(row: dict[str, str]) -> tuple:
    """Build the same simulator-relevant resume identity from one CSV row."""
    return (
        (
            row["joiner1_def"].strip(),
            row["joiner2_def"].strip(),
            row["joiner3_def"].strip(),
            row["joiner4_def"].strip(),
        ),
        (
            row["joiner1_atk"].strip(),
            row["joiner2_atk"].strip(),
            row["joiner3_atk"].strip(),
            row["joiner4_atk"].strip(),
        ),
        row["lead_inf_def"].strip(),
        row["lead_cav_def"].strip(),
        row["lead_arch_def"].strip(),
        row["lead_inf_atk"].strip(),
        row["lead_cav_atk"].strip(),
        row["lead_arch_atk"].strip(),
        _widget_signature(row.get("widget_inf_def", "")),
        _widget_signature(row.get("widget_cav_def", "")),
        _widget_signature(row.get("widget_arch_def", "")),
        _widget_signature(row.get("widget_inf_atk", "")),
        _widget_signature(row.get("widget_cav_atk", "")),
        _widget_signature(row.get("widget_arch_atk", "")),
        (
            float(row["infantry_pct_def"]),
            float(row["cavalry_pct_def"]),
            float(row["archers_pct_def"]),
        ),
        (
            float(row["infantry_pct_atk"]),
            float(row["cavalry_pct_atk"]),
            float(row["archers_pct_atk"]),
        ),
        int(float(row["defender_total_troops"])),
        int(float(row["attacker_total_troops"])),
        _optional_numeric_signature(row.get("base_stat_override_def", "")),
        _optional_numeric_signature(row.get("base_stat_override_atk", "")),
    )

def _prepare_csv() -> dict[tuple, list[int]]:
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    if (
        not RESUME_FROM_CSV
        or not OUTPUT_CSV.exists()
        or OUTPUT_CSV.stat().st_size == 0
    ):
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=CSV_COLUMNS).writeheader()
        return {}

    completed: dict[tuple, list[int]] = {}

    with OUTPUT_CSV.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SimulatorError(f"Resume CSV has no header: {OUTPUT_CSV}")

        fieldnames = list(reader.fieldnames)
        missing = [c for c in CSV_COLUMNS if c not in fieldnames]

        # Backward-compatible migrations:
        # 1) older files may lack defense_troop_variant;
        # 2) older files may have one shared base_stat_override column instead
        #    of the new side-specific columns.
        migratable = set(missing).issubset(
            {
                "defense_troop_variant",
                "base_stat_override_atk",
                "base_stat_override_def",
                "defense_troop_range",
                "attack_troop_range",
            }
        )
        has_legacy_base_stat = "base_stat_override" in fieldnames
        needs_split_base_stats = (
            "base_stat_override_atk" in missing
            or "base_stat_override_def" in missing
        )

        if missing and (not migratable or (needs_split_base_stats and not has_legacy_base_stat)):
            raise SimulatorError(
                f"Resume CSV is missing columns {missing}: {OUTPUT_CSV}"
            )
        rows = list(reader)

    migrated = False
    migration_messages = []

    # Older versions had only one troop split per defense scenario and therefore
    # no explicit defense_troop_variant column. Every existing row is variant 1.
    if "defense_troop_variant" in missing:
        for row in rows:
            row["defense_troop_variant"] = "1"
        migrated = True
        migration_messages.append("added defense_troop_variant=1")

    for column, fallback in (("defense_troop_range", "defense_troop_variant"), ("attack_troop_range", "attack_troop_variant")):
        if column in missing:
            for row in rows: row[column] = row.get(fallback, "1")
            migrated = True
            migration_messages.append(f"added {column}")

    # Older versions used one shared override for both sides. Preserve that
    # behavior by copying it into both new side-specific columns.
    if needs_split_base_stats:
        for row in rows:
            legacy_value = row.get("base_stat_override", "")
            row["base_stat_override_atk"] = legacy_value
            row["base_stat_override_def"] = legacy_value
        migrated = True
        migration_messages.append(
            "split legacy base_stat_override into attacker and defender columns"
        )

    if migrated:
        with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(
            "Resume migration: " + "; ".join(migration_messages) + ".",
            flush=True,
        )

    for row_number, row in enumerate(rows, start=2):
        if not any(str(v or "").strip() for v in row.values()):
            continue
        try:
            sig = _row_signature(row)
            batch = int(row["batch"])
            winrate = float(row["winrate"])
        except Exception as exc:
            raise SimulatorError(
                f"Invalid resume CSV row {row_number}: {exc}"
            ) from exc

        if not 0 <= winrate <= 100:
            raise SimulatorError(
                f"Invalid winrate on resume row {row_number}: {winrate}"
            )
        completed.setdefault(sig, []).append(batch)

    duplicate_labels = sum(
        len(batches) - len(set(batches))
        for batches in completed.values()
    )
    if duplicate_labels:
        print(
            f"Resume warning: found {duplicate_labels} duplicate batch label(s). "
            "All valid CSV rows still count as completed simulator batches; "
            "batch numbers are used only to choose labels for any new rows.",
            flush=True,
        )

    return completed


def _append_csv(row: dict[str, Any]) -> None:
    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writerow(row)


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
    if not build_conditions():
        raise SimulatorError("No lead/troop conditions were configured.")
    atk_source = _load_json(ATTACKER_JSON)
    def_source = _load_json(DEFENDER_JSON)
    atk_effective = False if HERO_PROGRESSION_CONFIG_ATK.get("enabled", False) else bool(atk_source.get("stats_include_heroes", False))
    def_effective = False if HERO_PROGRESSION_CONFIG_DEF.get("enabled", False) else bool(def_source.get("stats_include_heroes", False))
    print(
        "Profile progression: "
        f"ATK[configured={bool(HERO_PROGRESSION_CONFIG_ATK.get('enabled', False))}]; "
        f"DEF[configured={bool(HERO_PROGRESSION_CONFIG_DEF.get('enabled', False))}]; "
        f"special bonuses ATK/DEF={APPLY_SPECIAL_BONUSES_ATK}/{APPLY_SPECIAL_BONUSES_DEF}; "
        f"active widget buffs ATK/DEF={APPLY_WIDGET_BUFFS_ATK}/{APPLY_WIDGET_BUFFS_DEF}.",
        flush=True,
    )
    return atk_effective, def_effective
async def run_experiment() -> None:
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
        f"Profile mapping: attacker <- Player A ({ATTACKER_JSON.name}); "
        f"defender <- Player B ({DEFENDER_JSON.name})",
        flush=True,
    )

    first_atk_info, first_def_info = _save_first_prepared_profiles(
        conditions,
        attacker_baseline,
        defender_baseline,
        effective_stats_include_heroes_atk,
        effective_stats_include_heroes_def,
    )
    _augment_machine_settings(settings_path, first_atk_info, first_def_info)
    readable_settings = _write_readable_settings(
        conditions,
        effective_stats_include_heroes_atk,
        effective_stats_include_heroes_def,
        first_atk_info,
        first_def_info,
    )
    _copy_plotter_scripts()
    print(f"Readable experiment settings saved to: {readable_settings}", flush=True)
    print(f"First prepared attacker JSON saved to: {FIRST_ATTACKER_JSON}", flush=True)
    print(f"First prepared defender JSON saved to: {FIRST_DEFENDER_JSON}", flush=True)
    print("Effective troop stats (root + selected lead hero stats; active widget skill excluded):", flush=True)
    print(f"  Attacker: {first_atk_info.get('effective_troop_stats_text', '(unavailable)')}", flush=True)
    print(f"  Defender: {first_def_info.get('effective_troop_stats_text', '(unavailable)')}", flush=True)

    # Automatic widget role labels for self-documenting CSV output. These are
    # informational only; the shared client independently decides which widgets
    # actually activate for attacker vs defender.
    widget_type_by_hero: dict[str, str] = {}
    if HERO_STATS_LOOKUP.is_file():
        unique_leads = sorted({
            info["name"]
            for c in conditions
            for side_key in ("atk_leads", "def_leads")
            for info in c[side_key].values()
        })
        for hero_name in unique_leads:
            widget_type_by_hero[hero_name] = (
                get_hero_widget_type(hero_name, HERO_STATS_LOOKUP) or ""
            )

    # Validate every condition offline before Chromium is launched.
    for c in conditions:
        _make_profile(
            attacker_baseline,
            c["atk_leads"],
            c["atk_joiners"],
            c["atk_pct"],
            ATTACK_TOTAL_TROOPS,
            ATTACK_TROOP_QUALITY,
            ATTACK_BASE_STAT_OVERRIDE,
            "attacker",
        )
        _make_profile(
            defender_baseline,
            c["def_leads"],
            c["def_joiners"],
            c["def_pct"],
            DEFENSE_TOTAL_TROOPS,
            DEFENSE_TROOP_QUALITY,
            DEFENSE_BASE_STAT_OVERRIDE,
            "defender",
        )

    print(
        f"Preparing {len(conditions)} unique condition(s), "
        f"{BATCHES_PER_CONDITION} batch(es) per condition, "
        f"{SIMULATIONS_PER_BATCH} simulations per batch.",
        flush=True,
    )
    print(
        f"Total requested simulator batches: "
        f"{len(conditions) * BATCHES_PER_CONDITION:,}",
        flush=True,
    )

    completed = _prepare_csv()
    plan = []
    already_done = 0
    extra_existing = 0

    for c in conditions:
        sig = _condition_signature(c)
        existing_batches = list(completed.get(sig, []))
        existing_count = len(existing_batches)

        # BATCHES_PER_CONDITION is a target TOTAL, not an amount to add.
        already_done += min(existing_count, BATCHES_PER_CONDITION)
        extra_existing += max(0, existing_count - BATCHES_PER_CONDITION)
        needed = max(0, BATCHES_PER_CONDITION - existing_count)

        if needed:
            # Prefer unused positive batch labels. Existing duplicate labels do not
            # cause extra simulations: every valid row counts toward the target.
            used_labels = set(existing_batches)
            new_batch_labels = []
            candidate = 1
            while len(new_batch_labels) < needed:
                if candidate not in used_labels:
                    new_batch_labels.append(candidate)
                    used_labels.add(candidate)
                candidate += 1
            plan.append((c, new_batch_labels))

    total_target = len(conditions) * BATCHES_PER_CONDITION
    print(
        f"Resume scan: {already_done}/{total_target} target batch(es) already "
        f"complete; {total_target - already_done} remain.",
        flush=True,
    )
    if extra_existing:
        print(
            f"Resume note: the CSV already contains {extra_existing} batch(es) "
            "beyond the current target. They are kept, and no replacement "
            "batches are added for them.",
            flush=True,
        )

    if not plan:
        print(f"Nothing to run. Results already complete: {OUTPUT_CSV}", flush=True)
        return

    with tempfile.TemporaryDirectory(prefix="kingshot_lead_troop_") as tmp:
        tmpdir = Path(tmp)

        async with KingshotSimulatorSession(
            headless=HEADLESS,
            timeout_seconds=TIMEOUT_SECONDS,
        ) as simulator:

            for c, missing_batches in plan:
                condition_number = int(c["condition"])

                attacker_profile, atk_leads, atk_q = _make_profile(
                    attacker_baseline,
                    c["atk_leads"],
                    c["atk_joiners"],
                    c["atk_pct"],
                    ATTACK_TOTAL_TROOPS,
                    ATTACK_TROOP_QUALITY,
                    ATTACK_BASE_STAT_OVERRIDE,
                    "attacker",
                )
                defender_profile, def_leads, def_q = _make_profile(
                    defender_baseline,
                    c["def_leads"],
                    c["def_joiners"],
                    c["def_pct"],
                    DEFENSE_TOTAL_TROOPS,
                    DEFENSE_TROOP_QUALITY,
                    DEFENSE_BASE_STAT_OVERRIDE,
                    "defender",
                )

                attacker_path = tmpdir / "attacker_condition.json"
                defender_path = tmpdir / "defender_condition.json"
                _write_json(attacker_path, attacker_profile)
                _write_json(defender_path, defender_profile)

                print(
                    f"Condition {condition_number}/{len(conditions)}: "
                    f"{c['attack_formation']} vs {c['defense_scenario']}; "
                    f"attacker troop variant {c['attack_troop_variant']}={c['atk_pct']}, "
                    f"defender troop variant {c['defense_troop_variant']}={c['def_pct']}; "
                    f"attacker joiners={('/'.join(x for x in c['atk_joiners'] if x) or '(none)')}, "
                    f"defender joiners={('/'.join(x for x in c['def_joiners'] if x) or '(none)')}; "
                    f"missing batches={len(missing_batches)}",
                    flush=True,
                )

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
                    active_widget_heroes_attacker=c.get("atk_widget_buff_heroes", []),
                    active_widget_heroes_defender=c.get("def_widget_buff_heroes", []),
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
                        experiment="lead_troop",
                        attacker_info=atk_runtime_info,
                        defender_info=def_runtime_info,
                        context={
                            "condition": condition_number,
                            "batch": batch,
                            "attack_formation": c["attack_formation"],
                            "defense_scenario": c["defense_scenario"],
                            "attack_troop_variant": c["attack_troop_variant"],
                            "defense_troop_variant": c["defense_troop_variant"],
                        },
                    )

                    row = {
                        "condition": condition_number,
                        "batch": batch,
                        "defense_scenario": c["defense_scenario"],
                        "attack_formation": c["attack_formation"],
                        "defense_troop_variant": c["defense_troop_variant"],
                        "attack_troop_variant": c["attack_troop_variant"],
                        "defense_troop_range": c["defense_troop_range"],
                        "attack_troop_range": c["attack_troop_range"],

                        "joiner1_atk": c["atk_joiners"][0],
                        "joiner2_atk": c["atk_joiners"][1],
                        "joiner3_atk": c["atk_joiners"][2],
                        "joiner4_atk": c["atk_joiners"][3],
                        "joiner1_def": c["def_joiners"][0],
                        "joiner2_def": c["def_joiners"][1],
                        "joiner3_def": c["def_joiners"][2],
                        "joiner4_def": c["def_joiners"][3],

                        "lead_inf_atk": atk_leads["inf"]["name"],
                        "lead_cav_atk": atk_leads["cav"]["name"],
                        "lead_arch_atk": atk_leads["arch"]["name"],
                        "lead_inf_def": def_leads["inf"]["name"],
                        "lead_cav_def": def_leads["cav"]["name"],
                        "lead_arch_def": def_leads["arch"]["name"],

                        "widget_inf_atk": atk_leads["inf"]["widget_level"] or 0,
                        "widget_cav_atk": atk_leads["cav"]["widget_level"] or 0,
                        "widget_arch_atk": atk_leads["arch"]["widget_level"] or 0,
                        "widget_inf_def": def_leads["inf"]["widget_level"] or 0,
                        "widget_cav_def": def_leads["cav"]["widget_level"] or 0,
                        "widget_arch_def": def_leads["arch"]["widget_level"] or 0,

                        "widget_kind_inf_atk": widget_type_by_hero.get(atk_leads["inf"]["name"], ""),
                        "widget_kind_cav_atk": widget_type_by_hero.get(atk_leads["cav"]["name"], ""),
                        "widget_kind_arch_atk": widget_type_by_hero.get(atk_leads["arch"]["name"], ""),
                        "widget_kind_inf_def": widget_type_by_hero.get(def_leads["inf"]["name"], ""),
                        "widget_kind_cav_def": widget_type_by_hero.get(def_leads["cav"]["name"], ""),
                        "widget_kind_arch_def": widget_type_by_hero.get(def_leads["arch"]["name"], ""),

                        "infantry_pct_atk": c["atk_pct"][0],
                        "cavalry_pct_atk": c["atk_pct"][1],
                        "archers_pct_atk": c["atk_pct"][2],
                        "infantry_pct_def": c["def_pct"][0],
                        "cavalry_pct_def": c["def_pct"][1],
                        "archers_pct_def": c["def_pct"][2],

                        "infantry_atk": atk_q["inf"],
                        "cavalry_atk": atk_q["lanc"],
                        "archers_atk": atk_q["mark"],
                        "infantry_def": def_q["inf"],
                        "cavalry_def": def_q["lanc"],
                        "archers_def": def_q["mark"],

                        "attacker_total_troops": ATTACK_TOTAL_TROOPS,
                        "defender_total_troops": DEFENSE_TOTAL_TROOPS,
                        "base_stat_override_atk": json.dumps(ATTACK_BASE_STAT_OVERRIDE, sort_keys=True, separators=(",", ":")),
                        "base_stat_override_def": json.dumps(DEFENSE_BASE_STAT_OVERRIDE, sort_keys=True, separators=(",", ":")),
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
    conditions = build_conditions()
    print("Configuration is valid.", flush=True)
    print(f"Attacker profile: {ATTACKER_PROFILE} -> {ATTACKER_JSON.name}", flush=True)
    print(f"Defender profile: {DEFENDER_PROFILE} -> {DEFENDER_JSON.name}", flush=True)
    print(f"Unique lead/troop conditions: {len(conditions):,}", flush=True)
    print(f"Target simulator batches: {len(conditions) * BATCHES_PER_CONDITION:,}", flush=True)
    print(f"Simulations per batch: {SIMULATIONS_PER_BATCH:,}", flush=True)


if __name__ == "__main__":
    import sys
    if "--preview" in sys.argv:
        print("Checking Kingshot lead/troop configuration...", flush=True)
        preview_experiment()
    else:
        print("Running Kingshot lead/troop experiment...", flush=True)
        run_spyder_compatible(run_experiment)
