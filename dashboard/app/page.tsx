import reportData from "../data/decision-report.json";
import chartData from "../data/price-chart.json";
import type {
  DecisionReport,
  PriceChart as PriceChartData,
  Signal,
} from "../lib/decision-report";
import { AnalysisDialog } from "./AnalysisDialog";
import { FinancialTabs } from "./FinancialTabs";
import { PositioningTabs } from "./PositioningTabs";
import { PriceChart } from "./PriceChart";
import { SensitivityModel } from "./SensitivityModel";
import { StockControlPanel } from "./StockControlPanel";
import { VerdictMark } from "./VerdictMark";

const report = reportData as unknown as DecisionReport;
const chart = chartData as PriceChartData;

function SignalMark({ signal }: { signal: Signal }) {
  return <span className={`signal-mark signal-${signal}`} aria-hidden="true" />;
}

function SectionTitle({
  number,
  title,
  description,
  confidence,
  confidenceLevel = "high",
}: {
  number: string;
  title: string;
  description?: string;
  confidence?: string;
  confidenceLevel?: "high" | "medium" | "lagged";
}) {
  // 번호를 별도 열로 두면 42px + 24px 여백만큼 제목·본문이 전부 오른쪽으로
  // 밀린다. 번호는 신뢰도 태그와 같은 줄에 두고, 본문은 왼쪽 끝에서 시작한다.
  return (
    <header className="section-title">
      <div className="section-meta">
        <span className="section-number">{number}</span>
        {confidence ? (
          <div className={`confidence-tag confidence-${confidenceLevel}`}>
            {confidence}
          </div>
        ) : null}
      </div>
      <h2>{title}</h2>
      {description ? <p>{description}</p> : null}
    </header>
  );
}

export default function Home() {
  return (
    <StockControlPanel
      currentTicker={report.asset.ticker.split(" · ")[0]}
      currentName={report.asset.name}
    >
    <main>
      <header className="site-header">
        <a href="#top" className="site-name">
          자산 증식 프로그램
        </a>
        <nav aria-label="보고서 바로가기">
          <a href="#portfolio">내 비중</a>
          <a href="#price">가격</a>
          <a href="#review">재검토</a>
          <a href="#financials">현금흐름</a>
          <a href="#valuation">가치</a>
          <a href="#peers">비교</a>
          <a href="#positioning">수급</a>
        </nav>
        <span className="as-of">{report.meta.asOf}</span>
      </header>

      <article className="report" id="top">
        <div className="asset-bar">
          <span className="asset-name">{report.asset.name}</span>
          <span>{report.asset.ticker}</span>
          <span>{report.meta.financialAsOf} 반영</span>
          <span className="asset-price">
            {report.asset.current}{report.asset.currency === "KRW"
              ? "" : ` · ${report.asset.currentBase}`}
          </span>
        </div>

        <section className="hero">
          <div className="hero-copy">
            <VerdictMark verdict={report.decision.verdict} />
            <div className="hero-words">
              <p className="hero-verdict-code">{report.decision.verdict}</p>
              <h1>{report.decision.status}</h1>
            <p className="hero-headline">{report.decision.headline}</p>
            <p className="hero-reason">{report.decision.reason}</p>
              <AnalysisDialog analysis={report.detailedAnalysis} />
            </div>
          </div>

          <aside className="decision-sheet" aria-label="현재 판정 요약">
            <div className="decision-price decision-why">
              <span>차단 사유</span>
              <strong>{report.decision.primaryReason}</strong>
              <small>{report.decision.hurdleResult}</small>
            </div>
            <dl>
              <div>
                <dt>지금 할 일</dt>
                <dd>{report.decision.actionNow}</dd>
              </div>
              <div>
                <dt>요구수익률을 만족하는 가격</dt>
                <dd className="decision-entry">{report.decision.entryCeiling}</dd>
                <small>{report.decision.entryCeilingNote}</small>
              </div>
              <div>
                <dt>현재 참고가</dt>
                <dd>{report.asset.current}{report.asset.currency === "KRW"
                  ? "" : ` · ${report.asset.currentBase}`}</dd>
                <small>{report.asset.baseNote}</small>
              </div>
            </dl>
          </aside>
        </section>

        {report.sensitivity?.current ? (
          <SensitivityModel model={report.sensitivity} />
        ) : null}

        <section className="decision-evidence" aria-label="판정을 결정한 핵심 근거">
          <div className="evidence-intro">
            <span>판정을 결정한 근거</span>
            <strong>숫자보다 먼저 볼 의미</strong>
          </div>
          <div className="decision-evidence-grid">
            {report.decision.highlights.map((item) => (
              <article
                className={`decision-evidence-card evidence-card-${item.signal}`}
                key={item.meaning}
              >
                <div className={`confidence-tag confidence-${item.confidenceLevel}`}>
                  {item.confidence}
                </div>
                <h2>{item.meaning}</h2>
                <strong>{item.value}</strong>
                <p>{item.context}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="section" id="portfolio" aria-label="내 포트폴리오에 미치는 영향">
          <SectionTitle
            number="01"
            title="이 거래가 내 포트폴리오에서 차지하는 자리"
            description="비중은 분모를 섞지 않습니다. 세 분모를 각각 금액과 함께 봅니다."
            confidence="보유내역 + 승인 IPS"
            confidenceLevel="high"
          />
          <p className="trade-line">
            검토 대상 거래 · <strong>{report.portfolio.quantity}주</strong> ×{" "}
            {report.portfolio.priceKrw} ={" "}
            <strong>{report.portfolio.tradeValueKrw}</strong>
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th>분모</th><th>거래 전</th><th>거래 후</th><th>분모 금액</th>
              </tr>
            </thead>
            <tbody>
              {report.portfolio.weights.map((w) => (
                <tr key={w.label}>
                  <th scope="row" title={w.definition}>{w.label}</th>
                  <td>{w.before}</td>
                  <td><strong>{w.after}</strong></td>
                  <td>{w.denominator}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <dl className="portfolio-facts">
            <div>
              <dt>하방 예산</dt>
              <dd>
                {report.portfolio.bearBudget.used} / 한도{" "}
                {report.portfolio.bearBudget.limit}
                {report.portfolio.bearBudget.withinBudget ? " · 이내" : " · 초과"}
              </dd>
              <small>{report.portfolio.bearBudget.note}</small>
            </div>
            {report.portfolio.costs ? (
              <div>
                <dt>비용·세금</dt>
                <dd>{report.portfolio.costs["총계"]}</dd>
                <small>{report.portfolio.costs["내역"]}</small>
              </div>
            ) : null}
            <div>
              <dt>결정 스냅샷</dt>
              <dd className="mono">{report.portfolio.snapshotId}</dd>
              <small>이 판정을 나중에 다시 대조할 때 쓰는 ID</small>
            </div>
          </dl>

          <details className="disclosure issue-disclosure">
            <summary>
              <span>엔진 판정 코드 {report.decision.issues.length}건</span>
              <small>{report.decision.verdictNote}</small>
            </summary>
            <ul className="issue-list">
              {report.decision.issues.map((issue) => (
                <li className={`issue issue-${issue.severity}`} key={issue.code}>
                  <p>{issue.message}</p>
                  <div className="issue-tags">
                    <span className="issue-severity">{issue.severity}</span>
                    <code>{issue.code}</code>
                  </div>
                </li>
              ))}
            </ul>
            <p className="issue-note">{report.decision.bullOnlyNote}</p>
          </details>
        </section>

        <section className="section price-section" id="price">
          <SectionTitle
            number="02"
            title="급락했지만, 아직 목표 수익률을 만드는 가격은 아닙니다."
            description="차트에서는 현재가보다 ‘다시 분석할 가격’과의 거리를 먼저 보세요."
            confidence="실제 종가"
            confidenceLevel="high"
          />

          <div className="chart-shell">
            <div className="chart-head">
              <div>
                <strong>{report.asset.ticker.split(" · ")[0]} 일별 종가</strong>
                <span>{chart.summary.period}</span>
              </div>
              <span>{chart.summary.sessions} · {report.asset.currency}</span>
            </div>

            <PriceChart chart={chart} />

            <div className="chart-legend">
              <span>
                <i className="legend-rise" />
                상승일
              </span>
              <span>
                <i className="legend-fall" />
                하락일
              </span>
              <span>
                <i className="legend-current" />
                현재 참고가 {chart.currentLine.value}
              </span>
              <span>
                <i className="legend-entry" />
                {chart.entryLine.label} {chart.entryLine.value}
              </span>
            </div>
          </div>

          <details className="disclosure">
            <summary>
              <span>가격 데이터 자세히</span>
              <small>최근 급락·52주 고저점·조건부 매수가</small>
            </summary>
            <div className="price-evidence">
              {report.priceEvidence.map((item) => (
                <article className={`evidence-${item.signal}`} key={item.label}>
                  <div>
                    <SignalMark signal={item.signal} />
                    <span>{item.label}</span>
                  </div>
                  <strong>{item.value}</strong>
                  <p>{item.explanation}</p>
                </article>
              ))}
            </div>
          </details>
        </section>

        <section className="section review-section" id="review">
          <SectionTitle
            number="03"
            title="기다리기만 하는 판정이 아닙니다. 이때 다시 분석합니다."
            description="가격 조건과 사업 논지 훼손 조건을 분리했습니다."
            confidence="실행 기준"
            confidenceLevel="high"
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

        <section className="section" id="financials">
          <SectionTitle
            number="04"
            title="매출은 성장했지만, 투자비가 현금 유입을 넘어섰습니다."
            description="본업의 매출과 마진은 버텼습니다. 지금 더 중요한 것은 AI 투자가 언제 현금으로 돌아오느냐입니다."
            confidence="공시 확정"
            confidenceLevel="high"
          />

          <FinancialTabs
            cashFlow={report.cashFlow}
            incomeStatement={report.incomeStatement}
          />

          <aside className="plain-conclusion">
            <strong>결론</strong>
            <p>
              “실적이 나빠진 것이 아니라 미래에 돈을 썼다”는 해석은 손익계산서에서는
              맞습니다. 하지만 현금흐름에서는 그 지출이 영업현금보다 커졌습니다.
              이제 확인할 것은 지출의 존재가 아니라 <em>회수 속도</em>입니다.
            </p>
          </aside>
        </section>

        <section className="section valuation-section" id="valuation">
          <SectionTitle
            number="05"
            title="좋은 회사라는 가정까지 넣어도, 현재 가격의 기대수익은 부족합니다."
            description={report.valuation.conclusion}
            confidence="가정 기반 모델"
            confidenceLevel="medium"
          />
          <div className="valuation-callout">
            <span>현재 가격이 요구하는 미래</span>
            <strong>{report.valuation.impliedGrowth}</strong>
            <p>{report.valuation.modelInputs}</p>
          </div>
          <div className="valuation-answer">
            <span>그래서 언제 다시 보나?</span>
            <strong>{report.decision.entryCeiling}</strong>
            <p>{report.valuation.entryExplanation}</p>
          </div>
          <details className="disclosure">
            <summary>
              <span>가치평가 시나리오 자세히</span>
              <small>약세·기준·강세 가정과 24개월 연환산 수익</small>
            </summary>
            <div className="scenario-table">
              <div className="scenario-head">
                <span>시나리오</span>
                <span>확률</span>
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
                  <span>{scenario.probability}</span>
                  <span>{scenario.fcfGrowth}</span>
                  <span>{scenario.valuePerShare}</span>
                  <span>{scenario.totalGap}</span>
                  <strong className={`scenario-return return-${scenario.signal}`}>
                    {scenario.annualizedReturn}
                  </strong>
                </div>
              ))}
            </div>
          </details>
        </section>

        <section className="section" id="peers">
          <SectionTitle
            number="06"
            title={report.peersTable.title}
            description={report.peersTable.conclusion}
            confidence="공시·시장 데이터"
            confidenceLevel="high"
          />
          <div className="peer-summary" aria-label="현재가 기준 배수">
            {report.valuation.multiples.map((item) => (
              <div className="peer-highlight-bad" key={item.label}>
                <span>{item.label}</span>
                <strong>{item.value}</strong>
                <small>{item.basis}</small>
              </div>
            ))}
          </div>
          <details className="disclosure">
            <summary>
              <span>동종사 비교표 자세히</span>
              <small>매출·PER·PSR · 기준일 주의</small>
            </summary>
            <div className="data-table-wrap peer-wrap">
              <table className="data-table peer-table">
                <thead>
                  <tr>
                    <th>기업</th>
                    <th>기준</th>
                    <th>매출</th>
                    <th>PER</th>
                    <th>PSR</th>
                  </tr>
                </thead>
                <tbody>
                  {report.peersTable.rows.map((row) => (
                    <tr className={row.focus ? "focus-row" : ""} key={row.ticker}>
                      <th scope="row">
                        <strong>{row.ticker}</strong>
                        <span>{row.company}</span>
                      </th>
                      <td>{row.basis}</td>
                      <td>{row.revenue}</td>
                      <td>{row.per}</td>
                      <td>{row.psr}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="table-note">{report.peersTable.basisWarning}</p>
          </details>
        </section>

        <section className="section positioning-section detail-tier" id="positioning">
          <SectionTitle
            number="07"
            title={report.marketPositioning.title}
            description={report.marketPositioning.conclusion}
            confidence="지연 공시"
            confidenceLevel="lagged"
          />

          <aside className="positioning-decision">
            <span>그래서?</span>
            <p>{report.marketPositioning.decisionUse}</p>
          </aside>

          <PositioningTabs items={report.marketPositioning.items} />
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
                {source.url ? (
                  <a href={source.url} target="_blank" rel="noreferrer">
                    {source.detail}
                  </a>
                ) : (
                  source.detail
                )}
              </li>
            ))}
          </ul>
          <p className="footer-id">
            {report.meta.calculationId} · {report.meta.fundamentalsId} · {report.meta.priceAsOf}
          </p>
        </footer>
      </article>
    </main>
    </StockControlPanel>
  );
}
