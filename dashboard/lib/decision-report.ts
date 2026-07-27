export type Signal = "good" | "watch" | "bad" | "plain";

export interface DecisionReport {
  schemaVersion: string;
  meta: {
    asOf: string;
    priceAsOf: string;
    financialAsOf: string;
    calculationId: string;
  };
  asset: {
    name: string;
    ticker: string;
    currentUsd: string;
    currentKrw: string;
    exchangeNote: string;
  };
  decision: {
    status: string;
    headline: string;
    reason: string;
    currentPrice: string;
    entryCeiling: string;
    entryCeilingKrw: string;
    hurdleResult: string;
    actionNow: string;
  };
  priceEvidence: Array<{
    label: string;
    value: string;
    explanation: string;
    signal: Signal;
  }>;
  incomeStatement: {
    title: string;
    conclusion: string;
    equation: Array<{ label: string; value: string; sign?: string }>;
    rows: Array<{
      metric: string;
      value: string;
      comparison: string;
      interpretation: string;
      signal: Signal;
    }>;
  };
  cashFlow: {
    title: string;
    conclusion: string;
    rows: Array<{
      quarter: string;
      operatingCashFlow: string;
      capex: string;
      freeCashFlow: string;
      ocfHeight: string;
      capexHeight: string;
      signal: Signal;
    }>;
  };
  peers: {
    title: string;
    conclusion: string;
    rows: Array<{
      ticker: string;
      company: string;
      basis: string;
      revenue: string;
      grossMargin: string;
      operatingMargin: string;
      revenueGrowth: string;
      per: string;
      psr: string;
      focus?: boolean;
    }>;
  };
  valuation: {
    title: string;
    conclusion: string;
    impliedGrowth: string;
    modelInputs: string;
    scenarios: Array<{
      name: string;
      fcfGrowth: string;
      valuePerShare: string;
      totalGap: string;
      annualizedReturn: string;
      signal: Signal;
    }>;
  };
  reviewMap: Array<{
    when: string;
    state: string;
    action: string;
    why: string;
    signal: Signal;
  }>;
  detailedAnalysis: {
    title: string;
    paragraphs: string[];
    confirmed: string[];
    unresolved: string[];
    failureSignals: string[];
    caveats: string[];
  };
  sources: Array<{
    label: string;
    detail: string;
  }>;
}

export interface PriceChart {
  schemaVersion: string;
  viewBox: string;
  linePath: string;
  areaPath: string;
  guides: Array<{ value: string; y: string }>;
  xLabels: Array<{ label: string; x: string }>;
  entryLine: {
    value: string;
    y: string;
    labelRectY: string;
    labelTextY: string;
    label: string;
  };
  currentLine: {
    value: string;
    y: string;
    labelRectY: string;
    labelTextY: string;
    label: string;
  };
  markers: Array<{
    label: string;
    value: string;
    date: string;
    x: string;
    y: string;
    placement: string;
  }>;
  summary: {
    period: string;
    sessions: string;
    twoSessionMove: string;
    fromHigh: string;
    fromLow: string;
  };
}
