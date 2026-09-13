#!/usr/bin/env python3
"""
Kingshot full hero-composition analysis
=======================================

Given one or more Kingshot simulator CSV files, this script produces:

1. Predicted vs observed plots for three requested models
   - additive + duplicate only
   - additive + duplicate + all pair synergies
   - additive + duplicate + all pair synergies + selected triple synergies
   - top-N model-predicted lineups marked and listed in a right-side legend
   - title reports the model name, attacker/defender leads, troop percentages,
     and total simulations per tested lineup

2. Individual hero contribution plot
   - additive + duplicate model
   - adjusted for teammates
   - duplicate points are exact incremental values of that added copy
   - plots every observed/estimable copy increment by default (up to 4 in a four-slot squad)

3. Full pair-synergy plot
   - all pair interactions from the full pair model
   - CI 95%
   - raw / Holm / FDR significance columns in the CSV

4. Triple-synergy plot and table
   - triple effects come from a full pair + triple model
   - pair zero-sum and constituent-pair triple zero-sum constraints are used
     automatically for fixed-size squads with distinct heroes
   - for fixed four-slot designs with duplicates, a remaining one-dimensional
     common triple offset is resolved by constraining the mean triple synergy
     to zero (equivalently, the sum of all triple coefficients is zero)
   - CI 95%
   - raw / Holm / FDR significance columns in the CSV

Important identifiability handling
----------------------------------
When every squad contains the same number of DISTINCT heroes (for example all
C(9,4)=126 squads), hero main effects and all pair interactions are not
simultaneously identifiable without a convention. In that case the script
automatically uses the standard zero-sum interaction convention:

    for every hero H:
    sum of all pair-synergy coefficients involving H = 0

This makes pair synergies interpretable as deviations from each hero's average
pair compatibility, while the additive hero terms absorb average partner value.

Usage
-----
python kingshot_full_model_analysis.py data.csv

Multiple files can be combined:
python kingshot_full_model_analysis.py run1.csv run2.csv --out combined_analysis

Options:
--side auto|attacker|defender   (default: auto)
--trials-per-row 100
--top-n 10
--max-copy-plot N              optional cap; by default plot all observed copies
--pair-selection-method all|raw|fdr|holm
--pair-synergy-threshold 0.05   pair threshold; ignored when method=all
--triple-selection-method all|raw|fdr|holm
--triple-synergy-threshold 0.05 triple threshold; ignored when method=all
--selection-method / --synergy-threshold remain legacy common aliases
--pair-synergy / --no-pair-synergy
--triple-synergy / --no-triple-synergy
--attacker-json PATH            optional battle-profile JSON
--defender-json PATH            optional battle-profile JSON

If the JSON arguments are omitted, the script looks next to the first input
CSV for common names such as attacker_data_squirrel.json / attacker_data.json
and defender_data_squirrel.json / defender_data.json. These JSONs are used only
to document the battle setup in analysis_summary.txt.

Side handling:
- attacker: model the attacker joiner composition and attacker win rate
- defender: model the defender joiner composition and reverse the response so
  the modeled win rate is defender win probability = 100 - attacker win rate
- auto: choose the side with more unique hero lineups; ties fall back to attacker
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path
from kingshot_paths import JSON_DIR

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import statsmodels.api as sm
from scipy import stats
from scipy.linalg import null_space
from statsmodels.stats.multitest import multipletests


Z95 = 1.959963984540054


@dataclass
class FitResult:
    params: pd.Series
    covariance: np.ndarray
    prediction: np.ndarray
    dispersion: float
    deviance: float
    pearson_chi2: float
    df_resid: int
    constrained: bool
    constraint_description: str


def detect_side_slots(df: pd.DataFrame, side: str) -> list[str]:
    """Return the four joiner columns for the requested side, in slot order."""
    if side not in {"attacker", "defender"}:
        raise ValueError(f"Unknown side {side!r}; expected 'attacker' or 'defender'.")

    suffix = "_atk" if side == "attacker" else "_def"
    slots = [
        c for c in df.columns
        if c.startswith("joiner") and c.endswith(suffix)
    ]

    if not slots:
        raise ValueError(
            f"No joiner columns for {side} were found "
            f"(expected columns such as joiner1{suffix})."
        )

    def key(c):
        digits = "".join(ch for ch in c if ch.isdigit())
        return int(digits) if digits else 999

    slots = sorted(slots, key=key)

    if len(slots) != 4:
        raise ValueError(
            f"Expected exactly 4 {side} joiner columns, found {len(slots)}: {slots}"
        )

    return slots


def canonical_lineup(row: pd.Series, slots: list[str]) -> tuple[str, ...]:
    return tuple(sorted(str(row[c]) for c in slots))


def _unique_lineup_count(df: pd.DataFrame, side: str) -> int:
    slots = detect_side_slots(df, side)
    keys = df.apply(lambda r: canonical_lineup(r, slots), axis=1)
    return int(keys.nunique())


def choose_analysis_side(frames: list[pd.DataFrame], requested_side: str) -> tuple[str, int, int]:
    """
    Resolve --side.

    auto chooses the side with more unique hero compositions across all input
    rows. This captures the common experiment design where one side is fixed and
    the other side is varied. If both sides have the same number of unique
    compositions, attacker is used as a deterministic fallback and a warning is
    printed.
    """
    if requested_side not in {"auto", "attacker", "defender"}:
        raise ValueError(
            f"Unknown --side value {requested_side!r}; use auto, attacker, or defender."
        )

    combined = pd.concat(frames, ignore_index=True)
    n_attacker = _unique_lineup_count(combined, "attacker")
    n_defender = _unique_lineup_count(combined, "defender")

    if requested_side == "auto":
        if n_defender > n_attacker:
            chosen = "defender"
        elif n_attacker > n_defender:
            chosen = "attacker"
        else:
            chosen = "attacker"
            warnings.warn(
                "AUTO side detection found the same number of unique attacker "
                f"and defender lineups ({n_attacker} each). Falling back to "
                "attacker. Specify --side attacker or --side defender to choose "
                "explicitly.",
                RuntimeWarning,
            )
    else:
        chosen = requested_side

    return chosen, n_attacker, n_defender


def load_files(paths: list[Path], trials_per_row: int, requested_side: str):
    """
    Load one or more joiner-experiment CSVs.

    The simulator CSV stores attacker win rate. If defender is selected, the
    modeled response is reversed to defender win probability:

        defender win rate = 100 - attacker win rate

    Consequently every reported prediction, hero contribution, and synergy is
    from the perspective of the selected side.
    """
    frames = []
    attacker_slots0 = None
    defender_slots0 = None

    # First pass: load/validate files so AUTO can compare both sides globally.
    for path in paths:
        df = pd.read_csv(path).copy()

        if "winrate" not in df.columns:
            raise ValueError(f"{path}: missing 'winrate' column")

        attacker_slots = detect_side_slots(df, "attacker")
        defender_slots = detect_side_slots(df, "defender")

        if attacker_slots0 is None:
            attacker_slots0 = attacker_slots
            defender_slots0 = defender_slots
        else:
            if attacker_slots != attacker_slots0:
                raise ValueError(
                    "Attacker joiner-slot columns differ across input files:\n"
                    f"{attacker_slots0}\nvs\n{attacker_slots}"
                )
            if defender_slots != defender_slots0:
                raise ValueError(
                    "Defender joiner-slot columns differ across input files:\n"
                    f"{defender_slots0}\nvs\n{defender_slots}"
                )

        if not np.allclose(df["winrate"], np.round(df["winrate"])):
            raise ValueError(
                f"{path}: winrate contains non-integer percentages. "
                "This script reconstructs wins from integer percentages."
            )

        frames.append(df)

    chosen_side, n_attacker_lineups, n_defender_lineups = choose_analysis_side(
        frames, requested_side
    )

    slots = attacker_slots0 if chosen_side == "attacker" else defender_slots0

    processed = []
    for path, df in zip(paths, frames):
        # Preserve the simulator's original attacker-perspective win rate.
        df["attacker_winrate"] = df["winrate"].astype(float)

        if chosen_side == "defender":
            df["analysis_winrate"] = 100.0 - df["attacker_winrate"]
        else:
            df["analysis_winrate"] = df["attacker_winrate"]

        df["wins"] = np.rint(
            df["analysis_winrate"] * trials_per_row / 100.0
        ).astype(int)
        df["trials"] = int(trials_per_row)
        df["source_file"] = path.name
        df["lineup_key"] = df.apply(
            lambda r: canonical_lineup(r, slots), axis=1
        )
        processed.append(df)

    raw = pd.concat(processed, ignore_index=True)
    heroes = sorted(pd.unique(raw[slots].to_numpy().ravel()).tolist())

    rows = []
    for key, g in raw.groupby("lineup_key", sort=False):
        row = {
            "lineup_key": key,
            "wins": int(g["wins"].sum()),
            "trials": int(g["trials"].sum()),
            "mean_winrate": float(100 * g["wins"].sum() / g["trials"].sum()),
            "n_batches": int(len(g)),
        }

        for c in slots:
            row[c] = g[c].iloc[0]

        for h in heroes:
            row[f"n_{h}"] = key.count(h)

        rows.append(row)

    cond = pd.DataFrame(rows)
    cond["p_observed"] = cond["wins"] / cond["trials"]

    return (
        raw,
        cond,
        heroes,
        slots,
        chosen_side,
        n_attacker_lineups,
        n_defender_lineups,
    )


def choose_reference(heroes: list[str]) -> str:
    return "Petra" if "Petra" in heroes else heroes[-1]


def build_base_design(cond: pd.DataFrame, heroes: list[str]):
    reference = choose_reference(heroes)
    others = [h for h in heroes if h != reference]

    X = pd.DataFrame(index=cond.index)
    X["const"] = 1.0

    # One hero count omitted because total squad size is fixed.
    for h in others:
        X[f"n_{h}"] = cond[f"n_{h}"].astype(float)

    duplicate_terms = []
    for h in heroes:
        max_copy = int(cond[f"n_{h}"].max())
        for copy_no in range(2, max_copy + 1):
            col = f"copy{copy_no}_{h}"
            X[col] = (cond[f"n_{h}"] >= copy_no).astype(float)
            duplicate_terms.append((h, copy_no, col))

    return X, reference, others, duplicate_terms


def add_pair_terms(cond: pd.DataFrame, heroes: list[str], X_base: pd.DataFrame):
    X = X_base.copy()
    pairs = list(itertools.combinations(heroes, 2))

    for a, b in pairs:
        X[f"pair_{a}_{b}"] = (
            (cond[f"n_{a}"] > 0) & (cond[f"n_{b}"] > 0)
        ).astype(float)

    return X, pairs



def add_triple_terms(cond: pd.DataFrame, heroes: list[str], X_pair: pd.DataFrame):
    """Add all unordered three-hero presence interactions."""
    triples = list(itertools.combinations(heroes, 3))
    data = {}
    for a, b, c in triples:
        data[f"triple_{a}_{b}_{c}"] = (
            (cond[f"n_{a}"] > 0)
            & (cond[f"n_{b}"] > 0)
            & (cond[f"n_{c}"] > 0)
        ).astype(float)

    if data:
        X = pd.concat([X_pair, pd.DataFrame(data, index=cond.index)], axis=1)
    else:
        X = X_pair.copy()
    return X, triples


def hierarchical_pair_triple_constraint_matrix(
    heroes: list[str],
    pairs: list[tuple[str, str]],
    triples: list[tuple[str, str, str]],
    columns: list[str],
):
    """
    Hierarchical zero-sum constraints for fixed-size distinct-hero squads.

    Pair layer:
      for every hero H, sum of pair coefficients involving H = 0

    Triple layer:
      for every pair (A,B), sum of triple coefficients containing A and B = 0

    The second constraint makes each triple effect a deviation beyond its three
    lower-order pair relationships, while preserving a hierarchical ANOVA-like
    parameterization.
    """
    rows = []

    for hero in heroes:
        row = np.zeros(len(columns))
        for a, b in pairs:
            if hero in (a, b):
                row[columns.index(f"pair_{a}_{b}")] = 1.0
        rows.append(row)

    for a, b in pairs:
        row = np.zeros(len(columns))
        for x, y, z in triples:
            if a in (x, y, z) and b in (x, y, z):
                row[columns.index(f"triple_{x}_{y}_{z}")] = 1.0
        rows.append(row)

    return np.vstack(rows) if rows else np.zeros((0, len(columns)))


def can_use_hierarchical_pair_triple_constraints(
    cond: pd.DataFrame,
    heroes: list[str],
    X: pd.DataFrame,
    pairs: list[tuple[str, str]],
    triples: list[tuple[str, str, str]],
):
    squad_sizes = cond[[f"n_{h}" for h in heroes]].sum(axis=1)
    no_duplicates = all(int(cond[f"n_{h}"].max()) <= 1 for h in heroes)
    fixed_size = squad_sizes.nunique() == 1

    if not (no_duplicates and fixed_size):
        return False, None

    R = hierarchical_pair_triple_constraint_matrix(
        heroes, pairs, triples, list(X.columns)
    )
    B = null_space(R)
    rank_reduced = np.linalg.matrix_rank(np.asarray(X, float) @ B)
    if rank_reduced != B.shape[1]:
        return False, None
    return True, R


def fit_binomial_linear_constraints(
    cond: pd.DataFrame,
    X: pd.DataFrame,
    R: np.ndarray,
    description: str,
) -> FitResult:
    """Fit beta subject to R beta = 0 through beta = B theta."""
    Xarr = np.asarray(X, dtype=float)
    B = null_space(R)
    X_reduced = Xarr @ B

    endog = np.column_stack([
        cond["wins"],
        cond["trials"] - cond["wins"],
    ])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = sm.GLM(
            endog,
            X_reduced,
            family=sm.families.Binomial(link=sm.families.links.Identity()),
        ).fit()

    theta = np.asarray(res.params)
    beta = B @ theta
    pred = Xarr @ beta

    if pred.min() <= 0 or pred.max() >= 1:
        raise RuntimeError(
            "Constrained identity-link model predicted outside (0,1)."
        )

    dispersion = max(1.0, res.pearson_chi2 / res.df_resid)
    cov_theta = np.asarray(res.cov_params()) * dispersion
    cov_beta = B @ cov_theta @ B.T

    return FitResult(
        params=pd.Series(beta, index=X.columns),
        covariance=cov_beta,
        prediction=pred,
        dispersion=float(dispersion),
        deviance=float(res.deviance),
        pearson_chi2=float(res.pearson_chi2),
        df_resid=int(res.df_resid),
        constrained=True,
        constraint_description=description,
    )


def global_triple_zero_sum_constraint_matrix(
    triples: list[tuple[str, str, str]],
    columns: list[str],
) -> np.ndarray:
    """
    One symmetric identification constraint for the triple layer:

        sum(all triple coefficients) = 0

    In a fixed four-slot design that includes duplicate-containing squads, the
    complete additive + duplicate + pair + triple presence model can retain one
    structural degree of non-identifiability: all triple effects may be shifted
    by the same constant while compensating lower-order terms.  Setting the
    average triple synergy to zero fixes only that common offset and preserves
    every estimable contrast among triples.
    """
    row = np.zeros(len(columns))
    for a, b, c in triples:
        row[columns.index(f"triple_{a}_{b}_{c}")] = 1.0
    return row.reshape(1, -1)


def can_use_global_triple_zero_sum_constraint(
    cond: pd.DataFrame,
    heroes: list[str],
    X: pd.DataFrame,
    triples: list[tuple[str, str, str]],
):
    """
    Detect the expected single structural alias of a fixed four-slot design.

    The constraint is accepted only when:
      * every squad has exactly four hero slots,
      * the unconstrained design is deficient by exactly one rank, and
      * imposing sum(triples)=0 makes the reduced design full column rank.

    The final rank check is the decisive safeguard; if the dataset has any
    additional non-identifiability, this function refuses to hide it.
    """
    if not triples:
        return False, None

    squad_sizes = cond[[f"n_{h}" for h in heroes]].sum(axis=1)
    fixed_four = squad_sizes.nunique() == 1 and int(round(float(squad_sizes.iloc[0]))) == 4
    if not fixed_four:
        return False, None

    Xarr = np.asarray(X, dtype=float)
    rank = np.linalg.matrix_rank(Xarr)
    if X.shape[1] - rank != 1:
        return False, None

    R = global_triple_zero_sum_constraint_matrix(triples, list(X.columns))
    B = null_space(R)
    rank_reduced = np.linalg.matrix_rank(Xarr @ B)
    if rank_reduced != B.shape[1]:
        return False, None

    return True, R


def can_use_minimal_triple_layer_constraints(
    X: pd.DataFrame,
    triples: list[tuple[str, str, str]],
):
    """
    Construct the smallest possible identification constraint set entirely in
    the triple layer.

    This is a fallback for mixed fixed-size designs where lower-order terms
    (main/duplicate/pair effects) are already estimable, but adding the full
    triple layer creates several exact aliases.  Let N span null(X).  If the
    triple rows of N retain all null directions, constraining the triple
    coefficient vector to be orthogonal to those directions supplies exactly
    `n_columns - rank(X)` constraints and discards no estimable dimension.

    Equivalently, among coefficient vectors giving identical fitted values,
    this chooses the one with minimum Euclidean norm in the non-identifiable
    triple directions.  The final rank checks are decisive safeguards.
    """
    if not triples:
        return False, None

    Xarr = np.asarray(X, dtype=float)
    rank = np.linalg.matrix_rank(Xarr)
    deficiency = X.shape[1] - rank
    if deficiency <= 0:
        return False, None

    N = null_space(Xarr)
    if N.shape[1] != deficiency:
        return False, None

    triple_indices = [
        list(X.columns).index(f"triple_{a}_{b}_{c}")
        for a, b, c in triples
        if f"triple_{a}_{b}_{c}" in X.columns
    ]
    if not triple_indices:
        return False, None

    N_triple = N[triple_indices, :]
    if np.linalg.matrix_rank(N_triple) != deficiency:
        return False, None

    R = np.zeros((deficiency, X.shape[1]))
    R[:, triple_indices] = N_triple.T

    if np.linalg.matrix_rank(R) != deficiency:
        return False, None

    B = null_space(R)
    # Minimal identification only: the constrained parameter dimension must
    # equal the estimable rank of the original design, and all of it must remain.
    if B.shape[1] != rank:
        return False, None
    if np.linalg.matrix_rank(Xarr @ B) != rank:
        return False, None

    return True, R


def fit_full_triple_model(
    cond: pd.DataFrame,
    heroes: list[str],
    X_full: pd.DataFrame,
    pairs: list[tuple[str, str]],
    triples: list[tuple[str, str, str]],
) -> FitResult:
    """Fit all main, duplicate, pair, and triple terms simultaneously."""
    rank = np.linalg.matrix_rank(np.asarray(X_full, dtype=float))
    if rank == X_full.shape[1]:
        return fit_binomial_direct(cond, X_full)

    okay, R = can_use_hierarchical_pair_triple_constraints(
        cond, heroes, X_full, pairs, triples
    )
    if okay:
        return fit_binomial_linear_constraints(
            cond,
            X_full,
            R,
            (
                "hierarchical zero-sum interactions: pair coefficients sum to zero "
                "for each hero; triple coefficients sum to zero for each constituent pair"
            ),
        )

    # Mixed distinct/duplicate four-slot designs can be fully informative for
    # additive, duplicate, and pair effects yet retain exactly one structural
    # ambiguity in the absolute triple level.  Resolve only that common offset
    # with the symmetric convention mean(triple synergy) = 0.
    okay, R = can_use_global_triple_zero_sum_constraint(
        cond, heroes, X_full, triples
    )
    if okay:
        return fit_binomial_linear_constraints(
            cond,
            X_full,
            R,
            (
                "global zero-sum triple interactions: the sum (and therefore "
                "the mean) of all triple-synergy coefficients is constrained "
                "to zero; this resolves the single common triple-offset alias "
                "of the fixed four-slot design"
            ),
        )

    # More complicated mixed duplicate/distinct designs can create several
    # aliases only after the triple layer is added.  Identify those aliases with
    # the minimum number of triple-only constraints, while retaining every
    # estimable degree of freedom in the original design.
    okay, R = can_use_minimal_triple_layer_constraints(X_full, triples)
    if okay:
        return fit_binomial_linear_constraints(
            cond,
            X_full,
            R,
            (
                f"minimal triple-layer identification: {R.shape[0]} exact "
                "triple-only constraints remove the non-identifiable triple "
                "directions without discarding any estimable model dimension"
            ),
        )

    raise RuntimeError(
        f"Full pair + triple model is rank-deficient: rank {rank} for "
        f"{X_full.shape[1]} columns, and the automatic identification schemes "
        "could not resolve it without discarding estimable information."
    )


def triple_effect_table(
    full_fit: FitResult,
    triples: list[tuple[str, str, str]],
):
    names = list(full_fit.params.index)
    rows = []

    for a, b, c in triples:
        L = np.zeros(len(names))
        L[names.index(f"triple_{a}_{b}_{c}")] = 1.0
        r = linear_contrast(full_fit, L)
        rows.append({
            "triple": f"{a} + {b} + {c}",
            "hero_a": a,
            "hero_b": b,
            "hero_c": c,
            **r,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    reject_holm, p_holm, _, _ = multipletests(out["p_value"], method="holm")
    reject_fdr, p_fdr, _, _ = multipletests(out["p_value"], method="fdr_bh")
    out["p_holm"] = p_holm
    out["significant_holm_0_05"] = reject_holm
    out["p_fdr_bh"] = p_fdr
    out["significant_fdr_0_05"] = reject_fdr
    return out.sort_values("effect_pp", ascending=False).reset_index(drop=True)


def significance_mask(table: pd.DataFrame, method: str, threshold: float) -> pd.Series:
    # "all" explicitly keeps every estimable interaction. Threshold >= 1 is
    # retained as a legacy alias for v1.15/v1.16 configurations.
    if method == "all" or threshold >= 1:
        return pd.Series(True, index=table.index, dtype=bool)
    if method == "raw":
        return table["p_value"] < threshold
    if method == "fdr":
        return table["p_fdr_bh"] < threshold
    if method == "holm":
        return table["p_holm"] < threshold
    raise ValueError(f"Unknown significance method: {method}")


def _threshold_tag(threshold: float) -> str:
    return "all" if threshold >= 1 else f"{threshold:g}".replace(".", "p")


def _method_threshold_tag(method: str, threshold: float) -> str:
    return "all" if method == "all" else f"{method}_{_threshold_tag(threshold)}"


def _settings_tag(
    pair_synergy: bool,
    triple_synergy: bool,
    pair_method: str,
    pair_threshold: float,
    triple_method: str | None = None,
    triple_threshold: float | None = None,
) -> str:
    # Four-argument calls retain the v1.13 filename convention for compatibility.
    if triple_method is None and triple_threshold is None:
        if not pair_synergy:
            return "no_synergy"
        layer = "pair_triple" if triple_synergy else "pair"
        return f"{layer}_{_method_threshold_tag(pair_method, pair_threshold)}"
    if not pair_synergy:
        return "no_synergy"
    triple_method = triple_method or pair_method
    triple_threshold = pair_threshold if triple_threshold is None else triple_threshold
    pair_tag = f"pair_{_method_threshold_tag(pair_method, pair_threshold)}"
    if not triple_synergy:
        return pair_tag
    return f"{pair_tag}_triple_{_method_threshold_tag(triple_method, triple_threshold)}"


def pair_name(pair: tuple[str, str]) -> str:
    return f"pair_{pair[0]}_{pair[1]}"


def triple_name(triple: tuple[str, str, str]) -> str:
    return f"triple_{triple[0]}_{triple[1]}_{triple[2]}"


def build_selected_pair_design(
    X_base: pd.DataFrame,
    X_pair_full: pd.DataFrame,
    selected_pairs: list[tuple[str, str]],
):
    cols = list(X_base.columns) + [pair_name(p) for p in selected_pairs]
    return X_pair_full.loc[:, cols].copy()


def build_selected_pair_triple_design(
    X_base: pd.DataFrame,
    X_triple_full: pd.DataFrame,
    selected_pairs: list[tuple[str, str]],
    selected_triples: list[tuple[str, str, str]],
):
    """
    Build the selected pair + triple model using strong hierarchy.

    Every selected triple automatically brings in its three constituent pair
    terms, even when one of those parent pairs was not itself significant.
    """
    pair_set = set(selected_pairs)
    for a, b, c in selected_triples:
        pair_set.update({
            tuple(sorted((a, b))),
            tuple(sorted((a, c))),
            tuple(sorted((b, c))),
        })

    hierarchy_pairs = sorted(pair_set)
    added_for_hierarchy = sorted(set(hierarchy_pairs) - set(selected_pairs))
    cols = (
        list(X_base.columns)
        + [pair_name(p) for p in hierarchy_pairs]
        + [triple_name(t) for t in selected_triples]
    )
    return X_triple_full.loc[:, cols].copy(), hierarchy_pairs, added_for_hierarchy


def fit_selected_model(cond: pd.DataFrame, X: pd.DataFrame, label: str) -> FitResult:
    rank = np.linalg.matrix_rank(np.asarray(X, dtype=float))
    if rank != X.shape[1]:
        raise RuntimeError(
            f"{label} is rank-deficient (rank {rank}/{X.shape[1]}). "
            "The selected interaction set is not independently estimable."
        )
    return fit_binomial_direct(cond, X)


def fit_metrics(cond: pd.DataFrame, fit: FitResult) -> dict[str, float]:
    observed = cond["p_observed"].to_numpy()
    pred = fit.prediction
    rmse = 100 * np.sqrt(np.mean((observed - pred) ** 2))
    mae = 100 * np.mean(np.abs(observed - pred))
    gof_p = stats.chi2.sf(fit.deviance, fit.df_resid)
    return {
        "rmse_pp": float(rmse),
        "mae_pp": float(mae),
        "deviance": float(fit.deviance),
        "residual_df": int(fit.df_resid),
        "pearson_dispersion": float(fit.dispersion),
        "gof_p": float(gof_p),
    }


def fit_binomial_direct(cond: pd.DataFrame, X: pd.DataFrame) -> FitResult:
    endog = np.column_stack([
        cond["wins"],
        cond["trials"] - cond["wins"],
    ])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = sm.GLM(
            endog,
            X,
            family=sm.families.Binomial(link=sm.families.links.Identity()),
        ).fit()

    pred = np.asarray(res.predict(X), dtype=float)
    if pred.min() <= 0 or pred.max() >= 1:
        raise RuntimeError(
            "Identity-link binomial model predicted outside (0,1). "
            "A logit-link implementation would be required for this dataset."
        )

    dispersion = max(1.0, res.pearson_chi2 / res.df_resid)
    cov = res.cov_params().to_numpy() * dispersion

    return FitResult(
        params=res.params.copy(),
        covariance=cov,
        prediction=pred,
        dispersion=float(dispersion),
        deviance=float(res.deviance),
        pearson_chi2=float(res.pearson_chi2),
        df_resid=int(res.df_resid),
        constrained=False,
        constraint_description="none",
    )


def synergy_zero_sum_constraint_matrix(
    heroes: list[str],
    pairs: list[tuple[str, str]],
    columns: list[str],
):
    """
    R beta = 0, one row per hero:
      sum of pair coefficients involving that hero = 0
    """
    R = np.zeros((len(heroes), len(columns)))

    for i, hero in enumerate(heroes):
        for a, b in pairs:
            if hero in (a, b):
                R[i, columns.index(f"pair_{a}_{b}")] = 1.0

    return R


def can_use_zero_sum_synergy_constraints(
    cond: pd.DataFrame,
    heroes: list[str],
    X: pd.DataFrame,
    pairs: list[tuple[str, str]],
):
    # Intended for fixed-size squads with no duplicate heroes.
    squad_sizes = cond[[f"n_{h}" for h in heroes]].sum(axis=1)
    no_duplicates = all(int(cond[f"n_{h}"].max()) <= 1 for h in heroes)
    fixed_size = squad_sizes.nunique() == 1

    if not (no_duplicates and fixed_size):
        return False, None

    R = synergy_zero_sum_constraint_matrix(heroes, pairs, list(X.columns))
    B = null_space(R)

    # The constraint must fully resolve the rank deficiency, not add extra
    # unidentified directions.
    rank_reduced = np.linalg.matrix_rank(np.asarray(X, float) @ B)

    if rank_reduced != B.shape[1]:
        return False, None

    return True, R


def fit_binomial_zero_sum_synergies(
    cond: pd.DataFrame,
    X: pd.DataFrame,
    R: np.ndarray,
) -> FitResult:
    """
    Fit beta subject to R beta = 0 by reparameterizing beta = B theta,
    where columns of B span null(R).
    """
    Xarr = np.asarray(X, dtype=float)
    B = null_space(R)
    X_reduced = Xarr @ B

    endog = np.column_stack([
        cond["wins"],
        cond["trials"] - cond["wins"],
    ])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = sm.GLM(
            endog,
            X_reduced,
            family=sm.families.Binomial(link=sm.families.links.Identity()),
        ).fit()

    theta = np.asarray(res.params)
    beta = B @ theta
    pred = Xarr @ beta

    if pred.min() <= 0 or pred.max() >= 1:
        raise RuntimeError(
            "Constrained identity-link model predicted outside (0,1)."
        )

    dispersion = max(1.0, res.pearson_chi2 / res.df_resid)

    cov_theta = np.asarray(res.cov_params()) * dispersion
    cov_beta = B @ cov_theta @ B.T

    return FitResult(
        params=pd.Series(beta, index=X.columns),
        covariance=cov_beta,
        prediction=pred,
        dispersion=float(dispersion),
        deviance=float(res.deviance),
        pearson_chi2=float(res.pearson_chi2),
        df_resid=int(res.df_resid),
        constrained=True,
        constraint_description=(
            "zero-sum pair interactions: for each hero, the pair coefficients "
            "involving that hero sum to zero"
        ),
    )


def fit_full_model(
    cond: pd.DataFrame,
    heroes: list[str],
    X_full: pd.DataFrame,
    pairs: list[tuple[str, str]],
) -> FitResult:
    rank = np.linalg.matrix_rank(np.asarray(X_full, dtype=float))

    if rank == X_full.shape[1]:
        return fit_binomial_direct(cond, X_full)

    okay, R = can_use_zero_sum_synergy_constraints(
        cond, heroes, X_full, pairs
    )

    if okay:
        return fit_binomial_zero_sum_synergies(cond, X_full, R)

    raise RuntimeError(
        f"Full pair model is rank-deficient: rank {rank} for "
        f"{X_full.shape[1]} columns, and the automatic zero-sum synergy "
        "parameterization does not fully resolve it. This design needs a sparse "
        "interaction model or a custom constraint scheme."
    )


def linear_contrast(fit: FitResult, L: np.ndarray):
    beta = fit.params.to_numpy()
    est = float(L @ beta)
    se = float(np.sqrt(max(0.0, L @ fit.covariance @ L)))
    z = est / se if se > 0 else np.nan

    return {
        "effect_pp": 100 * est,
        "se_pp": 100 * se,
        "ci_low": 100 * (est - Z95 * se),
        "ci_high": 100 * (est + Z95 * se),
        "p_value": 2 * stats.norm.sf(abs(z)) if np.isfinite(z) else np.nan,
    }


def centered_first_copy_L(
    fit: FitResult,
    heroes: list[str],
    others: list[str],
    reference: str,
    hero: str,
):
    names = list(fit.params.index)
    k = len(heroes)
    L = np.zeros(len(names))

    for h in others:
        L[names.index(f"n_{h}")] -= 1.0 / k

    if hero != reference:
        L[names.index(f"n_{hero}")] += 1.0

    return L


def hero_effect_table(
    base_fit: FitResult,
    heroes,
    others,
    reference,
    duplicate_terms,
):
    """
    Hero contributions deliberately come from the additive + duplicate model,
    matching the earlier Kingshot contribution plots.
    """
    names = list(base_fit.params.index)
    dup_lookup = {(h, n): col for h, n, col in duplicate_terms}

    rows = []

    for hero in heroes:
        L1 = centered_first_copy_L(
            base_fit, heroes, others, reference, hero
        )
        r1 = linear_contrast(base_fit, L1)
        rows.append({
            "hero": hero,
            "copy": 1,
            "term": f"{hero} #1",
            **r1,
        })

        max_copy = max(
            [n for h, n, _ in duplicate_terms if h == hero],
            default=1,
        )

        for copy_no in range(2, max_copy + 1):
            L = L1.copy()
            L[names.index(dup_lookup[(hero, copy_no)])] += 1.0
            r = linear_contrast(base_fit, L)

            rows.append({
                "hero": hero,
                "copy": copy_no,
                "term": f"{hero} #{copy_no}",
                **r,
            })

    return pd.DataFrame(rows)


def synergy_effect_table(
    full_fit: FitResult,
    pairs: list[tuple[str, str]],
):
    names = list(full_fit.params.index)
    rows = []

    for a, b in pairs:
        L = np.zeros(len(names))
        L[names.index(f"pair_{a}_{b}")] = 1.0
        r = linear_contrast(full_fit, L)

        rows.append({
            "pair": f"{a} + {b}",
            "hero_a": a,
            "hero_b": b,
            **r,
        })

    out = pd.DataFrame(rows)

    reject_holm, p_holm, _, _ = multipletests(
        out["p_value"], method="holm"
    )
    reject_fdr, p_fdr, _, _ = multipletests(
        out["p_value"], method="fdr_bh"
    )

    out["p_holm"] = p_holm
    out["significant_holm_0_05"] = reject_holm
    out["p_fdr_bh"] = p_fdr
    out["significant_fdr_0_05"] = reject_fdr

    return out.sort_values("effect_pp", ascending=False).reset_index(drop=True)


def lineup_label(key: tuple[str, ...], heroes: list[str]) -> str:
    vals = list(key)
    out = []

    for h in heroes:
        out.extend([h] * vals.count(h))

    return "/".join(out)


def simulation_depth_title(trials: pd.Series) -> str:
    """Simulation depth across modeled lineup tests, e.g. 100-300 simulations/test."""
    vals = sorted(set(int(x) for x in trials))
    if len(vals) == 1:
        return f"{vals[0]:,} simulations/test"
    return f"{min(vals):,}-{max(vals):,} simulations/test"


def _fmt_pct(value: float) -> str:
    value = float(value)
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _profile_plot_info(path: Path | None) -> dict | None:
    """Extract selected leads and troop percentages from a prepared profile JSON."""
    if path is None or not Path(path).is_file():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    selected = data.get("selectedHeroes")
    leads = [str(x) for x in selected] if isinstance(selected, list) else []

    quantities = {"inf": 0.0, "lanc": 0.0, "mark": 0.0}
    troop_alias = {
        "inf": "inf", "infantry": "inf",
        "lanc": "lanc", "cav": "lanc", "cavalry": "lanc",
        "mark": "mark", "arch": "mark", "archer": "mark",
        "archers": "mark", "marksman": "mark", "marksmen": "mark",
    }
    troops = data.get("troops")
    if isinstance(troops, list):
        for row in troops:
            if not isinstance(row, dict):
                continue
            key = troop_alias.get(str(row.get("type", "")).strip().casefold())
            if key is None:
                continue
            try:
                quantities[key] += float(row.get("quantity", 0))
            except (TypeError, ValueError):
                pass

    total = sum(quantities.values())
    percentages = None
    if total > 0:
        percentages = (
            100.0 * quantities["inf"] / total,
            100.0 * quantities["lanc"] / total,
            100.0 * quantities["mark"] / total,
        )

    return {"leads": leads, "percentages": percentages}


def prediction_plot_context_lines(
    experiment_settings: list[tuple[Path, Path, dict]],
    attacker_json: Path | None,
    defender_json: Path | None,
    modeled_side: str,
) -> list[str]:
    """
    Build compact side-specific lead/troop lines for prediction-plot titles.

    The side whose joiner lineup is being modeled is shown first. Thus attacker
    appears first for attacker analyses and defender first for defender analyses.
    """
    contexts = []
    for _, _, data in experiment_settings:
        if not isinstance(data, dict):
            continue
        joiner = data.get("joiner_experiment")
        if not isinstance(joiner, dict):
            continue

        atk_leads = tuple(str(x) for x in joiner.get("attacker_lead_heroes", []) or [])
        def_leads = tuple(str(x) for x in joiner.get("defender_lead_heroes", []) or [])

        def pct_tuple(key):
            raw = joiner.get(key)
            if not isinstance(raw, dict):
                return None
            try:
                return (
                    float(raw["infantry"]),
                    float(raw["cavalry"]),
                    float(raw["archers"]),
                )
            except (KeyError, TypeError, ValueError):
                return None

        contexts.append(
            (
                atk_leads,
                def_leads,
                pct_tuple("attacker_troop_percentages"),
                pct_tuple("defender_troop_percentages"),
            )
        )

    mixed = False
    if contexts:
        first = contexts[0]
        mixed = any(c != first for c in contexts[1:])
        if mixed:
            atk_leads = def_leads = ()
            atk_pct = def_pct = None
        else:
            atk_leads, def_leads, atk_pct, def_pct = first
    else:
        atk = _profile_plot_info(attacker_json) or {}
        deff = _profile_plot_info(defender_json) or {}
        atk_leads = tuple(atk.get("leads", []) or [])
        def_leads = tuple(deff.get("leads", []) or [])
        atk_pct = atk.get("percentages")
        def_pct = deff.get("percentages")

    def side_line(label: str, leads, pct) -> str:
        if mixed:
            return f"{label}: mixed across input files"
        hero_text = "/".join(leads) if leads else "not available"
        if pct is None:
            pct_text = "not available"
        else:
            pct_text = "/".join(_fmt_pct(v) for v in pct)
        return f"{label}: {hero_text} – {pct_text}"

    atk_line = side_line("Attacker", atk_leads, atk_pct)
    def_line = side_line("Defender", def_leads, def_pct)
    if modeled_side == "defender":
        return [def_line, atk_line]
    return [atk_line, def_line]


def plot_prediction(
    cond,
    heroes,
    X_model,
    fit,
    out_dir,
    top_n,
    filename,
    model_name,
    context_lines,
    simulations_per_replicate,
):
    pred = fit.prediction

    Xarr = np.asarray(X_model, dtype=float)
    pred_var = np.einsum(
        "ij,jk,ik->i",
        Xarr,
        fit.covariance,
        Xarr,
    )
    pred_se = np.sqrt(np.maximum(pred_var, 0))
    avg_halfwidth_pp = float(np.mean(Z95 * pred_se * 100))

    res = cond.copy()
    res["predicted_pct"] = pred * 100
    res["observed_pct"] = res["p_observed"] * 100
    res["lineup"] = res["lineup_key"].apply(lambda k: lineup_label(k, heroes))
    res = res.sort_values("predicted_pct", ascending=False).reset_index(drop=True)
    res["rank"] = np.arange(1, len(res) + 1)

    top = res.head(min(top_n, len(res))).copy()
    rest = res.iloc[len(top):]
    lo = min(res["predicted_pct"].min(), res["observed_pct"].min())
    hi = max(res["predicted_pct"].max(), res["observed_pct"].max())

    fig, ax = plt.subplots(figsize=(14.5, 9.0))
    if len(rest):
        ax.scatter(rest["predicted_pct"], rest["observed_pct"], s=22, alpha=0.65)

    top_handles = []
    for _, r in top.iterrows():
        h = ax.plot(
            r["predicted_pct"],
            r["observed_pct"],
            marker="o",
            markersize=7,
            linestyle="None",
            label=f'{int(r["rank"]):>2} = {r["lineup"]}',
        )[0]
        top_handles.append(h)
        ax.annotate(
            str(int(r["rank"])),
            (r["predicted_pct"], r["observed_pct"]),
            xytext=(5, 4),
            textcoords="offset points",
            fontsize=9,
            fontweight="bold",
        )

    x = np.linspace(lo, hi, 400)
    ax.fill_between(x, x - avg_halfwidth_pp, x + avg_halfwidth_pp, color="gray", alpha=0.20)
    ax.plot(x, x, color="black")
    ax.set_xlabel("Model-predicted win rate (%)")
    ax.set_ylabel("Observed win rate (%)")
    title_lines = [
        "Model prediction vs observed results",
        f"Model: {model_name}",
        *context_lines,
        simulation_depth_title(cond["trials"]),
    ]
    ax.set_title("\n".join(title_lines), fontsize=12.5)

    agreement_handle = Line2D([], [], color="black", label="perfect agreement")
    ci_handle = Patch(color="gray", alpha=0.20, label="average CI 95%")
    handles = [agreement_handle, ci_handle] + top_handles
    ax.legend(
        handles=handles,
        labels=[h.get_label() for h in handles],
        bbox_to_anchor=(1.02, 1),
        loc="upper left",
        borderaxespad=0,
        fontsize=8.5,
        title=f"Top {len(top)} model-predicted lineups",
        title_fontsize=9.5,
    )

    fig.tight_layout(rect=[0, 0, 0.72, 1])
    fig.savefig(out_dir / filename, dpi=180)
    plt.close(fig)
    return res, top, avg_halfwidth_pp

def plot_heroes(hero_tbl, out_dir, max_copy_plot=None):
    hero_order = (
        hero_tbl.loc[hero_tbl["copy"] == 1]
        .sort_values("effect_pp", ascending=True)["hero"]
        .tolist()
    )
    ymap = {h: i for i, h in enumerate(hero_order)}

    fig, ax = plt.subplots(
        figsize=(10.0, max(6.0, 0.5 * len(hero_order) + 2.5))
    )

    d1 = hero_tbl.loc[hero_tbl["copy"] == 1].copy()
    y = np.array([ymap[h] for h in d1["hero"]], dtype=float) - 0.12
    x = d1["effect_pp"].to_numpy()
    err = np.vstack([
        x - d1["ci_low"].to_numpy(),
        d1["ci_high"].to_numpy() - x,
    ])

    ax.errorbar(
        x, y, xerr=err,
        fmt="o",
        capsize=4,
        linestyle="None",
        label="1st copy",
    )

    available_max_copy = int(hero_tbl["copy"].max()) if not hero_tbl.empty else 1
    plot_max_copy = (
        available_max_copy
        if max_copy_plot is None
        else min(int(max_copy_plot), available_max_copy)
    )

    for copy_no in range(2, plot_max_copy + 1):
        d = hero_tbl.loc[hero_tbl["copy"] == copy_no].copy()
        if d.empty:
            continue

        y = np.array(
            [ymap[h] for h in d["hero"]],
            dtype=float,
        ) + 0.12 + 0.12 * (copy_no - 2)

        x = d["effect_pp"].to_numpy()
        err = np.vstack([
            x - d["ci_low"].to_numpy(),
            d["ci_high"].to_numpy() - x,
        ])

        kwargs = {"color": "orange"} if copy_no == 2 else {}

        ax.errorbar(
            x, y, xerr=err,
            fmt="s" if copy_no == 2 else "D",
            capsize=4,
            linestyle="None",
            label=(
                "2nd copy (incremental value)"
                if copy_no == 2
                else f"copy #{copy_no} (incremental value)"
            ),
            **kwargs,
        )

    ax.axvline(0, color="black", linewidth=1)

    ax.set_yticks(
        [ymap[h] for h in hero_order],
        hero_order,
    )
    ax.set_xlabel(
        "Adjusted incremental contribution vs. average hero "
        "(percentage points)"
    )
    ax.set_ylabel("Hero")
    ax.set_title(
        "Contribution of individual hero's to the winrate",
        fontsize=14,
    )

    fig.text(
        0.5, 0.025,
        "Additive + duplicate model; contributions are adjusted for teammates. "
        "Duplicate points show the exact incremental value of that added copy. "
        "Error bars are CI 95%.",
        ha="center",
        fontsize=9,
    )

    ax.legend()
    fig.tight_layout(rect=[0, 0.07, 1, 1])

    fig.savefig(
        out_dir / "02_individual_hero_contributions.png",
        dpi=180,
    )
    plt.close(fig)


def plot_synergies(
    syn: pd.DataFrame,
    full_fit: FitResult,
    out_dir: Path,
    selection_method: str,
    threshold: float,
    filename: str,
):
    mask = significance_mask(syn, selection_method, threshold)
    d = syn.loc[mask].sort_values("effect_pp", ascending=True).reset_index(drop=True)
    if d.empty:
        return False

    labels = []
    for _, r in d.iterrows():
        suffix = ""
        if r["significant_holm_0_05"]:
            suffix = " **"
        elif r["significant_fdr_0_05"]:
            suffix = " *"
        labels.append(r["pair"] + suffix)

    y = np.arange(len(d))
    x = d["effect_pp"].to_numpy()
    err = np.vstack([x - d["ci_low"].to_numpy(), d["ci_high"].to_numpy() - x])
    fig, ax = plt.subplots(figsize=(10.5, max(7.0, 0.30 * len(d) + 3.0)))
    ax.errorbar(x, y, xerr=err, fmt="o", capsize=3, linestyle="None")
    ax.axvline(0, color="black", linewidth=1)
    ax.set_yticks(y, labels, fontsize=8)
    ax.set_xlabel("Pair interaction effect (percentage points)")
    ax.set_ylabel("Hero pair")
    parameterization = "Zero-sum interaction parameterization" if full_fit.constrained else "All pair terms fitted simultaneously"
    rule = "all estimable interactions" if selection_method == "all" or threshold >= 1 else f"{selection_method} p < {threshold:g}"
    ax.set_title(
        "Question: Which selected hero pairs show synergy or antagonism?\n"
        f"{parameterization}; {rule}; error bars are CI 95%",
        fontsize=14,
    )
    fig.text(0.5, 0.01, "* = significant at FDR 0.05; ** = significant at Holm 0.05", ha="center", fontsize=9)
    fig.tight_layout(rect=[0, 0.03, 1, 1])
    fig.savefig(out_dir / filename, dpi=180)
    plt.close(fig)
    return True


def plot_triple_synergies(
    triple_tbl: pd.DataFrame,
    out_dir: Path,
    selection_method: str,
    threshold: float,
    filename: str,
):
    """Plot only triple effects retained by the current selection rule."""
    if triple_tbl.empty:
        return False
    mask = significance_mask(triple_tbl, selection_method, threshold)
    d = triple_tbl.loc[mask].copy()
    if d.empty:
        return False

    d = d.sort_values("effect_pp", ascending=True).reset_index(drop=True)
    labels = []
    for _, r in d.iterrows():
        suffix = ""
        if r["significant_holm_0_05"]:
            suffix = " **"
        elif r["significant_fdr_0_05"]:
            suffix = " *"
        labels.append(r["triple"] + suffix)

    y = np.arange(len(d))
    x = d["effect_pp"].to_numpy()
    err = np.vstack([x - d["ci_low"].to_numpy(), d["ci_high"].to_numpy() - x])
    fig, ax = plt.subplots(figsize=(11.5, max(7.0, 0.38 * len(d) + 3.0)))
    ax.errorbar(x, y, xerr=err, fmt="o", capsize=3, linestyle="None")
    ax.axvline(0, color="black", linewidth=1)
    ax.set_yticks(y, labels, fontsize=8)
    ax.set_xlabel("Triple interaction effect (percentage points)")
    ax.set_ylabel("Hero triple")
    rule = "all estimable interactions" if selection_method == "all" or threshold >= 1 else f"{selection_method} p < {threshold:g}"
    ax.set_title(
        "Question: Which selected three-hero combinations show additional synergy or antagonism?\n"
        f"{rule}; error bars are CI 95%",
        fontsize=14,
    )
    fig.text(0.5, 0.01, "* = significant at FDR 0.05; ** = significant at Holm 0.05", ha="center", fontsize=9)
    fig.tight_layout(rect=[0, 0.03, 1, 1])
    fig.savefig(out_dir / filename, dpi=180)
    plt.close(fig)
    return True


def _next_numbered_folder(base: Path) -> Path:
    """Return base2, base3, ... using the first name that does not exist."""
    parent = base.parent
    name = base.name
    number = 2
    while True:
        candidate = parent / f"{name}{number}"
        if not candidate.exists():
            return candidate
        number += 1


def resolve_output_folder(base: Path, existing_action: str | None = None) -> Path | None:
    """Resolve an already-existing output directory, interactively or by policy."""
    if not base.exists():
        base.mkdir(parents=True, exist_ok=False)
        return base

    if not base.is_dir():
        raise RuntimeError(f"Output path already exists and is not a folder: {base}")

    print(f"Output folder already exists: {base}", flush=True)
    while True:
        if existing_action is not None:
            answer = existing_action.strip().lower()
        else:
            try:
                answer = input(
                    "Choose [o]verwrite existing files, [n]ew numbered folder, "
                    "or [q]uit: "
                ).strip().lower()
            except EOFError as exc:
                raise RuntimeError(
                    "The output folder already exists, but no interactive input is "
                    "available. Re-run in a terminal and choose overwrite/new, use "
                    "--existing-output new/overwrite, or specify a different --out folder."
                ) from exc
        if answer in {"o", "overwrite"}:
            print(f"Using existing output folder: {base}", flush=True)
            return base
        if answer in {"n", "new"}:
            candidate = _next_numbered_folder(base)
            candidate.mkdir(parents=True, exist_ok=False)
            print(f"Using new output folder: {candidate}", flush=True)
            return candidate
        if answer in {"q", "quit", "cancel"}:
            print("Analysis cancelled.", flush=True)
            return None

        if existing_action is not None:
            raise RuntimeError(f"Unknown --existing-output action: {existing_action!r}")
        print("Please enter o, n, or q.", flush=True)


def _copy_input_files(paths: list[Path], out_dir: Path) -> list[Path]:
    """Copy every input CSV into the output directory for reproducibility."""
    copied: list[Path] = []
    used_names: set[str] = set()

    for source in paths:
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Input CSV not found: {source}")

        target_name = source.name
        if target_name in used_names:
            stem = source.stem
            suffix = source.suffix
            i = 2
            while f"{stem}_{i}{suffix}" in used_names:
                i += 1
            target_name = f"{stem}_{i}{suffix}"

        used_names.add(target_name)
        target = out_dir / target_name

        # Avoid SameFileError if someone deliberately places the input inside
        # the output directory.
        try:
            same_file = target.exists() and source.samefile(target)
        except OSError:
            same_file = False

        if not same_file:
            shutil.copy2(source, target)
        copied.append(target)

    return copied


def _candidate_profile_json(side: str, csv_paths: list[Path]) -> tuple[Path | None, str | None]:
    """Best-effort auto-detection of the companion attacker/defender JSON."""
    if side not in {"attacker", "defender"}:
        raise ValueError(side)

    # The joiner experiment convention uses *_data_squirrel.json, whereas the
    # lead/troop experiment convention uses *_data.json. Prefer the convention
    # suggested by the first input CSV name, then fall back to the other.
    first_stem = csv_paths[0].stem.casefold() if csv_paths else ""
    if "lead_troop" in first_stem:
        exact_names = [
            f"{side}_data.json",
            f"{side}_data_squirrel.json",
            f"{side}.json",
        ]
    else:
        exact_names = [
            f"{side}_data_squirrel.json",
            f"{side}_data.json",
            f"{side}.json",
        ]

    search_dirs: list[Path] = []
    for csv_path in csv_paths:
        d = csv_path.resolve().parent
        if d not in search_dirs:
            search_dirs.append(d)
    cwd = Path.cwd().resolve()
    if cwd not in search_dirs:
        search_dirs.append(cwd)

    # Return the first exact conventional name in priority order. This keeps
    # mixed project folders convenient: kingshot_winrates.csv prefers the
    # joiner experiment's *_squirrel.json, while lead_troop CSVs prefer the
    # standard *_data.json profiles.
    for d in search_dirs:
        for name in exact_names:
            candidate = d / name
            if candidate.is_file():
                return candidate, None

    glob_matches: list[Path] = []
    for d in search_dirs:
        for p in sorted(d.glob(f"{side}_data*.json")):
            if p.is_file() and p not in glob_matches:
                glob_matches.append(p)

    if len(glob_matches) == 1:
        return glob_matches[0], None
    if len(glob_matches) > 1:
        return None, (
            f"multiple candidate {side} JSON files were found: "
            + ", ".join(str(p) for p in glob_matches)
            + f". Use --{side}-json to choose the correct one."
        )

    return None, (
        f"no companion {side} JSON was found. Use --{side}-json PATH to include "
        "lead heroes, troop levels, and stats in the summary."
    )


def resolve_profile_json(
    explicit: Path | None,
    side: str,
    csv_paths: list[Path],
) -> tuple[Path | None, str | None]:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{side.capitalize()} JSON not found: {path}")
        return path, None
    return _candidate_profile_json(side, csv_paths)


def _fmt_number(value) -> str:
    if value is None:
        return "not recorded"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if np.isfinite(x) and abs(x - round(x)) < 1e-10:
        return f"{int(round(x)):,}"
    return f"{x:g}"


def _unit_label(unit_type: str) -> str:
    return {
        "inf": "Infantry",
        "lanc": "Cavalry",
        "mark": "Archers",
    }.get(str(unit_type), str(unit_type))


def _skill_level_text(skill_levels) -> str:
    if not isinstance(skill_levels, dict) or not skill_levels:
        return "not recorded"

    def key_func(item):
        key = str(item[0])
        return (0, int(key)) if key.isdigit() else (1, key)

    return ", ".join(
        f"skill {key}={_fmt_number(value)}"
        for key, value in sorted(skill_levels.items(), key=key_func)
    )


def load_profile_metadata(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read battle-profile JSON {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Battle-profile JSON must contain one object: {path}")

    return data


def profile_summary_text(side: str, path: Path | None, note: str | None) -> str:
    title = side.upper()
    lines = [title]
    lines.append("-" * len(title))

    if path is None:
        lines.append(f"Metadata source: not available ({note})")
        return "\n".join(lines)

    data = load_profile_metadata(path)
    lines.append(f"Metadata source JSON: {path}")
    lines.append(
        "stats_include_heroes in source JSON: "
        + repr(data.get("stats_include_heroes", "not recorded"))
    )

    special = data.get("special_bonuses")
    if isinstance(special, dict):
        lines.append(
            "special_bonuses.includedInStats in source JSON: "
            + repr(special.get("includedInStats", "not recorded"))
        )
        lines.append("Special-bonus sections in source JSON:")
        for section in ("widgetLevels", "petLevels", "city", "appointment"):
            if section in special:
                lines.append(
                    f"- {section}: "
                    + json.dumps(special.get(section), ensure_ascii=False, sort_keys=True)
                )
    else:
        lines.append("special_bonuses in source JSON: not recorded")

    heroes = data.get("heroes", {})
    selected = data.get("selectedHeroes", [])
    if not isinstance(selected, list):
        selected = []
    if not isinstance(heroes, dict):
        heroes = {}

    lines.append("Lead heroes and skill levels:")
    if selected:
        for hero_name in selected:
            hero = heroes.get(hero_name, {}) if isinstance(heroes, dict) else {}
            if not isinstance(hero, dict):
                hero = {}
            unit = _unit_label(hero.get("type", "unknown"))
            skills = _skill_level_text(hero.get("skill_levels"))
            widget = hero.get("widget_level")
            widget_text = "" if widget is None else f"; widget level={_fmt_number(widget)}"
            hero_stats = hero.get("stats")
            hero_stats_text = ""
            if isinstance(hero_stats, dict):
                pieces = []
                for stat_name in ("attack", "defense", "lethality", "health"):
                    if stat_name in hero_stats:
                        pieces.append(f"{stat_name}={_fmt_number(hero_stats[stat_name])}")
                if pieces:
                    hero_stats_text = "; hero.stats: " + ", ".join(pieces)
            lines.append(
                f"- {hero_name} ({unit}): {skills}{widget_text}{hero_stats_text}"
            )
    else:
        lines.append("- not recorded in selectedHeroes")

    lines.append("Units used:")
    troops = data.get("troops", [])
    if isinstance(troops, list) and troops:
        for row in troops:
            if not isinstance(row, dict):
                continue
            unit = _unit_label(row.get("type", "unknown"))
            tier = row.get("tier")
            tg = row.get("fc_level", row.get("tg_level"))
            quantity = row.get("quantity")
            tier_text = f"T{_fmt_number(tier)}" if tier is not None else "T?"
            tg_text = f"TG{_fmt_number(tg)}" if tg is not None else "TG?"
            lines.append(
                f"- {unit}: {tg_text}, {tier_text}; quantity={_fmt_number(quantity)}"
            )
    else:
        lines.append("- not recorded")

    lines.append("Individual troop stats:")
    stats_block = data.get("stats", {})
    if isinstance(stats_block, dict) and stats_block:
        ordered_types = ["inf", "lanc", "mark"]
        remaining = [k for k in stats_block if k not in ordered_types]
        for unit_type in ordered_types + remaining:
            unit_stats = stats_block.get(unit_type)
            if not isinstance(unit_stats, dict):
                continue
            pieces = []
            for stat_name in ("attack", "defense", "lethality", "health"):
                if stat_name in unit_stats:
                    pieces.append(f"{stat_name}={_fmt_number(unit_stats[stat_name])}")
            if pieces:
                lines.append(f"- {_unit_label(unit_type)}: " + ", ".join(pieces))
        if len(lines) and lines[-1] == "Individual troop stats:":
            lines.append("- not recorded")
    else:
        lines.append("- not recorded")

    return "\n".join(lines)


def _hero_lookup_key(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "", str(name).casefold())


def _experiment_settings_records(csv_paths: list[Path]) -> list[tuple[Path, Path, dict]]:
    records = []
    for csv_path in csv_paths:
        csv_path = csv_path.resolve()
        settings_path = csv_path.with_name(csv_path.stem + "_experiment_settings.json")
        if not settings_path.is_file():
            continue
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read experiment settings {settings_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Experiment settings must contain one object: {settings_path}")
        records.append((csv_path, settings_path, data))
    return records


def _lookup_path_from_settings(settings_path: Path, data: dict) -> Path | None:
    opts = data.get("profile_options", {})
    if not isinstance(opts, dict):
        return None
    info = opts.get("hero_stats_lookup")
    if not isinstance(info, dict):
        return None

    candidates = []
    raw_path = info.get("path")
    if raw_path:
        candidates.append(Path(str(raw_path)))
    filename = info.get("filename")
    if filename:
        candidates.append(settings_path.parent / str(filename))
        candidates.append(Path(__file__).resolve().parent / str(filename))
        candidates.append(JSON_DIR / str(filename))

    for candidate in candidates:
        try:
            candidate = candidate.expanduser().resolve()
        except OSError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _gear_lookup_path_from_settings(settings_path: Path, data: dict) -> Path | None:
    opts = data.get("profile_options", {})
    if not isinstance(opts, dict):
        return None
    info = opts.get("hero_gear_lookup")
    if not isinstance(info, dict):
        return None

    candidates = []
    raw_path = info.get("path")
    if raw_path:
        candidates.append(Path(str(raw_path)))
    filename = info.get("filename")
    if filename:
        candidates.append(settings_path.parent / str(filename))
        candidates.append(Path(__file__).resolve().parent / str(filename))
        candidates.append(JSON_DIR / str(filename))

    for candidate in candidates:
        try:
            candidate = candidate.expanduser().resolve()
        except OSError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _read_gear_lookup_for_summary(path: Path) -> tuple[dict[str, dict[str, dict]], dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read hero-gear lookup {path}: {exc}") from exc

    gear_by_side = data.get("gear_by_side")
    if isinstance(gear_by_side, dict):
        return gear_by_side, data

    # Backward compatibility for the previous global gear lookup.
    legacy = data.get("gear_by_type")
    if isinstance(legacy, dict):
        return {
            "attacker": legacy,
            "defender": copy.deepcopy(legacy),
        }, data

    raise RuntimeError(f"Hero-gear lookup has no gear_by_side object: {path}")


def _read_hero_lookup_for_summary(path: Path) -> tuple[dict[str, dict], dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read hero-stat lookup {path}: {exc}") from exc
    heroes = data.get("heroes")
    if not isinstance(heroes, dict):
        raise RuntimeError(f"Hero-stat lookup has no heroes object: {path}")
    keyed = {}
    for canonical, entry in heroes.items():
        if not isinstance(entry, dict):
            continue
        record = dict(entry)
        record["canonical_name"] = canonical
        for alias in [canonical, *entry.get("aliases", [])]:
            keyed[_hero_lookup_key(alias)] = record
    return keyed, data


def _selected_heroes_for_summary(
    raw: pd.DataFrame,
    side: str,
    profile_path: Path | None,
) -> list[str]:
    suffix = "atk" if side == "attacker" else "def"
    lead_cols = [f"lead_inf_{suffix}", f"lead_cav_{suffix}", f"lead_arch_{suffix}"]
    found = []
    if all(c in raw.columns for c in lead_cols):
        for col in lead_cols:
            for value in raw[col].dropna().astype(str):
                name = value.strip()
                if name and name not in found:
                    found.append(name)
        if found:
            return found

    if profile_path is not None and profile_path.is_file():
        data = load_profile_metadata(profile_path)
        selected = data.get("selectedHeroes", [])
        if isinstance(selected, list):
            for value in selected:
                name = str(value).strip()
                if name and name not in found:
                    found.append(name)
    return found


def _format_bonus_sections(value) -> list[str]:
    if not isinstance(value, dict):
        return ["- no individual overrides configured"]
    lines = []
    for section in ("petLevels", "city", "appointment"):
        section_value = value.get(section)
        if not isinstance(section_value, dict):
            continue
        explicit = {k: v for k, v in section_value.items() if v is not None}
        preserved = [k for k, v in section_value.items() if v is None]
        if explicit:
            lines.append(
                f"- {section} explicit overrides: "
                + json.dumps(explicit, ensure_ascii=False, sort_keys=True)
            )
        if preserved:
            lines.append(
                f"- {section} preserved from input JSON: " + ", ".join(sorted(preserved))
            )
    if not lines:
        lines.append("- no individual overrides configured")
    return lines


def _requested_widgets_for_summary(
    raw: pd.DataFrame,
    side: str,
    profile_path: Path | None,
) -> dict[str, set[int]]:
    """Recover requested widget levels from CSV (lead/troop) or source JSON (joiner)."""
    suffix = "atk" if side == "attacker" else "def"
    requested: dict[str, set[int]] = {}

    lead_cols = [f"lead_inf_{suffix}", f"lead_cav_{suffix}", f"lead_arch_{suffix}"]
    widget_cols = [f"widget_inf_{suffix}", f"widget_cav_{suffix}", f"widget_arch_{suffix}"]
    if all(c in raw.columns for c in [*lead_cols, *widget_cols]):
        for lead_col, widget_col in zip(lead_cols, widget_cols):
            for hero_value, level_value in zip(raw[lead_col], raw[widget_col]):
                hero = str(hero_value).strip() if pd.notna(hero_value) else ""
                if not hero or pd.isna(level_value):
                    continue
                try:
                    level = int(float(level_value))
                except Exception:
                    continue
                if level > 0:
                    requested.setdefault(hero, set()).add(level)
        return requested

    if profile_path is not None and profile_path.is_file():
        data = load_profile_metadata(profile_path)
        selected = data.get("selectedHeroes", [])
        selected_keys = {
            _hero_lookup_key(name)
            for name in selected
            if str(name).strip()
        } if isinstance(selected, list) else set()
        special = data.get("special_bonuses", {})
        levels = special.get("widgetLevels", {}) if isinstance(special, dict) else {}
        if isinstance(levels, dict):
            for hero_name, raw_level in levels.items():
                hero = str(hero_name).strip()
                if not hero or _hero_lookup_key(hero) not in selected_keys:
                    continue
                try:
                    level = int(float(raw_level))
                except Exception:
                    continue
                if level > 0:
                    requested.setdefault(hero, set()).add(level)
    return requested


def _widget_summary_lines(
    raw: pd.DataFrame,
    side: str,
    profile_path: Path | None,
    lookup: dict[str, dict] | None,
    apply_special: bool,
) -> list[str]:
    side_label = side.capitalize()
    lines = [f"{side_label} widgets:"]
    requested = _requested_widgets_for_summary(raw, side, profile_path)

    if not apply_special:
        lines.append("- active widgetLevels: {} (APPLY_SPECIAL_BONUSES=False)")
        if requested:
            req_text = ", ".join(
                f"{hero} level(s) {sorted(levels)}" for hero, levels in requested.items()
            )
            lines.append(f"- requested/source widget levels ignored while disabled: {req_text}")
        return lines

    required_type = "offensive" if side == "attacker" else "defensive"
    active = []
    suppressed = []
    if not requested:
        lines.append("- active widgetLevels: {} (no positive widget levels requested/found)")
        return lines

    if lookup is None:
        lines.append("- widget roles could not be resolved because the lookup file is unavailable")
        return lines

    for hero, levels in requested.items():
        entry = lookup.get(_hero_lookup_key(hero))
        if entry is None:
            suppressed.append(f"{hero}: not found in lookup")
            continue
        widget_type = entry.get("widget_type")
        has_widget = bool(entry.get("has_widget", False))
        level_text = "/".join(str(x) for x in sorted(levels))
        if has_widget and widget_type == required_type:
            active.append(f"{hero}={level_text} ({widget_type})")
        else:
            reason = "no widget" if not has_widget else f"{widget_type} widget on {side} side"
            suppressed.append(f"{hero}={level_text} ({reason}; not active)")

    if active:
        lines.append("- active: " + ", ".join(active))
    else:
        lines.append("- active widgetLevels: {}")
    for item in suppressed:
        lines.append("- excluded: " + item)
    return lines


def experiment_profile_options_summary_text(
    records: list[tuple[Path, Path, dict]],
    raw: pd.DataFrame,
    attacker_json: Path | None,
    defender_json: Path | None,
) -> tuple[str, list[Path]]:
    """Describe client-side hero-stat/special-bonus processing and files to copy."""
    if not records:
        return (
            "Experiment settings sidecar: not found.\n"
            "Automatic 5-star hero-stat/widget processing cannot be determined from this CSV.\n"
            "The source-profile flags shown above are the only available provenance.",
            [],
        )

    auxiliary = []
    option_blobs = []
    for _, settings_path, data in records:
        auxiliary.append(settings_path)
        opts = data.get("profile_options", {})
        option_blobs.append(opts if isinstance(opts, dict) else {})

    common = all(blob == option_blobs[0] for blob in option_blobs[1:])
    if not common:
        lines = [
            "WARNING: input CSVs have different experiment profile options.",
            "Settings files:",
        ]
        lines.extend(f"- {path}" for _, path, _ in records)
        return "\n".join(lines), auxiliary

    opts = option_blobs[0]
    settings_path = records[0][1]

    # New metadata is side-specific. Fall back to the older global keys so old
    # experiment folders remain readable.
    add_stats_atk = bool(opts.get(
        "add_5star_hero_stats_atk", opts.get("add_5star_hero_stats", False)
    ))
    add_stats_def = bool(opts.get(
        "add_5star_hero_stats_def", opts.get("add_5star_hero_stats", False)
    ))
    add_gear_atk = bool(opts.get(
        "add_hero_gear_atk", opts.get("add_hero_gear", False)
    ))
    add_gear_def = bool(opts.get(
        "add_hero_gear_def", opts.get("add_hero_gear", False)
    ))
    requested_atk = opts.get(
        "stats_include_heroes_requested_atk",
        opts.get("stats_include_heroes_requested"),
    )
    requested_def = opts.get(
        "stats_include_heroes_requested_def",
        opts.get("stats_include_heroes_requested"),
    )
    effective_atk = opts.get(
        "stats_include_heroes_effective_atk",
        opts.get("stats_include_heroes_effective"),
    )
    effective_def = opts.get(
        "stats_include_heroes_effective_def",
        opts.get("stats_include_heroes_effective"),
    )
    apply_special = bool(opts.get("apply_special_bonuses", False))

    lines = [
        f"Experiment settings sidecar: {settings_path}",
        f"ADD_5STAR_HERO_STATS_ATK: {add_stats_atk}",
        f"ADD_5STAR_HERO_STATS_DEF: {add_stats_def}",
        f"ADD_HERO_GEAR_ATK: {add_gear_atk}",
        f"ADD_HERO_GEAR_DEF: {add_gear_def}",
    ]
    if "add_hero_stats_atk" in opts or "add_hero_stats_def" in opts:
        lines.extend([
            f"ADD_HERO_STATS_ATK: {bool(opts.get('add_hero_stats_atk', effective_atk is False))}",
            f"ADD_HERO_STATS_DEF: {bool(opts.get('add_hero_stats_def', effective_def is False))}",
        ])
    else:
        # Legacy metadata: expose the original simulator-facing flag only when
        # the experiment predates the intuitive ADD_HERO_STATS switch.
        lines.extend([
            f"STATS_INCLUDE_HEROES_ATK requested (legacy): {requested_atk!r}",
            f"stats_include_heroes attacker effective: {effective_atk!r}",
            f"STATS_INCLUDE_HEROES_DEF requested (legacy): {requested_def!r}",
            f"stats_include_heroes defender effective: {effective_def!r}",
        ])

    lookup_path = _lookup_path_from_settings(settings_path, records[0][2])
    keyed = None
    lookup_data = None
    if lookup_path is not None:
        auxiliary.append(lookup_path)
        keyed, lookup_data = _read_hero_lookup_for_summary(lookup_path)
        lines.append(f"Hero-stat/widget lookup: {lookup_path}")
        if lookup_data.get("verified_on"):
            lines.append(f"Lookup verified on: {lookup_data['verified_on']}")

    gear_lookup_path = _gear_lookup_path_from_settings(settings_path, records[0][2])
    gear_by_side = None
    gear_lookup_data = None
    if gear_lookup_path is not None:
        auxiliary.append(gear_lookup_path)
        gear_by_side, gear_lookup_data = _read_gear_lookup_for_summary(gear_lookup_path)
        lines.append(f"Hero-gear lookup: {gear_lookup_path}")

    for side, add_stats, add_gear, profile in (
        ("attacker", add_stats_atk, add_gear_atk, attacker_json),
        ("defender", add_stats_def, add_gear_def, defender_json),
    ):
        side_label = side.capitalize()
        side_gear = (
            gear_by_side.get(side, {})
            if isinstance(gear_by_side, dict)
            else None
        )

        if add_gear:
            if side_gear is None:
                lines.append(
                    f"{side_label} hero gear: enabled, but the referenced gear lookup "
                    "is not currently readable."
                )
            else:
                lines.append(f"{side_label} hero gear added by hero type:")
                for hero_type, unit_label in (
                    ("inf", "Infantry"), ("lanc", "Cavalry"), ("mark", "Archers")
                ):
                    g = side_gear.get(hero_type, {})
                    lines.append(
                        f"- {unit_label}: attack=+{_fmt_number(g.get('attack'))}, "
                        f"defense=+{_fmt_number(g.get('defense'))}, "
                        f"lethality=+{_fmt_number(g.get('lethality'))}, "
                        f"health=+{_fmt_number(g.get('health'))} percentage points"
                    )
        else:
            lines.append(f"{side_label} hero gear injection was not enabled.")

        if add_stats:
            if keyed is None:
                lines.append(
                    f"{side_label} 5-star hero-stat injection enabled, but the hero lookup "
                    "is not currently readable."
                )
            else:
                lines.append(
                    f"{side_label} injected maxed 5-star expedition hero stats "
                    "(including max passive widget Lethality/Health for Mythic heroes; "
                    "separate from root/base troop stats):"
                )
                selected = _selected_heroes_for_summary(raw, side, profile)
                if not selected:
                    lines.append("- selected lead heroes not recoverable from CSV/profile")
                for hero_name in selected:
                    entry = keyed.get(_hero_lookup_key(hero_name))
                    if entry is None:
                        lines.append(f"- {hero_name}: not found in lookup")
                        continue
                    stats = entry.get("stats", {})
                    widget_type = entry.get("widget_type")
                    widget_text = f"; widget={widget_type}" if widget_type else "; no widget"
                    hero_type = entry.get("type", "unknown")
                    gear = (
                        side_gear.get(hero_type, {})
                        if add_gear and isinstance(side_gear, dict)
                        else None
                    )
                    if isinstance(gear, dict):
                        final = {
                            stat: float(stats.get(stat, 0) or 0)
                            + float(gear.get(stat, 0) or 0)
                            for stat in ("attack", "defense", "lethality", "health")
                        }
                        lines.append(
                            f"- {hero_name} ({_unit_label(hero_type)}{widget_text}): "
                            f"5-star=ATK +{_fmt_number(stats.get('attack'))}, "
                            f"DEF +{_fmt_number(stats.get('defense'))}, "
                            f"LETH +{_fmt_number(stats.get('lethality'))}, "
                            f"HP +{_fmt_number(stats.get('health'))}; "
                            f"gear=ATK +{_fmt_number(gear.get('attack'))}, "
                            f"DEF +{_fmt_number(gear.get('defense'))}, "
                            f"LETH +{_fmt_number(gear.get('lethality'))}, "
                            f"HP +{_fmt_number(gear.get('health'))}; "
                            f"final hero.stats=ATK +{_fmt_number(final['attack'])}, "
                            f"DEF +{_fmt_number(final['defense'])}, "
                            f"LETH +{_fmt_number(final['lethality'])}, "
                            f"HP +{_fmt_number(final['health'])} percentage points"
                        )
                    else:
                        lines.append(
                            f"- {hero_name} ({_unit_label(hero_type)}{widget_text}): "
                            f"attack=+{_fmt_number(stats.get('attack'))}, "
                            f"defense=+{_fmt_number(stats.get('defense'))}, "
                            f"lethality=+{_fmt_number(stats.get('lethality'))}, "
                            f"health=+{_fmt_number(stats.get('health'))} percentage points"
                        )
        elif add_gear:
            lines.append(
                f"{side_label} 5-star lookup injection was disabled; hero gear was added "
                f"to the hero.stats values already present in the generated/input {side} JSON. "
                "Widget activation does not modify hero.stats."
            )
        else:
            lines.append(
                f"{side_label} 5-star lookup and hero-gear injection were both disabled; "
                f"hero.stats remain those from the input/generated {side} JSON. "
                "Widget activation does not modify hero.stats."
            )

    lines.append(f"APPLY_SPECIAL_BONUSES: {apply_special}")
    if apply_special:
        lines.append(
            "special_bonuses.includedInStats was forced to False so the simulator counts "
            "special bonuses. Widget levels are role-filtered automatically: offensive "
            "widgets only on attacker, defensive widgets only on defender. The widget skill/buff "
            "is separate from maxed passive widget stats already included by 5-star injection."
        )
    else:
        lines.append(
            "Special bonuses were explicitly disabled during simulator import: "
            "special_bonuses.includedInStats=True and widgetLevels={}."
        )

    lines.extend(_widget_summary_lines(raw, "attacker", attacker_json, keyed, apply_special))
    lines.extend(_widget_summary_lines(raw, "defender", defender_json, keyed, apply_special))
    if add_stats_atk or add_stats_def:
        lines.append(
            "Max passive widget/Exclusive Gear Lethality and Health are part of the maxed "
            "5-star lookup and are included whenever 5-star hero-stat injection is enabled, "
            "regardless of APPLY_SPECIAL_BONUSES or whether the widget skill is active."
        )

    for side_label, key in (
        ("Attacker", "special_bonuses_attacker"),
        ("Defender", "special_bonuses_defender"),
    ):
        lines.append(f"{side_label} individual pet/city/appointment overrides:")
        lines.extend(_format_bonus_sections(opts.get(key)))

    return "\n".join(lines), auxiliary


def _copy_auxiliary_files(paths: list[Path], out_dir: Path) -> list[Path]:
    copied = []
    seen = set()
    for source in paths:
        source = Path(source).resolve()
        if not source.is_file() or source in seen:
            continue
        seen.add(source)
        target = out_dir / source.name
        try:
            same = target.exists() and source.samefile(target)
        except OSError:
            same = False
        if not same:
            shutil.copy2(source, target)
        copied.append(target)
    return copied


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "csv",
        nargs="+",
        type=Path,
        help="One or more Kingshot simulator CSV files",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output folder; default is based on the first input filename",
    )
    parser.add_argument(
        "--existing-output",
        choices=("overwrite", "new", "quit"),
        default=None,
        help="What to do if the output folder already exists. If omitted, ask interactively.",
    )
    parser.add_argument(
        "--side",
        choices=("auto", "attacker", "defender"),
        default="auto",
        help=(
            "Which side's joiner composition to model. Default: auto, which "
            "chooses the side with more unique hero lineups. Defender mode "
            "uses defender win probability = 100 - attacker win rate."
        ),
    )
    parser.add_argument(
        "--trials-per-row",
        type=int,
        default=100,
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
    )
    parser.add_argument(
        "--max-copy-plot",
        type=int,
        default=None,
        help=(
            "Optional cap on copy increments shown in the individual-hero "
            "contribution plot. By default all observed/estimable copies are shown."
        ),
    )
    parser.add_argument(
        "--selection-method",
        choices=("all", "raw", "fdr", "holm"),
        default=None,
        help="Legacy common interaction-selection method for both pair and triple synergy.",
    )
    parser.add_argument(
        "--synergy-threshold",
        type=float,
        default=None,
        help="Legacy common p-value threshold for both pair and triple synergy.",
    )
    parser.add_argument(
        "--pair-selection-method",
        choices=("all", "raw", "fdr", "holm"),
        default=None,
        help="Method used to retain pair-synergy terms; all keeps every estimable pair.",
    )
    parser.add_argument(
        "--pair-synergy-threshold",
        type=float,
        default=None,
        help="Pair-synergy p-value threshold; ignored when pair selection method is all.",
    )
    parser.add_argument(
        "--triple-selection-method",
        choices=("all", "raw", "fdr", "holm"),
        default=None,
        help="Method used to retain triple-synergy terms; all keeps every estimable triple.",
    )
    parser.add_argument(
        "--triple-synergy-threshold",
        type=float,
        default=None,
        help="Triple-synergy p-value threshold; ignored when triple selection method is all.",
    )
    parser.add_argument("--pair-synergy", dest="pair_synergy", action="store_true", default=True, help="Include pair-synergy terms in the prediction model (default).")
    parser.add_argument("--no-pair-synergy", dest="pair_synergy", action="store_false", help="Use the additive/duplicate prediction model without pair synergy.")
    parser.add_argument("--triple-synergy", dest="triple_synergy", action="store_true", default=True, help="Include threshold-selected triple-synergy terms (requires pair synergy; default).")
    parser.add_argument("--no-triple-synergy", dest="triple_synergy", action="store_false", help="Do not include triple-synergy terms.")
    parser.add_argument(
        "--attacker-json",
        type=Path,
        default=None,
        help=(
            "Optional attacker battle-profile JSON used to document lead heroes, "
            "unit levels, and stats in analysis_summary.txt. If omitted, common "
            "filenames next to the first CSV are auto-detected."
        ),
    )
    parser.add_argument(
        "--defender-json",
        type=Path,
        default=None,
        help=(
            "Optional defender battle-profile JSON used to document lead heroes, "
            "unit levels, and stats in analysis_summary.txt. If omitted, common "
            "filenames next to the first CSV are auto-detected."
        ),
    )

    args = parser.parse_args()
    common_method = args.selection_method or "raw"
    common_threshold = 0.05 if args.synergy_threshold is None else args.synergy_threshold
    args.pair_selection_method = args.pair_selection_method or common_method
    args.triple_selection_method = args.triple_selection_method or common_method
    args.pair_synergy_threshold = common_threshold if args.pair_synergy_threshold is None else args.pair_synergy_threshold
    args.triple_synergy_threshold = common_threshold if args.triple_synergy_threshold is None else args.triple_synergy_threshold

    if args.out is None:
        args.out = args.csv[0].with_name(
            args.csv[0].stem + "_analysis"
        )

    resolved_out = resolve_output_folder(args.out, args.existing_output)
    if resolved_out is None:
        return
    args.out = resolved_out

    attacker_json, attacker_json_note = resolve_profile_json(
        args.attacker_json, "attacker", args.csv
    )
    defender_json, defender_json_note = resolve_profile_json(
        args.defender_json, "defender", args.csv
    )

    copied_inputs = _copy_input_files(args.csv, args.out)

    (
        raw,
        cond,
        heroes,
        slots,
        chosen_side,
        n_attacker_lineups,
        n_defender_lineups,
    ) = load_files(
        args.csv,
        args.trials_per_row,
        args.side,
    )

    experiment_settings = _experiment_settings_records(args.csv)
    plot_context_lines = prediction_plot_context_lines(
        experiment_settings,
        attacker_json,
        defender_json,
        chosen_side,
    )
    profile_options_text, auxiliary_sources = experiment_profile_options_summary_text(
        experiment_settings, raw, attacker_json, defender_json
    )
    copied_auxiliary = _copy_auxiliary_files(auxiliary_sources, args.out)

    print(
        f"Analysis side: {chosen_side} "
        f"(unique attacker lineups={n_attacker_lineups}, "
        f"unique defender lineups={n_defender_lineups}; "
        f"--side={args.side})"
    )
    if chosen_side == "defender":
        print(
            "Response reversed to defender win probability: "
            "100 - attacker winrate."
        )

    if not (0 < args.pair_synergy_threshold <= 1):
        raise ValueError("--pair-synergy-threshold must be greater than 0 and at most 1.")
    if not (0 < args.triple_synergy_threshold <= 1):
        raise ValueError("--triple-synergy-threshold must be greater than 0 and at most 1.")
    if not args.pair_synergy:
        args.triple_synergy = False

    X_base, reference, others, duplicate_terms = build_base_design(cond, heroes)
    X_pair_full, pairs = add_pair_terms(cond, heroes, X_base)
    X_triple_full, triples = add_triple_terms(cond, heroes, X_pair_full)

    base_rank = np.linalg.matrix_rank(np.asarray(X_base, dtype=float))
    if base_rank != X_base.shape[1]:
        raise RuntimeError(
            f"Even the additive + duplicate model is rank-deficient "
            f"(rank {base_rank}/{X_base.shape[1]})."
        )

    # Full interaction models remain the inference layer. The user-facing
    # prediction model below includes only the enabled synergy layers and terms
    # passing the selected threshold (or every term when threshold == 1).
    base_fit = fit_binomial_direct(cond, X_base)
    full_pair_fit = fit_full_model(cond, heroes, X_pair_full, pairs)
    full_triple_fit = fit_full_triple_model(cond, heroes, X_triple_full, pairs, triples)

    heroes_tbl = hero_effect_table(base_fit, heroes, others, reference, duplicate_terms)
    pair_tbl = synergy_effect_table(full_pair_fit, pairs)
    triple_tbl = triple_effect_table(full_triple_fit, triples)

    pair_select_mask = significance_mask(pair_tbl, args.pair_selection_method, args.pair_synergy_threshold)
    triple_select_mask = significance_mask(triple_tbl, args.triple_selection_method, args.triple_synergy_threshold)
    threshold_pairs = sorted([
        tuple(sorted((r.hero_a, r.hero_b)))
        for r in pair_tbl.loc[pair_select_mask].itertuples(index=False)
    ])
    threshold_triples = sorted([
        tuple(sorted((r.hero_a, r.hero_b, r.hero_c)))
        for r in triple_tbl.loc[triple_select_mask].itertuples(index=False)
    ])

    selected_pairs = threshold_pairs if args.pair_synergy else []
    selected_triples = threshold_triples if args.triple_synergy else []
    hierarchy_pairs = list(selected_pairs)
    added_for_hierarchy: list[tuple[str, str]] = []

    if not args.pair_synergy:
        X_model = X_base
        selected_fit = base_fit
        model_name = "additive + duplicate (synergy disabled)"
    elif args.triple_synergy and selected_triples:
        X_model, hierarchy_pairs, added_for_hierarchy = build_selected_pair_triple_design(
            X_base, X_triple_full, selected_pairs, selected_triples
        )
        selected_fit = fit_full_triple_model(
            cond, heroes, X_model, hierarchy_pairs, selected_triples
        )
        model_name = (
            f"additive + {len(hierarchy_pairs)} pair synergies + "
            f"{len(selected_triples)} triple synergies"
        )
    elif selected_pairs:
        X_model = build_selected_pair_design(X_base, X_pair_full, selected_pairs)
        selected_fit = fit_full_model(cond, heroes, X_model, selected_pairs)
        model_name = f"additive + {len(selected_pairs)} pair synergies"
    else:
        X_model = X_base
        selected_fit = base_fit
        model_name = "additive + duplicate (no synergy terms passed threshold)"

    included_pair_set = set(hierarchy_pairs if args.pair_synergy else [])
    included_triple_set = set(selected_triples)
    pair_tbl["significant_by_selection_rule"] = pair_select_mask.to_numpy()
    pair_tbl["included_in_prediction_model"] = pair_tbl.apply(
        lambda r: tuple(sorted((r["hero_a"], r["hero_b"]))) in included_pair_set, axis=1
    )
    pair_tbl["selected_for_reduced_model"] = pair_tbl["included_in_prediction_model"]
    triple_tbl["significant_by_selection_rule"] = triple_select_mask.to_numpy()
    triple_tbl["included_in_prediction_model"] = triple_tbl.apply(
        lambda r: tuple(sorted((r["hero_a"], r["hero_b"], r["hero_c"]))) in included_triple_set, axis=1
    )
    triple_tbl["selected_for_reduced_model"] = triple_tbl["included_in_prediction_model"]

    settings_tag = _settings_tag(
        args.pair_synergy, args.triple_synergy,
        args.pair_selection_method, args.pair_synergy_threshold,
        args.triple_selection_method, args.triple_synergy_threshold,
    )
    prediction_filename = f"01_model_prediction_{chosen_side}_{settings_tag}_top{args.top_n}.png"
    pair_plot_filename = f"03_pair_synergies_{chosen_side}_{_method_threshold_tag(args.pair_selection_method, args.pair_synergy_threshold)}.png"
    triple_plot_filename = f"04_triple_synergies_{chosen_side}_{_method_threshold_tag(args.triple_selection_method, args.triple_synergy_threshold)}.png"

    ranked, top, avg_ci = plot_prediction(
        cond, heroes, X_model, selected_fit, args.out, args.top_n,
        prediction_filename, model_name, plot_context_lines, args.trials_per_row,
    )
    plot_heroes(heroes_tbl, args.out, args.max_copy_plot)
    pair_plot_written = False
    triple_plot_written = False
    if args.pair_synergy:
        pair_plot_written = plot_synergies(
            pair_tbl, full_pair_fit, args.out, args.pair_selection_method,
            args.pair_synergy_threshold, pair_plot_filename,
        )
    if args.triple_synergy:
        triple_plot_written = plot_triple_synergies(
            triple_tbl, args.out, args.triple_selection_method, args.triple_synergy_threshold,
            triple_plot_filename,
        )

    ranked.to_csv(args.out / "ranked_lineup_predictions.csv", index=False)
    top.to_csv(args.out / "top_model_predicted_lineups.csv", index=False)
    heroes_tbl.to_csv(args.out / "hero_incremental_contributions.csv", index=False)
    pair_tbl.to_csv(args.out / "all_pair_synergies.csv", index=False)
    triple_tbl.to_csv(args.out / "all_triple_synergies.csv", index=False)
    pair_tbl.loc[pair_tbl["included_in_prediction_model"]].to_csv(
        args.out / "selected_pair_synergies.csv", index=False
    )
    triple_tbl.loc[triple_tbl["included_in_prediction_model"]].to_csv(
        args.out / "selected_triple_synergies.csv", index=False
    )

    model_cmp = pd.DataFrame([
        {
            "model": "additive + duplicate baseline",
            "n_parameters_columns": int(X_base.shape[1]),
            "rank": int(np.linalg.matrix_rank(np.asarray(X_base, dtype=float))),
            **fit_metrics(cond, base_fit),
            "constrained": base_fit.constrained,
        },
        {
            "model": model_name,
            "n_parameters_columns": int(X_model.shape[1]),
            "rank": int(np.linalg.matrix_rank(np.asarray(X_model, dtype=float))),
            **fit_metrics(cond, selected_fit),
            "constrained": selected_fit.constrained,
        },
    ])
    model_cmp.to_csv(args.out / "model_comparison.csv", index=False)

    inference_cmp = pd.DataFrame([
        {
            "model": "full pair inference model",
            "n_parameters_columns": int(X_pair_full.shape[1]),
            "rank": int(np.linalg.matrix_rank(np.asarray(X_pair_full, dtype=float))),
            **fit_metrics(cond, full_pair_fit),
            "constrained": full_pair_fit.constrained,
        },
        {
            "model": "full hierarchical pair + triple inference model",
            "n_parameters_columns": int(X_triple_full.shape[1]),
            "rank": int(np.linalg.matrix_rank(np.asarray(X_triple_full, dtype=float))),
            **fit_metrics(cond, full_triple_fit),
            "constrained": full_triple_fit.constrained,
        },
    ])
    inference_cmp.to_csv(args.out / "inference_model_comparison.csv", index=False)

    base_metrics = fit_metrics(cond, base_fit)
    selected_metrics = fit_metrics(cond, selected_fit)
    full_pair_metrics = fit_metrics(cond, full_pair_fit)
    full_triple_metrics = fit_metrics(cond, full_triple_fit)

    battle_configuration = "\n\n".join([
        profile_summary_text("attacker", attacker_json, attacker_json_note),
        profile_summary_text("defender", defender_json, defender_json_note),
    ])

    copied_input_lines = "\n".join(f"- {p.name}" for p in copied_inputs)
    copied_auxiliary_lines = (
        "\n".join(f"- {p.name}" for p in copied_auxiliary)
        if copied_auxiliary
        else "- none found"
    )

    summary = f"""Kingshot full-model analysis
============================

INPUT
-----
Files:
{chr(10).join("- " + str(p) for p in args.csv)}

Input file copy/copies saved in this output folder:
{copied_input_lines}

Experiment-settings / lookup copy/copies saved in this output folder:
{copied_auxiliary_lines}

BATTLE CONFIGURATION
--------------------
{battle_configuration}

EXPERIMENT HERO-STATS / SPECIAL-BONUS HANDLING
-----------------------------------------------
{profile_options_text}

Note: lead/unit/stat metadata are read from the companion JSON profile(s), not
from the result CSV. If the simulation script changed troop rows after loading
the baseline JSON, use --attacker-json / --defender-json with JSONs that reflect
the setup you want documented.

Modeled side:
{chosen_side}

Requested --side:
{args.side}

Unique attacker lineups:
{n_attacker_lineups}

Unique defender lineups:
{n_defender_lineups}

Modeled joiner slots:
{", ".join(slots)}

Response:
{"defender win probability = 100 - attacker winrate" if chosen_side == "defender" else "attacker win probability = winrate"}

Heroes detected:
{", ".join(heroes)}

Unique squads:
{len(cond)}

Replication / simulation depth:
{simulation_depth_title(cond["trials"])}

MODEL
-----
Hero-contribution plot:
- additive hero effects
- nonlinear duplicate adjustments
- adjusted for teammates

Interaction inference models (used to estimate p-values):
- full pair model: all {len(pairs)} pair interactions
- full pair + triple inference model: all {len(pairs)} pair interactions + all {len(triples)} triple interactions
- full pair model constrained: {full_pair_fit.constrained}
- full pair + triple model constrained: {full_triple_fit.constrained}
- triple constraint convention: {full_triple_fit.constraint_description}

Selected prediction model:
- pair synergy enabled = {args.pair_synergy}
- triple synergy enabled = {args.triple_synergy}
- pair selection method = {args.pair_selection_method} ({"all estimable interactions" if args.pair_selection_method == "all" else ("raw/unadjusted p-value" if args.pair_selection_method == "raw" else "multiplicity-adjusted p-value")})
- pair synergy threshold = {"ignored" if args.pair_selection_method == "all" else f"{args.pair_synergy_threshold:g}"}
- pair terms passing threshold = {len(threshold_pairs)}
- pair terms included after hierarchy = {len(included_pair_set)}
- triple selection method = {args.triple_selection_method} ({"all estimable interactions" if args.triple_selection_method == "all" else ("raw/unadjusted p-value" if args.triple_selection_method == "raw" else "multiplicity-adjusted p-value")})
- triple synergy threshold = {"ignored" if args.triple_selection_method == "all" else f"{args.triple_synergy_threshold:g}"}
- triple terms passing threshold = {len(threshold_triples)}
- triple terms included = {len(included_triple_set)}
- hierarchy-added parent pairs = {len(added_for_hierarchy)}
- final model = {model_name}

Selection note:
Raw means the ordinary, unadjusted p-value. FDR and Holm account for multiple
interaction tests. Method All keeps every estimable interaction in that enabled
layer and ignores its p threshold. Triple synergy requires pair synergy; selected
triples automatically retain their constituent pair terms.

FIT
---
Additive + duplicate baseline:
- RMSE = {base_metrics['rmse_pp']:.3f} pp
- MAE = {base_metrics['mae_pp']:.3f} pp
- Pearson dispersion = {base_metrics['pearson_dispersion']:.3f}
- goodness-of-fit p = {base_metrics['gof_p']:.4g}

Selected prediction model:
- RMSE = {selected_metrics['rmse_pp']:.3f} pp
- MAE = {selected_metrics['mae_pp']:.3f} pp
- Pearson dispersion = {selected_metrics['pearson_dispersion']:.3f}
- goodness-of-fit p = {selected_metrics['gof_p']:.4g}
- average prediction CI 95% half-width = ±{avg_ci:.3f} pp

Full pair inference model:
- RMSE = {full_pair_metrics['rmse_pp']:.3f} pp
- Pearson dispersion = {full_pair_metrics['pearson_dispersion']:.3f}

Full pair + triple inference model:
- RMSE = {full_triple_metrics['rmse_pp']:.3f} pp
- Pearson dispersion = {full_triple_metrics['pearson_dispersion']:.3f}


TOP MODEL-PREDICTED LINEUPS
---------------------------
{top[["rank","lineup","predicted_pct","observed_pct"]].to_string(index=False, float_format=lambda x: f"{x:.3f}")}

OUTPUTS
-------
{prediction_filename}
02_individual_hero_contributions.png
{pair_plot_filename if pair_plot_written else "(no pair-synergy plot: pair synergy disabled or no pair passed the threshold)"}
{triple_plot_filename if triple_plot_written else "(no triple-synergy plot: triple synergy disabled or no triple passed the threshold)"}
ranked_lineup_predictions.csv
top_model_predicted_lineups.csv
hero_incremental_contributions.csv
all_pair_synergies.csv
all_triple_synergies.csv
selected_pair_synergies.csv
selected_triple_synergies.csv
model_comparison.csv
inference_model_comparison.csv
analysis_summary.txt
Input CSV copy/copies (original filenames preserved when possible)
Experiment settings / hero-stat lookup copies when available
"""

    (args.out / "analysis_summary.txt").write_text(
        summary,
        encoding="utf-8",
    )

    print(summary)


if __name__ == "__main__":
    main()
