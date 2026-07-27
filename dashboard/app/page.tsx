import type { CSSProperties } from "react";
import reportData from "../data/decision-report.json";
import chartData from "../data/tsla-price-chart.json";
import type {
  DecisionReport,
  PriceChart,
  Signal,
} from "../lib/decision-report";
import { AnalysisDialog } from "./AnalysisDialog";

const report = reportData as DecisionReport;
const chart = chartData as PriceChart;

function SignalMark({ signal }: { signal: Signal }) {
  return <span className={`signal-mark signal-${signal}`} aria-hidden="true" />;
}

function SectionTitle({
  number,
  title,
  description,
}: {
  number: string;
  title: string;
  description?: string;
}) {
  return (
    <header className="section-title">
      <span>{number}</span>
      <div>
        <h2>{title}</h2>
        {description ? <p>{description}</p> : null}
      </div>
    </header>
  );
}

export default function Home() {
  return (
    <main>
      <header className="site-header">
        <a href="#top" className="site-name">
          자산 증식 프로그램
        </a>
        <nav aria-label="보고서 바로가기">
          <a href="#price">가격</a>
          <a href="#financials">실적</a>
          <a href="#peers">동종사</a>
          <a href="#valuation">가치</a>
          <a href="#review">재검토</a>
        </nav>
        <span className="as-of">{report.meta.asOf}</span>
      </header>

      <article className="report" id="top">
        <section className="hero">
          <div className="hero-copy">
            <div className="asset-line">
              <span>{report.asset.ticker}</span>
              <span>{report.meta.financialAsOf} 반영</span>
            </div>
            <div className="decision-status">
              <SignalMark signal="bad" />
              {report.decision.status}
            </div>
            <h1>{report.decision.headline}</h1>
            <p className="hero-reason">{report.decision.reason}</p>
            <AnalysisDialog analysis={report.detailedAnalysis} />
          </div>

          <aside className="decision-sheet" aria-label="현재 판정 요약">
            <div className="decision-price">
              <span>현재 참고가</span>
              <strong>{report.asset.currentUsd}</strong>
              <small>{report.asset.currentKrw}</small>
            </div>
            <dl>
              <div>
                <dt>매수 검토 가격</dt>
                <dd>{report.decision.entryCeiling}</dd>
                <small>{report.decision.entryCeilingKrw}</small>
              </div>
              <div>
                <dt>수익 허들</dt>
                <dd>{report.decision.hurdleResult}</dd>
              </div>
              <div>
                <dt>지금 할 일</dt>
                <dd>{report.decision.actionNow}</dd>
              </div>
            </dl>
          </aside>
        </section>

        <section className="section price-section" id="price">
          <SectionTitle
            number="01"
            title="급락한 것은 맞습니다. 그래도 아직 목표 매수가에는 닿지 않았습니다."
            description="최근 1년 일별 종가와 연 12% 목표를 만족하는 조건부 매수가를 함께 표시했습니다."
          />

          <div className="chart-shell">
            <div className="chart-head">
              <div>
                <strong>TSLA 일별 종가</strong>
                <span>{chart.summary.period}</span>
              </div>
              <span>{chart.summary.sessions} · USD</span>
            </div>

            <svg
              className="price-chart"
              viewBox={chart.viewBox}
              role="img"
              aria-label={`테슬라 일별 종가. 현재 참고가 ${chart.currentLine.value}, 매수 검토 상한 ${chart.entryLine.value}`}
            >
              <defs>
                <linearGradient id="area-fill" x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0%" stopColor="#3159d8" stopOpacity="0.2" />
                  <stop offset="100%" stopColor="#3159d8" stopOpacity="0" />
                </linearGradient>
              </defs>

              {chart.guides.map((guide) => (
                <g key={guide.value}>
                  <line
                    className="chart-grid"
                    x1="26"
                    x2="976"
                    y1={guide.y}
                    y2={guide.y}
                  />
                  <text className="chart-y-label" x="24" y={guide.y}>
                    {guide.value}
                  </text>
                </g>
              ))}

              <path className="chart-area" d={chart.areaPath} />
              <path className="chart-line" d={chart.linePath} />

              <line
                className="threshold-line current-threshold"
                x1="26"
                x2="976"
                y1={chart.currentLine.y}
                y2={chart.currentLine.y}
              />
              <g className="threshold-label current-label">
                <rect x="808" y={chart.currentLine.labelRectY} width="168" height="22" />
                <text x="818" y={chart.currentLine.labelTextY}>
                  현재 {chart.currentLine.value}
                </text>
              </g>

              <line
                className="threshold-line entry-threshold"
                x1="26"
                x2="976"
                y1={chart.entryLine.y}
                y2={chart.entryLine.y}
              />
              <g className="threshold-label entry-label">
                <rect x="690" y={chart.entryLine.labelRectY} width="286" height="22" />
                <text x="700" y={chart.entryLine.labelTextY}>
                  매수 검토 상한 {chart.entryLine.value}
                </text>
              </g>

              {chart.markers.map((marker) => (
                <g className={`chart-marker marker-${marker.placement}`} key={marker.label}>
                  <circle cx={marker.x} cy={marker.y} r="4" />
                  <text
                    x={marker.x}
                    y={marker.y}
                    dx="7"
                    dy={marker.placement === "above" ? "-10" : "20"}
                  >
                    {marker.label} {marker.value}
                  </text>
                </g>
              ))}

              {chart.xLabels.map((label) => (
                <text className="chart-x-label" x={label.x} y="348" key={label.label}>
                  {label.label}
                </text>
              ))}
            </svg>

            <div className="chart-legend">
              <span>
                <i className="legend-line" />
                일별 종가
              </span>
              <span>
                <i className="legend-current" />
                현재 참고가
              </span>
              <span>
                <i className="legend-entry" />
                조건부 매수 검토 상한
              </span>
            </div>
          </div>

          <div className="price-evidence">
            {report.priceEvidence.map((item) => (
              <article key={item.label}>
                <div>
                  <SignalMark signal={item.signal} />
                  <span>{item.label}</span>
                </div>
                <strong>{item.value}</strong>
                <p>{item.explanation}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="section" id="financials">
          <SectionTitle
            number="02"
            title="왜 영업이익은 줄었나"
            description={report.incomeStatement.conclusion}
          />

          <div className="financial-layout">
            <div className="income-block">
              <h3>{report.incomeStatement.title}</h3>
              <div className="profit-equation">
                {report.incomeStatement.equation.map((item) => (
                  <div className={`equation-item equation-${item.sign ?? "plus"}`} key={item.label}>
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
                    {report.incomeStatement.rows.map((row) => (
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

            <div className="cashflow-block">
              <h3>{report.cashFlow.title}</h3>
              <p className="block-conclusion">{report.cashFlow.conclusion}</p>
              <div className="cashflow-chart" aria-label="분기별 영업현금흐름과 설비투자 비교">
                {report.cashFlow.rows.map((row) => (
                  <div className="cash-quarter" key={row.quarter}>
                    <div className="bar-stage">
                      <div
                        className="cash-bar ocf-bar"
                        style={{ "--bar-height": row.ocfHeight } as CSSProperties}
                      >
                        <span>{row.operatingCashFlow}</span>
                      </div>
                      <div
                        className="cash-bar capex-bar"
                        style={{ "--bar-height": row.capexHeight } as CSSProperties}
                      >
                        <span>{row.capex}</span>
                      </div>
                    </div>
                    <strong>{row.quarter}</strong>
                    <p className={`fcf-result result-${row.signal}`}>
                      FCF {row.freeCashFlow}
                    </p>
                  </div>
                ))}
              </div>
              <div className="cash-legend">
                <span><i className="ocf-key" />영업현금흐름</span>
                <span><i className="capex-key" />CapEx</span>
              </div>
            </div>
          </div>

          <aside className="plain-conclusion">
            <strong>결론</strong>
            <p>
              “실적이 나빠진 것이 아니라 미래에 돈을 썼다”는 해석은 손익계산서에서는
              맞습니다. 하지만 현금흐름에서는 그 지출이 영업현금보다 커졌습니다.
              이제 확인할 것은 지출의 존재가 아니라 <em>회수 속도</em>입니다.
            </p>
          </aside>
        </section>

        <section className="section" id="peers">
          <SectionTitle
            number="03"
            title={report.peers.title}
            description={report.peers.conclusion}
          />
          <div className="data-table-wrap peer-wrap">
            <table className="data-table peer-table">
              <thead>
                <tr>
                  <th>기업</th>
                  <th>기준</th>
                  <th>매출</th>
                  <th>GPM</th>
                  <th>OPM</th>
                  <th>최근 매출 YoY</th>
                  <th>PER</th>
                  <th>PSR</th>
                </tr>
              </thead>
              <tbody>
                {report.peers.rows.map((row) => (
                  <tr className={row.focus ? "focus-row" : ""} key={row.ticker}>
                    <th scope="row">
                      <strong>{row.ticker}</strong>
                      <span>{row.company}</span>
                    </th>
                    <td>{row.basis}</td>
                    <td>{row.revenue}</td>
                    <td>{row.grossMargin}</td>
                    <td>{row.operatingMargin}</td>
                    <td>{row.revenueGrowth}</td>
                    <td>{row.per}</td>
                    <td>{row.psr}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="table-note">
            GPM이 ‘—’인 기업은 동일 기간·동일 기준으로 맞는 XBRL 값이 없어 추측하지
            않았습니다. 사업 구성과 기준일이 달라 절대 순위가 아니라 가격 프리미엄의
            크기를 확인하는 표입니다.
          </p>
        </section>

        <section className="section" id="valuation">
          <SectionTitle
            number="04"
            title={report.valuation.title}
            description={report.valuation.conclusion}
          />
          <div className="valuation-callout">
            <span>현재가에 내재된 요구</span>
            <strong>{report.valuation.impliedGrowth}</strong>
            <p>{report.valuation.modelInputs}</p>
          </div>
          <div className="scenario-table">
            <div className="scenario-head">
              <span>시나리오</span>
              <span>FCF 성장 가정</span>
              <span>주당가치</span>
              <span>현재가와 격차</span>
              <span>24개월 연환산</span>
            </div>
            {report.valuation.scenarios.map((scenario) => (
              <div className="scenario-data" key={scenario.name}>
                <strong>
                  <SignalMark signal={scenario.signal} />
                  {scenario.name}
                </strong>
                <span>{scenario.fcfGrowth}</span>
                <span>{scenario.valuePerShare}</span>
                <span>{scenario.totalGap}</span>
                <strong className={`scenario-return return-${scenario.signal}`}>
                  {scenario.annualizedReturn}
                </strong>
              </div>
            ))}
          </div>
          <div className="valuation-answer">
            <span>그래서 얼마면 다시 볼까?</span>
            <strong>$282.46 이하</strong>
            <p>
              강세 가치 $354.32가 24개월 뒤 실현된다는 매우 낙관적인 가정으로도
              연 12%를 얻기 위한 매수가 상한입니다. 자동 매수가 아니라 분석을 다시
              여는 가격입니다.
            </p>
          </div>
        </section>

        <section className="section" id="review">
          <SectionTitle
            number="05"
            title="가격이 움직이면, 판단을 다시 여는 지점"
            description="가격 경고와 사업 논지 훼손을 분리했습니다."
          />
          <div className="review-list">
            {report.reviewMap.map((item) => (
              <article key={item.when}>
                <SignalMark signal={item.signal} />
                <div className="review-when">{item.when}</div>
                <div className="review-state">{item.state}</div>
                <div className="review-action">
                  <strong>{item.action}</strong>
                  <p>{item.why}</p>
                </div>
              </article>
            ))}
          </div>
        </section>

        <footer>
          <div>
            <strong>근거와 기준시점</strong>
            <p>
              화면은 계산하지 않습니다. 가격 좌표, 매수가, 수익률, 막대 높이는 검증된
              로컬 산출물을 그대로 표시합니다.
            </p>
          </div>
          <ul>
            {report.sources.map((source) => (
              <li key={source.label}>
                <span>{source.label}</span>
                {source.detail}
              </li>
            ))}
          </ul>
          <p className="footer-id">
            {report.meta.calculationId} · {report.meta.priceAsOf}
          </p>
        </footer>
      </article>
    </main>
  );
}
