"use client";

import { useMemo, useState } from "react";
import type { DecisionReport } from "../lib/decision-report";

type Sensitivity = DecisionReport["sensitivity"];

/**
 * "무엇이 바뀌어야 통과하는가" 계기판.
 *
 * 이 컴포넌트는 계산하지 않는다. 가능한 조합의 확률가중 기대수익과 통과
 * 여부는 build_decision_report.py 가 전부 풀어 격자로 내려준다. 여기서는
 * 축 인덱스로 셀을 **조회**할 뿐이다. 브라우저가 값을 다시 만들면 화면이
 * 엔진과 다른 답을 낼 수 있다.
 */
export function SensitivityModel({ model }: { model: Sensitivity }) {
  const [growth, setGrowth] = useState(model.current.growth);
  const [probability, setProbability] = useState(model.current.probability);
  const [price, setPrice] = useState(model.current.price);

  const plane = model.cells[growth][probability];
  const [ret, isApproved, gap] = plane[price];
  const untouched =
    growth === model.current.growth &&
    probability === model.current.probability &&
    price === model.current.price;

  // 이 성장률·확률 조합에서 통과하는 가장 높은 가격. 없으면 가격으로는 못 넘는다.
  const approvedAt = useMemo(() => {
    for (let i = plane.length - 1; i >= 0; i -= 1) {
      if (plane[i][1]) return i;
    }
    return null;
  }, [plane]);

  const axis = (key: "growth" | "probability" | "price") => model.axes[key];
  const rows: Array<{
    key: "growth" | "probability" | "price";
    value: number;
    set: (n: number) => void;
    index: number;
  }> = [
    { key: "growth", value: growth, set: setGrowth, index: growth },
    { key: "probability", value: probability, set: setProbability, index: probability },
    { key: "price", value: price, set: setPrice, index: price },
  ];

  return (
    <section
      aria-label="민감도 모델"
      className={`model ${isApproved ? "model-approved" : "model-halt"}`}
      id="model"
    >
      <div className="model-readout">
        <p className="model-eyebrow">이 조건이면 연 수익률이</p>
        <p className="model-figure">
          {ret > 0 ? "+" : ""}
          {ret.toFixed(1)}%
        </p>
        <dl className="model-compare">
          <div>
            <dt>목표</dt>
            <dd>+{model.requiredReturn.toFixed(1)}%</dd>
          </div>
          <div>
            <dt>{gap >= 0 ? "목표 초과" : "목표까지"}</dt>
            <dd>
              {gap >= 0 ? "+" : ""}
              {gap.toFixed(1)}%p
            </dd>
          </div>
        </dl>
        {/* 문턱을 넘는 순간이 이 화면에서 가장 중요한 사건이다.
            key 가 바뀌며 재마운트돼 전환이 눈에 띈다. */}
        <p className="model-verdict" key={isApproved ? "approved" : "halt"}>
          {isApproved ? "통과" : "차단"}
        </p>
        {untouched ? (
          <p className="model-state">지금 승인된 가정 그대로입니다.</p>
        ) : (
          <button className="model-reset" onClick={() => {
            setGrowth(model.current.growth);
            setProbability(model.current.probability);
            setPrice(model.current.price);
          }} type="button">
            승인된 가정으로 되돌리기
          </button>
        )}
      </div>

      <div className="model-controls">
        <h2>무엇이 바뀌어야 통과하는가</h2>
        {rows.map((row) => {
          const a = axis(row.key);
          const shown = a.labels[row.index];
          return (
            <label className="model-row" key={row.key}>
              <span className="model-label">{a.label}</span>
              <input
                aria-valuetext={shown}
                max={a.values.length - 1}
                min={0}
                onChange={(event) => row.set(Number(event.target.value))}
                step={1}
                type="range"
                value={row.index}
              />
              <output className="model-value">{a.labels[row.index]}</output>
            </label>
          );
        })}

        <p className="model-finding">
          {approvedAt === null ? (
            <>
              이 조합에서는 <strong>가격을 아무리 낮춰도</strong> 요구수익률을
              넘지 못합니다. 격자 최저가 {model.axes.price.labels[0]}에서도 미달입니다.
            </>
          ) : (
            <>
              이 조합에서 통과하는 최고 가격은{" "}
              <strong>{model.axes.price.labels[approvedAt]}</strong>입니다. 현재 참고가{" "}
              {model.axes.price.labels[model.current.price]} 대비{" "}
              {approvedAt >= model.current.price
                ? "이미 그 아래입니다."
                : "그만큼 더 내려와야 합니다."}
            </>
          )}
        </p>
        <p className="model-note">{model.note}</p>
      </div>
    </section>
  );
}
