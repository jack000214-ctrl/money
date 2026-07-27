"use client";

import { useState, type CSSProperties } from "react";
import type { DecisionReport, Signal } from "../lib/decision-report";

type FinancialTabsProps = Pick<
  DecisionReport,
  "incomeStatement" | "cashFlow"
>;

function SignalMark({ signal }: { signal: Signal }) {
  return <span className={`signal-mark signal-${signal}`} aria-hidden="true" />;
}

export function FinancialTabs({
  incomeStatement,
  cashFlow,
}: FinancialTabsProps) {
  const [activeTab, setActiveTab] = useState<"cashflow" | "income">(
    "cashflow",
  );

  return (
    <div className="report-tabs financial-tabs">
      <div className="tab-list" role="tablist" aria-label="재무정보 보기">
        <button
          aria-controls="cashflow-panel"
          aria-selected={activeTab === "cashflow"}
          className={activeTab === "cashflow" ? "active" : ""}
          id="cashflow-tab"
          onClick={() => setActiveTab("cashflow")}
          role="tab"
          type="button"
        >
          <span>현금흐름</span>
          <small>판정에 더 중요</small>
        </button>
        <button
          aria-controls="income-panel"
          aria-selected={activeTab === "income"}
          className={activeTab === "income" ? "active" : ""}
          id="income-tab"
          onClick={() => setActiveTab("income")}
          role="tab"
          type="button"
        >
          <span>손익계산서</span>
          <small>왜 이익이 줄었나</small>
        </button>
      </div>

      <section
        aria-labelledby="cashflow-tab"
        className="tab-panel"
        hidden={activeTab !== "cashflow"}
        id="cashflow-panel"
        role="tabpanel"
      >
        <div className="cashflow-block">
          <h3>{cashFlow.title}</h3>
          <p className="block-conclusion">{cashFlow.conclusion}</p>
          <div className="chart-scroll">
          <svg
            aria-label="분기별 영업현금흐름과 설비투자 비교"
            className="cashflow-svg"
            role="img"
            viewBox={cashFlow.chart.viewBox}
          >
            {cashFlow.chart.ticks.map((tick) => (
              <g key={tick.label}>
                <line
                  className="cf-grid"
                  x1={cashFlow.chart.plotLeft}
                  x2={cashFlow.chart.plotRight}
                  y1={tick.y}
                  y2={tick.y}
                />
                <text className="cf-tick" x="46" y={tick.y}>
                  {tick.label}
                </text>
              </g>
            ))}
            {cashFlow.chart.groups.map((group) => (
              <g key={group.quarter}>
                <rect
                  className="cf-bar cf-ocf"
                  height={group.ocf.height}
                  width={cashFlow.chart.barWidth}
                  x={group.ocf.x}
                  y={group.ocf.y}
                />
                <text className="cf-value" x={group.ocf.labelX} y={group.ocf.labelY}>
                  {group.ocf.label}
                </text>
                <rect
                  className="cf-bar cf-capex"
                  height={group.capex.height}
                  width={cashFlow.chart.barWidth}
                  x={group.capex.x}
                  y={group.capex.y}
                />
                <text className="cf-value" x={group.capex.labelX} y={group.capex.labelY}>
                  {group.capex.label}
                </text>
                <text className="cf-quarter" x={group.labelX} y={cashFlow.chart.quarterLabelY}>
                  {group.quarter}
                </text>
                <text
                  className={`cf-fcf cf-fcf-${group.signal}`}
                  x={group.labelX}
                  y={cashFlow.chart.fcfLabelY}
                >
                  FCF {group.fcf}
                </text>
              </g>
            ))}
            <line
              className="cf-baseline"
              x1={cashFlow.chart.plotLeft}
              x2={cashFlow.chart.plotRight}
              y1={cashFlow.chart.baselineY}
              y2={cashFlow.chart.baselineY}
            />
          </svg>
          </div>
                    <div className="cash-legend">
            <span><i className="ocf-key" />영업현금흐름</span>
            <span><i className="capex-key" />CapEx</span>
          </div>
        </div>
      </section>

      <section
        aria-labelledby="income-tab"
        className="tab-panel"
        hidden={activeTab !== "income"}
        id="income-panel"
        role="tabpanel"
      >
        <div className="income-block">
          <h3>{incomeStatement.title}</h3>
          <div className="profit-equation">
            {incomeStatement.equation.map((item) => (
              <div
                className={`equation-item equation-${item.sign ?? "plus"}`}
                key={item.label}
              >
                <span>{item.label}</span>
                <strong>{item.value}</strong>
              </div>
            ))}
          </div>
          <div className="data-table-wrap">
            <table className="data-table income-table">
              <thead>
                <tr>
                  <th>2026 Q2</th>
                  <th>값</th>
                  <th>전년 동기</th>
                  <th>해석</th>
                </tr>
              </thead>
              <tbody>
                {incomeStatement.rows.map((row) => (
                  <tr key={row.metric}>
                    <th scope="row">{row.metric}</th>
                    <td>{row.value}</td>
                    <td>{row.comparison}</td>
                    <td>
                      <SignalMark signal={row.signal} />
                      {row.interpretation}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}
