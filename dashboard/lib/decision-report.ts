export type Signal = "good" | "watch" | "bad" | "plain";

export interface DecisionReport {
  schemaVersion: string;
  /** "무엇이 바뀌어야 통과하는가" 격자. 화면은 조회만 한다. */
  sensitivity: {
    axes: Record<
      "growth" | "probability" | "price",
      { label: string; unit: string; values: number[]; labels: string[] }
    >;
    current: { growth: number; probability: number; price: number };
    requiredReturn: number;
    /** [성장률][확률][가격] = [연 수익률, 통과 여부, 목표 대비 %p] */
    cells: Array<Array<Array<[number, number, number]>>>;
    note: string;
  };
  meta: {
    asOf: string;
    priceAsOf: string;
    financialAsOf: string;
    calculationId: string;
    fundamentalsId: string;
    engineVersion: string;
    generatedBy: string;
  };
  asset: {
    name: string;
    ticker: string;
    currentUsd: string;
    currentKrw: string;
    exchangeNote: string;
  };
  decision: {
    /** 엔진의 원본 판정. 자연어로 완화하지 않는다. */
    verdict: "BLOCKED" | "RESEARCH_REQUIRED" | "CAUTION" | "PROCEED";
    status: string;
    verdictNote: string;
    primaryReason: string;
    headline: string;
    reason: string;
    issues: Array<{ severity: string; code: string; message: string }>;
    measure: string;
    positionReturn: string;
    requiredReturn: string;
    entryCeiling: string;
    entryCeilingNote: string;
    /** 강세 시나리오 단독 기준. IPS 의 자가 아니므로 매수 기준이 아니다. */
    bullOnlyCeiling: string;
    bullOnlyNote: string;
    hurdleResult: string;
    actionNow: string;
    highlights: Array<{
      meaning: string;
      value: string;
      context: string;
      confidence: string;
      confidenceLevel: "high" | "medium" | "lagged";
      signal: Signal;
    }>;
  };
  priceEvidence: Array<{
    label: string;
    value: string;
    explanation: string;
    signal: Signal;
  }>;
  marketPositioning: {
    title: string;
    conclusion: string;
    items: Array<{
      id: string;
      label: string;
      status: string;
      value: string;
      comparison: string;
      asOf: string;
      interpretation: string;
      limitation: string;
      signal: Signal;
    }>;
    decisionUse: string;
  };
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
      signal: Signal;
    }>;
    /* 막대 좌표는 파이썬이 만든다. CSS 고정 px 로 그리면 폭이 좁아질 때
       값 라벨이 컬럼 밖으로 잘린다. */
    chart: {
      viewBox: string;
      barWidth: string;
      baselineY: string;
      plotLeft: string;
      plotRight: string;
      quarterLabelY: string;
      fcfLabelY: string;
      ticks: Array<{ label: string; y: string }>;
      groups: Array<{
        quarter: string;
        labelX: string;
        ocf: { x: string; y: string; height: string; label: string; labelX: string; labelY: string };
        capex: { x: string; y: string; height: string; label: string; labelX: string; labelY: string };
        fcf: string;
        signal: Signal;
      }>;
    };
  };
  /** 이 거래가 포트폴리오에서 차지하는 자리. 엔진이 계산한다. */
  portfolio: {
    quantity: number;
    priceKrw: string;
    tradeValueKrw: string;
    weights: Array<{
      label: string;
      before: string;
      after: string;
      denominator: string;
      definition: string;
    }>;
    bearBudget: {
      used: string;
      limit: string;
      note: string;
      withinBudget: boolean;
    };
    costs: { [key: string]: string } | null;
    snapshotId: string;
  };
  peersTable: {
    title: string;
    conclusion: string;
    /** 동종사 행은 수집 시점이 다르다. basis 없이 비교하면 안 된다. */
    basisWarning: string;
    rows: Array<{
      ticker: string;
      company: string;
      basis: string;
      revenue: string;
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
    entryExplanation: string;
    /** 현재가 기준 배수. 낡은 주가로 계산하면 여기서 어긋난다. */
    multiples: Array<{ label: string; value: string; basis: string }>;
    scenarios: Array<{
      name: string;
      probability: string;
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
    url?: string;
  }>;
}

export interface PriceChart {
  schemaVersion: string;
  viewBox: string;
  plotRight: string;
  tagX: string;
  tagTextX: string;
  linePath: string;
  areaPath: string;
  points: Array<{
    x: string;
    y: string;
    date: string;
    close: string;
    change: string;
    changeDirection: "up" | "down" | "flat";
    fromStart: string;
    volume: string;
  }>;
  segments: Array<{
    x1: string;
    y1: string;
    x2: string;
    y2: string;
    direction: "up" | "down" | "flat";
  }>;
  guides: Array<{ value: string; y: string }>;
  xLabels: Array<{ label: string; x: string }>;
  entryLine: {
    value: string;
    y: string;
    tagY: string;
    tagTextY: string;
    label: string;
  };
  currentLine: {
    value: string;
    y: string;
    tagY: string;
    tagTextY: string;
    label: string;
  };
  markers: Array<{
    label: string;
    value: string;
    date: string;
    x: string;
    y: string;
    placement: string;
    anchor: "start" | "end";
    dx: string;
  }>;
  summary: {
    period: string;
    sessions: string;
    twoSessionMove: string;
    fromHigh: string;
    fromLow: string;
  };
}
