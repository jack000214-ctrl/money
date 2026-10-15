"use client";

import { useRef } from "react";
import type { DecisionReport } from "../lib/decision-report";

export function AnalysisDialog({
  analysis,
}: {
  analysis: DecisionReport["detailedAnalysis"];
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  return (
    <>
      <button
        className="detail-open"
        type="button"
        onClick={() => dialogRef.current?.showModal()}
      >
        판정 근거 자세히 읽기
        <span aria-hidden="true">→</span>
      </button>
      <dialog className="analysis-dialog" ref={dialogRef}>
        <div className="dialog-top">
          <div>
            <p>상세 분석</p>
            <h2>{analysis.title}</h2>
          </div>
          <button
            aria-label="상세 분석 닫기"
            type="button"
            onClick={() => dialogRef.current?.close()}
          >
            닫기
          </button>
        </div>

        <div className="dialog-body">
          <section className="dialog-narrative">
            {(analysis.paragraphs ?? []).map((paragraph) => (
              <p key={paragraph}>{paragraph}</p>
            ))}
          </section>

          <div className="dialog-columns">
            <section>
              <h3>확인된 사실</h3>
              <ul>
                {(analysis.confirmed ?? []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </section>
            <section>
              <h3>아직 확인되지 않은 것</h3>
              <ul>
                {(analysis.unresolved ?? []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </section>
            <section>
              <h3>논지 훼손 신호</h3>
              <ul>
                {(analysis.failureSignals ?? []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </section>
            <section>
              <h3>이 분석의 한계</h3>
              <ul>
                {(analysis.caveats ?? []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </section>
          </div>
        </div>
      </dialog>
    </>
  );
}
