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

test("server-renders the information-first TSLA decision report", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /매력은 있지만, 지금은 한 번에 베팅할 가격이 아닙니다/);
  assert.match(html, /급락한 것은 맞습니다/);
  assert.match(html, /매출총이익률/);
  assert.match(html, /−\$1,092M/);
  assert.match(html, /General Motors/);
  assert.match(html, /10년 FCF 연 38\.2%/);
  assert.match(html, /\$282\.46 이하/);
  assert.doesNotMatch(html, /DEMO DATA|확률가중 기대수익|TWELVE/);
});

test("keeps calculations upstream and renders precomputed geometry", async () => {
  const [page, reportText, chartText] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../data/decision-report.json", import.meta.url), "utf8"),
    readFile(new URL("../data/tsla-price-chart.json", import.meta.url), "utf8"),
  ]);
  const report = JSON.parse(reportText);
  const chart = JSON.parse(chartText);

  assert.equal(report.schemaVersion, "decision-dashboard/0.2.0");
  assert.equal(report.meta.calculationId, "CALC-44eb75970b617b730887");
  assert.equal(chart.schemaVersion, "price-chart/1.0.0");
  assert.ok(chart.linePath.startsWith("M"));
  assert.match(chart.entryLine.labelRectY, /^\d+\.\d+$/);
  assert.ok(
    report.cashFlow.rows.every((row) => /^\d+%$/.test(row.ocfHeight)),
  );
  assert.doesNotMatch(
    page,
    /\bMath\.|\breduce\(|toLocaleString|Intl\.NumberFormat|\bNumber\(/,
  );
  await access(new URL("../scripts/build_price_chart.py", import.meta.url));
});
