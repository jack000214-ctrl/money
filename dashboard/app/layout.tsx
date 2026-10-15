import type { Metadata } from "next";
import "./globals.css";
import reportData from "../data/decision-report.json";
import type { DecisionReport } from "../lib/decision-report";

const report = reportData as unknown as DecisionReport;

// 메타데이터를 손으로 적으면 화면과 어긋난다. 실제로 판정을 BLOCKED 로 고친
// 뒤에도 여기만 "추가 매수 보류" 로 남아 있었고, 종목명도 TSLA 로 박혀 있어
// 다른 종목을 올리면 그대로 틀린 값이 나갔다. 리포트에서 읽는다.
export const metadata: Metadata = {
  title: "Smart Investment Advisor",
  description:
    "일별 가격, 재무·현금흐름, 동종사 비교와 역산 밸류에이션을 연결한 투자판정 보고서",
  icons: { icon: "/favicon.svg" },
  openGraph: {
    title: "Smart Investment Advisor",
    description: report.decision.headline,
  },
  twitter: {
    card: "summary",
    title: "Smart Investment Advisor",
    description: report.decision.headline,
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
