import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host =
    requestHeaders.get("x-forwarded-host") ??
    requestHeaders.get("host") ??
    "localhost:3000";
  const protocol =
    requestHeaders.get("x-forwarded-proto") ??
    (host.startsWith("localhost") ? "http" : "https");
  const imageUrl = `${protocol}://${host}/og-v2.png`;

  return {
    title: "TSLA 투자판정 — 자산 증식 프로그램",
    description:
      "일별 가격, 재무·현금흐름, 동종사 비교와 역산 밸류에이션을 연결한 투자판정 보고서",
    openGraph: {
      title: "TSLA 투자판정 — 추가 매수 보류",
      description: "급락했지만, 연 12% 목표를 만족하는 조건부 매수가는 아직 아래에 있습니다.",
      images: [{ url: imageUrl, width: 1672, height: 941 }],
    },
    twitter: {
      card: "summary_large_image",
      title: "TSLA 투자판정 — 추가 매수 보류",
      description: "일별 가격과 2026 Q2 현금흐름을 함께 본 투자판정 보고서",
      images: [imageUrl],
    },
  };
}

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
