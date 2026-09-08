#!/usr/bin/env python3
"""정기보고서에서 사업부문별 매출·영업이익·총자산을 뽑는다.

이 종목의 판단은 사실상 어느 부문의 판단인가 — 그 질문에 숫자로 답하기 위한
수집기다. 삼성전자 2026년 1분기 영업이익은 DS 부문이 93.8%, DX 부문이 5.2%다.
이걸 모르면 폴드8 뉴스와 D램 판가 뉴스에 같은 무게를 싣게 된다.

**토픽에 종속되지 않는다.** 폴드8이든 파운드리든 HBM이든, 그것이 속한 부문이
이익의 몇 %인지가 먼저다. 그래서 이슈마다 수집기를 만드는 대신 부문 배분을
한 번 만든다.

표는 DART 정기보고서의 '사업부문별 요약 재무 현황'에서 온다. 원문 1,327개 표
가운데 영업이익·총자산·부문을 동시에 담은 표는 하나뿐이라 판별이 깨끗하다.
찾지 못하면 조용히 비우지 않고 실패한다 — 부문 배분을 지어내는 것보다 없다고
말하는 편이 낫다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from calculators.contracts import ContractError, load_json
from cio.paths import KNOWLEDGE_DIR, ROOT

SCHEMA = "segment-summary/1.0.0"
OUT_DIR = KNOWLEDGE_DIR / "segments"
OBJECT_DIR = ROOT / "data" / "raw" / "dart" / "objects"
PERIODIC_FORMS = {"ANNUAL_REPORT", "HALF_REPORT", "QUARTER_REPORT"}
ROW_LABELS = {
    "매출액": "revenue",
    "영업이익": "operating_income",
    "총자산": "total_assets",
}
# K-IFRS 1108 은 단일 영업부문인 회사도 **그 사실을 공시하게** 한다. 그래서
# 표가 없는 이유를 추측하지 않아도 된다 — 원문이 직접 말한다.
#
#   "연결회사는 반도체 제품의 제조 및 판매의 단일영업부문으로 구성되어 있습니다"
#
# 이 문장을 읽지 않으면 "표를 못 찾았다"까지밖에 말할 수 없고, 그러면 표
# 형식이 바뀐 것과 나눌 게 없는 것이 같아 보인다. 둘은 완전히 다르다 —
# 앞은 고칠 수 있는 결함이고 뒤는 회사의 사실이다.
SINGLE_SEGMENT_RE = re.compile(
    r"단일\s*영업\s*부문|하나의\s*영업\s*부문|단일\s*보고\s*부문"
)
TABLE_RE = re.compile(r"<TABLE\b.*?</TABLE>", re.IGNORECASE | re.DOTALL)
ROW_RE = re.compile(r"<TR\b.*?</TR>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<T[DHE]\b.*?</T[DHE]>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def fail(message: str):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", fragment)).strip()


def _cells(row: str) -> list[str]:
    return [_text(cell) for cell in CELL_RE.findall(row)]


def _amount(text: str) -> float | None:
    """억원 단위 금액. 적자는 △ 또는 괄호로 오는 경우가 있다."""
    if not text or text in {"-", "—", "0"}:
        return 0.0 if text == "0" else None
    negative = text.startswith("△") or text.startswith("▲") or (
        text.startswith("(") and text.endswith(")")
    )
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _share(text: str) -> float | None:
    if not text or "%" not in text:
        return None
    negative = text.startswith("△") or text.startswith("▲")
    cleaned = re.sub(r"[^\d.]", "", text)
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def single_segment_statement(document: str) -> str | None:
    """단일 영업부문이라고 **공시가 직접 말한** 문장. 없으면 None.

    문장을 그대로 돌려주는 이유는 근거이기 때문이다. "단일 부문이다"라는 결론만
    남기면 그것이 우리 판정인지 회사의 공시인지 구분되지 않는다.
    """
    plain = re.sub(r"\s+", " ", TAG_RE.sub(" ", document))
    match = SINGLE_SEGMENT_RE.search(plain)
    if not match:
        return None
    # 문장 경계로 자른다. 앞뒤 주석 번호까지 딸려오면 인용이 지저분해진다.
    start = plain.rfind(".", 0, match.start()) + 1
    end = plain.find("다.", match.end())
    end = end + 2 if end != -1 else min(len(plain), match.end() + 120)
    return plain[start:end].strip() or None


def find_segment_table(document: str) -> str | None:
    """부문별 요약 재무 현황 표를 고른다.

    매출액만으로는 표가 여러 개 걸린다. 영업이익과 총자산을 함께 요구하면
    정기보고서 안에서 이 표만 남는다.
    """
    candidates = [
        table
        for table in TABLE_RE.findall(document)
        if "영업이익" in table and "총자산" in table and "부문" in table
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def parse_segment_table(table: str) -> tuple[list[str], list[dict[str, Any]]]:
    rows = [_cells(row) for row in ROW_RE.findall(table)]
    rows = [row for row in rows if row]
    if len(rows) < 3:
        raise ContractError(["부문별 표의 행이 부족하다"])
    header = rows[0]
    # ['부문', '구 분', '제58기 1분기', '제57기', '제56기']
    periods = [cell for cell in header[2:] if cell]
    if not periods:
        raise ContractError(["부문별 표에서 기간 열을 찾지 못했다"])

    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in rows[1:]:
        if row and row[0] in {"금액", "비중"}:
            continue
        label_index = 0
        if row[0] in ROW_LABELS:
            label = row[0]
            values = row[1:]
        elif len(row) > 1 and row[1] in ROW_LABELS:
            # 부문 이름이 붙은 첫 행. 이름은 rowspan 이라 이 행에만 나온다.
            current = {"name": row[0]}
            segments.append(current)
            label = row[1]
            values = row[2:]
            label_index = 1
        else:
            continue
        if current is None:
            raise ContractError(["부문 이름 없이 숫자 행이 먼저 나왔다"])
        series = []
        for index, period in enumerate(periods):
            amount = _amount(values[index * 2]) if index * 2 < len(values) else None
            share = (
                _share(values[index * 2 + 1])
                if index * 2 + 1 < len(values)
                else None
            )
            series.append(
                {"period": period, "amount": amount, "share_pct": share}
            )
        current[ROW_LABELS[label]] = series
        del label_index
    if not segments:
        raise ContractError(["부문별 표에서 부문을 하나도 읽지 못했다"])
    for segment in segments:
        if "operating_income" not in segment:
            raise ContractError(
                [f"{segment['name']}: 영업이익 행이 없다"]
            )
    return periods, segments


def _periodic_filing(asset_id: str) -> dict[str, Any]:
    path = KNOWLEDGE_DIR / "filings" / f"{asset_id}.json"
    if not path.exists():
        fail(
            f"{asset_id}: 공시 색인이 없다. "
            f"`python3.13 -m collectors.dart filings {asset_id} --since ...` 먼저"
        )
    data = load_json(path)
    rows = [
        row
        for row in data.get("filings") or []
        if isinstance(row, dict) and row.get("form") in PERIODIC_FORMS
    ]
    if not rows:
        fail(f"{asset_id}: 공시 색인에 정기보고서가 없다")
    rows.sort(key=lambda row: str(row.get("filing_date") or ""), reverse=True)
    return rows[0]


def _stored_document(rcept_no: str) -> tuple[str, Path] | None:
    for path in sorted(OBJECT_DIR.glob("*.json")):
        try:
            payload = load_json(path).get("payload") or {}
        except (ContractError, OSError):
            continue
        if payload.get("rcept_no") != rcept_no:
            continue
        documents = payload.get("documents")
        if not isinstance(documents, dict) or not documents:
            continue
        return max(documents.values(), key=len), path
    return None


def _fetched_document(rcept_no: str) -> tuple[str, Path]:
    """이미 받아둔 원문을 쓰고, 없으면 받아온다.

    refresh 가 이 수집기를 자동으로 돌릴 수 있어야 하므로 사람에게 선행
    명령을 요구하지 않는다. 원문은 내용 주소라 두 번 받아도 하나만 남는다.
    """
    stored = _stored_document(rcept_no)
    if stored:
        return stored
    from collectors.dart import fetch_document, load_key

    fetch_document(rcept_no, load_key())
    stored = _stored_document(rcept_no)
    if not stored:
        fail(f"접수번호 {rcept_no} 원문을 받았으나 다시 읽지 못했다")
    return stored


def collect(asset_id: str) -> dict[str, Any]:
    filing = _periodic_filing(asset_id)
    rcept_no = filing["accession_number"]
    document, object_path = _fetched_document(rcept_no)
    table = find_segment_table(document)
    if table is None:
        # 표가 없는 이유는 둘이고, 그 둘은 완전히 다르다 — 나눌 게 없거나(회사의
        # 사실), 표 형식이 바뀌었거나(우리 결함). 추측하지 않고 원문에 묻는다:
        # K-IFRS 1108 은 단일 영업부문인 회사도 그 사실을 공시하게 한다.
        statement = single_segment_statement(document)
        return {
            "schema_version": SCHEMA,
            "asset_id": asset_id,
            "source": "DART_PERIODIC_REPORT",
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "available": False,
            "rcept_no": rcept_no,
            "report_name": filing.get("description"),
            "filing_date": filing.get("filing_date"),
            "raw_payload_sha256": object_path.stem,
            "raw_locator": str(object_path.relative_to(ROOT)),
            "absence": "SINGLE_SEGMENT" if statement else "NO_TABLE_FOUND",
            "single_segment_statement": statement,
            "_reason": (
                f"이 회사는 단일 영업부문이라 부문 배분이 존재하지 않는다. "
                f"공시 원문이 그렇게 적고 있다 — \"{statement}\""
                if statement else
                f"{filing.get('description')} 에서 사업부문별 요약 표를 특정하지 "
                "못했고, 단일 영업부문이라는 공시도 없다. 표 형식이 바뀌었을 수 "
                "있으므로 원문을 직접 본다. 부문 배분을 지어내지 않는다"
            ),
            "_retry_note": (
                "회사의 사실이므로 다시 받을 것이 없다. 사업 구조가 바뀌어 "
                "부문이 나뉘면 그때 표가 생긴다"
                if statement else
                "같은 보고서에 대해 다시 돌려도 결과는 같다. 다음 정기보고서가 "
                "나오면 표 형식이 바뀌었을 수 있으므로 그때 다시 받는다"
            ),
        }
    try:
        periods, segments = parse_segment_table(table)
    except ContractError as exc:
        fail("; ".join(exc.errors))
    latest = periods[0]
    ranked = sorted(
        (
            {
                "name": segment["name"],
                "operating_income_share_pct": next(
                    (
                        item["share_pct"]
                        for item in segment.get("operating_income") or []
                        if item["period"] == latest
                    ),
                    None,
                ),
                "revenue_share_pct": next(
                    (
                        item["share_pct"]
                        for item in segment.get("revenue") or []
                        if item["period"] == latest
                    ),
                    None,
                ),
            }
            for segment in segments
        ),
        key=lambda item: item["operating_income_share_pct"] or -999,
        reverse=True,
    )
    return {
        "schema_version": SCHEMA,
        "asset_id": asset_id,
        "source": "DART_PERIODIC_REPORT",
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "rcept_no": rcept_no,
        "report_name": filing.get("description"),
        "filing_date": filing.get("filing_date"),
        "report_date": filing.get("report_date"),
        "unit": "억원",
        "periods": periods,
        "latest_period": latest,
        "raw_payload_sha256": object_path.stem,
        "raw_locator": str(object_path.relative_to(ROOT)),
        "segments": segments,
        "latest_mix": ranked,
        "_note": (
            "부문 배분은 정기보고서 '사업부문별 요약 재무 현황' 표에서 그대로 "
            "옮긴 값이다. 비중은 회사가 적은 값이며 부문 간 내부거래 제거 방식에 "
            "따라 합이 100을 넘거나 모자랄 수 있다. 최신 기간은 정기보고서 "
            "기준이라 그 이후 분기는 반영되지 않는다"
        ),
    }


def save(packet: dict[str, Any]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{packet['asset_id']}.json"
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=OUT_DIR, delete=False
        ) as handle:
            json.dump(packet, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3.13 -m collectors.kr_segments",
        description="정기보고서의 사업부문별 요약 재무 현황을 수집한다",
    )
    parser.add_argument("asset_id")
    args = parser.parse_args()
    packet = collect(args.asset_id)
    path = save(packet)
    if packet.get("available") is False:
        # 실패가 아니라 관측이다 — "이 보고서에는 없다"를 알아낸 것이고 그
        # 사실이 파일로 남는다. 0 이 아닌 코드로 끝내면 refresh 가 수집 실패로
        # 세고, 다음에 또 돌린다.
        print(f"{packet['asset_id']}  {packet['report_name']}")
        print(f"저장: {path.relative_to(ROOT)}")
        print("-" * 60)
        print(f"  부문 배분 없음 — {packet['_reason']}")
        return 0
    print(f"{packet['asset_id']}  {packet['report_name']}  ({packet['latest_period']})")
    print(f"저장: {path.relative_to(ROOT)}")
    print("-" * 60)
    for item in packet["latest_mix"]:
        share = item["operating_income_share_pct"]
        revenue = item["revenue_share_pct"]
        print(
            f"  {item['name']:<12} 영업이익 {share if share is not None else '-':>7}%"
            f"   매출 {revenue if revenue is not None else '-':>7}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
