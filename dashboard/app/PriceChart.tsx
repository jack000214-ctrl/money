"use client";

import { useCallback, useRef, useState } from "react";
import type { PriceChart as PriceChartData } from "../lib/decision-report";

/**
 * 가격 차트의 hover 크로스헤어.
 *
 * 이 컴포넌트는 **계산하지 않는다.** 날짜 포맷, 종가, 등락률, 거래량은 전부
 * build_price_chart.py 가 만들어 둔 문자열을 그대로 쓴다. 브라우저가 값을
 * 다시 만들면 화면이 두 번째 계산 주체가 되고, 그건 이 프로젝트가 계속
 * 막아온 "두 개의 진실" 이다.
 *
 * JS 가 하는 일은 하나뿐이다 — 마우스 x 로 가장 가까운 점을 고른다.
 */
export function PriceChart({ chart }: { chart: PriceChartData }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [active, setActive] = useState<number | null>(null);

  // viewBox 는 "0 0 1000 360". 화면 폭이 얼마든 좌표계는 1000 단위다.
  const VIEW_WIDTH = 1000;

  const pick = useCallback(
    (clientX: number) => {
      const svg = svgRef.current;
      if (!svg) return;
      const rect = svg.getBoundingClientRect();
      const viewX = ((clientX - rect.left) / rect.width) * VIEW_WIDTH;
      let best = 0;
      let bestGap = Infinity;
      for (let i = 0; i < chart.points.length; i += 1) {
        const gap = Math.abs(Number(chart.points[i].x) - viewX);
        if (gap < bestGap) {
          bestGap = gap;
          best = i;
        }
      }
      setActive(best);
    },
    [chart.points],
  );

  const point = active === null ? null : chart.points[active];
  // 툴팁이 오른쪽 끝에서 잘리지 않게 방향만 바꾼다.
  const flip = point ? Number(point.x) > VIEW_WIDTH * 0.62 : false;

  return (
    <div className="chart-hover-wrap">
      <div className="chart-scroll">
      <svg
        aria-label={`일별 종가. 현재 참고가 ${chart.currentLine.value}, ${chart.entryLine.label} ${chart.entryLine.value}`}
        className="price-chart"
        onMouseLeave={() => setActive(null)}
        onMouseMove={(event) => pick(event.clientX)}
        onTouchMove={(event) => pick(event.touches[0].clientX)}
        onTouchStart={(event) => pick(event.touches[0].clientX)}
        ref={svgRef}
        role="img"
        viewBox={chart.viewBox}
      >
        <defs>
          <linearGradient id="area-fill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#7c828d" stopOpacity="0.16" />
            <stop offset="100%" stopColor="#7c828d" stopOpacity="0" />
          </linearGradient>
        </defs>

        {chart.guides.map((guide) => (
          <g key={guide.value}>
            <line className="chart-grid" x1="26" x2={chart.plotRight} y1={guide.y} y2={guide.y} />
            <text className="chart-y-label" x="24" y={guide.y}>
              {guide.value}
            </text>
          </g>
        ))}

        <path className="chart-area" d={chart.areaPath} />
        <path className="chart-line" d={chart.linePath} />
        {chart.segments.map((segment, index) => (
          <line
            className={`price-segment segment-${segment.direction}`}
            key={`${segment.x1}-${segment.x2}-${index}`}
            vectorEffect="non-scaling-stroke"
            x1={segment.x1}
            x2={segment.x2}
            y1={segment.y1}
            y2={segment.y2}
          />
        ))}

        <line
          className="threshold-line current-threshold"
          x1="26"
          x2={chart.plotRight}
          y1={chart.currentLine.y}
          y2={chart.currentLine.y}
        />
        <g className="axis-tag current-tag">
          <rect height="18" rx="3" width="62" x={chart.tagX} y={chart.currentLine.tagY} />
          <text x={chart.tagTextX} y={chart.currentLine.tagTextY}>
            {chart.currentLine.value}
          </text>
        </g>

        <line
          className="threshold-line entry-threshold"
          x1="26"
          x2={chart.plotRight}
          y1={chart.entryLine.y}
          y2={chart.entryLine.y}
        />
        <g className="axis-tag entry-tag">
          <rect height="18" rx="3" width="62" x={chart.tagX} y={chart.entryLine.tagY} />
          <text x={chart.tagTextX} y={chart.entryLine.tagTextY}>
            {chart.entryLine.value}
          </text>
        </g>

        {chart.markers.map((marker) => (
          <g className={`chart-marker marker-${marker.placement}`} key={marker.label}>
            <circle cx={marker.x} cy={marker.y} r="4" />
            <text
              dx={marker.dx}
              dy={marker.placement === "above" ? "-10" : "20"}
              textAnchor={marker.anchor}
              x={marker.x}
              y={marker.y}
            >
              {marker.label} {marker.value}
            </text>
          </g>
        ))}

        {chart.xLabels.map((tick) => (
          <text className="chart-x-label" key={tick.label} x={tick.x} y="352">
            {tick.label}
          </text>
        ))}

        {point ? (
          <g className="chart-crosshair" pointerEvents="none">
            <line x1={point.x} x2={point.x} y1="12" y2="330" />
            <circle cx={point.x} cy={point.y} r="4.5" />
          </g>
        ) : null}
      </svg>
      </div>

      {point ? (
        <div
          className={`chart-tooltip${flip ? " tooltip-flip" : ""}`}
          style={{ left: `${(Number(point.x) / VIEW_WIDTH) * 100}%` }}
        >
          <strong>{point.date}</strong>
          <span className="tooltip-close">{point.close}</span>
          <span className={`tooltip-change change-${point.changeDirection}`}>
            전일 {point.change}
          </span>
          <span className="tooltip-meta">거래량 {point.volume}</span>
          <span className="tooltip-meta">기간 시작 대비 {point.fromStart}</span>
        </div>
      ) : null}
    </div>
  );
}
