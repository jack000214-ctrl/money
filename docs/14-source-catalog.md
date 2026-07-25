# 14. 공식 자료 및 데이터 소스 목록

## 1. 목적

설계와 구현에서 우선 확인할 공식 자료를 관리한다. API와 약관은 변경될 수 있으므로 실제 구현 시점에 다시 검토하고 확인일을 갱신한다.

- 마지막 확인일: 2026-07-25
- 원칙: 블로그 예제보다 기관·공급자 공식 문서를 우선

## 2. 한국 기업 공시와 재무정보

### OpenDART

- [OpenDART API 소개](https://opendart.fss.or.kr/intro/main.do)
- [공시정보 개발가이드](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001)
- 제공 범위: 공시 검색, 기업 개황, 공시 원문, 회사 고유번호
- 추가 범위: 정기보고서 재무정보, 지분공시, 주요사항보고서, 증권신고서

### 구현 시 확인할 내용

- 인증키 발급과 호출 제한
- 공시 정정 관계
- XBRL 원문과 표준 재무 API의 차이
- 연결·별도와 보고기간 처리
- 이용약관과 원문 저장 범위

## 3. 한국 시장 데이터

### KRX Data Marketplace

- [KRX Open API](https://openapi.krx.co.kr/contents/OPP/MAIN/main/index.cmd)
- [KRX 정보 이용정책 PDF](https://data.krx.co.kr/inc/datasale/Market%20Data%20Usage%20Polices_ko.pdf)
- 제공 범위: 지수, 주식, 증권상품, 채권, 파생상품, 일반상품, ESG 관련 통계 API

### 구현 시 확인할 내용

- 제공되는 통계와 제공되지 않는 실시간·과거 데이터의 구분
- 개인 내부 사용, 저장, 화면 표시, 재배포 권한
- 지수 라이선스
- 과거 구성종목과 상장폐지 데이터 확보 가능성

## 4. 계좌·시세·주문

### 한국투자증권 Open API

- [KIS Developers](https://apiportal.koreainvestment.com/intro)
- [공식 샘플 코드](https://github.com/koreainvestment/open-trading-api)
- 제공 범위: REST 조회·주문, WebSocket 실시간 시세·체결, 국내외 상품 API

### 구현 시 확인할 내용

- 사용자 증권사가 실제로 한국투자증권인지
- 모의투자와 실전 환경의 분리
- 토큰 수명과 발급 제한
- API별 호출 제한
- 거래 가능 시간과 주문 유형
- 계좌·잔고 연속조회
- 주문 타임아웃 후 상태 확인 방법
- 공식 공지와 보안 업데이트

다른 증권사를 선택하면 이 영역을 해당 증권사의 공식 API 문서로 교체한다.

## 5. 한국 거시 데이터

### 한국은행 ECOS

- [한국은행 경제통계시스템 Open API](https://ecos.bok.or.kr/api/)
- 후보 데이터: 기준금리, 시장금리, 환율, 통화, 물가, 경기 관련 통계

### 구현 시 확인할 내용

- 발표시점과 데이터 관측기간
- 개정 데이터의 과거 버전 확보 여부
- 계절조정 여부와 단위
- 업데이트 주기

## 6. 미국 확장 후보

### SEC EDGAR

- [SEC EDGAR API 문서](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- 용도: 미국 상장기업 제출자료와 회사 식별자

### FRED·ALFRED

- [FRED API 개요](https://fred.stlouisfed.org/docs/api/fred/overview.html)
- 용도: 미국 거시·금융 시계열
- ALFRED 활용 검토: 당시 공개된 거시 데이터 버전 재현

## 7. 투자 원칙과 기준 연구

### 공식 투자자 교육

- [Investor.gov 자산배분과 분산투자](https://www.investor.gov/introduction-investing/getting-started/asset-allocation)
- [Investor.gov 투자비용 안내](https://www.investor.gov/introduction-investing/getting-started/understanding-fees)
- [FINRA 주식 투자 안내](https://www.finra.org/investors/investing/investment-products/stocks)
- [FINRA 비용과 수수료](https://www.finra.org/investors/investing/investing-basics/fees-commissions)

이 자료들은 전략의 직접 신호가 아니라 투자정책, 분산, 비용 통제의 근거로 사용한다.

### 액티브 전략 기준

- [S&P SPIVA 연구 목록](https://www.spglobal.com/spdji/en/research-insights/spiva/)
- 용도: 액티브 운용의 기준지수 대비 성과와 성과 지속성 검토

SPIVA는 전략 데이터 소스가 아니라 복잡한 액티브 전략이 넘어야 할 기준을 이해하기 위한 참고자료다.

## 8. 한국 규제

- [자본시장법 제6조 금융투자업 정의](https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1027642775)
- [자본시장법 제17조 미등록 영업행위의 금지](https://www.law.go.kr/LSW/lsSideInfoP.do?docCls=jo&joBrNo=00&joNo=0017&lsiSeq=273695&urlMode=lsScJoRltInfoR)
- [자본시장법 제18조 투자자문업·투자일임업 등록](https://www.law.go.kr/LSW/lsSideInfoP.do?docCls=jo&joBrNo=00&joNo=0018&lsiSeq=273695&urlMode=lsScJoRltInfoR)

### 재검토가 필요한 시점

- 타인에게 서비스를 제공할 때
- 맞춤형 종목 추천을 유료화할 때
- 타인의 계좌 주문 권한을 받을 때
- 광고 또는 수익률 표현을 사용할 때
- 시장 데이터를 외부에 표시·재배포할 때

법률 적용 여부는 전문가에게 확인하며 이 문서의 설명을 법률의견으로 사용하지 않는다.

## 9. 소스 등록 형식

새 데이터 소스를 추가할 때 다음을 기록한다.

```yaml
source:
  name: string
  owner: string
  official_url: string
  data_scope: []
  authentication: string
  update_frequency: string
  point_in_time_support: boolean
  correction_policy: string
  storage_permission: string
  display_permission: string
  redistribution_permission: string
  rate_limits: string
  cost: string
  last_reviewed_at: date
  reviewer: string
```

## 10. 검토가 필요한 결정

- 실제 사용할 증권사
- KRX만으로 부족한 데이터 범위
- 유료 시세·컨센서스 필요 여부
- 미국 시장을 초기부터 포함할지
- 뉴스 공급자와 라이선스
