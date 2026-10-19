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
import datetime as dt
from datetime import date
from pathlib import Path


CURRENCY_FORMATS = {
    # 화면에 찍히는 문자열은 전부 여기서 만든다. 소수점 자릿수가 통화마다
    # 다르다 — 231,000.00 원은 아무도 그렇게 쓰지 않는다.
    "USD": lambda v: f"${v:,.2f}",
    "KRW": lambda v: f"{v:,.0f}원",
}
money = CURRENCY_FORMATS["USD"]


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


VIEW_WIDTH = 1000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--current", required=True, type=float)
    parser.add_argument("--entry-ceiling", required=True, type=float)
    parser.add_argument("--entry-label", required=True,
                        help="이 선이 무엇인지. 매수 트리거로 오해되지 않게 쓴다")
    parser.add_argument("--currency", default="USD", choices=sorted(CURRENCY_FORMATS),
                        help="표시 통화. 가격 문자열 형식을 정한다")
    # 시세 CSV 는 재현 테스트용으로 10년치를 담는다. 판단 화면에 10년 고점을
    # 띄우면 "고점 대비" 가 의사결정과 무관한 값이 된다. 화면은 창을 자른다.
    parser.add_argument("--lookback-days", type=int, default=365,
                        help="차트에 그릴 최근 일수 (0 이면 전체)")
    args = parser.parse_args()

    global money
    money = CURRENCY_FORMATS[args.currency]

    rows = read_prices(args.input)
    if args.lookback_days:
        cutoff = (dt.date.fromisoformat(rows[-1]["date"])
                  - dt.timedelta(days=args.lookback_days)).isoformat()
        windowed = [r for r in rows if r["date"] >= cutoff]
        if len(windowed) > 2:
            rows = windowed
    width, height = 1000, 360
    # 우측은 기준선 값 태그(62px) 자리를 비워둔다. 태그를 그림 위에 겹치면
    # 정작 봐야 할 가격 흐름을 가린다.
    left, right, top, bottom = 26, 74, 24, 42
    plot_width = width - left - right
    plot_height = height - top - bottom

    if args.entry_ceiling > 0:
        raw_low = min(min(row["low"] for row in rows), args.entry_ceiling)
    else:
        raw_low = min(row["low"] for row in rows)
    raw_high = max(row["high"] for row in rows)
    # 축 경계를 값 크기에 맞춰 반올림한다. 25 를 박아두면 USD 에서는 맞지만
    # KRW 에서는 231,000 을 25원 단위로 맞추는 무의미한 정밀도가 된다.
    grain = 10 ** math.floor(math.log10(max(raw_high - raw_low, 1))) / 4
    y_min = math.floor(raw_low / grain) * grain
    y_max = math.ceil(raw_high / grain) * grain

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
    segments = []
    for index in range(1, len(points)):
        previous = rows[index - 1]["close"]
        current = rows[index]["close"]
        direction = "up" if current > previous else "down" if current < previous else "flat"
        segments.append({
            "x1": f"{points[index - 1][0]:.2f}",
            "y1": f"{points[index - 1][1]:.2f}",
            "x2": f"{points[index][0]:.2f}",
            "y2": f"{points[index][1]:.2f}",
            "direction": direction,
        })

    # 눈금 간격을 값 범위에서 만든다. 50 을 박아두면 USD 에서는 그럴듯하지만
    # KRW(128,500~362,500)에서는 눈금이 수천 개 생겨 축이 검게 칠해진다.
    span = y_max - y_min
    step = 10 ** math.floor(math.log10(span)) / 2 if span > 0 else 1
    while span / step > 8:
        step *= 2
    first = math.ceil(y_min / step) * step
    guide_values, value = [], first
    while value <= y_max:
        guide_values.append(value)
        value += step
    guides = [
        {"value": money(value), "y": f"{y(value):.2f}"}
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

    # hover 용 점 데이터. 좌표와 **표시 문자열까지** 여기서 만든다.
    # 브라우저가 날짜를 포맷하거나 등락률을 다시 계산하면 화면이 두 번째
    # 계산 주체가 된다. JS 는 마우스 x 로 가장 가까운 점을 고르기만 한다.
    first_close = rows[0]["close"]
    points = []
    for index, row in enumerate(rows):
        previous = rows[index - 1]["close"] if index else row["close"]
        change = (row["close"] / previous - 1) * 100 if index else 0.0
        points.append({
            "x": f"{x(index):.2f}",
            "y": f"{y(row['close']):.2f}",
            "date": date.fromisoformat(row["date"]).strftime("%Y.%m.%d"),
            "close": money(row["close"]),
            "change": f"{change:+.2f}%",
            "changeDirection": "up" if change > 0 else "down" if change < 0 else "flat",
            "fromStart": f"{(row['close'] / first_close - 1) * 100:+.1f}%",
            "volume": f"{row['volume'] / 1e6:,.1f}M주",
        })

    max_index = max(range(len(rows)), key=lambda index: rows[index]["close"])
    min_index = min(range(len(rows)), key=lambda index: rows[index]["close"])
    latest = rows[-1]
    two_sessions_ago = rows[-3]

    def marker(label, index, placement):
        px = x(index)
        # viewBox 오른쪽(1000) 가까이면 바깥으로 써봐야 잘린다. 안쪽으로 돌린다.
        outward = px > VIEW_WIDTH * 0.8
        return {
            "label": label,
            "value": money(rows[index]["close"]),
            "date": rows[index]["date"],
            "x": f"{px:.2f}",
            "y": f"{y(rows[index]['close']):.2f}",
            "placement": placement,
            "anchor": "end" if outward else "start",
            "dx": "-7" if outward else "7",
        }

    # 마커도 전 구간 기준이다. "52주" 라고 쓰면 시세 파일이 더 길 때 틀린다.
    markers = [
        marker("기간 고점", max_index, "below"),
        marker("기간 저점", min_index, "above"),
        # 현재 참고가 기준선 라벨이 같은 높이에 있다. 아래로 내려 겹침을 피한다.
        marker("최근 종가", len(rows) - 1, "below"),
    ]

    output = {
        "schemaVersion": "price-chart/1.0.0",
        "viewBox": f"0 0 {width} {height}",
        "plotRight": f"{width - right:.0f}",
        "tagX": f"{width - right + 4:.0f}",
        "tagTextX": f"{width - right + 35:.0f}",
        "linePath": line_path,
        "areaPath": area_path,
        "segments": segments,
        "points": points,
        "guides": guides,
        "xLabels": x_labels,
        # 기준선 값은 실거래 차트처럼 우측 축 태그로 붙인다. 데이터 위에
        # 큰 박스를 띄우면 정작 봐야 할 가격 흐름을 가린다. 설명 문구는
        # 차트 밖 범례로 내린다.
        "entryLine": {
            "value": money(args.entry_ceiling),
            "y": f"{y(args.entry_ceiling):.2f}",
            "tagY": f"{y(args.entry_ceiling) - 9:.2f}",
            "tagTextY": f"{y(args.entry_ceiling) + 4:.2f}",
            "label": args.entry_label,
        },
        "currentLine": {
            "value": money(args.current),
            "y": f"{y(args.current):.2f}",
            "tagY": f"{y(args.current) - 9:.2f}",
            "tagTextY": f"{y(args.current) + 4:.2f}",
            "label": "현재 참고가",
        },
        "markers": markers,
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
