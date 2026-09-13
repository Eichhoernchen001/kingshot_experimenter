#!/usr/bin/env python3
"""Compact troop-range parsing shared by the GUI and regression tests."""
from __future__ import annotations

import re
from typing import Any


def format_split(split: list[Any] | tuple[Any, ...]) -> str:
    def fmt(value: Any) -> str:
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:g}"
    return "/".join(fmt(value) for value in split)


def parse_variant_line(text: str) -> list[list[float | int]]:
    """Expand ``start:end/start:end/constant - step`` into valid splits."""
    raw = text.strip()
    match = re.fullmatch(r"(.+?)\s+-\s+([0-9]+(?:\.[0-9]+)?)", raw)
    body = match.group(1).strip() if match else raw
    step = float(match.group(2)) if match else None
    tokens = [part.strip() for part in body.split("/")]
    if len(tokens) != 3:
        raise ValueError("Use three troop entries: Infantry/Cavalry/Archers.")
    parsed: list[tuple[float, float]] = []
    has_range = False
    for token in tokens:
        if ":" in token:
            bits = [part.strip() for part in token.split(":")]
            if len(bits) != 2:
                raise ValueError(f"Invalid range {token!r}; use start:end.")
            start, end = map(float, bits)
            has_range = True
        else:
            start = end = float(token)
        if start < 0 or end < 0:
            raise ValueError("Troop percentages cannot be negative.")
        parsed.append((start, end))
    if has_range and (step is None or step <= 0):
        raise ValueError("A range needs a positive step, e.g. 70:30/30:70/0 - 10.")
    if not has_range and step is not None:
        raise ValueError("Remove the step from a single split, or add start:end to a troop type.")
    counts: list[int] = []
    if has_range:
        assert step is not None
        for start, end in parsed:
            if abs(end - start) < 1e-9:
                continue
            raw_steps = abs(end - start) / step
            if abs(raw_steps - round(raw_steps)) > 1e-8:
                raise ValueError(f"Range {start:g}:{end:g} is not evenly divisible by step {step:g}.")
            counts.append(int(round(raw_steps)) + 1)
        if len(set(counts)) != 1:
            raise ValueError("All changing troop ranges on a line must produce the same number of values.")
        count = counts[0]
    else:
        count = 1
    if count > 10:
        raise ValueError(
            f"This line creates {count} splits, but one range may use at most 10 colors. "
            "Split it across additional lines."
        )
    result: list[list[float | int]] = []
    for index in range(count):
        values = []
        for start, end in parsed:
            if count == 1 or abs(end - start) < 1e-9:
                value = start
            else:
                direction = 1.0 if end > start else -1.0
                value = start + direction * step * index
            values.append(value)
        if abs(sum(values) - 100.0) > 1e-8:
            raise ValueError(f"Generated split {format_split(values)} sums to {sum(values):g}, not 100.")
        result.append([int(value) if float(value).is_integer() else value for value in values])
    return result


def parse_variant_lines(text: str) -> tuple[list[list[float | int]], list[int], list[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Add at least one troop split or range.")
    variants: list[list[float | int]] = []
    groups: list[int] = []
    for group, line in enumerate(lines, start=1):
        expanded = parse_variant_line(line)
        variants.extend(expanded)
        groups.extend([group] * len(expanded))
    return variants, groups, lines
