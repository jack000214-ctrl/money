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
  // 문구는 바뀔 수 있으니 뜻을 검사한다: 가격만으로는 안 풀린다는 설명이 있어야 한다.
  assert.match(report.decision.entryCeilingNote, /가격/);
  assert.match(report.decision.entryCeilingNote, /FCF/);
  // 전문용어가 화면 문구로 새지 않는지도 함께 본다.
  assert.doesNotMatch(report.decision.entryCeilingNote, /PROBABILITY_WEIGHTED|확률가중/);
  assert.doesNotMatch(report.decision.hurdleResult, /PROBABILITY_WEIGHTED|확률가중/);
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

test("no markdown or raw engine jargon leaks into display copy", async () => {
  const report = await readReport();
  const html = await (await render()).text();

  // HTML 은 마크다운을 렌더하지 않는다. **강조** 를 넣으면 별표가 그대로 찍힌다.
  const text = JSON.stringify(report);
  assert.ok(!/\*\*/.test(text), "리포트 문구에 마크다운 ** 가 남아 있다");

  // 헤드라인은 36px 로 커진다. 엔진 메시지를 그대로 넣으면
  // "기대수익 -50.0%(PROBABILITY_WEIGHTED)가..." 가 화면을 덮는다.
  assert.ok(
    report.decision.primaryReason.length <= 20,
    `헤드라인이 너무 길다: ${report.decision.primaryReason}`,
  );
  assert.doesNotMatch(report.decision.primaryReason, /[A-Z_]{6,}/,
    "헤드라인에 엔진 상수명이 그대로 들어갔다");

  // 다만 원문은 사라지면 안 된다. issue 목록에 그대로 있어야 한다.
  assert.match(html, /PROBABILITY_WEIGHTED/);
});

test("layout does not hardcode how many items the data has", async () => {
  // 스냅샷이 3분기 → 4분기로 늘었을 때 .cashflow-chart 가 repeat(3, 1fr) 로
  // 박혀 있어서 네 번째 분기가 다음 행으로 밀려 고정 높이를 뚫고 나갔다.
  // 시나리오 표도 열을 하나 늘리자 같은 식으로 어긋났다.
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  const report = await readReport();

  // 줄 첫머리에 걸어야 한다. indexOf 로 찾으면 `.financial-tabs .cashflow-chart`
  // 같은 더 긴 셀렉터가 먼저 잡혀 엉뚱한 규칙을 검사하게 된다.
  const block = (selector) => {
    const at = css.indexOf(`\n${selector} {`);
    assert.notEqual(at, -1, `${selector} 규칙이 없다`);
    return css.slice(at, css.indexOf("}", at));
  };

  // 개수가 변하는 목록은 열 수를 박으면 안 된다.
  assert.doesNotMatch(block(".peer-summary"), /grid-template-columns:\s*repeat\(\d/);

  // 현금흐름 막대는 CSS 고정 px 로 그리면 폭이 좁을 때 값 라벨이 잘린다.
  // 좌표를 파이썬이 만들고 SVG 가 비율로 줄어들게 한다.
  assert.doesNotMatch(css, /\.cash-bar\s*\{[^}]*width:\s*\d+px/);
  assert.equal(
    report.cashFlow.chart.groups.length,
    report.cashFlow.rows.length,
    "차트 그룹 수와 표 행 수가 다르다",
  );

  // 시나리오 표는 열 수가 고정이지만, 헤더·본문·CSS 셋이 같아야 한다.
  const scenarioCols = block(".scenario-head,\n.scenario-data")
    .match(/grid-template-columns:([^;]+);/)[1]
    .trim()
    .split(/\s+/).length;
  const html = await (await render()).text();
  const head = html.match(/<div class="scenario-head">[\s\S]*?<\/div>(?=<div)/)[0];
  assert.equal(
    (head.match(/<span>/g) || []).length,
    scenarioCols,
    "시나리오 표 헤더 열수와 CSS 열수가 다르다",
  );
  assert.equal(report.valuation.scenarios.length, 3);
});

test("chart labels stay inside the viewBox and carry their own geometry", async () => {
  const chart = JSON.parse(
    await readFile(new URL("../data/tsla-price-chart.json", import.meta.url), "utf8"),
  );
  const [, , viewWidth] = chart.viewBox.split(" ").map(Number);

  // 오른쪽 끝 마커를 바깥으로 쓰면 viewBox 밖으로 나가 잘린다.
  for (const marker of chart.markers) {
    if (Number(marker.x) > viewWidth * 0.8) {
      assert.equal(marker.anchor, "end",
        `${marker.label} 이 오른쪽 끝인데 바깥으로 정렬돼 잘린다`);
      assert.ok(marker.dx.startsWith("-"), `${marker.label} 의 dx 가 안쪽이 아니다`);
    }
  }

  // 기준선 값은 우측 축 태그. 태그 자리는 plot 바깥이어야 데이터를 안 가린다.
  assert.ok(Number(chart.tagX) >= Number(chart.plotRight),
    "기준선 값 태그가 그래프 영역 위에 겹쳐 있다");
  assert.match(chart.currentLine.tagY, /^[\d.]+$/);
  assert.match(chart.entryLine.tagY, /^[\d.]+$/);

  // hover 문구는 파이썬이 만든다. 브라우저가 날짜·등락률을 다시 만들면
  // 화면이 두 번째 계산 주체가 된다.
  assert.equal(chart.points.length, chart.segments.length + 1);
  for (const key of ["date", "close", "change", "volume", "fromStart"]) {
    assert.ok(chart.points[0][key], `points[].${key} 가 비어 있다`);
  }
  const client = await readFile(new URL("../app/PriceChart.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(client, /toLocaleString|Intl\.|toFixed/,
    "차트 컴포넌트가 표시값을 직접 포맷하고 있다");
});

test("keeps calculations upstream and renders precomputed geometry", async () => {
  const [page, financialTabs, positioningTabs, chartText] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/FinancialTabs.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/PositioningTabs.tsx", import.meta.url), "utf8"),
    readFile(new URL("../data/tsla-price-chart.json", import.meta.url), "utf8"),
  ]);
  void financialTabs;
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
  // 막대 좌표·눈금은 전부 상류에서 온다.
  const cf = report.cashFlow.chart;
  assert.ok(cf.ticks.length >= 2, "y축 눈금이 없다");
  assert.ok(cf.groups.every((g) => /^[\d.]+$/.test(g.ocf.height)));
  assert.ok(cf.groups.every((g) => /^[\d.]+$/.test(g.capex.height)));
  assert.doesNotMatch(
    `${page}\n${financialTabs}\n${positioningTabs}`,
    /\bMath\.|\breduce\(|toLocaleString|Intl\.NumberFormat|\bNumber\(/,
  );
  await access(new URL("../scripts/build_price_chart.py", import.meta.url));
  await access(new URL("../scripts/build_decision_report.py", import.meta.url));
});
