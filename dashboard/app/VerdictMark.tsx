import type { DecisionReport } from "../lib/decision-report";

type Verdict = DecisionReport["decision"]["verdict"];

/**
 * 판정 도장.
 *
 * 한국식 채점 기호를 그대로 쓴다 — X 차단 / △ 조사필요·주의 / O 통과.
 * 설명 없이도 읽히는 기호라 판정을 한 눈에 전달한다. 문자를 타이포로
 * 찍지 않고 도형으로 그리는 이유는, 획 굵기와 여백을 판정의 무게에
 * 맞춰 조절하기 위해서다.
 */
const MARKS: Record<Verdict, { tone: string; label: string }> = {
  BLOCKED: { tone: "halt", label: "차단" },
  RESEARCH_REQUIRED: { tone: "hold", label: "조사 필요" },
  CAUTION: { tone: "hold", label: "주의" },
  PROCEED: { tone: "approved", label: "통과" },
};

export function VerdictMark({
  verdict,
  size = 168,
}: {
  verdict: Verdict;
  size?: number;
}) {
  const mark = MARKS[verdict];
  const stroke = 11;

  return (
    <svg
      aria-label={`판정 ${mark.label}`}
      className={`verdict-mark mark-${mark.tone}`}
      height={size}
      role="img"
      viewBox="0 0 120 120"
      width={size}
    >
      {verdict === "BLOCKED" ? (
        <>
          <line className="stroke-draw draw-1" pathLength="100" strokeLinecap="round"
                strokeWidth={stroke} x1="26" x2="94" y1="26" y2="94" />
          <line className="stroke-draw draw-2" pathLength="100" strokeLinecap="round"
                strokeWidth={stroke} x1="94" x2="26" y1="26" y2="94" />
        </>
      ) : verdict === "PROCEED" ? (
        <circle className="stroke-draw draw-1" cx="60" cy="60" fill="none"
          pathLength="100" r="36" strokeLinecap="round" strokeWidth={stroke} />
      ) : (
        <path
          className="stroke-draw draw-1"
          d="M60 24 L96 92 L24 92 Z"
          fill="none"
          pathLength="100"
          strokeLinejoin="round"
          strokeWidth={stroke}
        />
      )}
    </svg>
  );
}
