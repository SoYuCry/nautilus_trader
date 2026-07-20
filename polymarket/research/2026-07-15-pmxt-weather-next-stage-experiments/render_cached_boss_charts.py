from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont


HERE = Path(__file__).resolve().parent
EVENT_LEVEL_ROOT = HERE / "event_level"

LIFECYCLE_CSV = EVENT_LEVEL_ROOT / "lifecycle" / "lifecycle_metrics.csv"
ACTIVE_SET_CSV = EVENT_LEVEL_ROOT / "active_set" / "active_set_metrics.csv"
SNAPSHOTS_CSV = EVENT_LEVEL_ROOT / "distribution" / "representative_event_snapshots.csv"

LIFECYCLE_OUTPUT = EVENT_LEVEL_ROOT / "lifecycle" / "lifecycle_activity_curve.png"
ACTIVE_SET_OUTPUT = EVENT_LEVEL_ROOT / "active_set" / "all_market_vs_active_set_comparison.png"
SNAPSHOTS_OUTPUT = EVENT_LEVEL_ROOT / "distribution" / "representative_event_snapshots.png"

LIFECYCLE_ORDER = [">24h", "6-24h", "1-6h", "<1h"]
SNAPSHOT_ORDER = ["T-48", "T-24", "T-6", "T-1"]

BLUE = "#1f6fb2"
ORANGE = "#d36c2d"
GREEN = "#2a8f5a"
PURPLE = "#7356a4"
INK = "#1f2933"
GRID = "#d7dde5"
MUTED = "#5f6b7a"


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/msyh.ttc",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.ImageFont,
    fill: str = INK,
) -> None:
    width, height = _text_size(draw, text, font)
    draw.text((xy[0] - width / 2, xy[1] - height / 2), text, font=font, fill=fill)


def _fmt(value: float, metric: str) -> str:
    if not math.isfinite(float(value)):
        return "n/a"
    if metric in {"zero_rate", "top3_probability_mass"}:
        return f"{value:.1%}"
    if metric == "ic":
        return f"{value:+.3f}"
    if metric == "crossing_markout":
        return f"{value:+.4f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _delta_annotation(metric: str, delta: float) -> tuple[str, str]:
    if metric == "zero_rate":
        return f"Dynamic - all: {_fmt(delta, metric)} zero-rate", GREEN if delta <= 0 else "#b42318"
    if metric == "crossing_markout":
        return f"Dynamic - all: {_fmt(delta, metric)} crossing", GREEN if delta >= 0 else "#b42318"
    if metric == "top3_probability_mass":
        return f"Dynamic concentration: {_fmt(delta, metric)}", GREEN if delta >= 0 else MUTED
    return f"Dynamic - all: {_fmt(delta, metric)}", GREEN if delta >= 0 else "#b42318"


def _require_columns(frame: pd.DataFrame, path: Path, columns: set[str]) -> None:
    missing = sorted(columns.difference(frame.columns))
    if missing:
        raise ValueError(f"{path} missing required columns: {missing}")


def _scale(value: float, low: float, high: float, top: int, bottom: int) -> float:
    if math.isclose(low, high):
        high = low + 1.0
    return bottom - (float(value) - low) / (high - low) * (bottom - top)


def _nice_bounds(values: list[float], include_zero: bool = True) -> tuple[float, float]:
    finite = [float(value) for value in values if pd.notna(value) and math.isfinite(float(value))]
    if not finite:
        return 0.0, 1.0
    low = min(finite)
    high = max(finite)
    if include_zero:
        low = min(0.0, low)
        high = max(0.0, high)
    if math.isclose(low, high):
        pad = max(1.0, abs(high) * 0.1)
        return low - pad, high + pad
    pad = (high - low) * 0.12
    if include_zero and low >= 0.0:
        return 0.0, high + pad
    if include_zero and high <= 0.0:
        return low - pad, 0.0
    return low - pad, high + pad


def _draw_axis_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    title: str,
    labels: list[str],
    values: list[float],
    color: str,
    unit: str,
    include_zero: bool = True,
) -> None:
    left, top, right, bottom = box
    title_font = _font(23, bold=True)
    label_font = _font(17)
    small_font = _font(15)
    draw.text((left, top - 38), title, font=title_font, fill=INK)
    low, high = _nice_bounds(values, include_zero=include_zero)
    for tick in range(4):
        y = top + tick * (bottom - top) / 3
        value = high - tick * (high - low) / 3
        draw.line((left, y, right, y), fill=GRID, width=1)
        draw.text((left - 76, y - 9), _fmt(value, ""), font=small_font, fill=MUTED)
    draw.line((left, bottom, right, bottom), fill=INK, width=2)
    draw.line((left, top, left, bottom), fill=INK, width=2)
    coords: list[tuple[float, float]] = []
    for index, (label, value) in enumerate(zip(labels, values, strict=True)):
        x = left + index * (right - left) / max(1, len(labels) - 1)
        draw.line((x, bottom, x, bottom + 6), fill=INK, width=1)
        _draw_centered(draw, (x, bottom + 22), label, label_font)
        if pd.isna(value):
            continue
        y = _scale(float(value), low, high, top, bottom)
        coords.append((x, y))
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)
        _draw_centered(draw, (x, y - 22), _fmt(float(value), ""), small_font, fill=color)
    if len(coords) > 1:
        draw.line(coords, fill=color, width=4)
    draw.text((right - 95, top + 8), unit, font=small_font, fill=MUTED)


def render_lifecycle(lifecycle: pd.DataFrame) -> None:
    _require_columns(
        lifecycle,
        LIFECYCLE_CSV,
        {"lifecycle_bucket", "updates_per_minute", "trades_per_minute", "active_market_count"},
    )
    order = [bucket for bucket in LIFECYCLE_ORDER if bucket in set(lifecycle["lifecycle_bucket"])]
    if not order:
        raise ValueError("No lifecycle buckets available for rendering")
    summary = (
        lifecycle.groupby("lifecycle_bucket", observed=True)
        .agg(
            updates_per_minute=("updates_per_minute", "median"),
            trades_per_minute=("trades_per_minute", "median"),
            active_market_count=("active_market_count", "median"),
        )
        .reindex(order)
    )

    image = Image.new("RGB", (1400, 1180), "white")
    draw = ImageDraw.Draw(image)
    draw.text((55, 28), "Lifecycle activity: separate raw-unit panels", font=_font(34, bold=True), fill=INK)
    draw.text(
        (55, 72),
        "Event medians by time-to-expiry bucket. Updates/min, trades/min, and active markets are not forced onto one scale.",
        font=_font(19),
        fill=MUTED,
    )
    panels = [
        ("Updates per minute", "updates_per_minute", BLUE, "raw updates/min"),
        ("Trades per minute", "trades_per_minute", ORANGE, "raw trades/min"),
        ("Active market count", "active_market_count", GREEN, "raw markets"),
    ]
    for row, (title, column, color, unit) in enumerate(panels):
        top = 170 + row * 310
        values = [float(value) if pd.notna(value) else math.nan for value in summary[column].tolist()]
        _draw_axis_panel(draw, (150, top, 1290, top + 210), title, order, values, color, unit)

    LIFECYCLE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(LIFECYCLE_OUTPUT)


def render_active_set(active: pd.DataFrame) -> None:
    _require_columns(
        active,
        ACTIVE_SET_CSV,
        {"scope", "ic", "zero_rate", "crossing_markout", "top3_probability_mass", "rows", "event_slug"},
    )
    summary = (
        active.groupby("scope", observed=True)
        .agg(
            events=("event_slug", "nunique"),
            rows=("rows", "sum"),
            ic=("ic", "median"),
            zero_rate=("zero_rate", "median"),
            crossing_markout=("crossing_markout", "median"),
            top3_probability_mass=("top3_probability_mass", "median"),
        )
        .reindex(["all_market", "dynamic_active"])
    )
    if summary[["ic", "zero_rate", "crossing_markout", "top3_probability_mass"]].isna().any().any():
        raise ValueError("Active-set cache must contain all_market and dynamic_active rows")

    image = Image.new("RGB", (1500, 1020), "white")
    draw = ImageDraw.Draw(image)
    draw.text((55, 28), "All-market vs dynamic-active comparison", font=_font(34, bold=True), fill=INK)
    draw.text(
        (55, 72),
        "Median event-level diagnostics across IC, zero-rate, crossing markout, and Top3 probability mass.",
        font=_font(19),
        fill=MUTED,
    )
    metrics = [
        ("Information coefficient", "ic", "rank IC"),
        ("Zero-rate", "zero_rate", "fraction"),
        ("Crossing markout", "crossing_markout", "price pts"),
        ("Top3 probability mass", "top3_probability_mass", "fraction"),
    ]
    boxes = [(105, 160, 705, 455), (825, 160, 1425, 455), (105, 565, 705, 860), (825, 565, 1425, 860)]
    colors = {"all_market": BLUE, "dynamic_active": ORANGE}
    for (title, metric, unit), box in zip(metrics, boxes, strict=True):
        left, top, right, bottom = box
        draw.text((left, top - 48), title, font=_font(24, bold=True), fill=INK)
        draw.text((right - 95, top - 42), unit, font=_font(15), fill=MUTED)
        values = [float(summary.loc[scope, metric]) for scope in ["all_market", "dynamic_active"]]
        low, high = _nice_bounds(values, include_zero=True)
        if metric in {"zero_rate", "top3_probability_mass"}:
            low, high = 0.0, max(1.0, high)
        zero_y = _scale(0.0, low, high, top, bottom)
        for tick in range(4):
            y = top + tick * (bottom - top) / 3
            value = high - tick * (high - low) / 3
            draw.line((left, y, right, y), fill=GRID, width=1)
            draw.text((left - 72, y - 9), _fmt(value, metric), font=_font(14), fill=MUTED)
        draw.line((left, zero_y, right, zero_y), fill=INK, width=2)
        draw.line((left, top, left, bottom), fill=INK, width=2)
        bar_width = 120
        centers = [left + 210, right - 210]
        for scope, value, x in zip(["all_market", "dynamic_active"], values, centers, strict=True):
            y = _scale(value, low, high, top, bottom)
            draw.rectangle((x - bar_width / 2, min(y, zero_y), x + bar_width / 2, max(y, zero_y)), fill=colors[scope])
            _draw_centered(draw, (x, y - 22 if y < zero_y else y + 22), _fmt(value, metric), _font(16, bold=True), fill=colors[scope])
            _draw_centered(draw, (x, bottom + 25), scope.replace("_", " "), _font(16), fill=INK)
        delta = values[1] - values[0]
        annotation, annotation_color = _delta_annotation(metric, delta)
        draw.text(
            (left + 14, top + 12),
            annotation,
            font=_font(16, bold=True),
            fill=annotation_color,
        )
    draw.rectangle((55, 935, 1445, 975), outline=GRID, width=1)
    draw.text(
        (75, 945),
        f"Events: {int(summary.loc['all_market', 'events'])}; rows: all_market={int(summary.loc['all_market', 'rows']):,}, dynamic_active={int(summary.loc['dynamic_active', 'rows']):,}.",
        font=_font(17),
        fill=MUTED,
    )
    ACTIVE_SET_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(ACTIVE_SET_OUTPUT)


def render_representative_snapshots(snapshots: pd.DataFrame) -> None:
    _require_columns(
        snapshots,
        SNAPSHOTS_CSV,
        {"event_slug", "city", "event_date", "snapshot", "market_index", "market_label", "normalized_probability"},
    )
    counts = snapshots.groupby("event_slug")["snapshot"].agg(lambda values: set(values))
    complete_events = sorted(slug for slug, seen in counts.items() if set(SNAPSHOT_ORDER).issubset(seen))
    if not complete_events:
        raise ValueError("No representative event has all T-48/T-24/T-6/T-1 snapshots")
    event_sizes = snapshots[snapshots["event_slug"].isin(complete_events)].groupby("event_slug").size()
    representative = event_sizes.sort_values(ascending=False).index[0]
    event = snapshots[snapshots["event_slug"].eq(representative)].copy()
    event["market_index"] = pd.to_numeric(event["market_index"], errors="coerce")
    event = event.sort_values(["market_index", "market_label", "snapshot"])
    labels = (
        event[["market_index", "market_label"]]
        .drop_duplicates()
        .sort_values(["market_index", "market_label"])["market_label"]
        .astype(str)
        .tolist()
    )
    pivot = event.pivot_table(
        index="market_label",
        columns="snapshot",
        values="normalized_probability",
        aggfunc="last",
    ).reindex(index=labels, columns=SNAPSHOT_ORDER)

    city = str(event["city"].dropna().iloc[0])
    event_date = str(event["event_date"].dropna().iloc[0])
    image = Image.new("RGB", (1650, 1120), "white")
    draw = ImageDraw.Draw(image)
    draw.text((55, 28), "Representative event probability snapshots", font=_font(34, bold=True), fill=INK)
    draw.text(
        (55, 72),
        f"{city} {event_date}: same market bins shown at T-48, T-24, T-6, and T-1.",
        font=_font(19),
        fill=MUTED,
    )
    boxes = [(105, 155, 760, 500), (890, 155, 1545, 500), (105, 645, 760, 990), (890, 645, 1545, 990)]
    for snapshot, box, color in zip(SNAPSHOT_ORDER, boxes, [BLUE, PURPLE, ORANGE, GREEN], strict=True):
        left, top, right, bottom = box
        draw.text((left, top - 44), snapshot, font=_font(26, bold=True), fill=INK)
        values = [float(value) if pd.notna(value) else 0.0 for value in pivot[snapshot].tolist()]
        high = max(0.05, max(values) * 1.18)
        for tick in range(4):
            y = top + tick * (bottom - top) / 3
            value = high - tick * high / 3
            draw.line((left, y, right, y), fill=GRID, width=1)
            draw.text((left - 60, y - 9), f"{value:.0%}", font=_font(14), fill=MUTED)
        draw.line((left, bottom, right, bottom), fill=INK, width=2)
        draw.line((left, top, left, bottom), fill=INK, width=2)
        slot = (right - left) / max(1, len(labels))
        bar_width = max(10, slot * 0.72)
        for index, (label, value) in enumerate(zip(labels, values, strict=True)):
            x = left + index * slot + slot / 2
            y = _scale(value, 0.0, high, top, bottom)
            draw.rectangle((x - bar_width / 2, y, x + bar_width / 2, bottom), fill=color)
            if value >= high * 0.16:
                _draw_centered(draw, (x, y - 16), f"{value:.0%}", _font(13, bold=True), fill=color)
            if len(labels) <= 12:
                draw.text((x - 28, bottom + 10), str(label)[:9], font=_font(12), fill=INK)
        mass = sum(values)
        peak_index = max(range(len(values)), key=lambda idx: values[idx]) if values else 0
        note = f"mass={mass:.3f}; peak={labels[peak_index][:18]} ({values[peak_index]:.1%})"
        draw.text((left + 8, bottom + 48), note, font=_font(16), fill=MUTED)

    SNAPSHOTS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(SNAPSHOTS_OUTPUT)


def main() -> None:
    lifecycle = pd.read_csv(LIFECYCLE_CSV)
    active = pd.read_csv(ACTIVE_SET_CSV)
    snapshots = pd.read_csv(SNAPSHOTS_CSV)
    render_lifecycle(lifecycle)
    render_active_set(active)
    render_representative_snapshots(snapshots)
    for output in (LIFECYCLE_OUTPUT, ACTIVE_SET_OUTPUT, SNAPSHOTS_OUTPUT):
        print(output.relative_to(HERE))


if __name__ == "__main__":
    main()
