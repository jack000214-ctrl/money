#!/usr/bin/env python3
"""일별 가격 CSV를 계산 없는 화면용 SVG 좌표로 변환한다.

브라우저는 이 파일이 만든 path와 좌표를 그대로 그린다. 축 범위, 좌표,
고점·저점·기준선의 위치를 화면에서 다시 계산하지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import date
from pathlib import Path


def read_prices(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as source:
        rows = list(csv.DictReader(line for line in source if not line.startswith("#")))
    return [
        {
            "date": row["date"],
            "close": float(row["close"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "volume": int(row["volume"]),
        }
        for row in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--current", required=True, type=float)
    parser.add_argument("--entry-ceiling", required=True, type=float)
    args = parser.parse_args()

    rows = read_prices(args.input)
    width, height = 1000, 360
    left, right, top, bottom = 26, 24, 24, 42
    plot_width = width - left - right
    plot_height = height - top - bottom

    raw_low = min(min(row["low"] for row in rows), args.entry_ceiling)
    raw_high = max(row["high"] for row in rows)
    y_min = math.floor(raw_low / 25) * 25
    y_max = math.ceil(raw_high / 25) * 25

    def x(index: int) -> float:
        return left + index / (len(rows) - 1) * plot_width

    def y(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_height

    points = [(x(index), y(row["close"])) for index, row in enumerate(rows)]
    line_path = " ".join(
        f"{'M' if index == 0 else 'L'}{px:.2f},{py:.2f}"
        for index, (px, py) in enumerate(points)
    )
    area_path = (
        line_path
        + f" L{points[-1][0]:.2f},{top + plot_height:.2f}"
        + f" L{points[0][0]:.2f},{top + plot_height:.2f} Z"
    )

    guide_values = list(range(int(y_min), int(y_max) + 1, 50))
    guides = [
        {"value": f"${value}", "y": f"{y(value):.2f}"}
        for value in reversed(guide_values)
    ]

    month_indices = [0]
    previous_month = rows[0]["date"][:7]
    for index, row in enumerate(rows[1:], 1):
        month = row["date"][:7]
        if month != previous_month:
            month_indices.append(index)
            previous_month = month
    selected_months = month_indices[::2]
    if month_indices[-1] not in selected_months:
        selected_months.append(month_indices[-1])
    x_labels = [
        {
            "label": date.fromisoformat(rows[index]["date"]).strftime("%Y.%m"),
            "x": f"{x(index):.2f}",
        }
        for index in selected_months
    ]

    max_index = max(range(len(rows)), key=lambda index: rows[index]["close"])
    min_index = min(range(len(rows)), key=lambda index: rows[index]["close"])
    latest = rows[-1]
    two_sessions_ago = rows[-3]

    output = {
        "schemaVersion": "price-chart/1.0.0",
        "viewBox": f"0 0 {width} {height}",
        "linePath": line_path,
        "areaPath": area_path,
        "guides": guides,
        "xLabels": x_labels,
        "entryLine": {
            "value": f"${args.entry_ceiling:,.2f}",
            "y": f"{y(args.entry_ceiling):.2f}",
            "labelRectY": f"{y(args.entry_ceiling) - 26:.2f}",
            "labelTextY": f"{y(args.entry_ceiling) - 11:.2f}",
            "label": "연 12% 목표를 만족하는 강세 가정 매수가 상한",
        },
        "currentLine": {
            "value": f"${args.current:,.2f}",
            "y": f"{y(args.current):.2f}",
            "labelRectY": f"{y(args.current) - 26:.2f}",
            "labelTextY": f"{y(args.current) - 11:.2f}",
            "label": "현재 참고가",
        },
        "markers": [
            {
                "label": "52주 고점",
                "value": f"${rows[max_index]['close']:,.2f}",
                "date": rows[max_index]["date"],
                "x": f"{x(max_index):.2f}",
                "y": f"{y(rows[max_index]['close']):.2f}",
                "placement": "below",
            },
            {
                "label": "52주 저점",
                "value": f"${rows[min_index]['close']:,.2f}",
                "date": rows[min_index]["date"],
                "x": f"{x(min_index):.2f}",
                "y": f"{y(rows[min_index]['close']):.2f}",
                "placement": "above",
            },
            {
                "label": "최근 종가",
                "value": f"${latest['close']:,.2f}",
                "date": latest["date"],
                "x": f"{x(len(rows) - 1):.2f}",
                "y": f"{y(latest['close']):.2f}",
                "placement": "above",
            },
        ],
        "summary": {
            "period": f"{rows[0]['date']} — {latest['date']}",
            "sessions": f"{len(rows)} 거래일",
            "twoSessionMove": f"{(latest['close'] / two_sessions_ago['close'] - 1) * 100:+.1f}%",
            "fromHigh": f"{(args.current / rows[max_index]['close'] - 1) * 100:+.1f}%",
            "fromLow": f"{(args.current / rows[min_index]['close'] - 1) * 100:+.1f}%",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
