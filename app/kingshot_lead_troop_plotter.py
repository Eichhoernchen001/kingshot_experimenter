#!/usr/bin/env python3
"""Plot lead/troop experiment results. See README.txt for startup details."""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import re
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# =============================================================================
# CONFIGURATION
# =============================================================================

SCRIPT_FOLDER = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_FOLDER.parent if SCRIPT_FOLDER.name == "app" else SCRIPT_FOLDER
PLOT_BY = "defender"  # default; kingshot_config.json / --plot-by can override
WINRATE_SIDE = 'attacker'

# Keep this script standalone when copied into a results folder. The app copy
# and the results copy both find the top-level config without extra imports.
for _cfg_path in (
    SCRIPT_FOLDER / "kingshot_config.json",
    SCRIPT_FOLDER.parent / "kingshot_config.json",
    SCRIPT_FOLDER.parent.parent / "kingshot_config.json",
):
    if _cfg_path.is_file():
        try:
            _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
            PLOT_BY = _cfg.get("plotter", {}).get("plot_by", PLOT_BY)
            WINRATE_SIDE = _cfg.get('plotter', {}).get('winrate_side', 'defender' if PLOT_BY == 'attacker' else 'attacker')
        except Exception:
            pass
        break
PLOT_BY = 'defender' if WINRATE_SIDE == 'attacker' else 'attacker'
SHOW_50_PERCENT_REFERENCE = False
SHOW_MEAN_LABELS = False
FIGURE_WIDTH = 13.5
FIGURE_HEIGHT = 7.5
DPI = 180
HATCH_PATTERNS = ("", "///", "\\\\", "xxx", "...", "+++", "ooo", "***", "---", "OO")

# =============================================================================
# END CONFIGURATION
# =============================================================================

CSV_FILENAME = "kingshot_lead_troop_winrates.csv"
DEFAULT_EXPERIMENT_FOLDER = PACKAGE_ROOT / "results" / "lead_troop_experiment"
if (SCRIPT_FOLDER / CSV_FILENAME).is_file():
    EXPERIMENT_FOLDER = SCRIPT_FOLDER
elif (DEFAULT_EXPERIMENT_FOLDER / CSV_FILENAME).is_file():
    EXPERIMENT_FOLDER = DEFAULT_EXPERIMENT_FOLDER
elif (PACKAGE_ROOT / "lead_troop_experiment" / CSV_FILENAME).is_file():
    EXPERIMENT_FOLDER = PACKAGE_ROOT / "lead_troop_experiment"
else:
    EXPERIMENT_FOLDER = DEFAULT_EXPERIMENT_FOLDER
INPUT_CSV = EXPERIMENT_FOLDER / CSV_FILENAME
OUTPUT_FOLDER = EXPERIMENT_FOLDER

REQUIRED_COLUMNS = [
    "defense_scenario", "attack_formation",
    "infantry_pct_atk", "cavalry_pct_atk", "archers_pct_atk",
    "infantry_pct_def", "cavalry_pct_def", "archers_pct_def",
    "winrate",
]


def _fmt_pct(value) -> str:
    value = float(value)
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:g}"


def _split_label(row, side: str) -> str:
    return "/".join(
        _fmt_pct(row[f"{kind}_pct_{side}"])
        for kind in ("infantry", "cavalry", "archers")
    )


def _safe_filename(text: str) -> str:
    text = re.sub(r"[^\w.-]+", "_", str(text).strip())
    return re.sub(r"_+", "_", text).strip("_")


def _validate(df: pd.DataFrame) -> None:
    if PLOT_BY not in {"attacker", "defender"}:
        raise ValueError("PLOT_BY must be 'attacker' or 'defender'.")
    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"Input CSV is missing required column(s): {missing}")
    if df.empty:
        raise ValueError("Input CSV contains no rows.")
    if df["winrate"].isna().any():
        raise ValueError("Input CSV contains missing winrate values.")
    if not ((df["winrate"] >= 0) & (df["winrate"] <= 100)).all():
        raise ValueError("winrate must be between 0 and 100.")


def _add_split_labels(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    result["attacker_troop_split"] = result.apply(lambda row: _split_label(row, "atk"), axis=1)
    result["defender_troop_split"] = result.apply(lambda row: _split_label(row, "def"), axis=1)
    if "attack_troop_range" not in result:
        result["attack_troop_range"] = 1
    if "defense_troop_range" not in result:
        result["defense_troop_range"] = 1
    return result


def _condition_summary(df: pd.DataFrame) -> pd.DataFrame:
    group_cols = [
        "defense_scenario", "attack_formation",
        "infantry_pct_atk", "cavalry_pct_atk", "archers_pct_atk",
        "infantry_pct_def", "cavalry_pct_def", "archers_pct_def",
        "attack_troop_range", "defense_troop_range",
    ]
    summary = (
        df.groupby(group_cols, dropna=False, sort=False)["winrate"]
        .agg(n_batches="count", mean_winrate="mean", sd_winrate="std")
        .reset_index()
    )
    summary["se_winrate"] = summary["sd_winrate"] / np.sqrt(summary["n_batches"])
    return _add_split_labels(summary)


def _perspective_fields() -> dict[str, str]:
    if PLOT_BY == "defender":
        return {
            "fixed_formation": "defense_scenario",
            "fixed_split": "defender_troop_split",
            "x_formation": "attack_formation",
            "variant_split": "attacker_troop_split",
            "variant_range": "attack_troop_range",
            "fixed_label": "Defense",
            "x_label": "Attacker lead formation",
            "legend_title": "Attacker troop split\nInf/Cav/Arch",
        }
    return {
        "fixed_formation": "attack_formation",
        "fixed_split": "attacker_troop_split",
        "x_formation": "defense_scenario",
        "variant_split": "defender_troop_split",
        "variant_range": "defense_troop_range",
        "fixed_label": "Attack",
        "x_label": "Defender lead formation",
        "legend_title": "Defender troop split\nInf/Cav/Arch",
    }


def _saved_design(folder):
    snapshot = folder / 'run_configuration.json'
    if snapshot.is_file():
        cfg = json.loads(snapshot.read_text(encoding='utf-8-sig')).get('configuration', {})
        return cfg, cfg.get('lead_troop', {})
    settings = folder / 'kingshot_lead_troop_winrates_experiment_settings.json'
    if settings.is_file():
        data = json.loads(settings.read_text(encoding='utf-8-sig'))
        cfg = data.get('configuration', {})
        return cfg, cfg.get('lead_troop', data.get('formation_settings', {}))
    return {}, {}


def saved_player_names(folder):
    settings = folder / 'kingshot_lead_troop_winrates_experiment_settings.json'
    names = json.loads(settings.read_text(encoding='utf-8-sig')).get('player_names', {}) if settings.is_file() else {}
    cfg, design = _saved_design(folder)
    assignment = design.get('assignment', {'attacker': 'A', 'defender': 'B'})
    return {side: names.get(side) or cfg.get('profiles', {}).get(assignment[side], {}).get('name') or side.title()
            for side in ('attacker', 'defender')}


def player_matchup_title(folder: Path) -> str:
    names = saved_player_names(folder)
    return f"{names['attacker']} (attacker) vs {names['defender']} (defender)"


def _formation_label(form):
    return ' + '.join(str(v.get('name', '') if isinstance(v, dict) else v)
                      for v in (form['leads'][r] for r in ('inf', 'cav', 'arch')))


def _formation_order(observed, design, side):
    saved = [_formation_label(f) for f in design.get(side, {}).get('formations', [])]
    return list(dict.fromkeys([n for n in saved if n in observed] + list(observed)))


def _sequence_styles(df, fields, design, side):
    """A sequence has one global hatch; a split has a color within that sequence.

    Local range IDs may restart for each hero squad. Full saved ranges keep styles
    stable in partial results; CSV-only plots reconstruct them from observed rows.
    """
    formation, group, split = (fields[k] for k in ('x_formation', 'variant_range', 'variant_split'))
    sequences = {}
    for form in design.get(side, {}).get('formations', []):
        name = _formation_label(form)
        variants = form.get('troop_variants_pct', [])
        groups = form.get('troop_variant_groups', [1] * len(variants))
        for number, ratios in zip(groups, variants):
            label = '/'.join(_fmt_pct(x) for x in ratios)
            sequences.setdefault((name, int(number)), set()).add(label)
    for name, number, label in df[[formation, group, split]].itertuples(index=False, name=None):
        sequences.setdefault((name, int(number)), set()).add(label)
    def split_key(label):
        return tuple(float(x) for x in label.split('/'))
    signatures = {key: tuple(sorted(values, key=split_key, reverse=True)) for key, values in sequences.items()}
    # Stable under formation reordering; no dependency on hero names or local IDs.
    unique = sorted(set(signatures.values()), key=lambda seq: tuple(split_key(x) for x in seq), reverse=True)
    cmap = plt.get_cmap('tab10')
    allocated = {}; pattern = 0
    def hatch(index):
        if index == 0: return ''
        return HATCH_PATTERNS[1 + (index - 1) % (len(HATCH_PATTERNS) - 1)] * (1 + (index - 1) // (len(HATCH_PATTERNS) - 1))
    for seq in unique:
        for i, label in enumerate(seq):
            allocated[seq, label] = (cmap(i % 10), hatch(pattern + i // 10))
        pattern += max(1, (len(seq) + 9) // 10)
    styles = {(name, number, label): allocated[seq, label]
              for (name, number), seq in signatures.items() for label in seq}
    return styles


def _stored_winrate_side(df, path=None):
    if 'winrate_side' in df.columns:
        values=set(df['winrate_side'].dropna().astype(str).str.strip())
        if len(values)!=1 or not values.issubset({'attacker','defender'}):
            raise ValueError('CSV must contain one consistent attacker/defender winrate_side.')
        return next(iter(values))
    if path is not None:
        settings=Path(path).with_name(Path(path).stem+'_experiment_settings.json')
        if settings.is_file():
            side=json.loads(settings.read_text(encoding='utf-8-sig')).get('winrate_side')
            if side in ('attacker','defender'):return side
    return None


def make_plots(df: pd.DataFrame, output_folder: Path) -> tuple[pd.DataFrame, int]:
    global PLOT_BY, WINRATE_SIDE
    output_folder.mkdir(parents=True, exist_ok=True)
    stored=_stored_winrate_side(df, INPUT_CSV)
    if stored is not None:WINRATE_SIDE=stored
    else:
        counts={side:len(df[[formation,*[f'{unit}_pct_{short}' for unit in ('infantry','cavalry','archers')]]].drop_duplicates())
                for side,short,formation in [('attacker','atk','attack_formation'),('defender','def','defense_scenario')]}
        if counts['attacker']==1 and counts['defender']>1:WINRATE_SIDE='defender'
        elif counts['defender']==1 and counts['attacker']>1:WINRATE_SIDE='attacker'
    if WINRATE_SIDE not in ('attacker', 'defender'):raise ValueError('Unknown win-rate side.')
    PLOT_BY = 'defender' if WINRATE_SIDE == 'attacker' else 'attacker'
    df = df.copy()
    if stored is None and WINRATE_SIDE=='defender':df['winrate']=100.0-df['winrate']
    df = _add_split_labels(df)
    summary = _condition_summary(df)
    summary['winrate_side'] = WINRATE_SIDE
    summary.to_csv(output_folder / "condition_summary.csv", index=False)

    fields = _perspective_fields()
    fixed_formation = fields["fixed_formation"]
    fixed_split = fields["fixed_split"]
    x_formation = fields["x_formation"]
    variant_split = fields["variant_split"]
    variant_range = fields["variant_range"]

    _, design = _saved_design(Path(INPUT_CSV).parent)
    names = saved_player_names(Path(INPUT_CSV).parent)
    split_styles = _sequence_styles(df, fields, design, WINRATE_SIDE)
    style_order = list(dict.fromkeys(df[[x_formation, variant_range, variant_split]].itertuples(index=False, name=None)))
    fields['legend_title'] = f"{names[WINRATE_SIDE]} ({WINRATE_SIDE}) troop split\nInf/Cav/Arch"

    plot_keys = list(dict.fromkeys(zip(df[fixed_formation], df[fixed_split])))

    for plot_number, (fixed_name, fixed_troops) in enumerate(plot_keys, start=1):
        mask = (df[fixed_formation] == fixed_name) & (df[fixed_split] == fixed_troops)
        d = df.loc[mask].copy()
        smask = (summary[fixed_formation] == fixed_name) & (summary[fixed_split] == fixed_troops)
        s = summary.loc[smask].copy()

        x_order = _formation_order(list(dict.fromkeys(d[x_formation].tolist())), design, WINRATE_SIDE)
        x_centers = np.arange(len(x_order), dtype=float)
        variants_by_x = {
            x_name: list(dict.fromkeys(zip(
                d.loc[d[x_formation] == x_name, variant_range].astype(int),
                d.loc[d[x_formation] == x_name, variant_split],
            )))
            for x_name in x_order
        }
        max_variants = max(len(values) for values in variants_by_x.values())
        group_width = 0.78
        bar_width = group_width / max_variants

        fig, ax = plt.subplots(figsize=(FIGURE_WIDTH, FIGURE_HEIGHT))
        used_splits = set()

        for x_center, x_name in zip(x_centers, x_order):
            variants = variants_by_x[x_name]
            offsets = (np.arange(len(variants)) - (len(variants) - 1) / 2) * bar_width
            for offset, key in zip(offsets, variants):
                group, split = key
                style_key = (x_name, group, split)
                color, hatch = split_styles[style_key]
                row = s[(s[x_formation] == x_name) & (s[variant_split] == split) & (s[variant_range].astype(int) == group)]
                if len(row) != 1:
                    raise ValueError(
                        f"Expected one summarized condition for {x_name} / {split}, found {len(row)}."
                    )
                row = row.iloc[0]
                xpos = x_center + offset
                mean = float(row["mean_winrate"])
                se = float(row["se_winrate"]) if pd.notna(row["se_winrate"]) else 0.0

                ax.bar(
                    xpos, mean, width=bar_width * 0.90,
                    color=color, hatch=hatch,
                    edgecolor="black", linewidth=0.6, zorder=2,
                )
                ax.errorbar(
                    xpos, mean, yerr=se, fmt="none", ecolor="black",
                    elinewidth=1.3, capsize=4, capthick=1.3, zorder=5,
                )

                values = d.loc[
                    (d[x_formation] == x_name) & (d[variant_split] == split) & (d[variant_range].astype(int) == group), "winrate"
                ].to_numpy(dtype=float)
                jitter = (
                    np.array([0.0]) if len(values) == 1
                    else np.linspace(-bar_width * 0.25, bar_width * 0.25, len(values))
                )
                ax.scatter(
                    xpos + jitter, values, s=27, facecolor="white",
                    edgecolor="black", linewidth=0.7, zorder=6,
                )
                if SHOW_MEAN_LABELS:
                    ax.text(
                        xpos, min(98.0, mean + se + 2.0), f"{mean:.1f}%",
                        ha="center", va="bottom", fontsize=8.5,
                        fontweight="bold", zorder=7,
                    )
                used_splits.add(style_key)

        if SHOW_50_PERCENT_REFERENCE:
            ax.axhline(50, color="black", linestyle="--", linewidth=1.0, zorder=1)

        ax.set_ylim(0, 100)
        ax.set_ylabel(f"{WINRATE_SIDE.capitalize()} win rate (%)")
        ax.set_xlabel(fields["x_label"])
        ax.set_xticks(x_centers)
        ax.set_xticklabels(x_order, rotation=18, ha="right")
        ax.set_title(
            player_matchup_title(Path(INPUT_CSV).parent) + '\n' + f"{fields['fixed_label']}: {fixed_troops} ({fixed_name})",
            fontsize=14, pad=14,
        )
        ax.grid(axis="y", alpha=0.20, linewidth=0.7, zorder=0)

        legend_handles = []
        legend_labels = []
        seen_styles = set()
        for key in style_order:
            _, group, split = key
            color, hatch = split_styles[key]
            identity = (split, color, hatch)
            if key in used_splits and identity not in seen_styles:
                seen_styles.add(identity)
                legend_handles.append(Patch(facecolor=color, hatch=hatch, edgecolor="black"))
                legend_labels.append(f"{split}%")
        legend_handles.extend([
            Line2D([], [], marker="o", linestyle="None", markerfacecolor="white",
                   markeredgecolor="black", markersize=6),
            Line2D([], [], marker="_", linestyle="None", color="black",
                   markersize=12, markeredgewidth=1.5),
        ])
        legend_labels.extend(["individual batch", "mean ± SE"])
        if SHOW_50_PERCENT_REFERENCE:
            legend_handles.append(Line2D([], [], color="black", linestyle="--", linewidth=1.0))
            legend_labels.append("50% win rate")

        legend_columns = max(1, (len(legend_labels) + 19) // 20)
        figure_width = FIGURE_WIDTH + 2.0 * (legend_columns - 1)
        fig.set_size_inches(figure_width, FIGURE_HEIGHT)
        ax.legend(
            legend_handles, legend_labels, title=fields["legend_title"], ncol=legend_columns,
            bbox_to_anchor=(1.02, 1), loc="upper left", borderaxespad=0,
            fontsize=9, title_fontsize=9.5,
        )
        fig.tight_layout(rect=[0, 0, 1 - 2.3 * legend_columns / figure_width, 1])

        filename = (
            f"{WINRATE_SIDE}_winrate_{PLOT_BY}_view_{plot_number:02d}_"
            f"{_safe_filename(fixed_troops)}_{_safe_filename(fixed_name)}.png"
        )
        fig.savefig(output_folder / filename, dpi=DPI, bbox_inches="tight")
        plt.close(fig)

    return summary, len(plot_keys)


def main() -> None:
    global PLOT_BY, WINRATE_SIDE, EXPERIMENT_FOLDER, INPUT_CSV, OUTPUT_FOLDER
    parser = argparse.ArgumentParser(description="Plot Kingshot lead/troop experiment results.")
    parser.add_argument("--plot-by", choices=("attacker", "defender"), default=None)
    parser.add_argument('--winrate-side', choices=('attacker', 'defender'), default=None,
                        help='Whose win rate to plot; fixes the opposite side in each chart.')
    parser.add_argument('--experiment-folder', type=Path, default=None)
    args = parser.parse_args()
    if args.experiment_folder is not None:
        EXPERIMENT_FOLDER = args.experiment_folder
        INPUT_CSV = EXPERIMENT_FOLDER / CSV_FILENAME
        OUTPUT_FOLDER = EXPERIMENT_FOLDER
    if args.winrate_side is not None:
        WINRATE_SIDE = args.winrate_side
        if args.plot_by == WINRATE_SIDE:
            parser.error('--plot-by must be the opposite side to --winrate-side.')
    elif args.plot_by is not None:
        WINRATE_SIDE = 'defender' if args.plot_by == 'attacker' else 'attacker'
    PLOT_BY = 'defender' if WINRATE_SIDE == 'attacker' else 'attacker'

    print(f"Plotted win rate: {WINRATE_SIDE}; fixed side per chart: {PLOT_BY}")
    print(f"Experiment folder: {EXPERIMENT_FOLDER.resolve()}")
    print(f"Input CSV: {INPUT_CSV.resolve()}")
    print(f"Plot output: {OUTPUT_FOLDER.resolve()}")
    print()

    if not INPUT_CSV.is_file():
        raise FileNotFoundError(f"Could not find the lead/troop result CSV at: {INPUT_CSV.resolve()}")

    df = pd.read_csv(INPUT_CSV)
    _validate(df)
    summary, plot_count = make_plots(df, OUTPUT_FOLDER)

    print(f"Input rows: {len(df):,}")
    print(f"Unique conditions: {len(summary):,}")
    print(f"{PLOT_BY.capitalize()}-fixed plots created: {plot_count}")
    print(f"Output folder: {OUTPUT_FOLDER.resolve()}")
    print()
    print(summary[[
        "defense_scenario", "defender_troop_split",
        "attack_formation", "attacker_troop_split",
        "n_batches", "mean_winrate", "se_winrate",
    ]].to_string(index=False))


if __name__ == "__main__":
    main()
