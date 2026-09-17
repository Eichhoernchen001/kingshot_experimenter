#!/usr/bin/env python3
"""Shared JSON configuration helpers for the Kingshot desktop UI and scripts."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from kingshot_progression import (
    GEAR_SLOTS,
    ROLES,
    default_all_gear,
    load_progression_lookup,
    profile_hero_config,
    validate_gear_piece,
)

CONFIG_FILENAME = "kingshot_config.json"
SCHEMA_VERSION = 13
STAT_NAMES = ("attack", "defense", "lethality", "health")
JSON_TYPES = {"inf": "inf", "cav": "lanc", "arch": "mark"}
PET_KEYS = (
    "alpha-black-panther", "giant-rhino", "regal-white-lion",
    "ironclad-war-elephant", "ironclad-war-bear", "grizzly-bear", "moose",
)
CITY_KEYS = ("lethality", "attack", "defense", "health", "enemyAttack", "enemyDefense")


def _quality() -> dict[str, dict[str, int]]:
    return {role: {"tier": 11, "tg_level": 8} for role in ROLES}


def _zero_stats() -> dict[str, dict[str, float]]:
    return {kind: {stat: 0.0 for stat in STAT_NAMES} for kind in ("inf", "lanc", "mark")}


def _special(*, maxed: bool = False) -> dict[str, Any]:
    return {
        "petLevels": {key: 10 if maxed else 0 for key in PET_KEYS},
        "city": {key: 2 if maxed else 0 for key in CITY_KEYS},
        "appointment": {"kingdom": 0, "power": 0},
    }


def _profile(player_file: str, *, configured: bool, maxed: bool) -> dict[str, Any]:
    return {
        "player_file": player_file,
        "name": "Player A" if "playerA" in player_file else "Player B",
        "base_stats": _zero_stats(),
        "special_bonuses": _special(maxed=maxed),
        "hero_progression": {
            "enabled": configured,
            "stars_enabled": configured,
            "widgets_enabled": configured,
            "stars_source": "manual",
            "defaults": {"star_step": 30, "widget_level": 10},
            "heroes": {},
        },
        "gear_enabled": configured,
        "special_bonuses_enabled": True,
        "hero_gear": default_all_gear(maxed=maxed) if configured else default_all_gear(maxed=False),
    }


def default_config() -> dict[str, Any]:
    """Fresh configuration matching the bundled example experiment."""
    return {
        "schema_version": SCHEMA_VERSION,
        "profiles": {
            "A": _profile("json/playerA_data.json", configured=True, maxed=True),
            # B defaults to its imported JSON hero values so the supplied baseline
            # remains usable until the user chooses explicit progression settings.
            "B": _profile("json/playerB_data.json", configured=True, maxed=False),
        },
        "assignment": {"attacker": "A", "defender": "B"},
        "lookups": {
            "hero_stats": "json/kingshot_hero_data.json",
            "hero_progression": "json/kingshot_hero_data.json",
        },
        "battle_options": {},
        "run": {
            "simulations_per_batch": 100,
            "batches_per_condition": 1,
            "resume": False,
            "headless": True,
            "timeout_seconds": 30,
            "delay_between_batches_seconds": 0.5,
        },
        "battle_setup": {
            "attacker": {
                "total_troops": 1_600_000,
                "troop_quality": _quality(),
            },
            "defender": {
                "total_troops": 1_600_000,
                "troop_quality": _quality(),
            },
        },
        "lead_troop": {
            "attacker": {
                "formations": [
                    {
                        "joiners": ["Yang", "Triton", "Gordon", "Fahd"],
                        "leads": {"inf": "Triton", "cav": "Thrud", "arch": "Marlin"},
                        "widget_buffs": {"inf": True, "cav": True, "arch": True},
                        "troop_variants_pct": [[70,30,0],[60,40,0],[50,50,0],[40,60,0],[30,70,0]],
                        "troop_variant_groups": [1,1,1,1,1],
                        "troop_range_lines": ["70:30/30:70/0 - 10"],
                    },
                    {
                        "joiners": ["Vivian", "Thrud", "Amane", "Chenko"],
                        "leads": {"inf": "Triton", "cav": "Petra", "arch": "Yang"},
                        "widget_buffs": {"inf": True, "cav": True, "arch": True},
                        "troop_variants_pct": [
                            [20,5,75],[30,5,65],[40,5,55],[50,5,45],[60,5,35],
                            [20,10,70],[30,10,60],[40,10,50],[50,10,40],[60,10,30],
                            [20,15,65],[30,15,55],[40,15,45],[50,15,35],[60,15,25],
                        ],
                        "troop_variant_groups": [1,1,1,1,1,2,2,2,2,2,3,3,3,3,3],
                        "troop_range_lines": [
                            "20:60/5/75:35 - 10",
                            "20:60/10/70:30 - 10",
                            "20:60/15/65:25 - 10",
                        ],
                    },
                ],
            },
            "defender": {
                "formations": [{
                    "joiners": None,
                    "leads": {"inf": "Triton", "cav": "Petra", "arch": "Yang"},
                    "widget_buffs": {"inf": True, "cav": True, "arch": True},
                    "troop_variants_pct": [[50,10,40]],
                    "troop_variant_groups": [1],
                    "troop_range_lines": ["50/10/40"],
                }],
            },
            "only_matchups": None,
        },
        "joiner": {
            "attacker": {
                "leads": {"inf": "Triton", "cav": "Thrud", "arch": "Marlin"},
                "widget_buffs": {"inf": True, "cav": True, "arch": True},
                "troop_percentages": [50,50,0],
                "pools": {"4": [], "3": [], "2": [], "1": []},
                "manual_slots": [["Hilde"],["Thrud"],["Amadeus"],["Triton"]],
            },
            "defender": {
                "leads": {"inf": "Triton", "cav": "Sophia", "arch": "Vivian"},
                "widget_buffs": {"inf": True, "cav": True, "arch": True},
                "troop_percentages": [60,10,30],
                "pools": {"4": [], "3": [], "2": [], "1": ["Yang","Thrud","Saul","Fahd","Howard","Chenko","Amane","Gordon","Triton","Hilde","Vivian","Petra"]},
                "manual_slots": [[],[],[],[]],
            },
        },
        "plotter": {"plot_by": "defender", "show_50_percent_reference": False, "show_mean_labels": False},
        "analysis": {
            "side": "auto",
            "selection_method": "raw",
            "synergy_threshold": 0.05,
            "pair_selection_method": "raw",
            "pair_synergy_threshold": 0.05,
            "triple_selection_method": "raw",
            "triple_synergy_threshold": 0.05,
            "pair_synergy": True,
            "triple_synergy": True,
            "top_n": 10,
        },
    }


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        result = copy.deepcopy(base)
        for key, value in override.items():
            result[key] = _deep_merge(result[key], value) if key in result else copy.deepcopy(value)
        return result
    return copy.deepcopy(override)


def _migrate_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """Best-effort migration from the first GUI schema without discarding designs."""
    if int(raw.get("schema_version", 1)) >= 2:
        return raw
    result = default_config()
    for key in ("assignment", "plotter", "analysis"):
        if isinstance(raw.get(key), dict):
            result[key] = _deep_merge(result[key], raw[key])
    old_profiles = raw.get("profiles", {})
    old_battle = raw.get("battle_options", {})
    for letter in ("A", "B"):
        if isinstance(old_profiles.get(letter), dict):
            result["profiles"][letter]["player_file"] = old_profiles[letter].get("player_file", result["profiles"][letter]["player_file"])
    # The old options were role-specific. Carry the assigned profile's intent to
    # the profile-centric progression model.
    assignment = result["assignment"]
    for side in ("attacker", "defender"):
        old_side = old_battle.get(side, {}) if isinstance(old_battle, dict) else {}
        letter = assignment.get(side, "A" if side == "attacker" else "B")
        if letter in result["profiles"]:
            enabled = bool(old_side.get("add_5star_hero_stats", False) or old_side.get("add_hero_gear", False))
            result["profiles"][letter]["hero_progression"]["enabled"] = enabled
            result["profiles"][letter]["hero_progression"]["defaults"] = {"star_step": 30, "widget_level": 10 if enabled else 0}
            result["profiles"][letter]["hero_gear"] = default_all_gear(maxed=bool(old_side.get("add_hero_gear", False))) if enabled else default_all_gear()
    result["battle_options"]["apply_widget_buffs"] = bool(old_battle.get("apply_special_bonuses", True)) if isinstance(old_battle, dict) else True

    old_lt = raw.get("lead_troop", {})
    if isinstance(old_lt, dict):
        if isinstance(old_lt.get("run"), dict): result["run"] = copy.deepcopy(old_lt["run"])
        for side, coll in (("attacker","formations"),("defender","scenarios")):
            old_side = old_lt.get(side, {})
            if isinstance(old_side, dict):
                for shared in ("total_troops","troop_quality","base_stat_override"):
                    if shared in old_side: result["battle_setup"][side][shared] = copy.deepcopy(old_side[shared])
                if coll in old_side:
                    target_coll = "formations"
                    result["lead_troop"][side][target_coll] = copy.deepcopy(old_side[coll])
        result["lead_troop"]["only_matchups"] = copy.deepcopy(old_lt.get("only_matchups"))
    old_join = raw.get("joiner", {})
    if isinstance(old_join, dict):
        if isinstance(old_join.get("run"), dict) and not isinstance(old_lt.get("run"), dict): result["run"] = copy.deepcopy(old_join["run"])
        for side in ("attacker","defender"):
            old_side = old_join.get(side, {})
            if isinstance(old_side, dict):
                for key in ("leads","troop_percentages","pools","manual_slots"):
                    if key in old_side: result["joiner"][side][key] = copy.deepcopy(old_side[key])
    result["schema_version"] = SCHEMA_VERSION
    return result


def _json_profile_values(script_folder: Path, player_file: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return root stats and supported specials from a player export, if readable."""
    try:
        path = Path(player_file)
        if not path.is_absolute():
            path = Path(script_folder) / path
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _zero_stats(), _special(maxed=False)
    raw_stats = data.get("stats", {}) if isinstance(data, dict) else {}
    stats = _zero_stats()
    for kind in stats:
        row = raw_stats.get(kind, {}) if isinstance(raw_stats, dict) else {}
        for stat in STAT_NAMES:
            try: stats[kind][stat] = float(row.get(stat, 0.0))
            except Exception: stats[kind][stat] = 0.0
    raw_special = data.get("special_bonuses", {}) if isinstance(data, dict) else {}
    special = _special(maxed=False)
    for section in ("petLevels", "city"):
        values = raw_special.get(section, {}) if isinstance(raw_special, dict) else {}
        if isinstance(values, dict):
            for key in special[section]:
                try: special[section][key] = float(values.get(key, 0) or 0)
                except Exception: special[section][key] = 0
    return stats, special


def _relocate_bundled_json_paths(root: Path, cfg: dict[str, Any]) -> None:
    """Accept older flat-folder settings without redirecting custom files."""
    bundled = {
        "playerA_data.json", "playerB_data.json",
        "kingshot_hero_data.json", "kingshot_hero_gear.json",
    }

    def relocate(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        path = Path(value.replace("\\", "/"))
        if path.is_absolute():
            return value
        if len(path.parts) == 1 and path.name in bundled:
            destination = root / "json" / path.name
            if not (root / path).is_file() and destination.is_file():
                return "json/" + path.name
        if len(path.parts) == 2 and path.parts[0] == "json" and path.name in bundled:
            return path.as_posix()
        return value

    for profile in cfg.get("profiles", {}).values():
        if isinstance(profile, dict) and "player_file" in profile:
            profile["player_file"] = relocate(profile["player_file"])
    for key, value in cfg.get("lookups", {}).items():
        cfg["lookups"][key] = relocate(value)


def _migrate_to_current(script_folder: Path, raw: dict[str, Any]) -> dict[str, Any]:
    old = _migrate_v1(raw)
    result = _deep_merge(default_config(), old)
    plotter = result['plotter']
    plotter['winrate_side'] = plotter.get('winrate_side', 'defender' if plotter.get('plot_by') == 'attacker' else 'attacker')
    plotter['plot_by'] = 'defender' if plotter['winrate_side'] == 'attacker' else 'attacker'
    result["assignment"] = {"attacker": "A", "defender": "B"}
    lookups = result.setdefault("lookups", {})
    if Path(str(lookups.get("hero_stats", ""))).name == "kingshot_hero_stats_5star.json":
        lookups["hero_stats"] = "kingshot_hero_data.json"
    if Path(str(lookups.get("hero_progression", ""))).name == "kingshot_hero_progression.json":
        lookups["hero_progression"] = "kingshot_hero_data.json"

    _relocate_bundled_json_paths(Path(script_folder), result)

    # One run configuration now governs both experiment types.
    if isinstance(old.get("run"), dict):
        result["run"] = _deep_merge(default_config()["run"], old["run"])
    else:
        for section in ("lead_troop", "joiner"):
            candidate = old.get(section, {}).get("run") if isinstance(old.get(section), dict) else None
            if isinstance(candidate, dict):
                result["run"] = _deep_merge(default_config()["run"], candidate); break
    result.get("lead_troop", {}).pop("run", None)
    result.get("joiner", {}).pop("run", None)

    old_profiles = old.get("profiles", {}) if isinstance(old.get("profiles"), dict) else {}
    old_setup = old.get("battle_setup", {}) if isinstance(old.get("battle_setup"), dict) else {}
    old_special_switch = bool(old.get("battle_options", {}).get("apply_special_bonuses", True))
    for letter, side in (("A", "attacker"), ("B", "defender")):
        profile = result["profiles"][letter]
        old_profile = old_profiles.get(letter, {}) if isinstance(old_profiles.get(letter), dict) else {}
        json_stats, json_special = (_json_profile_values(script_folder, profile["player_file"])
                                    if "base_stats" not in old_profile or "special_bonuses" not in old_profile
                                    else ({}, {}))
        if "base_stats" not in old_profile:
            override = old_setup.get(side, {}).get("base_stat_override") if isinstance(old_setup.get(side), dict) else None
            profile["base_stats"] = (
                {kind: {stat: float(override) for stat in STAT_NAMES} for kind in ("inf", "lanc", "mark")}
                if override is not None else json_stats
            )
        if "special_bonuses" not in old_profile:
            special_file = old_profile.get("special_file")
            loaded_special = None
            if special_file:
                try:
                    spath = Path(special_file)
                    if not spath.is_absolute(): spath = Path(script_folder) / spath
                    loaded_special = json.loads(spath.read_text(encoding="utf-8"))
                except Exception: loaded_special = None
            profile["special_bonuses"] = copy.deepcopy(loaded_special if isinstance(loaded_special, dict) else json_special)
            if not old_special_switch:
                profile["special_bonuses"] = _special(maxed=False)
        special = profile.setdefault("special_bonuses", _special(maxed=False))
        special["appointment"] = {"kingdom": 0, "power": 0}
        prog = profile.setdefault("hero_progression", {})
        prior_enabled = bool(prog.get("enabled", False))
        prog.setdefault("stars_enabled", prior_enabled)
        prog.setdefault("widgets_enabled", prior_enabled)
        prog.setdefault("stars_source", "manual")
        profile.setdefault("gear_enabled", prior_enabled)
        profile.setdefault("special_bonuses_enabled", old_special_switch)
        profile.pop("special_file", None)

    lead = result["lead_troop"]
    defender = lead.setdefault("defender", {})
    old_defender = old.get("lead_troop", {}).get("defender", {}) if isinstance(old.get("lead_troop"), dict) else {}
    if isinstance(old_defender, dict) and "formations" not in old_defender and "scenarios" in old_defender:
        defender["formations"] = copy.deepcopy(old_defender["scenarios"])
    defender.pop("scenarios", None)
    for side in ("attacker", "defender"):
        for item in lead.get(side, {}).get("formations", []):
            variants = item.get("troop_variants_pct", [])
            groups = item.get("troop_variant_groups")
            if not isinstance(groups, list) or len(groups) != len(variants):
                item["troop_variant_groups"] = [i // 10 + 1 for i in range(len(variants))]
            lines = item.get("troop_range_lines")
            if not isinstance(lines, list) or not lines:
                item["troop_range_lines"] = ["/".join(f"{float(v):g}" for v in split) for split in variants]
            toggles = item.get("widget_buffs")
            if not isinstance(toggles, dict):
                toggles = {}
            item["widget_buffs"] = {role: bool(toggles.get(role, True)) for role in ROLES}
    # v7 manual joiner slots are single-choice dropdowns. Preserve the first
    # legacy alternative in each slot and discard additional alternatives.
    for side in ("attacker", "defender"):
        side_cfg = result.get("joiner", {}).get(side, {})
        slots = side_cfg.get("manual_slots", []) if isinstance(side_cfg, dict) else []
        if isinstance(slots, list):
            normalized = []
            for slot in slots[:4]:
                if isinstance(slot, list) and slot:
                    normalized.append([slot[0]])
                else:
                    normalized.append([])
            while len(normalized) < 4:
                normalized.append([])
            side_cfg["manual_slots"] = normalized
        toggles = side_cfg.get("widget_buffs", {}) if isinstance(side_cfg, dict) else {}
        if not isinstance(toggles, dict):
            toggles = {}
        if isinstance(side_cfg, dict):
            side_cfg["widget_buffs"] = {role: bool(toggles.get(role, True)) for role in ROLES}

    # v8: active widget skills are formation-specific on tabs 2 and 3. Remove
    # the old profile-wide gate so legacy saved files cannot silently override
    # those formation controls. Also migrate the old analysis alpha field.
    for profile in result.get("profiles", {}).values():
        if isinstance(profile, dict):
            profile.pop("widget_buffs_enabled", None)
    if isinstance(result.get("battle_options"), dict):
        result["battle_options"].pop("apply_widget_buffs", None)
    raw_analysis = raw.get("analysis", {}) if isinstance(raw, dict) else {}
    analysis = result.setdefault("analysis", {})
    if isinstance(raw_analysis, dict) and "synergy_threshold" not in raw_analysis and "alpha" in raw_analysis:
        analysis["synergy_threshold"] = raw_analysis.get("alpha", 0.05)
    analysis.pop("alpha", None)
    analysis.setdefault("pair_synergy", True)
    analysis.setdefault("triple_synergy", True)
    # v9: pair and triple interaction layers can use different p-value methods
    # and thresholds. Existing common settings seed both independent controls.
    legacy_method = analysis.get("selection_method", "raw")
    legacy_threshold = analysis.get("synergy_threshold", 0.05)
    raw_analysis_dict = raw_analysis if isinstance(raw_analysis, dict) else {}
    if "pair_selection_method" not in raw_analysis_dict:
        analysis["pair_selection_method"] = legacy_method
    if "pair_synergy_threshold" not in raw_analysis_dict:
        analysis["pair_synergy_threshold"] = legacy_threshold
    if "triple_selection_method" not in raw_analysis_dict:
        analysis["triple_selection_method"] = legacy_method
    if "triple_synergy_threshold" not in raw_analysis_dict:
        analysis["triple_synergy_threshold"] = legacy_threshold
    # Retain the old common fields as pair aliases for backwards compatibility.
    analysis["selection_method"] = analysis.get("pair_selection_method", legacy_method)
    analysis["synergy_threshold"] = analysis.get("pair_synergy_threshold", legacy_threshold)
    if not analysis.get("pair_synergy", True):
        analysis["triple_synergy"] = False

    from kingshot_profile_template import detach_config
    detach_config(result, script_folder, read_legacy=int(raw.get("schema_version", 1)) < 10)
    from kingshot_sequences import ensure_sequences
    ensure_sequences(result)
    from kingshot_players import migrate_players
    migrate_players(result)
    result["schema_version"] = SCHEMA_VERSION
    return result


def config_path(script_folder: Path) -> Path:
    return Path(script_folder) / CONFIG_FILENAME


def load_config(script_folder: Path, *, create_if_missing: bool = False) -> dict[str, Any]:
    path = config_path(script_folder)
    defaults = default_config()
    if not path.is_file():
        for letter in ("A", "B"):
            stats, special = _json_profile_values(Path(script_folder), defaults["profiles"][letter]["player_file"])
            defaults["profiles"][letter]["base_stats"] = stats
            defaults["profiles"][letter]["special_bonuses"] = special
        from kingshot_profile_template import detach_config
        detach_config(defaults, script_folder, read_legacy=True)
        from kingshot_sequences import ensure_sequences
        ensure_sequences(defaults)
        from kingshot_players import migrate_players
        migrate_players(defaults)
        if create_if_missing: save_config(script_folder, defaults)
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"{path} must contain a JSON object.")
    return _migrate_to_current(Path(script_folder), raw)


def save_config(script_folder: Path, config: dict[str, Any]) -> Path:
    path = config_path(script_folder)
    clean = copy.deepcopy(config); clean["schema_version"] = SCHEMA_VERSION
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def resolve_path(script_folder: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute(): path = Path(script_folder) / path
    return path.resolve()


def relative_if_possible(script_folder: Path, value: str | Path) -> str:
    path = Path(value).resolve()
    try: return path.relative_to(Path(script_folder).resolve()).as_posix()
    except ValueError: return str(path)


def load_hero_catalog(script_folder: Path, cfg: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    cfg = cfg or load_config(script_folder)
    path = resolve_path(script_folder, cfg["lookups"]["hero_progression"])
    return load_progression_lookup(path)


def _check_split(split: Any, label: str, errors: list[str]) -> None:
    if not isinstance(split, (list, tuple)) or len(split) != 3:
        errors.append(f"{label}: troop split must contain exactly 3 values."); return
    try: vals = [float(x) for x in split]
    except Exception: errors.append(f"{label}: troop split contains a non-numeric value."); return
    if any(v < 0 for v in vals): errors.append(f"{label}: troop percentages cannot be negative.")
    if abs(sum(vals)-100.0) > 1e-8: errors.append(f"{label}: troop percentages must sum to 100 (got {sum(vals):g}).")


def _validate_single_config(script_folder: Path, cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []; warnings: list[str] = []; root = Path(script_folder)
    profiles = cfg.get("profiles", {})
    try: heroes = load_hero_catalog(root, cfg)
    except Exception as exc: heroes = {}; errors.append(f"Hero progression lookup: {exc}")
    for key in ("hero_stats","hero_progression"):
        value = cfg.get("lookups", {}).get(key)
        if not value or not resolve_path(root, value).is_file(): errors.append(f"Lookup not found: {key} -> {value}")
    for letter in ("A","B"):
        p = profiles.get(letter)
        if not isinstance(p, dict): errors.append(f"Profile {letter} is missing."); continue
        # Player paths are import-history labels, not live run inputs.
        stats = p.get("base_stats")
        if not isinstance(stats, dict): errors.append(f"Profile {letter}: base stats are missing.")
        else:
            for kind in ("inf", "lanc", "mark"):
                row = stats.get(kind)
                if not isinstance(row, dict): errors.append(f"Profile {letter}: base stats missing {kind}."); continue
                for stat in STAT_NAMES:
                    try:
                        if float(row.get(stat, -1)) < 0: raise ValueError
                    except Exception: errors.append(f"Profile {letter}: {kind} {stat} must be a non-negative number.")
        prog = p.get("hero_progression", {})
        if not isinstance(prog, dict): errors.append(f"Profile {letter}: hero progression is invalid.")
        else:
            for hero_name in heroes:
                hcfg = profile_hero_config(p, hero_name)
                try:
                    step=int(hcfg.get("star_step",30)); widget=int(hcfg.get("widget_level",0))
                    if not 0<=step<=30: raise ValueError
                    if not 0<=widget<=10: raise ValueError
                except Exception: errors.append(f"Profile {letter}: invalid star/widget setting for {hero_name}.")
            if prog.get("stars_source", "manual") not in {"manual", "json"}:
                errors.append(f"Profile {letter}: invalid star-stat source.")
        gear = p.get("hero_gear", {})
        for role in ROLES:
            gset = gear.get(role, {}) if isinstance(gear,dict) else {}
            for slot in GEAR_SLOTS:
                try: validate_gear_piece(gset.get(slot,{}), label=f"Profile {letter} {role} {slot}")
                except Exception as exc: errors.append(str(exc))
        special = p.get("special_bonuses", {})
        for switch in ("special_bonuses_enabled",):
            if not isinstance(p.get(switch, True), bool):
                errors.append(f"Profile {letter}: {switch} must be true or false.")
        for section, maximum in (("petLevels", 10), ("city", 2)):
            values = special.get(section, {}) if isinstance(special, dict) else {}
            if not isinstance(values, dict): errors.append(f"Profile {letter}: {section} must be an object."); continue
            for key, value in values.items():
                try:
                    number = float(value)
                    if not 0 <= number <= maximum: raise ValueError
                except Exception: errors.append(f"Profile {letter}: {section}.{key} must be between 0 and {maximum}.")

    assignment=cfg.get("assignment",{})
    if set(assignment.values()) != {"A","B"}:
        errors.append("Choose a different player for attacker and defender.")

    def validate_quality(q: Any,label: str)->None:
        if not isinstance(q,dict): errors.append(f"{label}: troop quality is missing."); return
        for role in ROLES:
            item=q.get(role)
            if not isinstance(item,dict): errors.append(f"{label}: troop quality missing {role}."); continue
            try:
                if int(item.get("tier",0))<1 or int(item.get("tg_level",-1))<0: raise ValueError
            except Exception: errors.append(f"{label}: {role} tier must be >=1 and TG >=0.")
    for side in ("attacker","defender"):
        setup=cfg.get("battle_setup",{}).get(side,{})
        try:
            if int(setup.get("total_troops",0))<=0: raise ValueError
        except Exception: errors.append(f"{side.title()} total troops must be a positive integer.")
        validate_quality(setup.get("troop_quality"), side.title())

    type_for_role={"inf":"inf","cav":"lanc","arch":"mark"}
    def validate_leads(leads:Any,label:str)->None:
        if not isinstance(leads,dict): errors.append(f"{label}: leads are missing."); return
        for role in ROLES:
            name=str(leads.get(role,"")).strip()
            if not name: errors.append(f"{label}: no {role} lead selected."); continue
            if heroes and name not in heroes: errors.append(f"{label}: unknown hero {name!r}."); continue
            if heroes and heroes[name].get("type")!=type_for_role[role]: errors.append(f"{label}: {name} is not the correct troop type for {role}.")

    lt=cfg.get("lead_troop",{})
    for side,coll in (("attacker","formations"),("defender","formations")):
        items=lt.get(side,{}).get(coll,[])
        if not isinstance(items,list) or not items: errors.append(f"Lead/troop {side}: at least one setup is required."); continue
        for i,item in enumerate(items,1):
            validate_leads(item.get("leads"),f"Lead/troop {side} setup {i}")
            widget_buffs=item.get("widget_buffs", {})
            if not isinstance(widget_buffs, dict):
                errors.append(f"Lead/troop {side} setup {i}: widget_buffs must be an object.")
            else:
                for role in ROLES:
                    if role not in widget_buffs or not isinstance(widget_buffs.get(role), bool):
                        errors.append(f"Lead/troop {side} setup {i}: widget_buffs.{role} must be true or false.")
            joiners=item.get("joiners")
            if joiners is not None and (not isinstance(joiners,list) or len(joiners)!=4 or any(not str(x).strip() for x in joiners)): errors.append(f"Lead/troop {side} setup {i}: joiners must be None or exactly four heroes.")
            variants=item.get("troop_variants_pct",[])
            if not isinstance(variants,list) or not variants: errors.append(f"Lead/troop {side} setup {i}: at least one troop split is required.")
            else:
                for j,s in enumerate(variants,1): _check_split(s,f"Lead/troop {side} setup {i}, split {j}",errors)
            groups=item.get("troop_variant_groups",[])
            if not isinstance(groups,list) or len(groups)!=len(variants):
                errors.append(f"Lead/troop {side} setup {i}: range-pattern groups do not match the splits.")
            elif any(groups.count(group)>10 for group in set(groups)):
                errors.append(f"Lead/troop {side} setup {i}: each range line may create at most 10 splits.")

    joiner=cfg.get("joiner",{})
    for side in ("attacker","defender"):
        s=joiner.get(side,{})
        validate_leads(s.get("leads"),f"Joiner {side}"); _check_split(s.get("troop_percentages"),f"Joiner {side}",errors)
        widget_buffs=s.get("widget_buffs", {})
        if not isinstance(widget_buffs, dict):
            errors.append(f"Joiner {side}: widget_buffs must be an object.")
        else:
            for role in ROLES:
                if role not in widget_buffs or not isinstance(widget_buffs.get(role), bool):
                    errors.append(f"Joiner {side}: widget_buffs.{role} must be true or false.")
        pools=s.get("pools",{}); seen={}
        for m in ("4","3","2","1"):
            vals=pools.get(m,[]) if isinstance(pools,dict) else []
            if not isinstance(vals,list): errors.append(f"Joiner {side}: max-{m} pool must be a list."); continue
            for hero in vals:
                hero=str(hero).strip()
                if heroes and hero not in heroes: errors.append(f"Joiner {side}: unknown pooled hero {hero!r}.")
                if hero in seen: errors.append(f"Joiner {side}: {hero} occurs in multiple max-copy pools.")
                seen[hero]=m
        slots=s.get("manual_slots",[])
        if not isinstance(slots,list) or len(slots)!=4:
            errors.append(f"Joiner {side}: manual_slots must contain four slot lists.")
        else:
            for i,slot in enumerate(slots,1):
                if not isinstance(slot,list):
                    errors.append(f"Joiner {side}: manual slot {i} must be a list.")
                    continue
                if len(slot)>1:
                    errors.append(f"Joiner {side}: manual slot {i} may contain at most one fixed hero.")
                for hero in slot:
                    hero=str(hero).strip()
                    if heroes and hero not in heroes:
                        errors.append(f"Joiner {side}: unknown manual-slot hero {hero!r}.")

    run=cfg.get("run",{})
    for key in ("simulations_per_batch","batches_per_condition","timeout_seconds"):
        try:
            if int(run.get(key,0))<1: raise ValueError
        except Exception: errors.append(f"Run setting {key} must be an integer >=1.")
    try:
        if float(run.get("delay_between_batches_seconds",0))<0: raise ValueError
    except Exception: errors.append("Run delay must be >=0.")
    if cfg.get("plotter",{}).get("plot_by","defender") not in {"attacker","defender"}: errors.append("Plotter perspective must be attacker or defender.")
    a=cfg.get("analysis",{})
    if a.get("side","auto") not in {"auto","attacker","defender"}: errors.append("Analysis side must be auto, attacker, or defender.")
    legacy_method=a.get("selection_method","raw"); legacy_threshold=a.get("synergy_threshold",.05)
    for layer in ("pair", "triple"):
        method=a.get(f"{layer}_selection_method", legacy_method)
        if method not in {"all","raw","fdr","holm"}: errors.append(f"Analysis {layer} selection method must be all, raw, fdr, or holm.")
        try:
            threshold=float(a.get(f"{layer}_synergy_threshold", legacy_threshold))
            if not 0<threshold<=1: raise ValueError
        except Exception: errors.append(f"Analysis {layer} synergy threshold must be greater than 0 and at most 1.")
    for key in ("pair_synergy", "triple_synergy"):
        if not isinstance(a.get(key, True), bool): errors.append(f"Analysis {key} must be true or false.")
    if not bool(a.get("pair_synergy", True)) and bool(a.get("triple_synergy", False)):
        errors.append("Triple synergy requires pair synergy.")
    from kingshot_adaptive import validate_options
    errors.extend(validate_options(cfg["joiner"]))
    from kingshot_profile_template import validate_formation_stats
    from kingshot_players import assignment as roles_for
    for section in ('joiner','lead_troop'):
        for side,letter in roles_for(cfg[section]).items():
            formations=[cfg[section][side]] if section=='joiner' else cfg[section][side]['formations']
            for formation in formations:
                try: validate_formation_stats(profiles.get(letter, {}), formation, heroes)
                except Exception as exc: errors.append(f"{side.title()} hero settings: {exc}")
    warnings.extend(cfg.get("migration_notes", []))
    return errors,warnings


def validate_config(script_folder, cfg):
    from kingshot_sequences import ensure_sequences, design_errors
    cfg=ensure_sequences(copy.deepcopy(cfg))
    errors=[];warnings=[]
    for section,items in cfg['experiments'].items():
        for index,item in enumerate(items,1):
            single=copy.deepcopy(cfg);single[section]=copy.deepcopy(item)
            local,notes=_validate_single_config(script_folder,single)
            errors.extend(f'{section} experiment {index}: {e}' for e in local+design_errors(item,section))
            warnings.extend(notes)
    return list(dict.fromkeys(errors)),list(dict.fromkeys(warnings))


def apply_config_to_globals(target: dict[str, Any], section: str, config=None, output_folder=None) -> dict[str, Any]:
    """Load the shared v2 config and populate legacy script globals."""
    script_folder=Path(target["SCRIPT_FOLDER"]); cfg=copy.deepcopy(config) if config is not None else load_config(script_folder)
    profiles=cfg["profiles"]
    from kingshot_players import assignment
    cfg["assignment"]=assignment(cfg[section])
    atk_letter=cfg["assignment"]["attacker"]; def_letter=cfg["assignment"]["defender"]
    player_files={k:script_folder / "json/player_template.json" for k in profiles}
    special_atk=copy.deepcopy(profiles[atk_letter]["special_bonuses"]); special_def=copy.deepcopy(profiles[def_letter]["special_bonuses"])
    special_atk["appointment"]={"kingdom":0,"power":0}; special_def["appointment"]={"kingdom":0,"power":0}
    target.update({
        "ATTACKER_PROFILE":atk_letter,"DEFENDER_PROFILE":def_letter,"PLAYER_FILES":player_files,
        "ATTACKER_JSON":player_files[atk_letter],"DEFENDER_JSON":player_files[def_letter],
        "SPECIAL_STATS_CONFIG_ATK":special_atk,
        "SPECIAL_STATS_CONFIG_DEF":special_def,
        "HERO_STATS_LOOKUP":resolve_path(script_folder,cfg["lookups"]["hero_stats"]),
        "HERO_PROGRESSION_LOOKUP":resolve_path(script_folder,cfg["lookups"]["hero_progression"]),
        "HERO_PROGRESSION_CONFIG_ATK":_deep_merge(profiles[atk_letter]["hero_progression"], {"gear_enabled": bool(profiles[atk_letter].get("gear_enabled", True))}),
        "HERO_PROGRESSION_CONFIG_DEF":_deep_merge(profiles[def_letter]["hero_progression"], {"gear_enabled": bool(profiles[def_letter].get("gear_enabled", True))}),
        "HERO_GEAR_CONFIG_ATK":copy.deepcopy(profiles[atk_letter]["hero_gear"]),
        "HERO_GEAR_CONFIG_DEF":copy.deepcopy(profiles[def_letter]["hero_gear"]),
        "PROFILE_CONFIG_ATK":copy.deepcopy(profiles[atk_letter]),
        "PROFILE_CONFIG_DEF":copy.deepcopy(profiles[def_letter]),
        "APPLY_SPECIAL_BONUSES_ATK":bool(profiles[atk_letter].get("special_bonuses_enabled", True)),
        "APPLY_SPECIAL_BONUSES_DEF":bool(profiles[def_letter].get("special_bonuses_enabled", True)),
        # Active widget skills are formation-scoped. The per-formation allowlist
        # decides which lead widgets are requested; role filtering still happens
        # in the simulator client as a second safety check.
        "APPLY_WIDGET_BUFFS_ATK":True,
        "APPLY_WIDGET_BUFFS_DEF":True,
        # Legacy aggregate flags retained for older helper code / metadata.
        "APPLY_SPECIAL_BONUSES":bool(profiles[atk_letter].get("special_bonuses_enabled", True) or profiles[def_letter].get("special_bonuses_enabled", True)),
        "APPLY_WIDGET_BUFFS":True,
        # Compatibility values for old metadata fields. New preparation uses the
        # explicit progression configs below.
        "ADD_5STAR_HERO_STATS_ATK":False,"ADD_HERO_GEAR_ATK":False,"ADD_HERO_STATS_ATK":True,
        "ADD_5STAR_HERO_STATS_DEF":False,"ADD_HERO_GEAR_DEF":False,"ADD_HERO_STATS_DEF":True,
    })
    setup={side:cfg.get("player_setup",{}).get(letter,cfg["battle_setup"]["attacker" if letter=="A" else "defender"]) for side,letter in cfg["assignment"].items()}
    cfg["battle_setup"]=copy.deepcopy(setup)
    target.update({
        "ATTACK_TOTAL_TROOPS":int(setup["attacker"]["total_troops"]),
        "DEFENSE_TOTAL_TROOPS":int(setup["defender"]["total_troops"]),
        "ATTACK_TROOP_QUALITY":copy.deepcopy(setup["attacker"]["troop_quality"]),
        "DEFENSE_TROOP_QUALITY":copy.deepcopy(setup["defender"]["troop_quality"]),
        "ATTACK_BASE_STAT_OVERRIDE":copy.deepcopy(profiles[atk_letter]["base_stats"]),
        "DEFENSE_BASE_STAT_OVERRIDE":copy.deepcopy(profiles[def_letter]["base_stats"]),
    })
    exp=cfg[section]; run=cfg["run"]
    from kingshot_adaptive import options
    target["ADAPTIVE_CONFIG"]=options(exp) if section=="joiner" else {"mode":"complete"}
    target.update({
        "SIMULATIONS_PER_BATCH":int(run["simulations_per_batch"]),"BATCHES_PER_CONDITION":int(run["batches_per_condition"]),
        "RESUME_FROM_CSV":bool(run["resume"]),"HEADLESS":bool(run["headless"]),"TIMEOUT_SECONDS":int(run["timeout_seconds"]),
        "DELAY_BETWEEN_BATCHES_SECONDS":float(run["delay_between_batches_seconds"]),
    })
    if section=="lead_troop":
        target.update({"ATTACK_FORMATIONS":copy.deepcopy(exp["attacker"]["formations"]),"DEFENSE_SCENARIOS":copy.deepcopy(exp["defender"]["formations"]),"ONLY_MATCHUPS":copy.deepcopy(exp.get("only_matchups"))})
    elif section=="joiner":
        atk=exp["attacker"]; deff=exp["defender"]
        target.update({"ATTACK_LEADS":copy.deepcopy(atk["leads"]),"DEFENSE_LEADS":copy.deepcopy(deff["leads"]),"ATTACK_WIDGET_BUFFS":copy.deepcopy(atk.get("widget_buffs", {r:True for r in ROLES})),"DEFENSE_WIDGET_BUFFS":copy.deepcopy(deff.get("widget_buffs", {r:True for r in ROLES})),"ATTACK_TROOP_PERCENTAGES":tuple(atk["troop_percentages"]),"DEFENSE_TROOP_PERCENTAGES":tuple(deff["troop_percentages"])})
        for side_key,prefix in (("attacker","atk"),("defender","def")):
            side_cfg=exp[side_key]; pools=side_cfg["pools"]
            for m in (4,3,2,1): target[f"joiner_pool_{prefix}_max_{m}"]=list(pools.get(str(m),[]))
            slots=side_cfg["manual_slots"]
            for i in range(4): target[f"joiner{i+1}_{prefix}"]=list(slots[i])
    else: raise ValueError(f"Unknown config section: {section}")
    from kingshot_run_history import configured_result_folder
    from kingshot_sequences import varied_side
    target['WINRATE_SIDE'] = varied_side(cfg[section], section)
    cfg['winrate_side'] = target['WINRATE_SIDE']
    folder = Path(output_folder) if output_folder is not None else configured_result_folder(script_folder / 'results', cfg, section)
    csv_name = 'kingshot_winrates.csv' if section == 'joiner' else 'kingshot_lead_troop_winrates.csv'
    target.update(EXPERIMENT_FOLDER=folder, OUTPUT_CSV=folder / csv_name,
                  SETTINGS_TXT=folder / 'experiment_settings.txt',
                  FIRST_ATTACKER_JSON=folder / 'attacker_data.json',
                  FIRST_DEFENDER_JSON=folder / 'defender_data.json')
    target["ACTIVE_KINGSHOT_CONFIG"]=cfg
    return cfg
