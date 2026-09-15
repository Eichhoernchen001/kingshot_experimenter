#!/usr/bin/env python3
"""Tkinter control panel for the Kingshot simulator workflow.

The UI edits kingshot_config.json and launches the existing experiment,
plotting, and analysis scripts. It uses only the Python standard library.
"""

from __future__ import annotations

import copy
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from kingshot_paths import APP_DIR, ROOT_DIR, JSON_DIR, RESULTS_DIR, LAST_RUN_STATS_FILE
from kingshot_run_history import FOLDERS, result_folder, previous_runs, import_run, configured_result_folder, DEFAULT_FOLDER_LABEL
from kingshot_config import (
    CONFIG_FILENAME,
    default_config,
    load_config,
    load_hero_catalog,
    relative_if_possible,
    save_config,
    validate_config,
)
from kingshot_progression import (
    GEAR_SLOTS,
    gear_enhancement_to_xp,
    gear_xp_to_enhancement,
    layered_hero_stats,
    parse_star_step_label,
    profile_hero_config,
    star_step_choices,
    widget_skill_rank,
)
from kingshot_joiner_pools import (
    mapping_to_pools as _pool_groups,
    pools_to_mapping as _pool_mapping,
    set_repeat_limit as _set_repeat_limit,
)
from kingshot_special_bonuses import apply_special_bonus_preview
from kingshot_ranges import (
    format_split as _format_split,
    parse_variant_line as _parse_variant_line,
    parse_variant_lines as _parse_variant_lines,
)

ROOT = ROOT_DIR
ROLE_LABELS = {"inf": "Infantry", "cav": "Cavalry", "arch": "Archers"}
ROLE_TYPES = {"inf": "inf", "cav": "lanc", "arch": "mark"}
WINDOW_SIGNATURE = "© by a squirrel 🐿️ [718]"
HERO_GROUP_HEADERS = ("---Rare---", "---Epic---")


def _hero_group_label(info: dict[str, Any]) -> str:
    rarity = str(info.get("rarity", "")).strip().casefold()
    if rarity == "rare":
        return "Rare"
    if rarity == "epic":
        return "Epic"
    try:
        generation = int(info.get("generation", 1))
    except Exception:
        generation = 1
    return f"Gen {generation}"


def _grouped_hero_names(catalog: dict[str, dict[str, Any]], names: list[str] | None = None) -> list[str]:
    """Order heroes as Rare, Epic, then Legendary generations.

    The first entries mirror the examples requested in the GUI: Olive for Rare,
    Howard for Epic, and Jabel for Gen 1. Remaining heroes retain catalogue order.
    """
    allowed = set(names) if names is not None else set(catalog)
    groups: dict[str, list[str]] = {}
    for name, info in catalog.items():
        if name in allowed:
            groups.setdefault(_hero_group_label(info), []).append(name)
    preferred_first = {"Rare": "Olive", "Epic": "Howard", "Gen 1": "Jabel"}
    for label, first in preferred_first.items():
        values = groups.get(label, [])
        if first in values:
            groups[label] = [first, *[name for name in values if name != first]]
    labels = [label for label in ("Rare", "Epic") if groups.get(label)]
    generations = sorted(
        (label for label in groups if label.startswith("Gen ")),
        key=lambda label: int(label.split()[-1]),
    )
    result: list[str] = []
    for label in [*labels, *generations]:
        result.extend(groups[label])
    return result


def _grouped_hero_values(catalog: dict[str, dict[str, Any]], names: list[str] | None = None) -> list[str]:
    ordered = _grouped_hero_names(catalog, names)
    groups: dict[str, list[str]] = {}
    for name in ordered:
        groups.setdefault(_hero_group_label(catalog[name]), []).append(name)
    labels = [label for label in ("Rare", "Epic") if groups.get(label)]
    labels += sorted(
        (label for label in groups if label.startswith("Gen ")),
        key=lambda label: int(label.split()[-1]),
    )
    values: list[str] = []
    for label in labels:
        values.append(f"---{label}---")
        values.extend(groups[label])
    return values


def _is_hero_group_header(value: str) -> bool:
    text = str(value).strip()
    return text.startswith("---") and text.endswith("---")


def _hero_combobox(parent: tk.Widget, variable: tk.StringVar, values: list[str], **kwargs: Any) -> ttk.Combobox:
    """Readonly hero dropdown with non-selectable visual group headings."""
    combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", **kwargs)
    valid_values = [value for value in values if not _is_hero_group_header(value)]
    state = {"last": variable.get() if variable.get() in valid_values else (valid_values[0] if valid_values else "")}

    def remember(*_args: Any) -> None:
        value = variable.get()
        if value in valid_values:
            state["last"] = value

    def reject_header(_event: Any = None) -> None:
        if _is_hero_group_header(variable.get()):
            variable.set(state["last"])

    variable.trace_add("write", remember)
    combo.bind("<<ComboboxSelected>>", reject_header, add="+")
    return combo


def _add_window_signature(window: tk.Misc) -> ttk.Label:
    """Place the requested unobtrusive credit at a window's lower-right edge."""
    label = ttk.Label(window, text=WINDOW_SIGNATURE, foreground="#777")
    label.place(relx=1.0, rely=1.0, x=-7, y=-5, anchor="se")
    label.lift()
    window.after_idle(label.lift)
    return label


def _parse_int(text: str, label: str) -> int:
    try:
        value = int(str(text).strip().replace(",", ""))
    except Exception as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    return value


def _parse_float(text: str, label: str) -> float:
    try:
        return float(str(text).strip())
    except Exception as exc:
        raise ValueError(f"{label} must be numeric.") from exc


def _split_text_to_triplet(text: str) -> list[float]:
    cleaned = text.strip().replace("/", " ").replace(",", " ")
    parts = [p for p in cleaned.split() if p]
    if len(parts) != 3:
        raise ValueError("Troop split must contain three values: Infantry / Cavalry / Archers.")
    values = [float(x) for x in parts]
    if abs(sum(values) - 100.0) > 1e-8:
        raise ValueError(f"Troop split must sum to 100; got {sum(values):g}.")
    return [int(v) if v.is_integer() else v for v in values]


def _human_path(path: str) -> str:
    return path if len(path) <= 58 else "…" + path[-57:]


def _zero_stat_vector() -> dict[str, float]:
    return {stat: 0.0 for stat in ("attack", "defense", "lethality", "health")}


def _imported_hero_stats(app: "KingshotApp", profile: dict[str, Any], name: str) -> dict[str, float]:
    zero = _zero_stat_vector()
    try:
        path = Path(str(profile.get("player_file", "")).strip())
        path = path if path.is_absolute() else ROOT / path
        heroes = json.loads(path.read_text(encoding="utf-8")).get("heroes", {})
        keys = {name.casefold(), str(app.hero_catalog[name].get("simulator_name", name)).casefold()}
        entry = next((value for key, value in heroes.items() if str(key).casefold() in keys), {})
        return {stat: float(entry.get("stats", {}).get(stat, 0.0)) for stat in zero}
    except Exception:
        return zero


def _profile_additive_rows(
    app: "KingshotApp", profile: dict[str, Any], leads: dict[str, str]
) -> dict[str, dict[str, float]]:
    """Base + enabled stars + passive widget stats + gear, before Special Bonuses."""
    prog = profile.get("hero_progression", {}) if isinstance(profile, dict) else {}
    result: dict[str, dict[str, float]] = {}
    for role, kind in (("inf", "inf"), ("cav", "lanc"), ("arch", "mark")):
        row = {
            stat: float(profile.get("base_stats", {}).get(kind, {}).get(stat, 0.0))
            for stat in ("attack", "defense", "lethality", "health")
        }
        name = str(leads.get(role, "")).strip()
        if name in app.hero_catalog and prog.get("enabled", True):
            hero = app.hero_catalog[name]
            gear = profile.get("hero_gear", {}).get(role, {})
            imported = _imported_hero_stats(app, profile, name) if prog.get("stars_source") == "json" else None
            layer = layered_hero_stats(
                hero,
                profile_hero_config(profile, name),
                gear,
                use_stars=bool(prog.get("stars_enabled", True)),
                use_passive_widget=bool(prog.get("widgets_enabled", True)),
                use_gear=bool(profile.get("gear_enabled", True)),
                imported_stats=imported,
            )
            for stat in row:
                row[stat] += float(layer["final_hero_stats"][stat])
        result[kind] = row
    return result


def _formation_widget_bonus_vector(
    app: "KingshotApp",
    profile: dict[str, Any],
    leads: dict[str, str],
    widget_buffs: dict[str, bool] | None,
    side: str,
) -> dict[str, float]:
    """Sum active compatible widget Expedition skill percentages by stat."""
    vector = _zero_stat_vector()
    toggles = widget_buffs if isinstance(widget_buffs, dict) else {}
    required = "offensive" if side == "attacker" else "defensive"
    for role in ("inf", "cav", "arch"):
        if not bool(toggles.get(role, True)):
            continue
        name = str(leads.get(role, "")).strip()
        hero = app.hero_catalog.get(name, {})
        gear = hero.get("exclusive_gear", {}) if isinstance(hero, dict) else {}
        if not gear.get("has_widget") or gear.get("widget_type") != required:
            continue
        stat = str(gear.get("active_expedition_stat", "")).strip()
        values = gear.get("active_expedition_values", [5.0, 7.5, 10.0, 12.5, 15.0])
        if stat not in vector or not isinstance(values, list):
            continue
        rank = widget_skill_rank(int(profile_hero_config(profile, name).get("widget_level", 0)))
        if rank <= 0:
            continue
        try:
            vector[stat] += float(values[min(rank, len(values)) - 1])
        except (TypeError, ValueError, IndexError):
            continue
    return vector


def _configured_final_stats_pair(
    app: "KingshotApp",
    attacker_leads: dict[str, str],
    attacker_widgets: dict[str, bool] | None,
    defender_leads: dict[str, str],
    defender_widgets: dict[str, bool] | None,
) -> dict[str, dict[str, dict[str, float]]]:
    profiles = app.profiles_tab.current_profiles()
    atk_profile, def_profile = profiles["A"], profiles["B"]
    atk_rows = _profile_additive_rows(app, atk_profile, attacker_leads)
    def_rows = _profile_additive_rows(app, def_profile, defender_leads)
    atk_rows = apply_special_bonus_preview(
        atk_rows,
        own_special=atk_profile.get("special_bonuses"),
        opponent_special=def_profile.get("special_bonuses"),
        own_enabled=bool(atk_profile.get("special_bonuses_enabled", True)),
        opponent_enabled=bool(def_profile.get("special_bonuses_enabled", True)),
        own_extra_positive=_formation_widget_bonus_vector(app, atk_profile, attacker_leads, attacker_widgets, "attacker"),
    )
    def_rows = apply_special_bonus_preview(
        def_rows,
        own_special=def_profile.get("special_bonuses"),
        opponent_special=atk_profile.get("special_bonuses"),
        own_enabled=bool(def_profile.get("special_bonuses_enabled", True)),
        opponent_enabled=bool(atk_profile.get("special_bonuses_enabled", True)),
        own_extra_positive=_formation_widget_bonus_vector(app, def_profile, defender_leads, defender_widgets, "defender"),
    )
    return {"attacker": atk_rows, "defender": def_rows}


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.inner = ttk.Frame(canvas)
        self.inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.canvas = canvas


class FileRow(ttk.Frame):
    def __init__(self, parent: tk.Widget, label: str, variable: tk.StringVar, patterns: list[tuple[str, str]]):
        super().__init__(parent)
        self.var = variable
        self.patterns = patterns
        ttk.Label(self, text=label, width=18).pack(side="left")
        ttk.Entry(self, textvariable=variable).pack(side="left", fill="x", expand=True, padx=(4, 6))
        ttk.Button(self, text="Browse…", command=self.browse).pack(side="left")

    def browse(self) -> None:
        current = self.var.get().strip()
        initial = JSON_DIR
        if current:
            p = Path(current)
            if not p.is_absolute():
                p = ROOT / p
            if p.parent.exists():
                initial = p.parent
        path = filedialog.askopenfilename(parent=self, initialdir=initial, filetypes=self.patterns)
        if path:
            self.var.set(relative_if_possible(ROOT, path))


class RunSettingsFrame(ttk.LabelFrame):
    def __init__(self, parent: tk.Widget, title: str):
        super().__init__(parent, text=title)
        self.sim = tk.StringVar()
        self.batches = tk.StringVar()
        self.resume = tk.BooleanVar()
        self.headless = tk.BooleanVar()
        self.timeout = tk.StringVar()
        self.delay = tk.StringVar()
        fields = [
            ("Simulations/batch", self.sim, 10),
            ("Batches/condition", self.batches, 10),
            ("Timeout (s)", self.timeout, 8),
            ("Delay (s)", self.delay, 8),
        ]
        for col, (label, var, width) in enumerate(fields):
            ttk.Label(self, text=label).grid(row=0, column=col * 2, sticky="w", padx=(8, 3), pady=7)
            ttk.Entry(self, textvariable=var, width=width).grid(row=0, column=col * 2 + 1, sticky="w", padx=(0, 8), pady=7)
        ttk.Checkbutton(self, text="Resume existing CSV", variable=self.resume).grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 7))
        ttk.Checkbutton(self, text="Headless browser", variable=self.headless).grid(row=1, column=2, columnspan=2, sticky="w", padx=8, pady=(0, 7))

    def load(self, cfg: dict[str, Any]) -> None:
        self.sim.set(str(cfg["simulations_per_batch"]))
        self.batches.set(str(cfg["batches_per_condition"]))
        self.resume.set(bool(cfg["resume"]))
        self.headless.set(bool(cfg["headless"]))
        self.timeout.set(str(cfg["timeout_seconds"]))
        self.delay.set(str(cfg["delay_between_batches_seconds"]))

    def dump(self) -> dict[str, Any]:
        return {
            "simulations_per_batch": _parse_int(self.sim.get(), "Simulations per batch"),
            "batches_per_condition": _parse_int(self.batches.get(), "Batches per condition"),
            "resume": bool(self.resume.get()),
            "headless": bool(self.headless.get()),
            "timeout_seconds": _parse_int(self.timeout.get(), "Timeout"),
            "delay_between_batches_seconds": _parse_float(self.delay.get(), "Delay"),
        }


class SideTroopFrame(ttk.LabelFrame):
    def __init__(self, parent: tk.Widget, title: str, *, include_split: bool, hero_choices: dict[str, list[str]] | None = None):
        super().__init__(parent, text=title)
        self.include_split = include_split
        self.total = tk.StringVar()
        self.split = tk.StringVar()
        self.quality_vars: dict[tuple[str, str], tk.StringVar] = {}
        self.lead_vars: dict[str, tk.StringVar] = {}
        row = 0
        if hero_choices is not None:
            ttk.Label(self, text="Lead heroes").grid(row=row, column=0, sticky="w", padx=8, pady=(7, 3))
            leadbox = ttk.Frame(self)
            leadbox.grid(row=row, column=1, columnspan=5, sticky="ew", padx=8, pady=(7, 3))
            for i, role in enumerate(("inf", "cav", "arch")):
                var = tk.StringVar()
                self.lead_vars[role] = var
                ttk.Label(leadbox, text=ROLE_LABELS[role]).grid(row=0, column=i * 2, sticky="w", padx=(0, 3))
                _hero_combobox(leadbox, var, hero_choices[role], width=16).grid(row=0, column=i * 2 + 1, sticky="w", padx=(0, 10))
            row += 1
        ttk.Label(self, text="Total troops").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(self, textvariable=self.total, width=14).grid(row=row, column=1, sticky="w", pady=4)
        if include_split:
            ttk.Label(self, text="Split Inf/Cav/Arch").grid(row=row, column=2, sticky="w", padx=(14, 4), pady=4)
            ttk.Entry(self, textvariable=self.split, width=14).grid(row=row, column=3, sticky="w", pady=4)
        row += 1

        ttk.Label(self, text="Troop quality").grid(row=row, column=0, sticky="nw", padx=8, pady=(5, 8))
        quality = ttk.Frame(self)
        quality.grid(row=row, column=1, columnspan=5, sticky="w", padx=8, pady=(5, 8))
        ttk.Label(quality, text="Type", width=11).grid(row=0, column=0)
        ttk.Label(quality, text="Tier", width=7).grid(row=0, column=1)
        ttk.Label(quality, text="TG", width=7).grid(row=0, column=2)
        for r, role in enumerate(("inf", "cav", "arch"), start=1):
            ttk.Label(quality, text=ROLE_LABELS[role]).grid(row=r, column=0, sticky="w")
            tier = tk.StringVar(); tg = tk.StringVar()
            self.quality_vars[(role, "tier")] = tier
            self.quality_vars[(role, "tg_level")] = tg
            ttk.Entry(quality, textvariable=tier, width=7).grid(row=r, column=1, padx=3)
            ttk.Entry(quality, textvariable=tg, width=7).grid(row=r, column=2, padx=3)

    def load(self, cfg: dict[str, Any]) -> None:
        self.total.set(str(cfg["total_troops"]))
        if self.include_split:
            self.split.set(_format_split(cfg["troop_percentages"]))
        for role in ("inf", "cav", "arch"):
            self.quality_vars[(role, "tier")].set(str(cfg["troop_quality"][role]["tier"]))
            self.quality_vars[(role, "tg_level")].set(str(cfg["troop_quality"][role]["tg_level"]))
        if self.lead_vars:
            for role in ("inf", "cav", "arch"):
                self.lead_vars[role].set(cfg["leads"][role])

    def dump_common(self) -> dict[str, Any]:
        quality = {}
        for role in ("inf", "cav", "arch"):
            quality[role] = {
                "tier": _parse_int(self.quality_vars[(role, "tier")].get(), f"{ROLE_LABELS[role]} tier"),
                "tg_level": _parse_int(self.quality_vars[(role, "tg_level")].get(), f"{ROLE_LABELS[role]} TG level"),
            }
        result: dict[str, Any] = {
            "total_troops": _parse_int(self.total.get(), "Total troops"),
            "troop_quality": quality,
        }
        if self.include_split:
            result["troop_percentages"] = _split_text_to_triplet(self.split.get())
        if self.lead_vars:
            result["leads"] = {role: self.lead_vars[role].get() for role in ("inf", "cav", "arch")}
        return result

class JoinerFixedSetupFrame(ttk.LabelFrame):
    """Experiment-specific fixed leads, active widget choices, and troop split."""
    def __init__(self, parent: tk.Widget, title: str, app: "KingshotApp", side: str, on_change: Callable[[], None] | None = None):
        super().__init__(parent, text=title)
        self.app = app
        self.side = side
        self.on_change = on_change
        self.lead_vars: dict[str, tk.StringVar] = {}
        self.widget_vars: dict[str, tk.BooleanVar] = {}
        self.widget_boxes: dict[str, ttk.Checkbutton] = {}
        self.split = tk.StringVar()
        ttk.Label(self, text="Lead heroes").grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 4))
        for row, role in enumerate(("inf", "cav", "arch"), start=1):
            var = tk.StringVar(); self.lead_vars[role] = var
            ttk.Label(self, text=ROLE_LABELS[role], width=10).grid(row=row, column=0, sticky="w", padx=(8, 4), pady=3)
            combo = _hero_combobox(self, var, app.hero_dropdown_choices[role], width=18)
            combo.grid(row=row, column=1, sticky="ew", padx=(0, 5), pady=3)
            wvar = tk.BooleanVar(value=True); self.widget_vars[role] = wvar
            box = ttk.Checkbutton(self, text="Use widget buff", variable=wvar, command=self._notify)
            box.grid(row=row, column=2, sticky="w", padx=(3, 8), pady=3); self.widget_boxes[role] = box
            combo.bind("<<ComboboxSelected>>", lambda _e, r=role: (self._sync_widget_state(r, auto_enable=True), self._notify()), add="+")
        ttk.Label(self, text="Troop split Inf/Cav/Arch").grid(row=4, column=0, sticky="w", padx=8, pady=(7, 4))
        ttk.Entry(self, textvariable=self.split, width=18).grid(row=4, column=1, sticky="w", padx=(0, 8), pady=(7, 4))
        ttk.Label(self, text="Total troops, Tier/TG and root stats are shared on tab 1.", foreground="#555", wraplength=380).grid(row=5, column=0, columnspan=3, sticky="w", padx=8, pady=(3, 8))
        self.columnconfigure(1, weight=1)

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def current_selection(self) -> tuple[dict[str, str], dict[str, bool]]:
        return (
            {role:self.lead_vars[role].get().strip() for role in ("inf","cav","arch")},
            {role:bool(self.widget_vars[role].get()) for role in ("inf","cav","arch")},
        )

    def _widget_compatible(self, role: str) -> bool:
        hero = self.app.hero_catalog.get(self.lead_vars[role].get().strip(), {})
        gear = hero.get("exclusive_gear", {}) if isinstance(hero, dict) else {}
        required = "offensive" if self.side == "attacker" else "defensive"
        return bool(gear.get("has_widget")) and gear.get("widget_type") == required

    def _sync_widget_state(self, role: str, *, auto_enable: bool) -> None:
        if not self._widget_compatible(role):
            self.widget_vars[role].set(False)
            self.widget_boxes[role].configure(state="disabled")
        else:
            self.widget_boxes[role].configure(state="normal")
            if auto_enable:
                self.widget_vars[role].set(True)

    def load(self, cfg: dict[str, Any]) -> None:
        self.split.set(_format_split(cfg["troop_percentages"]))
        toggles = cfg.get("widget_buffs", {}) if isinstance(cfg.get("widget_buffs"), dict) else {}
        for role in ("inf","cav","arch"):
            self.lead_vars[role].set(cfg["leads"][role])
            self.widget_vars[role].set(bool(toggles.get(role, True)))
            self._sync_widget_state(role, auto_enable=False)

    def dump(self) -> dict[str, Any]:
        return {
            "leads": {r:self.lead_vars[r].get().strip() for r in ("inf","cav","arch")},
            "widget_buffs": {r:bool(self.widget_vars[r].get()) for r in ("inf","cav","arch")},
            "troop_percentages": _split_text_to_triplet(self.split.get()),
        }


class BaseStatsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Widget, profile: dict[str, Any], on_save: Callable[[dict[str, Any]], None]):
        super().__init__(parent); self.signature=_add_window_signature(self); self.title("Base stats"); self.transient(parent); self.grab_set()
        self.profile=profile; self.on_save=on_save; self.vars={}
        body=ttk.Frame(self); body.pack(fill="both",expand=True,padx=12,pady=10)
        for col,label in enumerate(("Troop type","ATK","DEF","LETH","Health")):
            ttk.Label(body,text=label,font=("TkDefaultFont",9,"bold")).grid(row=0,column=col,padx=5,pady=5)
        values=profile.get("base_stats",{})
        for row,(role,kind) in enumerate((("inf","inf"),("cav","lanc"),("arch","mark")),start=1):
            ttk.Label(body,text=ROLE_LABELS[role]).grid(row=row,column=0,sticky="w",padx=5,pady=4)
            for col,stat in enumerate(("attack","defense","lethality","health"),start=1):
                var=tk.StringVar(value=str(values.get(kind,{}).get(stat,0))); self.vars[(kind,stat)]=var
                ttk.Entry(body,textvariable=var,width=11).grid(row=row,column=col,padx=4,pady=4)
        controls=ttk.Frame(self); controls.pack(fill="x",padx=12,pady=(0,28))
        ttk.Button(controls,text="Use player JSON",command=self.use_json).pack(side="left")
        ttk.Button(controls,text="Set all…",command=self.set_all).pack(side="left",padx=6)
        ttk.Button(controls,text="Set to min (0)",command=lambda:self.fill(0)).pack(side="left")
        ttk.Button(controls,text="Cancel",command=self.destroy).pack(side="right")
        ttk.Button(controls,text="Save",command=self.save).pack(side="right",padx=6)
        self.geometry("590x270"); self.wait_window(self)
    def fill(self,value:float)->None:
        for var in self.vars.values(): var.set(f"{value:g}")
    def set_all(self)->None:
        value=simpledialog.askfloat("Set all base stats","Value for all 12 base stats:",parent=self,minvalue=0)
        if value is not None:self.fill(value)
    def use_json(self)->None:
        try:
            path=Path(self.profile["player_file"]); path=path if path.is_absolute() else ROOT/path
            stats=json.loads(path.read_text(encoding="utf-8"))["stats"]
            for key,var in self.vars.items(): var.set(str(stats[key[0]][key[1]]))
        except Exception as exc: messagebox.showerror("Could not load JSON stats",str(exc),parent=self)
    def save(self)->None:
        try:
            result={kind:{} for kind in ("inf","lanc","mark")}
            for (kind,stat),var in self.vars.items():
                value=_parse_float(var.get(),f"{kind} {stat}")
                if value<0: raise ValueError("Base stats cannot be negative.")
                result[kind][stat]=value
            self.on_save(result); self.destroy()
        except Exception as exc: messagebox.showerror("Invalid base stats",str(exc),parent=self)


class HeroLevelDialog(tk.Toplevel):
    def __init__(self,parent:tk.Widget,app:"KingshotApp",profile:dict[str,Any],kind:str,on_save:Callable[[dict[str,Any]],None]):
        super().__init__(parent); self.signature=_add_window_signature(self); self.app=app; self.profile=copy.deepcopy(profile); self.kind=kind; self.on_save=on_save
        self.title("Hero stars" if kind=="stars" else "Passive widgets"); self.transient(parent); self.grab_set()
        prog=self.profile.setdefault("hero_progression",{}); self.enabled=tk.BooleanVar(value=bool(prog.get("stars_enabled" if kind=="stars" else "widgets_enabled",True)))
        top=ttk.Frame(self); top.pack(fill="x",padx=10,pady=(10,5))
        ttk.Checkbutton(top,text=("Apply star stats" if kind=="stars" else "Apply passive widget stats"),variable=self.enabled).pack(side="left")
        self.status=tk.StringVar(value=("Player JSON hero.stats" if kind=="stars" and prog.get("stars_source")=="json" else "Manual values"))
        ttk.Label(top,textvariable=self.status,foreground="#555").pack(side="right")
        self.tree=ttk.Treeview(self,columns=("hero","value"),show="headings",height=16)
        self.tree.heading("hero",text="Hero"); self.tree.heading("value",text="Stars" if kind=="stars" else "Widget level")
        self.tree.column("hero",width=210); self.tree.column("value",width=130,anchor="center"); self.tree.pack(fill="both",expand=True,padx=10,pady=5)
        self.tree.tag_configure("group", font=("TkDefaultFont", 9, "bold"))
        self.tree.bind("<<TreeviewSelect>>",lambda _e:self.select())
        edit=ttk.Frame(self); edit.pack(fill="x",padx=10,pady=5); self.selected=tk.StringVar(value="—"); self.value=tk.StringVar()
        ttk.Label(edit,textvariable=self.selected,width=22).pack(side="left")
        values=star_step_choices() if kind=="stars" else [str(i) for i in range(11)]
        self.box=ttk.Combobox(edit,textvariable=self.value,values=values,state="readonly",width=16); self.box.pack(side="left",padx=5)
        ttk.Button(edit,text="Apply",command=self.apply).pack(side="left")
        controls=ttk.Frame(self); controls.pack(fill="x",padx=10,pady=(3,28))
        if kind=="stars": ttk.Button(controls,text="Use player JSON",command=self.use_json).pack(side="left")
        else: ttk.Button(controls,text="Use player JSON",command=self.widgets_json).pack(side="left")
        ttk.Button(controls,text="Set all to min",command=lambda:self.set_all(0)).pack(side="left",padx=6)
        ttk.Button(controls,text="Set all to max",command=lambda:self.set_all(30 if kind=="stars" else 10)).pack(side="left")
        ttk.Button(controls,text="Cancel",command=self.destroy).pack(side="right")
        ttk.Button(controls,text="Save",command=self.save).pack(side="right",padx=6)
        self.refresh(); self.geometry("470x620"); self.wait_window(self)
    def _value_for(self,name:str)->int:
        cfg=profile_hero_config(self.profile,name); return int(cfg["star_step" if self.kind=="stars" else "widget_level"])
    def refresh(self)->None:
        selected=self.tree.selection()[0] if self.tree.selection() else None; self.tree.delete(*self.tree.get_children())
        values = self.app.hero_dropdown_all if self.kind == "stars" else self.app.all_heroes
        for name in values:
            if _is_hero_group_header(name):
                self.tree.insert("", "end", iid=f"group:{name}", values=(name, ""), tags=("group",))
                continue
            value=self._value_for(name); label=star_step_choices()[value] if self.kind=="stars" else str(value)
            self.tree.insert("","end",iid=name,values=(name,label))
        if selected in self.app.all_heroes:self.tree.selection_set(selected); self.select()
    def select(self)->None:
        if not self.tree.selection():return
        name=self.tree.selection()[0]
        if name.startswith("group:"):
            self.selected.set("—"); self.value.set(""); self.box.configure(state="disabled"); return
        self.selected.set(name); value=self._value_for(name)
        self.value.set(star_step_choices()[value] if self.kind=="stars" else str(value))
        if self.kind=="widgets" and not self.app.hero_catalog[name].get("exclusive_gear",{}).get("has_widget"):self.box.configure(state="disabled")
        else:self.box.configure(state="readonly")
    def apply(self)->None:
        if not self.tree.selection():return
        name=self.tree.selection()[0]
        if name.startswith("group:"): return
        prog=self.profile.setdefault("hero_progression",{}); item=prog.setdefault("heroes",{}).setdefault(name,{})
        item["star_step" if self.kind=="stars" else "widget_level"] = parse_star_step_label(self.value.get()) if self.kind=="stars" else int(self.value.get())
        if self.kind=="stars": prog["stars_source"]="manual"
        self.status.set("Manual values"); self.enabled.set(True); self.refresh(); self.tree.selection_set(name); self.select()
    def set_all(self,value:int)->None:
        prog=self.profile.setdefault("hero_progression",{}); key="star_step" if self.kind=="stars" else "widget_level"
        prog.setdefault("defaults",{})[key]=value; prog["heroes"]={name:{**cfg, key:value} for name,cfg in prog.get("heroes",{}).items() if isinstance(cfg,dict)}
        if self.kind=="stars":prog["stars_source"]="manual"
        self.status.set("Manual values"); self.enabled.set(True); self.refresh()
    def use_json(self)->None:
        self.profile.setdefault("hero_progression",{})["stars_source"]="json"; self.enabled.set(True); self.status.set("Player JSON hero.stats")
        messagebox.showinfo(
            "Player JSON hero stats",
            "Current player exports contain combined hero.stats, not a separate star level. "
            "The JSON button therefore uses those values exactly as the star/hero layer. "
            "Disable Gear and passive Widgets if the export already includes them.",
            parent=self,
        )
    def widgets_json(self)->None:
        try:
            path=Path(self.profile["player_file"]); path=path if path.is_absolute() else ROOT/path
            heroes=json.loads(path.read_text(encoding="utf-8")).get("heroes",{}); by_key={str(k).casefold():v for k,v in heroes.items()}
            prog=self.profile.setdefault("hero_progression",{}); out=prog.setdefault("heroes",{})
            prog.setdefault("defaults",{})["widget_level"]=0
            for name in self.app.all_heroes:
                entry=by_key.get(name.casefold(),{}); out.setdefault(name,{})["widget_level"]=int(entry.get("widget_level",0) or 0)
            self.enabled.set(True); self.status.set("Values copied from player JSON"); self.refresh()
        except Exception as exc:messagebox.showerror("Could not load widgets",str(exc),parent=self)
    def save(self)->None:
        prog=self.profile.setdefault("hero_progression",{}); prog["stars_enabled" if self.kind=="stars" else "widgets_enabled"]=bool(self.enabled.get())
        prog["enabled"]=bool(prog.get("stars_enabled") or prog.get("widgets_enabled") or self.profile.get("gear_enabled"))
        self.on_save(self.profile); self.destroy()


class GearDialog(tk.Toplevel):
    def __init__(self,parent:tk.Widget,profile:dict[str,Any],on_save:Callable[[dict[str,Any]],None]):
        super().__init__(parent); self.signature=_add_window_signature(self); self.profile=copy.deepcopy(profile); self.on_save=on_save; self.title("Hero gear"); self.transient(parent); self.grab_set()
        self.enabled=tk.BooleanVar(value=bool(self.profile.get("gear_enabled",True))); self.vars={}
        top=ttk.Frame(self); top.pack(fill="x",padx=10,pady=(10,0)); ttk.Checkbutton(top,text="Apply hero gear stats",variable=self.enabled).pack(side="left")
        notebook=ttk.Notebook(self); notebook.pack(fill="both",expand=True,padx=10,pady=6)
        for role in ("inf","cav","arch"):
            frame=ttk.Frame(notebook); notebook.add(frame,text=ROLE_LABELS[role])
            for col,label in enumerate(("Piece","Quality","Gear XP (0–100)","Mastery")):ttk.Label(frame,text=label).grid(row=0,column=col,padx=5,pady=5)
            for row,slot in enumerate(GEAR_SLOTS,start=1):
                piece=self.profile.get("hero_gear",{}).get(role,{}).get(slot,{})
                ttk.Label(frame,text=slot.title()).grid(row=row,column=0,padx=5,pady=4,sticky="w")
                quality=str(piece.get("quality","none")).casefold()
                display_xp=gear_enhancement_to_xp(quality,piece.get("enhancement",0))
                for col,(field,default,values) in enumerate((("quality","none",["None","Mythic","Red"]),("enhancement",display_xp,None),("mastery",0,None)),start=1):
                    var=tk.StringVar(value=str(piece.get(field,default)).title() if field=="quality" else str(default if field=="enhancement" else piece.get(field,default))); self.vars[(role,slot,field)]=var
                    if values: ttk.Combobox(frame,textvariable=var,values=values,state="readonly",width=12).grid(row=row,column=col,padx=5,pady=4)
                    else: ttk.Entry(frame,textvariable=var,width=12).grid(row=row,column=col,padx=5,pady=4)
        ttk.Label(self,text="Red gear automatically adds 100 effective enhancement levels to the displayed 0–100 XP.",foreground="#555").pack(anchor="w",padx=12,pady=(0,4))
        controls=ttk.Frame(self); controls.pack(fill="x",padx=10,pady=(0,28))
        ttk.Button(controls,text="Use player JSON",command=self.use_json).pack(side="left")
        ttk.Button(controls,text="Set all to min",command=lambda:self.fill(False)).pack(side="left",padx=6)
        ttk.Button(controls,text="Set all to max",command=lambda:self.fill(True)).pack(side="left")
        ttk.Button(controls,text="Cancel",command=self.destroy).pack(side="right"); ttk.Button(controls,text="Save",command=self.save).pack(side="right",padx=6)
        self.geometry("690x450"); self.wait_window(self)
    def fill(self,maxed:bool)->None:
        values=("Red","100","20") if maxed else ("None","0","0")
        for role in ("inf","cav","arch"):
            for slot in GEAR_SLOTS:
                for field,value in zip(("quality","enhancement","mastery"),values):self.vars[(role,slot,field)].set(value)
        self.enabled.set(True)
    def use_json(self)->None:
        try:
            path=Path(self.profile["player_file"]); path=path if path.is_absolute() else ROOT/path
            data=json.loads(path.read_text(encoding="utf-8")); gear=data.get("hero_gear")
            if not isinstance(gear,dict):
                self.fill(False); messagebox.showinfo("No gear block","This player JSON has no separate hero_gear block, so gear was set to minimum (zero).",parent=self); return
            self.profile["hero_gear"]=copy.deepcopy(gear)
            for role in ("inf","cav","arch"):
                for slot in GEAR_SLOTS:
                    piece=gear.get(role,{}).get(slot,{})
                    quality=str(piece.get("quality","none")).casefold()
                    self.vars[(role,slot,"quality")].set(quality.title())
                    self.vars[(role,slot,"enhancement")].set(str(gear_enhancement_to_xp(quality,piece.get("enhancement",0))))
                    self.vars[(role,slot,"mastery")].set(str(piece.get("mastery",0)))
            self.enabled.set(True)
        except Exception as exc:messagebox.showerror("Could not load gear",str(exc),parent=self)
    def save(self)->None:
        from kingshot_progression import validate_gear_piece
        try:
            gear={}
            for role in ("inf","cav","arch"):
                gear[role]={}
                for slot in GEAR_SLOTS:
                    quality=self.vars[(role,slot,"quality")].get().casefold()
                    xp=_parse_int(self.vars[(role,slot,"enhancement")].get(),"Gear XP")
                    piece={"quality":quality,"enhancement":gear_xp_to_enhancement(quality,xp),"mastery":_parse_int(self.vars[(role,slot,"mastery")].get(),"Mastery")}
                    gear[role][slot]=validate_gear_piece(piece,label=f"{ROLE_LABELS[role]} {slot}")
            self.profile["hero_gear"]=gear; self.profile["gear_enabled"]=bool(self.enabled.get()); prog=self.profile.setdefault("hero_progression",{}); prog["enabled"]=bool(prog.get("stars_enabled") or prog.get("widgets_enabled") or self.profile["gear_enabled"])
            self.on_save(self.profile); self.destroy()
        except Exception as exc:messagebox.showerror("Invalid gear",str(exc),parent=self)


class MultiHeroDialog(tk.Toplevel):
    def __init__(self, parent: tk.Widget, heroes: list[str], selected: list[str], title: str):
        super().__init__(parent)
        self.signature = _add_window_signature(self)
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.result: list[str] | None = None
        ttk.Label(self, text="Select zero or more heroes. Ctrl/Shift selects multiple.").pack(anchor="w", padx=10, pady=(10, 5))
        frame = ttk.Frame(self); frame.pack(fill="both", expand=True, padx=10)
        scrollbar = ttk.Scrollbar(frame, orient="vertical")
        lb = tk.Listbox(frame, selectmode="extended", height=18, exportselection=False, yscrollcommand=scrollbar.set)
        scrollbar.config(command=lb.yview)
        lb.pack(side="left", fill="both", expand=True); scrollbar.pack(side="right", fill="y")
        for hero in heroes:
            lb.insert("end", hero)
        selected_set = set(selected)
        for i, hero in enumerate(heroes):
            if hero in selected_set:
                lb.selection_set(i)
        buttons = ttk.Frame(self); buttons.pack(fill="x", padx=10, pady=(10, 28))
        ttk.Button(buttons, text="Clear", command=lambda: lb.selection_clear(0, "end")).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        def accept() -> None:
            self.result = [heroes[i] for i in lb.curselection()]
            self.destroy()
        ttk.Button(buttons, text="OK", command=accept).pack(side="right", padx=(0, 6))
        self.geometry("390x490")
        self.wait_window(self)


class FormationDialog(tk.Toplevel):
    def __init__(self, parent: tk.Widget, app: "KingshotApp", initial: dict[str, Any] | None, title: str, side: str):
        super().__init__(parent)
        self.signature = _add_window_signature(self)
        self.app = app
        self.side = side
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.result: dict[str, Any] | None = None
        hero_choices = app.hero_choices
        all_heroes = app.all_heroes
        initial = copy.deepcopy(initial) if initial else {
            "joiners": None,
            "leads": {r: (hero_choices[r][0] if hero_choices[r] else "") for r in ("inf", "cav", "arch")},
            "widget_buffs": {r: True for r in ("inf", "cav", "arch")},
            "troop_variants_pct": [[50, 50, 0]],
        }
        body = ttk.Frame(self); body.pack(fill="both", expand=True, padx=12, pady=10)
        ttk.Label(body, text="Lead heroes", font=("TkDefaultFont", 9, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 5))
        self.leads: dict[str, tk.StringVar] = {}
        self.widget_vars: dict[str, tk.BooleanVar] = {}
        self.widget_boxes: dict[str, ttk.Checkbutton] = {}
        stored_widget = initial.get("widget_buffs", {}) if isinstance(initial.get("widget_buffs"), dict) else {}
        for i, role in enumerate(("inf", "cav", "arch"), start=1):
            ttk.Label(body, text=ROLE_LABELS[role]).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=initial["leads"][role]); self.leads[role] = var
            combo = _hero_combobox(body, var, app.hero_dropdown_choices[role], width=22)
            combo.grid(row=i, column=1, sticky="ew", pady=3)
            wvar = tk.BooleanVar(value=bool(stored_widget.get(role, True))); self.widget_vars[role] = wvar
            box = ttk.Checkbutton(body, text="Use widget buff", variable=wvar)
            box.grid(row=i, column=2, sticky="w", padx=(8, 0), pady=3); self.widget_boxes[role] = box
            combo.bind("<<ComboboxSelected>>", lambda _e, r=role: self._sync_widget_state(r, auto_enable=True), add="+")

        self.no_joiners = tk.BooleanVar(value=initial.get("joiners") is None)
        ttk.Checkbutton(body, text="No joiners for this setup", variable=self.no_joiners, command=self._sync_joiner_state).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))
        self.joiners: list[tk.StringVar] = []
        self.joiner_boxes: list[ttk.Combobox] = []
        initial_joiners = initial.get("joiners") or [all_heroes[0] if all_heroes else ""] * 4
        for i in range(4):
            ttk.Label(body, text=f"Joiner {i+1}").grid(row=5+i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=initial_joiners[i]); self.joiners.append(var)
            box = _hero_combobox(body, var, app.hero_dropdown_all, width=24)
            box.grid(row=5+i, column=1, columnspan=2, sticky="ew", pady=2); self.joiner_boxes.append(box)

        ttk.Label(body, text="Troop splits and ranges", font=("TkDefaultFont", 9, "bold")).grid(row=9, column=0, columnspan=3, sticky="w", pady=(12, 3))
        ttk.Label(body, text="One split or range per line. Example: 70:30/30:70/0 - 10", wraplength=430).grid(row=10, column=0, columnspan=3, sticky="w")
        self.variants = tk.Text(body, width=34, height=9)
        self.variants.grid(row=11, column=0, columnspan=3, sticky="nsew", pady=(4, 8))
        source_lines = initial.get("troop_range_lines")
        if not isinstance(source_lines, list) or not source_lines:
            source_lines = [_format_split(x) for x in initial["troop_variants_pct"]]
        self.variants.insert("1.0", "\n".join(str(x) for x in source_lines))
        body.columnconfigure(1, weight=1); body.rowconfigure(11, weight=1)

        buttons = ttk.Frame(self); buttons.pack(fill="x", padx=12, pady=(0, 28))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save setup", command=self._accept).pack(side="right", padx=(0, 7))
        self._sync_joiner_state()
        for role in ("inf", "cav", "arch"):
            self._sync_widget_state(role, auto_enable=False)
        self.geometry("560x630")
        self.wait_window(self)

    def _widget_compatible(self, role: str) -> bool:
        hero = self.app.hero_catalog.get(self.leads[role].get().strip(), {})
        gear = hero.get("exclusive_gear", {}) if isinstance(hero, dict) else {}
        required = "offensive" if self.side == "attacker" else "defensive"
        return bool(gear.get("has_widget")) and gear.get("widget_type") == required

    def _sync_widget_state(self, role: str, *, auto_enable: bool) -> None:
        compatible = self._widget_compatible(role)
        if not compatible:
            self.widget_vars[role].set(False)
            self.widget_boxes[role].configure(state="disabled")
        else:
            self.widget_boxes[role].configure(state="normal")
            if auto_enable:
                self.widget_vars[role].set(True)

    def _sync_joiner_state(self) -> None:
        state = "disabled" if self.no_joiners.get() else "readonly"
        for box in self.joiner_boxes:
            box.configure(state=state)

    def _accept(self) -> None:
        try:
            variants, groups, lines = _parse_variant_lines(self.variants.get("1.0", "end"))
            joiners = None if self.no_joiners.get() else [v.get().strip() for v in self.joiners]
            if joiners is not None and (len(joiners) != 4 or any(not x for x in joiners)):
                raise ValueError("Choose all four joiners or select 'No joiners'.")
            leads = {role: self.leads[role].get().strip() for role in ("inf", "cav", "arch")}
            if any(not x for x in leads.values()):
                raise ValueError("Choose all three lead heroes.")
            self.result = {
                "joiners": joiners,
                "leads": leads,
                "widget_buffs": {role: bool(self.widget_vars[role].get()) for role in ("inf", "cav", "arch")},
                "troop_variants_pct": variants,
                "troop_variant_groups": groups,
                "troop_range_lines": lines,
            }
            self.destroy()
        except Exception as exc:
            messagebox.showerror("Invalid setup", str(exc), parent=self)


class SpecialStatsDialog(tk.Toplevel):
    def __init__(self,parent:tk.Widget,profile:dict[str,Any],on_save:Callable[[dict[str,Any]],None]):
        super().__init__(parent); self.signature=_add_window_signature(self); self.profile=copy.deepcopy(profile); self.on_save=on_save; self.title("Pets and city stats"); self.transient(parent); self.grab_set(); self.vars={}
        data=self.profile.get("special_bonuses",{}); body=ttk.Frame(self); body.pack(fill="both",expand=True,padx=12,pady=10)
        for row,(section,title,maximum) in enumerate((("petLevels","Pets (maximum 10)",10),("city","City bonuses (maximum 2)",2))):
            box=ttk.LabelFrame(body,text=title); box.grid(row=row,column=0,sticky="ew",pady=5)
            items=data.get(section,{})
            for r,key in enumerate(items):
                ttk.Label(box,text=key,width=28).grid(row=r,column=0,sticky="w",padx=7,pady=3)
                var=tk.StringVar(value=str(items[key])); self.vars[(section,key)]=var; ttk.Entry(box,textvariable=var,width=10).grid(row=r,column=1,padx=7,pady=3)
        ttk.Label(body,text="Appointments were removed and are always written as 0.",foreground="#555").grid(row=2,column=0,sticky="w",pady=(6,0))
        controls=ttk.Frame(self); controls.pack(fill="x",padx=12,pady=(0,28))
        ttk.Button(controls,text="Use player JSON",command=self.use_json).pack(side="left")
        ttk.Button(controls,text="Set to min (0)",command=lambda:self.fill(False)).pack(side="left",padx=6)
        ttk.Button(controls,text="Set to max",command=lambda:self.fill(True)).pack(side="left")
        ttk.Button(controls,text="Cancel",command=self.destroy).pack(side="right"); ttk.Button(controls,text="Save",command=self.save).pack(side="right",padx=6)
        self.geometry("520x620"); self.wait_window(self)
    def fill(self,maxed:bool)->None:
        for (section,_),var in self.vars.items():var.set("10" if maxed and section=="petLevels" else "2" if maxed else "0")
    def use_json(self)->None:
        try:
            path=Path(self.profile["player_file"]); path=path if path.is_absolute() else ROOT/path
            special=json.loads(path.read_text(encoding="utf-8")).get("special_bonuses",{})
            for key,var in self.vars.items():var.set(str(special.get(key[0],{}).get(key[1],0) or 0))
        except Exception as exc:messagebox.showerror("Could not load special stats",str(exc),parent=self)
    def save(self)->None:
        try:
            result={"petLevels":{},"city":{},"appointment":{"kingdom":0,"power":0}}
            for (section,key),var in self.vars.items():
                value=_parse_float(var.get(),f"{section}.{key}"); maximum=10 if section=="petLevels" else 2
                if not 0<=value<=maximum:raise ValueError(f"{key} must be between 0 and {maximum}.")
                result[section][key]=int(value) if value.is_integer() else value
            self.profile["special_bonuses"]=result; self.on_save(self.profile); self.destroy()
        except Exception as exc:messagebox.showerror("Invalid special stats",str(exc),parent=self)


def _fill_stats_tree(tree: ttk.Treeview, rows: dict[str, dict[str, float]]) -> None:
    tree.delete(*tree.get_children())
    for role, kind in (("inf", "inf"), ("cav", "lanc"), ("arch", "mark")):
        row = rows.get(kind, {})
        tree.insert(
            "",
            "end",
            values=(ROLE_LABELS[role], *(f"{float(row.get(stat, 0.0)):.2f}" for stat in ("attack", "defense", "lethality", "health"))),
        )


class FinalStatsFrame(ttk.LabelFrame):
    """Two-sided configured-stat preview used where one fixed formation exists."""
    def __init__(
        self,
        parent: tk.Widget,
        app: "KingshotApp",
        provider: Callable[[], dict[str, dict[str, dict[str, float]]]],
        *,
        note: str,
    ):
        super().__init__(parent, text="Final Stats")
        self.app = app
        self.provider = provider
        body = ttk.Frame(self); body.pack(fill="x", padx=7, pady=(6, 3))
        self.trees: dict[str, ttk.Treeview] = {}
        for col, side in enumerate(("attacker", "defender")):
            box = ttk.LabelFrame(body, text=side.capitalize()); box.grid(row=0, column=col, sticky="nsew", padx=(0, 5) if col == 0 else (5, 0))
            tree = ttk.Treeview(box, columns=("troop", "attack", "defense", "lethality", "health"), show="headings", height=3)
            for key, label, width in (("troop", "Troop", 95), ("attack", "ATK", 78), ("defense", "DEF", 78), ("lethality", "LETH", 78), ("health", "Health", 78)):
                tree.heading(key, text=label); tree.column(key, width=width, anchor="w" if key == "troop" else "center")
            tree.pack(fill="x", padx=5, pady=5); self.trees[side] = tree
            body.columnconfigure(col, weight=1)
        ttk.Label(self, text=note, foreground="#555", wraplength=1000).pack(anchor="w", padx=7, pady=(2, 7))

    def refresh(self) -> None:
        rows = self.provider()
        for side in ("attacker", "defender"):
            _fill_stats_tree(self.trees[side], rows.get(side, {}))


class ProfileStatsFrame(ttk.LabelFrame):
    OPTION_LABELS = (
        ("stars", "Stars"),
        ("widget_stats", "Widget stats"),
        ("gear", "Gear"),
        ("special", "Pet / City buffs"),
    )

    def __init__(self,parent:tk.Widget,app:"KingshotApp",letter:str,role_label:str,on_change:Callable[[],None]):
        super().__init__(parent,text=f"Player {letter} — {role_label}"); self.app=app; self.letter=letter; self.profile={}; self.leads={}; self.player_file=tk.StringVar(); self.on_change=on_change
        FileRow(self,"Player JSON",self.player_file,[("JSON","*.json"),("All files","*.*")]).pack(fill="x",padx=7,pady=7)
        self.player_name = tk.StringVar()
        name_row = ttk.Frame(self); name_row.pack(fill='x', padx=7, pady=(0, 7))
        ttk.Label(name_row, text='Player name', width=14).pack(side='left')
        ttk.Entry(name_row, textvariable=self.player_name).pack(side='left', fill='x', expand=True)
        options=ttk.LabelFrame(self,text="Use bonuses")
        options.pack(fill="x",padx=7,pady=(0,6))
        self.option_vars:dict[str,tk.BooleanVar]={}
        for col,(key,label) in enumerate(self.OPTION_LABELS):
            var=tk.BooleanVar(); self.option_vars[key]=var
            ttk.Checkbutton(options,text=label,variable=var,command=lambda k=key:self._toggle_option(k)).grid(row=0,column=col,sticky="w",padx=(7 if col==0 else 3,7),pady=5)
        buttons=ttk.Frame(self); buttons.pack(fill="x",padx=7,pady=(0,5))
        for text_,method in (("Base Stats…",self.edit_base),("Stars…",self.edit_stars),("Widgets…",self.edit_widgets),("Gear…",self.edit_gear),("Pets / City…",self.edit_special)):
            ttk.Button(buttons,text=text_,command=method).pack(side="left",padx=(0,5))
        self.tree=ttk.Treeview(self,columns=("troop","attack","defense","lethality","health"),show="headings",height=3)
        for key,label,width in (("troop","Troop",105),("attack","Attack",85),("defense","Defense",85),("lethality","Lethality",85),("health","Health",85)):
            self.tree.heading(key,text=label); self.tree.column(key,width=width,anchor="w" if key=="troop" else "center")
        self.tree.pack(fill="x",padx=7,pady=(0,7))
        ttk.Label(self,text="Shown totals: Base + enabled Stars + passive Widget stats + Gear, then enabled Pet/City Special Bonuses (including opponent enemy-down effects). Active lead-widget skill buffs are controlled per formation on tabs 2 and 3 and are not included here.",foreground="#555",wraplength=440).pack(anchor="w",padx=7,pady=(0,7))

    def load(self,profile:dict[str,Any],leads:dict[str,str])->None:
        self.profile=copy.deepcopy(profile); self.player_file.set(profile["player_file"]); self.leads=copy.deepcopy(leads); self._sync_option_vars()
        self.player_name.set(profile.get('name', '') or ('Attacker' if self.letter == 'A' else 'Defender'))

    def dump(self)->dict[str,Any]:
        self.profile['name'] = self.player_name.get().strip()
        self.profile["player_file"]=self.player_file.get().strip(); return copy.deepcopy(self.profile)

    def _sync_option_vars(self)->None:
        prog=self.profile.get("hero_progression",{}) if isinstance(self.profile,dict) else {}
        values={
            "stars":bool(prog.get("stars_enabled",True)),
            "widget_stats":bool(prog.get("widgets_enabled",True)),
            "gear":bool(self.profile.get("gear_enabled",True)),
            "special":bool(self.profile.get("special_bonuses_enabled",True)),
        }
        for key,value in values.items():self.option_vars[key].set(value)

    def _toggle_option(self,key:str)->None:
        prog=self.profile.setdefault("hero_progression",{})
        value=bool(self.option_vars[key].get())
        if key=="stars":prog["stars_enabled"]=value
        elif key=="widget_stats":prog["widgets_enabled"]=value
        elif key=="gear":self.profile["gear_enabled"]=value
        elif key=="special":self.profile["special_bonuses_enabled"]=value
        prog["enabled"]=bool(prog.get("stars_enabled") or prog.get("widgets_enabled") or self.profile.get("gear_enabled"))
        self.on_change()

    def _json_data(self)->dict[str,Any]:
        path=Path(self.player_file.get().strip()); path=path if path.is_absolute() else ROOT/path
        return json.loads(path.read_text(encoding="utf-8"))

    def _imported_hero(self,name:str)->dict[str,float]:
        zero={s:0.0 for s in ("attack","defense","lethality","health")}
        try:
            heroes=self._json_data().get("heroes",{}); keys={name.casefold(),str(self.app.hero_catalog[name].get("simulator_name",name)).casefold()}
            entry=next((v for k,v in heroes.items() if str(k).casefold() in keys),{}); return {s:float(entry.get("stats",{}).get(s,0)) for s in zero}
        except Exception:return zero

    def _additive_rows(self)->dict[str,dict[str,float]]:
        prog=self.profile.get("hero_progression",{})
        result={}
        for role,kind in (("inf","inf"),("cav","lanc"),("arch","mark")):
            row={s:float(self.profile.get("base_stats",{}).get(kind,{}).get(s,0)) for s in ("attack","defense","lethality","health")}; name=self.leads.get(role)
            if name in self.app.hero_catalog and prog.get("enabled",True):
                hero=self.app.hero_catalog[name]; gear=self.profile.get("hero_gear",{}).get(role,{})
                imported=self._imported_hero(name) if prog.get("stars_source")=="json" else None
                layer=layered_hero_stats(hero,profile_hero_config(self.profile,name),gear,use_stars=bool(prog.get("stars_enabled",True)),use_passive_widget=bool(prog.get("widgets_enabled",True)),use_gear=bool(self.profile.get("gear_enabled",True)),imported_stats=imported)
                for stat in row:row[stat]+=float(layer["final_hero_stats"][stat])
            result[kind]=row
        return result

    def refresh(self,opponent_profile:dict[str,Any]|None=None)->None:
        self._sync_option_vars(); self.tree.delete(*self.tree.get_children())
        rows=self._additive_rows()
        rows=apply_special_bonus_preview(
            rows,
            own_special=self.profile.get("special_bonuses"),
            opponent_special=(opponent_profile or {}).get("special_bonuses"),
            own_enabled=bool(self.profile.get("special_bonuses_enabled",True)),
            opponent_enabled=bool((opponent_profile or {}).get("special_bonuses_enabled",True)),
        )
        _fill_stats_tree(self.tree, rows)

    def display_rows(self, rows: dict[str, dict[str, float]]) -> None:
        _fill_stats_tree(self.tree, rows)

    def _sync_file(self)->None:self.profile["player_file"]=self.player_file.get().strip()
    def _saved(self,value:dict[str,Any])->None:self.profile=value; self.on_change()
    def edit_base(self)->None:self._sync_file(); BaseStatsDialog(self,self.profile,lambda v:(self.profile.__setitem__("base_stats",v),self.on_change()))
    def edit_stars(self)->None:self._sync_file(); HeroLevelDialog(self,self.app,self.profile,"stars",self._saved)
    def edit_widgets(self)->None:self._sync_file(); HeroLevelDialog(self,self.app,self.profile,"widgets",self._saved)
    def edit_gear(self)->None:self._sync_file(); GearDialog(self,self.profile,self._saved)
    def edit_special(self)->None:self._sync_file(); SpecialStatsDialog(self,self.profile,self._saved)


def _swap_button(parent: tk.Widget, command: Callable[[], None]) -> ttk.Button:
    ttk.Style(parent).configure('Swap.TButton', font=('TkDefaultFont', 9, 'bold'), anchor='center')
    button = ttk.Button(parent, text='Swap\nattacker ↔ defender', command=command,
                        width=19, padding=(8, 12), style='Swap.TButton')
    button.grid(row=0, column=1, padx=8, pady=8)
    return button


class ProfilesTab(ScrollableFrame):
    def __init__(self,app:"KingshotApp",parent:tk.Widget):
        super().__init__(parent); self.app=app; root=self.inner
        topbar=ttk.Frame(root); topbar.pack(fill="x",padx=14,pady=(10,0))
        ttk.Button(topbar,text="Use last saved settings",command=lambda:self.app.reload_saved_tab("profiles")).pack(side="right")
        imports = ttk.Frame(self.inner)
        imports.pack(fill='x', padx=14, pady=6)
        ttk.Label(imports, text='Import from previous...').pack(side='left', padx=(0, 8))
        for section, label in [('lead_troop', 'Lead + Troops'), ('joiner', 'Joiner')]:
            ttk.Button(imports, text=label, command=lambda s=section: app.import_previous(s)).pack(side='left', padx=(0, 8))
        players = ttk.Frame(root); players.pack(fill='x', padx=12, pady=(6, 12))
        left = ttk.Frame(players); left.grid(row=0, column=0, sticky='nsew')
        right = ttk.Frame(players); right.grid(row=0, column=2, sticky='nsew')
        players.columnconfigure(0, weight=1, uniform='players')
        players.columnconfigure(2, weight=1, uniform='players')
        self.player_a = ProfileStatsFrame(left, app, 'A', 'attacker', self.refresh_displays)
        self.player_b = ProfileStatsFrame(right, app, 'B', 'defender', self.refresh_displays)
        self.player_a.pack(fill='x'); self.player_b.pack(fill='x')
        self.atk_setup = SideTroopFrame(left, 'Shared battle setup — attacker', include_split=False)
        self.def_setup = SideTroopFrame(right, 'Shared battle setup — defender', include_split=False)
        self.atk_setup.pack(fill='x', pady=(10, 0)); self.def_setup.pack(fill='x', pady=(10, 0))
        _swap_button(players, self.swap_players)

    def current_profiles(self)->dict[str,dict[str,Any]]:
        return {"A":self.player_a.dump(),"B":self.player_b.dump()}

    def swap_players(self)->None:
        # Read both sides before changing either, including unsaved controls.
        try:
            profiles = self.current_profiles()
            attacker_setup = self.atk_setup.dump_common()
            defender_setup = self.def_setup.dump_common()
        except (ValueError, TypeError) as exc:
            messagebox.showerror('Could not swap players', str(exc), parent=self)
            return
        # Leads belong to the experiment tabs; retain each side's lead context.
        attacker_leads = copy.deepcopy(self.player_a.leads)
        defender_leads = copy.deepcopy(self.player_b.leads)
        self.player_a.load(profiles['B'], attacker_leads)
        self.player_b.load(profiles['A'], defender_leads)
        self.atk_setup.load(defender_setup)
        self.def_setup.load(attacker_setup)
        self.refresh_displays()

    def refresh_displays(self)->None:
        if not (self.player_a.profile and self.player_b.profile):
            return
        self.player_a.refresh(self.player_b.profile); self.player_b.refresh(self.player_a.profile)
        if hasattr(self.app,"joiner_tab"):
            self.app.joiner_tab.refresh_final_stats()

    def load(self,cfg:dict[str,Any])->None:
        def leads(side:str)->dict[str,str]:
            items=cfg["lead_troop"][side]["formations"]; return copy.deepcopy(items[0]["leads"] if items else {})
        self.player_a.load(cfg["profiles"]["A"],leads("attacker")); self.player_b.load(cfg["profiles"]["B"],leads("defender")); self.atk_setup.load(cfg["battle_setup"]["attacker"]); self.def_setup.load(cfg["battle_setup"]["defender"]); self.refresh_displays()

    def commit(self,cfg:dict[str,Any])->None:
        cfg["profiles"]={"A":self.player_a.dump(),"B":self.player_b.dump()}; cfg["assignment"]={"attacker":"A","defender":"B"}; cfg["battle_setup"]={"attacker":self.atk_setup.dump_common(),"defender":self.def_setup.dump_common()}
        cfg["battle_options"]={
            "apply_special_bonuses":bool(cfg["profiles"]["A"].get("special_bonuses_enabled",True) or cfg["profiles"]["B"].get("special_bonuses_enabled",True)),
        }

class FormationListEditor(ttk.LabelFrame):
    def __init__(self, parent: tk.Widget, title: str, app: "KingshotApp", collection: str, side: str, on_change: Callable[[], None] | None = None):
        super().__init__(parent, text=title)
        self.app = app; self.collection = collection; self.side = side; self.on_change = on_change
        self.items: list[dict[str, Any]] = []
        self.tree = ttk.Treeview(self, columns=("leads", "widget", "joiners", "ranges", "splits"), show="headings", height=8)
        self.tree.heading("leads", text="Lead formation"); self.tree.heading("widget", text="Widget buffs"); self.tree.heading("joiners", text="Joiners"); self.tree.heading("ranges", text="Ranges"); self.tree.heading("splits", text="Generated splits")
        self.tree.column("leads", width=235); self.tree.column("widget", width=115); self.tree.column("joiners", width=245); self.tree.column("ranges", width=70, anchor="center"); self.tree.column("splits", width=105, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=7, pady=(7, 4))
        buttons = ttk.Frame(self); buttons.pack(fill="x", padx=7, pady=(0, 7))
        ttk.Button(buttons, text="Add…", command=self.add).pack(side="left")
        ttk.Button(buttons, text="Edit…", command=self.edit).pack(side="left", padx=5)
        ttk.Button(buttons, text="Duplicate", command=self.duplicate).pack(side="left")
        ttk.Button(buttons, text="Remove", command=self.remove).pack(side="left", padx=5)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        self.tree.bind("<<TreeviewSelect>>", lambda _e:self._notify(), add="+")

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def selected_item(self) -> dict[str, Any] | None:
        idx = self.selected_index()
        if idx is None:
            return self.items[0] if self.items else None
        return self.items[idx] if 0 <= idx < len(self.items) else None

    def _widget_compatible(self, item: dict[str, Any], role: str) -> bool:
        name = str(item.get("leads", {}).get(role, "")).strip()
        hero = self.app.hero_catalog.get(name, {})
        gear = hero.get("exclusive_gear", {}) if isinstance(hero, dict) else {}
        required = "offensive" if self.side == "attacker" else "defensive"
        return bool(gear.get("has_widget")) and gear.get("widget_type") == required

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, item in enumerate(self.items):
            leads = "/".join(item["leads"][r] for r in ("inf", "cav", "arch"))
            toggles=item.get("widget_buffs",{}) if isinstance(item.get("widget_buffs"),dict) else {}
            enabled=[ROLE_LABELS[r] for r in ("inf","cav","arch") if bool(toggles.get(r,True)) and self._widget_compatible(item,r)]
            widget=", ".join(enabled) if enabled else "Off"
            joiners = "None" if item.get("joiners") is None else "/".join(item["joiners"])
            ranges=len(set(item.get("troop_variant_groups",[]))) or len(item.get("troop_range_lines",[])) or len(item["troop_variants_pct"])
            self.tree.insert("", "end", iid=str(i), values=(leads, widget, joiners, ranges, len(item["troop_variants_pct"])))
        if self.items and not self.tree.selection():
            self.tree.selection_set("0")
        self._notify()

    def load(self, items: list[dict[str, Any]]) -> None:
        self.items = copy.deepcopy(items)
        for item in self.items:
            toggles=item.get("widget_buffs",{}) if isinstance(item.get("widget_buffs"),dict) else {}
            item["widget_buffs"]={
                role: bool(toggles.get(role,True)) and self._widget_compatible(item,role)
                for role in ("inf","cav","arch")
            }
        self._refresh()

    def add(self) -> None:
        dialog = FormationDialog(self, self.app, None, f"Add {self.collection[:-1]}", self.side)
        if dialog.result:
            self.items.append(dialog.result); self._refresh()

    def selected_index(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def edit(self) -> None:
        idx = self.selected_index()
        if idx is None:
            messagebox.showinfo("Select a setup", "Select a row first.", parent=self)
            return
        dialog = FormationDialog(self, self.app, self.items[idx], f"Edit {self.collection[:-1]}", self.side)
        if dialog.result:
            self.items[idx] = dialog.result; self._refresh()

    def duplicate(self) -> None:
        idx = self.selected_index()
        if idx is not None:
            self.items.insert(idx + 1, copy.deepcopy(self.items[idx])); self._refresh(); self.tree.selection_set(str(idx + 1))

    def remove(self) -> None:
        idx = self.selected_index()
        if idx is not None and messagebox.askyesno("Remove setup", "Remove the selected setup?", parent=self):
            self.items.pop(idx); self._refresh()


class LeadTroopTab(ScrollableFrame):
    def __init__(self, app: "KingshotApp", parent: tk.Widget):
        super().__init__(parent); self.app=app; root=self.inner
        topbar=ttk.Frame(root); topbar.pack(fill="x",padx=14,pady=(10,0))
        ttk.Button(topbar,text="Use last saved settings",command=lambda:self.app.reload_saved_tab("lead_troop")).pack(side="right")
        ttk.Label(root,text="Total troops, troop Tier/TG, stats and progression are shared on tab 1. Active widget skill use is selected per lead hero inside each formation; incompatible offensive/defensive widgets are disabled automatically. A single Final Stats table is not shown here because each formation has its own hero/widget combination.",foreground="#555",wraplength=1000).pack(fill="x",padx=14,pady=(6,4))
        self.atk_list=FormationListEditor(root,"Attacker formations",app,"formations","attacker"); self.atk_list.pack(fill="both",expand=True,padx=12,pady=6)
        self.def_list=FormationListEditor(root,"Defender formations",app,"formations","defender"); self.def_list.pack(fill="both",expand=True,padx=12,pady=(6,12))

    def load(self,cfg:dict[str,Any])->None:
        sec=cfg["lead_troop"]; self.atk_list.load(sec["attacker"]["formations"]); self.def_list.load(sec["defender"]["formations"])

    def commit(self,cfg:dict[str,Any])->None:
        sec=cfg["lead_troop"]; sec["attacker"]={"formations":copy.deepcopy(self.atk_list.items)}; sec["defender"]={"formations":copy.deepcopy(self.def_list.items)}


class JoinerPoolDialog(tk.Toplevel):
    """One-click mutually exclusive 1×/2×/3×/4× choices per hero."""
    def __init__(
        self,
        parent: tk.Widget,
        heroes: list[str],
        mapping: dict[str, int],
        side_label: str,
        on_save: Callable[[dict[str, int]], None],
        grouped_values: list[str] | None = None,
    ):
        super().__init__(parent)
        self.signature = _add_window_signature(self)
        self.title(f"Select {side_label.casefold()} joiners")
        self.transient(parent); self.grab_set()
        self.heroes=heroes; self.grouped_values=grouped_values or heroes; self.mapping=copy.deepcopy(mapping); self.on_save=on_save
        self.vars: dict[tuple[str, int], tk.BooleanVar] = {}

        ttk.Label(
            self,
            text="Choose at most one repeat limit per hero. Click the selected box again to exclude that hero.",
            foreground="#555",
            wraplength=540,
        ).pack(fill="x",padx=12,pady=(12,6))

        outer=ttk.Frame(self); outer.pack(fill="both",expand=True,padx=12,pady=4)
        canvas=tk.Canvas(outer,highlightthickness=0); scroll=ttk.Scrollbar(outer,orient="vertical",command=canvas.yview)
        table=ttk.Frame(canvas); window_id=canvas.create_window((0,0),window=table,anchor="nw")
        table.bind("<Configure>",lambda _e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",lambda e:canvas.itemconfigure(window_id,width=e.width))
        canvas.configure(yscrollcommand=scroll.set); canvas.pack(side="left",fill="both",expand=True); scroll.pack(side="right",fill="y")

        ttk.Label(table,text="Hero",font=("TkDefaultFont",9,"bold")).grid(row=0,column=0,sticky="w",padx=7,pady=5)
        for column,maximum in enumerate((1,2,3,4),start=1):
            ttk.Label(table,text=f"{maximum}×",font=("TkDefaultFont",9,"bold"),anchor="center").grid(row=0,column=column,padx=12,pady=5)
        table.columnconfigure(0,weight=1)
        row=1
        for value in self.grouped_values:
            if _is_hero_group_header(value):
                ttk.Label(table,text=value,font=("TkDefaultFont",9,"bold")).grid(row=row,column=0,columnspan=5,sticky="w",padx=7,pady=(8,3))
                row += 1
                continue
            hero=value
            ttk.Label(table,text=hero).grid(row=row,column=0,sticky="w",padx=7,pady=2)
            for column,maximum in enumerate((1,2,3,4),start=1):
                var=tk.BooleanVar(value=self.mapping.get(hero)==maximum); self.vars[(hero,maximum)]=var
                ttk.Checkbutton(table,variable=var,command=lambda h=hero,m=maximum:self.toggle(h,m)).grid(row=row,column=column,padx=12,pady=2)
            row += 1

        self.status=tk.StringVar(); ttk.Label(self,textvariable=self.status,foreground="#555").pack(anchor="w",padx=12,pady=(4,0)); self.refresh_status()
        buttons=ttk.Frame(self); buttons.pack(fill="x",padx=12,pady=(6,28))
        ttk.Button(buttons,text="Clear all",command=self.clear_all).pack(side="left")
        ttk.Button(buttons,text="Cancel",command=self.destroy).pack(side="right")
        ttk.Button(buttons,text="Save",command=self.save).pack(side="right",padx=6)
        self.geometry("570x680"); self.wait_window(self)

    def toggle(self,hero:str,maximum:int)->None:
        selected=bool(self.vars[(hero,maximum)].get())
        for other in (1,2,3,4):
            if other != maximum:self.vars[(hero,other)].set(False)
        _set_repeat_limit(self.mapping,hero,maximum,selected)
        self.refresh_status()

    def clear_all(self)->None:
        self.mapping.clear()
        for var in self.vars.values():var.set(False)
        self.refresh_status()

    def refresh_status(self)->None:
        selected=len(self.mapping); self.status.set(f"{selected} hero{'es' if selected != 1 else ''} selected")

    def save(self)->None:
        self.on_save(copy.deepcopy(self.mapping)); self.destroy()


class JoinerPoolSummary(ttk.LabelFrame):
    def __init__(self,parent:tk.Widget,app:"KingshotApp",side_label:str):
        super().__init__(parent,text=f"{side_label} joiner pool"); self.app=app; self.side_label=side_label; self.mapping:dict[str,int]={}; self.labels={}
        for row,maximum in enumerate((4,3,2,1)):
            ttk.Label(self,text=f"{maximum}×",font=("TkDefaultFont",9,"bold"),width=4).grid(row=row,column=0,sticky="nw",padx=(8,3),pady=4)
            label=ttk.Label(self,text="—",wraplength=430,justify="left"); label.grid(row=row,column=1,sticky="ew",padx=3,pady=4); self.labels[maximum]=label
        self.columnconfigure(1,weight=1)
        ttk.Button(self,text=f"Edit {side_label.casefold()} joiners…",command=self.edit).grid(row=4,column=1,sticky="e",padx=8,pady=(6,8))

    def load(self,pools:dict[str,list[str]])->None:
        self.mapping=_pool_mapping(pools); self.refresh()

    def refresh(self)->None:
        groups=_pool_groups(self.mapping,self.app.all_heroes)
        for maximum in (4,3,2,1):
            names=groups[str(maximum)]; self.labels[maximum].configure(text=", ".join(names) if names else "—")

    def edit(self)->None:
        def saved(mapping:dict[str,int])->None:self.mapping=mapping; self.refresh()
        JoinerPoolDialog(self,self.app.all_heroes,self.mapping,self.side_label,saved,self.app.hero_dropdown_all)

    def dump(self)->dict[str,list[str]]:
        return _pool_groups(self.mapping,self.app.all_heroes)


class ManualSlotsFrame(ttk.LabelFrame):
    def __init__(self, parent: tk.Widget, title: str, app: "KingshotApp"):
        super().__init__(parent, text=title); self.app = app
        self.slots: list[list[str]] = [[], [], [], []]
        self.vars: list[tk.StringVar] = []
        values=["Pool-owned", *app.hero_dropdown_all]
        for i in range(4):
            ttk.Label(self, text=f"Slot {i+1}", width=8).grid(row=i, column=0, sticky="w", padx=7, pady=4)
            var=tk.StringVar(value="Pool-owned"); self.vars.append(var)
            _hero_combobox(self,var,values,width=26).grid(row=i,column=1,sticky="ew",padx=(3,7),pady=4)
        self.columnconfigure(1,weight=1)
        ttk.Label(self, text="Pool-owned leaves that slot to the selected joiner pool. Choosing a hero fixes that one slot to that hero.", foreground="#555", wraplength=420).grid(row=4, column=0, columnspan=2, sticky="w", padx=7, pady=(5, 7))

    def load(self, slots: list[list[str]]) -> None:
        self.slots = copy.deepcopy(slots) if isinstance(slots,list) else [[],[],[],[]]
        while len(self.slots)<4:self.slots.append([])
        self.slots=self.slots[:4]
        for i,values in enumerate(self.slots):
            hero=str(values[0]).strip() if isinstance(values,list) and values else "Pool-owned"
            self.vars[i].set(hero if hero in self.app.all_heroes else "Pool-owned")
        self.sync()

    def sync(self)->None:
        self.slots=[]
        for var in self.vars:
            value=var.get().strip()
            self.slots.append([] if value=="Pool-owned" or not value else [value])

    def dump(self)->list[list[str]]:
        self.sync(); return copy.deepcopy(self.slots)


class JoinerTab(ScrollableFrame):
    def __init__(self, app: "KingshotApp", parent: tk.Widget):
        super().__init__(parent); self.app=app; root=self.inner
        topbar=ttk.Frame(root); topbar.pack(fill="x",padx=14,pady=(10,0))
        ttk.Button(topbar,text="Use last saved settings",command=lambda:self.app.reload_saved_tab("joiner")).pack(side="right")
        ttk.Label(root,text="Total troops, troop Tier/TG, stats and progression are shared on tab 1. Run settings for both experiments are on tab 4.",foreground="#555",wraplength=1000).pack(fill="x",padx=14,pady=(6,4))
        sides=ttk.Frame(root); sides.pack(fill="x",padx=12,pady=6)
        self.atk=JoinerFixedSetupFrame(sides,"Attacker fixed setup",app,"attacker",self.refresh_final_stats); self.atk.grid(row=0,column=0,sticky="nsew",padx=(0,6))
        self.deff=JoinerFixedSetupFrame(sides,"Defender fixed setup",app,"defender",self.refresh_final_stats); self.deff.grid(row=0,column=2,sticky="nsew",padx=(6,0)); sides.columnconfigure(0,weight=1); sides.columnconfigure(2,weight=1)
        _swap_button(sides, self.swap_fixed)
        pools=ttk.Frame(root); pools.pack(fill="x",padx=12,pady=6)
        self.atk_pool=JoinerPoolSummary(pools,app,"Attacker"); self.atk_pool.grid(row=0,column=0,sticky="nsew",padx=(0,6))
        self.def_pool=JoinerPoolSummary(pools,app,"Defender"); self.def_pool.grid(row=0,column=2,sticky="nsew",padx=(6,0))
        pools.columnconfigure(0,weight=1); pools.columnconfigure(2,weight=1)
        _swap_button(pools, self.swap_pools)
        manual=ttk.Frame(root); manual.pack(fill="x",padx=12,pady=(6,6))
        self.atk_slots=ManualSlotsFrame(manual,"Attacker manual slots",app); self.atk_slots.grid(row=0,column=0,sticky="nsew",padx=(0,6))
        self.def_slots=ManualSlotsFrame(manual,"Defender manual slots",app); self.def_slots.grid(row=0,column=2,sticky="nsew",padx=(6,0)); manual.columnconfigure(0,weight=1); manual.columnconfigure(2,weight=1)
        _swap_button(manual, self.swap_manual)
        self.final_stats=FinalStatsFrame(
            root, app, self._configured_stats,
            note="Configured view uses the fixed attacker/defender leads above. Enabled compatible widget skills are included; joiner pool/manual-slot choices do not change these troop-stat totals.",
        ); self.final_stats.pack(fill="x",padx=12,pady=(6,12))

    def _configured_stats(self)->dict[str,dict[str,dict[str,float]]]:
        atk_leads,atk_widgets=self.atk.current_selection(); def_leads,def_widgets=self.deff.current_selection()
        return _configured_final_stats_pair(self.app,atk_leads,atk_widgets,def_leads,def_widgets)

    def refresh_final_stats(self)->None:
        if hasattr(self,"final_stats"):
            self.final_stats.refresh()

    def swap_fixed(self)->None:
        atk=self.atk.dump(); deff=self.deff.dump(); self.atk.load(deff); self.deff.load(atk); self.refresh_final_stats()

    def swap_pools(self)->None:
        atk=self.atk_pool.dump(); deff=self.def_pool.dump(); self.atk_pool.load(deff); self.def_pool.load(atk)

    def swap_manual(self)->None:
        atk=self.atk_slots.dump(); deff=self.def_slots.dump(); self.atk_slots.load(deff); self.def_slots.load(atk)

    def load(self,cfg:dict[str,Any])->None:
        sec=cfg["joiner"]; self.atk.load(sec["attacker"]); self.deff.load(sec["defender"]); self.atk_pool.load(sec["attacker"]["pools"]); self.def_pool.load(sec["defender"]["pools"]); self.atk_slots.load(sec["attacker"]["manual_slots"]); self.def_slots.load(sec["defender"]["manual_slots"]); self.refresh_final_stats()

    def commit(self,cfg:dict[str,Any])->None:
        sec=cfg["joiner"]; atk=self.atk.dump(); deff=self.deff.dump(); atk["pools"]=self.atk_pool.dump(); deff["pools"]=self.def_pool.dump(); atk["manual_slots"]=self.atk_slots.dump(); deff["manual_slots"]=self.def_slots.dump(); sec["attacker"]=atk; sec["defender"]=deff


class RunTab(ttk.Frame):
    def __init__(self, app: "KingshotApp", parent: tk.Widget):
        super().__init__(parent); self.app=app
        self.run=RunSettingsFrame(self,"Run settings — applies to both experiments"); self.run.pack(fill="x",padx=12,pady=(12,6))
        folders = ttk.LabelFrame(self, text='Results folder names')
        folders.pack(fill='x', padx=12, pady=6)
        self.result_names = {s: tk.StringVar(value='') for s in FOLDERS}
        for row, (section, label) in enumerate([('lead_troop', 'Lead + Troops'), ('joiner', 'Joiner')]):
            ttk.Label(folders, text=label, width=16).grid(row=row, column=0, padx=8, pady=4, sticky='w')
            ttk.Entry(folders, textvariable=self.result_names[section], width=30).grid(row=row, column=1, padx=4, pady=4)
            ttk.Label(folders, text='Blank = automatic player names' + (' + troop ratios' if section == 'joiner' else '')).grid(row=row, column=2, padx=8, sticky='w')
        actions=ttk.LabelFrame(self,text="Workflow"); actions.pack(fill="x",padx=12,pady=6)
        self.plot_by=tk.StringVar(); self.analysis_side=tk.StringVar(); self.top_n=tk.StringVar()
        self.pair_selection=tk.StringVar(); self.pair_threshold=tk.StringVar()
        self.triple_selection=tk.StringVar(); self.triple_threshold=tk.StringVar()
        self.pair_synergy=tk.BooleanVar(value=True); self.triple_synergy=tk.BooleanVar(value=True)
        self.selection_labels={"all":"All", "raw":"Raw p-value", "fdr":"FDR-adjusted p", "holm":"Holm-adjusted p"}

        ttk.Label(actions,text="Lead + troop",width=16,font=("TkDefaultFont",9,"bold")).grid(row=0,column=0,sticky="w",padx=8,pady=8)
        ttk.Button(actions,text="Preview",command=lambda:app.run_script("kingshot_lead_troop_experiment.py",["--preview"])).grid(row=0,column=1,padx=4,pady=8)
        ttk.Button(actions,text="Run experiment",command=lambda:app.run_script("kingshot_lead_troop_experiment.py")).grid(row=0,column=2,padx=4,pady=8)
        ttk.Button(actions,text="Create plots",command=self.plot_results).grid(row=0,column=3,padx=4,pady=8)
        ttk.Button(actions,text="Open folder",command=lambda:self.open_results('lead_troop')).grid(row=0,column=4,padx=4,pady=8)
        ttk.Label(actions,text="Plot by").grid(row=0,column=5,padx=(18,3))
        ttk.Combobox(actions,textvariable=self.plot_by,values=["defender","attacker"],state="readonly",width=10).grid(row=0,column=6,padx=(0,8))

        ttk.Label(actions,text="Joiners",width=16,font=("TkDefaultFont",9,"bold")).grid(row=1,column=0,sticky="w",padx=8,pady=8)
        ttk.Button(actions,text="Preview",command=lambda:app.run_script("kingshot_joiner_experiment.py",["--preview"])).grid(row=1,column=1,padx=4,pady=8)
        ttk.Button(actions,text="Run experiment",command=lambda:app.run_script("kingshot_joiner_experiment.py")).grid(row=1,column=2,padx=4,pady=8)
        ttk.Button(actions,text="Create model and plots",command=self.analyze).grid(row=1,column=3,padx=4,pady=8)
        ttk.Button(actions,text="Open folder",command=lambda:self.open_results('joiner')).grid(row=1,column=4,padx=4,pady=8)

        modelopts=ttk.Frame(actions); modelopts.grid(row=2,column=1,columnspan=6,sticky="w",padx=4,pady=(0,4))
        ttk.Label(modelopts,text="Model side").grid(row=0,column=0,sticky="w",padx=(4,3),pady=2)
        ttk.Combobox(modelopts,textvariable=self.analysis_side,values=["auto","attacker","defender"],state="readonly",width=10).grid(row=0,column=1,sticky="w",padx=(0,14),pady=2)
        ttk.Checkbutton(modelopts,text="Pair synergy",variable=self.pair_synergy,command=self._sync_synergy_controls).grid(row=0,column=2,sticky="w",padx=(0,8),pady=2)
        ttk.Label(modelopts,text="Method").grid(row=0,column=3,sticky="w",padx=(0,3),pady=2)
        self.pair_method_box=ttk.Combobox(modelopts,textvariable=self.pair_selection,values=list(self.selection_labels.values()),state="readonly",width=17)
        self.pair_method_box.grid(row=0,column=4,sticky="w",padx=(0,8),pady=2); self.pair_method_box.bind("<<ComboboxSelected>>",lambda _e:self._sync_synergy_controls(),add="+")
        ttk.Label(modelopts,text="p threshold").grid(row=0,column=5,sticky="w",padx=(0,3),pady=2)
        self.pair_threshold_entry=ttk.Entry(modelopts,textvariable=self.pair_threshold,width=7); self.pair_threshold_entry.grid(row=0,column=6,sticky="w",padx=(0,14),pady=2)
        ttk.Label(modelopts,text="Label top").grid(row=0,column=7,sticky="w",padx=(0,3),pady=2)
        ttk.Entry(modelopts,textvariable=self.top_n,width=5).grid(row=0,column=8,sticky="w",pady=2)
        ttk.Label(modelopts,text="joiner combinations").grid(row=0,column=9,sticky="w",padx=(3,0),pady=2)

        self.triple_box=ttk.Checkbutton(modelopts,text="Triple synergy",variable=self.triple_synergy,command=self._sync_synergy_controls)
        self.triple_box.grid(row=1,column=2,sticky="w",padx=(0,8),pady=2)
        ttk.Label(modelopts,text="Method").grid(row=1,column=3,sticky="w",padx=(0,3),pady=2)
        self.triple_method_box=ttk.Combobox(modelopts,textvariable=self.triple_selection,values=list(self.selection_labels.values()),state="readonly",width=17)
        self.triple_method_box.grid(row=1,column=4,sticky="w",padx=(0,8),pady=2); self.triple_method_box.bind("<<ComboboxSelected>>",lambda _e:self._sync_synergy_controls(),add="+")
        ttk.Label(modelopts,text="p threshold").grid(row=1,column=5,sticky="w",padx=(0,3),pady=2)
        self.triple_threshold_entry=ttk.Entry(modelopts,textvariable=self.triple_threshold,width=7); self.triple_threshold_entry.grid(row=1,column=6,sticky="w",padx=(0,14),pady=2)

        controls=ttk.Frame(self); controls.pack(fill="x",padx=12,pady=(6,4))
        ttk.Button(controls,text="Save configuration",command=app.save).pack(side="left")
        ttk.Button(controls,text="Validate configuration",command=app.validate).pack(side="left",padx=6)
        ttk.Button(controls,text="Stop running task",command=app.stop_process).pack(side="right")

        logframe=ttk.LabelFrame(self,text="Run log"); logframe.pack(fill="both",expand=True,padx=12,pady=(4,12))
        self.log=tk.Text(logframe,wrap="word",height=20,state="disabled"); scroll=ttk.Scrollbar(logframe,orient="vertical",command=self.log.yview); self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left",fill="both",expand=True,padx=(6,0),pady=6); scroll.pack(side="right",fill="y",padx=(0,6),pady=6)

    def _selection_code(self,var:tk.StringVar)->str:
        reverse={label:key for key,label in self.selection_labels.items()}
        return reverse.get(var.get(), "raw")

    def output_folder(self, section):
        cfg = copy.deepcopy(self.app.config_data)
        cfg['profiles'] = self.app.profiles_tab.current_profiles()
        if section == 'joiner':
            self.app.joiner_tab.commit(cfg)
        cfg['result_names'] = {s: v.get() for s, v in self.result_names.items()}
        return configured_result_folder(RESULTS_DIR, cfg, section)

    def open_results(self, section):
        try:
            self.app.open_folder(self.output_folder(section))
        except ValueError as exc:
            messagebox.showerror('Invalid folder name', str(exc), parent=self)

    def _sync_synergy_controls(self)->None:
        pair_on=bool(self.pair_synergy.get())
        self.pair_method_box.configure(state="readonly" if pair_on else "disabled")
        self.pair_threshold_entry.configure(state="normal" if pair_on and self._selection_code(self.pair_selection)!="all" else "disabled")
        if not pair_on:
            self.triple_synergy.set(False)
        self.triple_box.configure(state="normal" if pair_on else "disabled")
        triple_on=pair_on and bool(self.triple_synergy.get())
        self.triple_method_box.configure(state="readonly" if triple_on else "disabled")
        self.triple_threshold_entry.configure(state="normal" if triple_on and self._selection_code(self.triple_selection)!="all" else "disabled")

    def load(self,cfg:dict[str,Any])->None:
        self.run.load(cfg["run"]); self.plot_by.set(cfg["plotter"]["plot_by"]); a=cfg["analysis"]; self.analysis_side.set(a["side"])
        legacy_method=a.get("selection_method","raw"); legacy_threshold=a.get("synergy_threshold",a.get("alpha",.05))
        pair_method=a.get("pair_selection_method",legacy_method); triple_method=a.get("triple_selection_method",legacy_method)
        pair_threshold=float(a.get("pair_synergy_threshold",legacy_threshold)); triple_threshold=float(a.get("triple_synergy_threshold",legacy_threshold))
        if pair_threshold>=1 and pair_method!="all": pair_method="all"
        if triple_threshold>=1 and triple_method!="all": triple_method="all"
        self.pair_selection.set(self.selection_labels.get(pair_method,self.selection_labels["raw"]))
        self.triple_selection.set(self.selection_labels.get(triple_method,self.selection_labels["raw"]))
        self.pair_threshold.set(str(0.05 if pair_method=="all" and pair_threshold>=1 else pair_threshold))
        self.triple_threshold.set(str(0.05 if triple_method=="all" and triple_threshold>=1 else triple_threshold))
        self.top_n.set(str(a["top_n"])); self.pair_synergy.set(bool(a.get("pair_synergy",True))); self.triple_synergy.set(bool(a.get("triple_synergy",True))); self._sync_synergy_controls()

    def commit(self,cfg:dict[str,Any])->None:
        pair_method=self._selection_code(self.pair_selection); triple_method=self._selection_code(self.triple_selection)
        for section in FOLDERS:
            self.output_folder(section)
        cfg['result_names'] = {s: v.get().strip() for s, v in self.result_names.items()}
        pair_threshold=0.05 if pair_method=="all" else _parse_float(self.pair_threshold.get(),"Pair synergy p threshold")
        triple_threshold=0.05 if triple_method=="all" else _parse_float(self.triple_threshold.get(),"Triple synergy p threshold")
        cfg["run"]=self.run.dump(); cfg["plotter"]["plot_by"]=self.plot_by.get(); cfg["analysis"]={"side":self.analysis_side.get(),"selection_method":pair_method,"synergy_threshold":pair_threshold,"pair_selection_method":pair_method,"pair_synergy_threshold":pair_threshold,"triple_selection_method":triple_method,"triple_synergy_threshold":triple_threshold,"pair_synergy":bool(self.pair_synergy.get()),"triple_synergy":bool(self.triple_synergy.get()),"top_n":_parse_int(self.top_n.get(),"Label top joiner combinations")}

    def plot_results(self)->None:
        try:
            folder = self.output_folder('lead_troop')
        except ValueError as exc:
            messagebox.showerror('Invalid folder name', str(exc), parent=self); return
        self.app.run_script("kingshot_lead_troop_plotter.py",["--plot-by",self.plot_by.get(), '--experiment-folder', str(folder)])

    def analyze(self)->None:
        try:
            folder = self.output_folder('joiner')
        except ValueError as exc:
            messagebox.showerror('Invalid folder name', str(exc), parent=self); return
        pair_method=self._selection_code(self.pair_selection); triple_method=self._selection_code(self.triple_selection)
        pair_threshold="0.05" if pair_method=="all" else self.pair_threshold.get(); triple_threshold="0.05" if triple_method=="all" else self.triple_threshold.get()
        csv=folder/"kingshot_winrates.csv"; args=[str(csv),"--side",self.analysis_side.get(),"--pair-selection-method",pair_method,"--pair-synergy-threshold",pair_threshold,"--triple-selection-method",triple_method,"--triple-synergy-threshold",triple_threshold,"--top-n",self.top_n.get(),"--existing-output","overwrite",("--pair-synergy" if self.pair_synergy.get() else "--no-pair-synergy"),("--triple-synergy" if self.triple_synergy.get() else "--no-triple-synergy")]; self.app.run_script("kingshot_full_model_analysis.py",args)

    def append_log(self,text:str)->None:
        self.log.configure(state="normal"); self.log.insert("end",text); self.log.see("end"); self.log.configure(state="disabled")

class KingshotApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.signature = _add_window_signature(self)
        self.title("Kingshot Experiment Manager")
        self.geometry("1180x820")
        self.minsize(1180, 680)
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        try:
            self.config_data = load_config(ROOT, create_if_missing=True)
            self.hero_catalog = load_hero_catalog(ROOT, self.config_data)
        except Exception as exc:
            messagebox.showerror("Startup error", str(exc))
            self.config_data = default_config(); self.hero_catalog = {}
        self.all_heroes = _grouped_hero_names(self.hero_catalog)
        self.hero_dropdown_all = _grouped_hero_values(self.hero_catalog)
        self.hero_choices = {
            role: _grouped_hero_names(
                self.hero_catalog,
                [name for name, info in self.hero_catalog.items() if info.get("type") == ROLE_TYPES[role]],
            )
            for role in ("inf", "cav", "arch")
        }
        self.hero_dropdown_choices = {
            role: _grouped_hero_values(self.hero_catalog, self.hero_choices[role])
            for role in ("inf", "cav", "arch")
        }

        top = ttk.Frame(self); top.pack(fill="x", padx=12, pady=(10, 0))
        ttk.Label(top, text="Kingshot Experiment Manager", font=("TkDefaultFont", 15, "bold")).pack(side="left")
        self.status = tk.StringVar(value=f"Configuration: {ROOT / CONFIG_FILENAME}")
        ttk.Label(top, textvariable=self.status, foreground="#555").pack(side="right")

        self.notebook = ttk.Notebook(self); self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 28))
        self.profiles_tab = ProfilesTab(self, self.notebook)
        self.lead_tab = LeadTroopTab(self, self.notebook)
        self.joiner_tab = JoinerTab(self, self.notebook)
        self.run_tab = RunTab(self, self.notebook)
        self.notebook.add(self.profiles_tab, text="1. Players & bonuses")
        self.notebook.add(self.lead_tab, text="2. Lead + troops")
        self.notebook.add(self.joiner_tab, text="3. Joiners")
        self.notebook.add(self.run_tab, text="4. Run & analyze")
        self.load_into_ui()
        self.after(100, self._drain_output)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def import_previous(self, section):
        if self.process and self.process.poll() is None:
            messagebox.showinfo('Experiment running', 'Wait for the current task to finish before importing settings.', parent=self)
            return
        try:
            folders = previous_runs(RESULTS_DIR, section)
        except OSError as exc:
            messagebox.showerror('Could not list previous runs', str(exc), parent=self); return
        if not folders:
            messagebox.showinfo('No previous runs', f'No saved settings found in\n{RESULTS_DIR / FOLDERS[section]}', parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title('Import from previous — ' + ('Joiner' if section == 'joiner' else 'Lead + Troops'))
        dialog.transient(self); dialog.geometry('650x380')
        ttk.Label(dialog, text=f'Saved runs in {RESULTS_DIR / FOLDERS[section]}', wraplength=610).pack(anchor='w', padx=12, pady=12)
        frame = ttk.Frame(dialog); frame.pack(fill='both', expand=True, padx=12)
        listing = tk.Listbox(frame, exportselection=False)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=listing.yview)
        listing.configure(yscrollcommand=scroll.set)
        listing.pack(side='left', fill='both', expand=True); scroll.pack(side='right', fill='y')
        base = RESULTS_DIR / FOLDERS[section]
        for folder in folders:
            listing.insert('end', '(Default folder)' if folder == base else folder.name)
        listing.selection_set(0)
        def accept(_event=None):
            chosen = listing.curselection()
            if not chosen:
                return
            folder = folders[chosen[0]]
            old = None
            try:
                old = copy.deepcopy(self.commit_ui())
                cfg, notes = import_run(folder, section, old, ROOT)
                self.config_data = cfg
                self.load_into_ui()
                self.run_tab.result_names[section].set(DEFAULT_FOLDER_LABEL if folder == base else folder.name)
            except Exception as exc:
                if old is not None:
                    self.config_data = old
                    self.load_into_ui()
                messagebox.showerror('Could not import settings', str(exc), parent=dialog)
                return
            dialog.destroy()
            self.status.set(f'Imported {FOLDERS[section]} settings from {folder}')
            if notes:
                messagebox.showwarning('Previous settings imported — review needed', '\n\n'.join(notes), parent=self)
        buttons = ttk.Frame(dialog); buttons.pack(fill='x', padx=12, pady=12)
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right')
        ttk.Button(buttons, text='Import', command=accept).pack(side='right', padx=6)
        listing.bind('<Double-1>', accept)
        dialog.grab_set(); listing.focus_set()

    def read_last_run_stats(self, *, show_error: bool = False) -> dict[str, Any] | None:
        path = LAST_RUN_STATS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("snapshot is not a JSON object")
            for side in ("attacker", "defender"):
                rows = data.get(side, {}).get("final_stats")
                if not isinstance(rows, dict):
                    raise ValueError(f"snapshot is missing {side} final_stats")
            return data
        except Exception as exc:
            if show_error:
                messagebox.showinfo(
                    "No last-run stats",
                    f"No completed-run stat snapshot is available yet.\n\n{path}\n\n{exc}",
                    parent=self,
                )
            return None

    def reload_saved_tab(self, section: str) -> None:
        """Reload only tab 1, 2, or 3 from the configuration currently saved on disk."""
        try:
            saved = load_config(ROOT, create_if_missing=False)
            if section == "profiles":
                self.profiles_tab.load(saved)
                label = "Players & bonuses"
            elif section == "lead_troop":
                self.lead_tab.load(saved)
                label = "Lead + troops"
            elif section == "joiner":
                self.joiner_tab.load(saved)
                label = "Joiners"
            else:
                raise ValueError(f"Unknown settings section: {section}")
            self.status.set(f"Reloaded {label} from saved configuration: {ROOT / CONFIG_FILENAME}")
        except Exception as exc:
            messagebox.showerror("Could not load saved settings", str(exc), parent=self)

    def load_into_ui(self) -> None:
        self.profiles_tab.load(self.config_data); self.lead_tab.load(self.config_data); self.joiner_tab.load(self.config_data); self.run_tab.load(self.config_data)

    def commit_ui(self) -> dict[str, Any]:
        cfg = copy.deepcopy(self.config_data)
        self.profiles_tab.commit(cfg); self.lead_tab.commit(cfg); self.joiner_tab.commit(cfg); self.run_tab.commit(cfg)
        self.config_data = cfg
        return cfg

    def save(self, *, quiet: bool = False) -> bool:
        try:
            cfg = self.commit_ui(); path = save_config(ROOT, cfg)
            self.status.set(f"Saved: {path}")
            if not quiet: messagebox.showinfo("Saved", f"Configuration saved to\n{path}", parent=self)
            return True
        except Exception as exc:
            messagebox.showerror("Could not save", str(exc), parent=self); return False

    def validate(self, *, quiet: bool = False) -> bool:
        try:
            cfg = self.commit_ui()
            errors, warnings = validate_config(ROOT, cfg)
        except Exception as exc:
            messagebox.showerror("Validation error", str(exc), parent=self); return False
        if errors:
            messagebox.showerror("Configuration needs attention", "\n\n".join(errors), parent=self); return False
        if warnings and not quiet:
            messagebox.showwarning("Configuration valid with warning", "\n\n".join(warnings), parent=self)
        elif not quiet:
            messagebox.showinfo("Configuration valid", "No configuration errors were found.", parent=self)
        return True

    def run_script(self, filename: str, args: list[str] | None = None) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Task already running", "Stop or wait for the current task before starting another.", parent=self); return
        if not self.save(quiet=True) or not self.validate(quiet=True):
            return
        script = APP_DIR / filename
        if not script.is_file():
            messagebox.showerror("Missing script", f"Could not find {script}", parent=self); return
        args = args or []
        self.run_tab.append_log(f"\n{'='*72}\nRunning: {script.name} {' '.join(args)}\n{'='*72}\n")
        cmd = [sys.executable, "-u", str(script), *args]
        kwargs: dict[str, Any] = {"cwd": str(ROOT), "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "text": True, "bufsize": 1}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.process = subprocess.Popen(cmd, **kwargs)
        except Exception as exc:
            messagebox.showerror("Could not start", str(exc), parent=self); return
        threading.Thread(target=self._reader_thread, args=(self.process,), daemon=True).start()

    def _reader_thread(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self.output_queue.put(line)
        code = process.wait(); self.output_queue.put(f"\n[Task finished with exit code {code}]\n")

    def _drain_output(self) -> None:
        try:
            while True:
                line = self.output_queue.get_nowait()
                self.run_tab.append_log(line)
                if line.startswith('KINGSHOT_ANALYSIS_NOTICE:'):
                    try:
                        notice = json.loads(line.split(':', 1)[1])
                    except (ValueError, TypeError):
                        continue
                    if isinstance(notice, str):
                        messagebox.showwarning('Synergy analysis unavailable', notice, parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain_output)

    def stop_process(self) -> None:
        if self.process and self.process.poll() is None:
            if messagebox.askyesno("Stop task", "Stop the currently running task?", parent=self):
                self.process.terminate(); self.run_tab.append_log("\n[Stop requested]\n")
        else:
            messagebox.showinfo("No task running", "There is no active task.", parent=self)

    def open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt": os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin": subprocess.Popen(["open", str(path)])
            else: subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Could not open folder", str(exc), parent=self)

    def _close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("Task running", "A task is still running. Close the manager and stop it?", parent=self):
                return
            self.process.terminate()
        self.destroy()


if __name__ == "__main__":
    app = KingshotApp()
    app.mainloop()
