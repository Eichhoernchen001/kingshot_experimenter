"""Small, GUI-independent helpers for editing joiner repeat limits."""

from __future__ import annotations


REPEAT_LIMITS = (4, 3, 2, 1)


def pools_to_mapping(pools: dict[str, list[str]]) -> dict[str, int]:
    """Convert the config's grouped pools into one mutually exclusive choice per hero."""
    mapping: dict[str, int] = {}
    for maximum in REPEAT_LIMITS:
        for hero in pools.get(str(maximum), []):
            mapping[str(hero)] = maximum
    return mapping


def mapping_to_pools(mapping: dict[str, int], hero_order: list[str]) -> dict[str, list[str]]:
    """Convert per-hero repeat limits back to stable, config-compatible groups."""
    result = {str(maximum): [] for maximum in REPEAT_LIMITS}
    for hero in hero_order:
        maximum = mapping.get(hero)
        if maximum in REPEAT_LIMITS:
            result[str(maximum)].append(hero)
    return result


def set_repeat_limit(
    mapping: dict[str, int],
    hero: str,
    maximum: int,
    selected: bool,
) -> None:
    """Select exactly one repeat limit for a hero, or remove it when unchecked."""
    if maximum not in REPEAT_LIMITS:
        raise ValueError("Joiner repeat limit must be 1, 2, 3, or 4.")
    if selected:
        mapping[hero] = maximum
    elif mapping.get(hero) == maximum:
        mapping.pop(hero, None)
