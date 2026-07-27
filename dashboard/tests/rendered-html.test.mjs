import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html", host: "localhost" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

const readReport = async () =>
  JSON.parse(
    await readFile(new URL("../data/decision-report.json", import.meta.url), "utf8"),
  );

test("server-renders the information-first TSLA decision report", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /숫자보다 먼저 볼 의미/);
  assert.match(html, /공시 확정/);
  assert.match(html, /role="tab"/);
  assert.match(html, /현금흐름/);
  assert.match(html, /손익계산서/);
  assert.match(html, /<details class="disclosure"/);
  assert.match(html, /13F 기관 보유/);
  assert.match(html, /매출총이익률/);
  assert.match(html, /−\$1,092M/);
  assert.match(html, /General Motors/);
  assert.match(html, /10년 FCF 연 38\.2%/);
  assert.doesNotMatch(html, /DEMO DATA|TWELVE/);
});

test("renders the engine verdict without softening it", async () => {
  const html = await (await render()).text();
  const report = await readReport();

  // SKILL 은 "엔진의 BLOCKED 를 자연어로 완화" 를 금지한다. 원본 판정과
  // issue code 가 화면에 그대로 있어야 한다.
  assert.equal(report.decision.verdict, "BLOCKED");
  assert.match(html, /EXPECTED_RETURN_BELOW_HURDLE/);
  assert.ok(report.decision.issues.length > 0);
  for (const issue of report.decision.issues) {
    assert.match(html, new RegExp(issue.code));
  }
  // 결정 스냅샷 ID 가 있어야 나중에 이 판정을 대조할 수 있다.
  assert.match(report.portfolio.snapshotId, /^TRADE-SNAPSHOT-[0-9a-f]{20}$/);
  assert.match(html, new RegExp(report.portfolio.snapshotId));
});

test("entry price uses the IPS measure, not the bull scenario alone", async () => {
  const report = await readReport();
  const strip = (s) => Number(s.replace(/[^0-9.]/g, ""));

  // 한때 화면은 강세 시나리오 단독으로 만든 $282.46 을 매수 트리거로 보여줬다.
  // IPS 는 확률가중이라 그 가격에서도 기대수익이 요구수익률에 한참 못 미친다.
  assert.equal(report.decision.measure, "PROBABILITY_WEIGHTED");
  const hurdle = strip(report.decision.entryCeiling);
  const bullOnly = strip(report.decision.bullOnlyCeiling);
  assert.ok(
    hurdle < bullOnly,
    `IPS 기준 가격(${hurdle})은 강세 단독 가격(${bullOnly})보다 낮아야 한다`,
  );
  assert.match(report.decision.entryCeilingNote, /가격 조정으로는/);
});

test("multiples are computed at the report's own price", async () => {
  const report = await readReport();
  const num = (s) => Number(s.replace(/[^0-9.]/g, ""));
  const price = num(report.asset.currentUsd);

  // PER 325x / PSR 11.9x 는 급락 전 주가 $349 로 계산된 값이었다.
  // 배수와 주가가 같은 시점인지 역산해 확인한다.
  const shares = 3540; // 백만주 · 스냅샷의 희석주식수
  for (const label of ["PER", "PSR"]) {
    const m = report.valuation.multiples.find((x) => x.label === label);
    const denominator = num(m.basis.match(/\$[\d,]+M/)[0]);
    const impliedPrice = (num(m.value) * denominator) / shares;
    assert.ok(
      Math.abs(impliedPrice - price) / price < 0.02,
      `${label} ${m.value} 가 현재가 $${price} 와 맞지 않는다 ` +
        `(역산 $${impliedPrice.toFixed(2)})`,
    );
  }
});

test("year-over-year rows really compare the same quarter", async () => {
  const report = await readReport();
  const revenue = report.incomeStatement.rows.find((r) => r.metric === "매출");
  // TTM 창(4분기)의 첫 행과 비교하면 전년 동기가 아니라 3분기 전이 된다.
  // 그때는 매출 YoY 가 +0.5% 로 조용히 틀렸다.
  assert.match(revenue.comparison, /\+25\.5%/);
});

test("cited calculation and snapshot actually resolve", async () => {
  const report = await readReport();
  assert.match(report.meta.calculationId, /^CALC-[0-9a-f]{20}$/);
  assert.match(report.meta.fundamentalsId, /^FUND-[0-9a-f]{20}$/);
  // 하드코딩된 ID 를 그냥 찍으면 존재하지 않는 계산을 출처처럼 보여주게 된다.
  await access(
    new URL(
      `../../knowledge/calculations/${report.meta.calculationId}.json`,
      import.meta.url,
    ),
  );
});

test("no stale figures survive anywhere in the report", async () => {
  const text = await readFile(
    new URL("../data/decision-report.json", import.meta.url),
    "utf8",
  );
  for (const stale of ["325.0x", "11.9x", "325배", "11.9배"]) {
    assert.ok(!text.includes(stale), `낡은 수치 ${stale} 가 남아 있다`);
  }
});

test("portfolio impact is reported with all three denominators", async () => {
  const report = await readReport();
  const labels = report.portfolio.weights.map((w) => w.label);
  assert.deepEqual(labels, ["총자산", "주식 슬리브", "위험자산"]);
  for (const w of report.portfolio.weights) {
    assert.match(w.before, /%$/);
    assert.match(w.after, /%$/);
    assert.match(w.denominator, /^₩[\d,]+$/);
    assert.ok(w.definition.length > 0, "분모 정의가 없으면 비중을 오독한다");
  }
  assert.match(report.portfolio.bearBudget.used, /%$/);
  assert.equal(typeof report.portfolio.bearBudget.withinBudget, "boolean");
});

test("keeps calculations upstream and renders precomputed geometry", async () => {
  const [page, financialTabs, positioningTabs, chartText] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/FinancialTabs.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/PositioningTabs.tsx", import.meta.url), "utf8"),
    readFile(new URL("../data/tsla-price-chart.json", import.meta.url), "utf8"),
  ]);
  const report = await readReport();
  const chart = JSON.parse(chartText);

  assert.equal(report.schemaVersion, "decision-dashboard/0.6.0");
  assert.equal(report.decision.highlights.length, 4);
  assert.equal(report.marketPositioning.items.length, 4);
  assert.equal(
    new Set(report.marketPositioning.items.map((item) => item.id)).size,
    4,
  );
  assert.equal(chart.schemaVersion, "price-chart/1.0.0");
  assert.ok(chart.linePath.startsWith("M"));
  assert.equal(chart.segments.length, 250);
  assert.ok(chart.segments.some((segment) => segment.direction === "up"));
  assert.ok(chart.segments.some((segment) => segment.direction === "down"));
  assert.ok(report.cashFlow.rows.every((row) => /^\d+%$/.test(row.ocfHeight)));
  assert.doesNotMatch(
    `${page}\n${financialTabs}\n${positioningTabs}`,
    /\bMath\.|\breduce\(|toLocaleString|Intl\.NumberFormat|\bNumber\(/,
  );
  await access(new URL("../scripts/build_price_chart.py", import.meta.url));
  await access(new URL("../scripts/build_decision_report.py", import.meta.url));
});
