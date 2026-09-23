"""Compact hourly rain timeline for the TC002's 52×16 display."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from .validation import parse_color

RAIN_THRESHOLD_MM = 0.1
MAX_HOURS = 10


def _number(value: Any, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, min(maximum, number)) if math.isfinite(number) else 0.0


def _hours_until(item: dict[str, Any], index: int, now: datetime) -> int:
    stamp = item.get("datetime")
    if isinstance(stamp, str):
        try:
            instant = datetime.fromisoformat(stamp)
            if instant.tzinfo is not None:
                seconds = (instant - now).total_seconds()
                return 0 if seconds <= 1800 else min(99, math.ceil(seconds / 3600))
        except ValueError:
            pass
    return index + 1


def rain_timeline(
    forecasts: list[Any], *, chart_type: str, autoscale: bool, chart_color: str,
    unit: str = "mm", now: datetime | None = None,
) -> tuple[list[list[Any]], list[int], str]:
    """Return bounded drawing commands, hundredths of mm and the headline."""

    now = now or datetime.now(UTC)
    entries = [item for item in forecasts[:MAX_HOURS] if isinstance(item, dict)]
    if not entries:
        return [], [], "DATI ASSENTI"
    factor = 25.4 if unit.lower() in {"in", "inch", "inches"} else 1.0
    amounts = [_number(item.get("precipitation"), 50.0) * factor for item in entries]
    amounts = [min(50.0, amount) for amount in amounts]
    probabilities = [
        _number(item.get("precipitation_probability"), 100.0) for item in entries
    ]
    first_wet = next((i for i, amount in enumerate(amounts) if amount >= RAIN_THRESHOLD_MM), None)
    if first_wet is not None:
        eta = _hours_until(entries[first_wet], first_wet, now)
        headline = "PIOVE ORA" if eta == 0 else f"TRA {eta}H"
        accent = "#68D9FF"
    elif max(probabilities) >= 50:
        headline = f"RISCHIO {round(max(probabilities))}%"
        accent = "#FFC76A"
    else:
        headline = f"SECCO {len(entries)}H"
        accent = "#8CE0AF"

    commands: list[list[Any]] = [
        ["line", 0, 15, 51, 15, "#223544"],
        ["text", 8, 6, headline, accent],
    ]
    if first_wet is None and max(probabilities) < 50:
        commands.append(["circleFill", 2, 3, 1, accent])
        for x, y in ((2, 0), (2, 6), (0, 3), (4, 3), (0, 1), (4, 1), (0, 5), (4, 5)):
            commands.append(["pixel", x, y, accent])
    else:
        # A five-pixel droplet leaves room for a short, readable headline.
        for x, y in ((2, 0), (1, 2), (3, 2), (0, 4), (4, 4), (1, 5), (2, 6), (3, 5)):
            commands.append(["pixel", x, y, accent])

    scale = max(0.5, max(amounts)) if autoscale else 4.0
    base = parse_color(chart_color)
    base_rgb = ((base >> 16) & 255, (base >> 8) & 255, base & 255)
    points: list[tuple[int, int]] = []
    point_commands: list[list[Any]] = []
    for index, (amount, chance) in enumerate(zip(amounts, probabilities, strict=True)):
        x = 1 + index * 5
        if amount >= RAIN_THRESHOLD_MM:
            height = max(1, min(6, round(amount / scale * 6)))
            color = "#FF77B6" if amount >= 2.0 else f"#{base_rgb[0]:02X}{base_rgb[1]:02X}{base_rgb[2]:02X}"
        elif chance >= 50:
            height = max(1, min(5, round(chance / 100 * 5)))
            color = "#9B7440"
        else:
            height = 0
            color = "#344A5A"
        if chart_type == "lineChart":
            y = 14 - height
            points.append((x + 1, y))
            point_commands.append(["pixel", x + 1, y, color])
        elif height:
            commands.append(["rectFill", x, 15 - height, 3, height, color])
        else:
            commands.append(["pixel", x + 1, 14, color])
    if chart_type == "lineChart":
        for (left_x, left_y), (right_x, right_y) in pairwise(points):
            commands.append(["line", left_x, left_y, right_x, right_y, chart_color])
        commands.extend(point_commands)
    return commands, [round(amount * 100) for amount in amounts], headline
