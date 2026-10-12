#!/usr/bin/env python3
"""토스증권 거래내역서로 거래 원장을 대조하고 고친다.

거래 원장(`knowledge/portfolio/trades/`)을 사용자 진술로 이어 적으면 조용히
틀어진다. 2026-10-12 에 실제로 그랬다 — 두 달 사이 매매 일곱 건이 빠져 삼성E&A
0주가 30주로, RXRX 0주가 2,350주로 남아 있었고, 그 원장 위에서 "30주 보유"를
전제한 판단이 기록됐다. 증권사가 발급한 내역서가 있으면 그것이 권위다.

내역서는 PDF 다. `pdftotext -layout`(poppler)으로 글자를 뽑아 읽는다. 이
저장소가 쓰는 유일한 외부 실행 파일이며, 없으면 미리 뽑은 .txt 를 넘기면 된다.

**파싱이 맞는지는 내역서 스스로 증명한다.** 모든 행에 그 거래 뒤의 예수금 잔액과
종목 잔고가 찍혀 있으므로, 한 행이라도 빠뜨리거나 숫자를 잘못 읽으면 다음 행에서
잔액이 이어지지 않는다. 이어지지 않는 내역서로는 원장을 고치지 않는다.

조회 기간 안의 원장 행은 내역서 행으로 바꾼다. 기간 밖의 행은 건드리지 않고,
같은 거래로 대응되는 행의 note 는 이어받는다. holdings.csv 는 고치지 않는다 —
평가액과 정책 주석이 들어 있어 기계가 다시 쓸 수 있는 파일이 아니다. 수량이
어긋난 종목만 보고한다. 계좌번호와 성명은 읽지도 남기지도 않는다.

사용:
    python3.13 -m collectors.toss_statement check 거래내역서_20251013_20261012_1.pdf
    python3.13 -m collectors.toss_statement apply 거래내역서_20251013_20261012_1.pdf
    python3.13 -m collectors.toss_statement check 내역서.pdf --map US0378331005=AAPL
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TRADES_DIR = ROOT / "knowledge" / "portfolio" / "trades"
HOLDINGS = ROOT / "knowledge" / "portfolio" / "holdings.csv"
ASSETS = ROOT / "config" / "assets.json"
# 원장은 .gitignore 대상이라 git 이 옛 버전을 갖고 있지 않다. 덮어쓰기 전에 여기 둔다.
BACKUP_DIR = ROOT / "data" / "cache" / "portfolio-backup"

KR_FIELDS = ["settle_date", "side", "quantity", "price_krw", "fee_krw", "tax_krw", "note"]
US_FIELDS = ["settle_date", "side", "quantity", "price_krw", "price_usd", "fx_rate",
             "fee_krw", "note"]
CLOSED_FIELDS = ["settle_date", "name", "market", "side", "quantity", "price_krw",
                 "fee_krw", "tax_krw", "price_usd", "balance"]
CLOSED_FILE = "_closed"

# 내역서는 ISIN 과 한글 종목명만 찍는다. 등록 자산의 원장 파일명(티커)으로 옮기려면
# 대응표가 필요하다. config/assets.json 에 isin 이 있으면 그쪽이 먼저다.
ISIN_TO_ASSET = {
    "US8085247976": "SCHD",
    "US75629V1044": "RXRX",
    "US4592001014": "IBM",
    "US88160R1014": "TSLA",
    "US88636X7324": "MSTY",
}

# 거래구분 → 예수금이 움직이는 방향. 여기 없는 구분은 잔액 변화로 방향을 추정하고
# 경고로 보고한다. 매매(구매·판매)의 방향은 추정하지 않는다.
KRW_CASH_SIGN = {
    "구매": -1, "판매": 1, "이자입금": 1, "이벤트 입금": 1, "이체입금": 1, "이체출금": -1,
    "환전원화출금": -1, "환전원화입금": 1, "해외채권세금출금": -1, "외화이자세금출금": -1,
}
USD_CASH_SIGN = {
    "구매": -1, "판매": 1, "외화증권배당금입금": 1, "외화이자입금": 1, "외화세금환급": 1,
    "외화채권이자": 1, "외화채권원리금입금": 1, "환전외화입금취소": -1, "환전외화입금": 1,
    "환전외화출금": -1,
}
TRADE_SIDE = {"구매": "BUY", "판매": "SELL"}

DATE_LINE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\s+(\S.*)$")
NUMBER = re.compile(r"^-?\d[\d,]*(\.\d+)?$")
DOLLARS = re.compile(r"\(\$ ?([\d,]+(?:\.\d+)?)\)")
ISIN = re.compile(r"\(([A-Z]{2}[0-9A-Z]{9}\d)\)")
KR_CODE = re.compile(r"\(A(\d{6})\)")
PERIOD = re.compile(
    r"조회 기간\s+(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*~\s*"
    r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일"
)
CENT = Decimal("0.01")


class StatementError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _dec(token: str) -> Decimal:
    return Decimal(token.replace(",", ""))


def plain(value: Any) -> str:
    """원장 표기. 정수는 정수로, 소수는 뒤 0 을 뗀다 (1429.00 → 1429)."""
    return format(Decimal(str(value)).normalize(), "f")


def _same(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left or 0).replace(",", "")) == Decimal(str(right or 0).replace(",", ""))
    except InvalidOperation:
        return str(left) == str(right)


# ── 읽기 ────────────────────────────────────────────────────────────────

def read_text(path: Path) -> str:
    if path.suffix.lower() != ".pdf":
        return path.read_text(encoding="utf-8")
    tool = shutil.which("pdftotext")
    if not tool:
        raise StatementError(
            "pdftotext 가 없다. poppler 를 설치하거나(`brew install poppler`), "
            "다른 곳에서 `pdftotext -layout 내역서.pdf 내역서.txt` 로 뽑은 .txt 를 넘긴다"
        )
    done = subprocess.run([tool, "-layout", str(path), "-"], capture_output=True, text=True)
    if done.returncode != 0:
        raise StatementError(f"pdftotext 실패: {done.stderr.strip()[:200]}")
    return done.stdout


def _split_tail(rest: str, count: int, line_no: int) -> tuple[list[str], list[Decimal]]:
    tokens = rest.split()
    tail = tokens[-count:]
    if len(tail) != count or not all(NUMBER.match(token) for token in tail):
        raise StatementError(
            f"{line_no}행: 숫자 열 {count}개를 읽지 못했다 — 내역서 양식이 바뀌었을 수 있다"
        )
    return tokens[:-count], [_dec(token) for token in tail]


def _take_type(head: list[str], known: dict[str, int]) -> tuple[str, str]:
    text = " ".join(head)
    for kind in sorted(list(known) + list(TRADE_SIDE), key=len, reverse=True):
        if text.startswith(kind):
            return kind, text[len(kind):].strip()
    return (head[0] if head else ""), " ".join(head[1:])


def parse_text(text: str) -> dict[str, Any]:
    """`pdftotext -layout` 출력을 행으로 옮긴다. 검증은 `verify` 가 한다."""
    lines = text.split("\n")
    period = PERIOD.search(text)
    if not period:
        raise StatementError("조회 기간을 찾지 못했다 — 토스증권 거래내역서가 맞는지 확인한다")
    numbers = [int(value) for value in period.groups()]
    start = dt.date(*numbers[:3]).isoformat()
    end = dt.date(*numbers[3:]).isoformat()
    section: str | None = None
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if line.startswith("원화 거래내역"):
            section = "KRW"
        elif line.startswith("달러 거래내역"):
            section = "USD"
        match = DATE_LINE.match(line)
        if not match or section is None:
            continue
        date = "-".join(match.group(1, 2, 3))
        if section == "KRW":
            head, nums = _split_tail(match.group(4), 10, index + 1)
            fx = None
            if head and NUMBER.match(head[-1]):   # 환전 행은 환율이 한 칸 더 있다
                fx = _dec(head.pop())
            kind, name = _take_type(head, KRW_CASH_SIGN)
            code = KR_CODE.search(name)
            qty, amount, settle, price, fee, trade_tax, other_tax, _repay, position, balance = nums
            rows.append({
                "section": "KRW", "line": index + 1, "date": date, "type": kind,
                "security": code.group(1) if code else None,
                # 매매가 아닌 행의 이름 칸에는 이체 상대의 성명이 들어 있다. 버린다.
                "name": KR_CODE.sub("", name).strip() if code else "",
                "market": "KR", "fx": fx, "quantity": qty, "amount": amount,
                "settle": settle, "price": price, "fee": fee,
                "tax": trade_tax + other_tax, "position": position, "balance": balance,
            })
            continue
        head, nums = _split_tail(match.group(4), 10, index + 1)
        kind, name = _take_type(head, USD_CASH_SIGN)
        fx, qty, amount, settle, price, fee, tax, _repay, position, balance = nums
        dollars: list[Decimal] = []
        isin = ISIN.search(name)
        for look in range(index + 1, min(index + 4, len(lines))):
            if DATE_LINE.match(lines[look]):
                break
            found = DOLLARS.findall(lines[look])
            if found and not dollars:
                dollars = [_dec(value) for value in found]
            wrapped = ISIN.search(lines[look])   # 이름이 길면 ISIN 이 다음 줄로 넘어간다
            if wrapped and not isin:
                isin = wrapped
        if len(dollars) != 7:
            raise StatementError(f"{index + 1}행: 달러 금액 7개를 읽지 못했다")
        rows.append({
            "section": "USD", "line": index + 1, "date": date, "type": kind,
            "security": isin.group(1) if isin else None,
            "name": ISIN.sub("", name).strip() if isin else "",
            "market": "US", "fx": fx, "quantity": qty, "amount": amount, "settle": settle,
            "price": price, "fee": fee, "tax": tax, "position": position, "balance": balance,
            "settle_usd": dollars[1], "price_usd": dollars[2], "balance_usd": dollars[6],
        })
    if not rows:
        raise StatementError("거래 행을 하나도 읽지 못했다")
    return {"period_start": start, "period_end": end, "rows": rows}


def _is_transfer(kind: str) -> bool:
    """입고·출고는 주식만 움직이고 예수금은 그대로다. 잔액 칸에는 0 이 찍힌다."""
    return kind.endswith(("입고", "출고"))


def _position_delta(row: dict[str, Any]) -> Decimal | None:
    if row["type"] == "구매" or row["type"].endswith("입고"):
        return row["quantity"]
    if row["type"] == "판매" or row["type"].endswith("출고"):
        return -row["quantity"]
    return None


def verify(statement: dict[str, Any]) -> tuple[list[str], list[str]]:
    """예수금 잔액과 종목 잔고가 행마다 이어지는지 본다.

    돌려주는 것은 (문제, 경고)다. 문제가 하나라도 있으면 파싱을 믿을 수 없다.
    """
    problems: list[str] = []
    warnings: list[str] = []
    guessed: set[str] = set()
    for section, signs, key, tolerance in (
        ("KRW", KRW_CASH_SIGN, "settle", Decimal(0)),
        ("USD", USD_CASH_SIGN, "settle_usd", CENT),
    ):
        balance_key = "balance" if section == "KRW" else "balance_usd"
        previous: Decimal | None = None
        for row in statement["rows"]:
            if row["section"] != section or _is_transfer(row["type"]):
                continue
            current = row[balance_key]
            if previous is not None:
                sign = signs.get(row["type"])
                if sign is None:
                    moved = current - previous
                    if abs(abs(moved) - row[key]) <= tolerance:
                        guessed.add(row["type"])
                        previous = current
                        continue
                    problems.append(
                        f"{row['line']}행 {row['date']} 모르는 거래구분 '{row['type']}' — "
                        "잔액 변화로도 방향을 정하지 못했다"
                    )
                elif abs(previous + sign * row[key] - current) > tolerance:
                    problems.append(
                        f"{row['line']}행 {row['date']} {row['type']}: {section} 잔액 "
                        f"{current:,} 이 직전 {previous:,} 에서 이어지지 않는다"
                    )
            previous = current
    positions: dict[str, Decimal] = {}
    for row in statement["rows"]:
        delta = _position_delta(row)
        if delta is None or not row["security"]:
            continue
        key = row["security"]
        if key in positions and positions[key] + delta != row["position"]:
            problems.append(
                f"{row['line']}행 {row['date']} {row['name']}: 잔고 {row['position']} 가 "
                f"직전 {positions[key]} 에서 이어지지 않는다"
            )
        positions[key] = row["position"]
    if guessed:
        warnings.append(
            "모르는 거래구분의 예수금 방향을 잔액 변화로 추정했다: " + ", ".join(sorted(guessed))
        )
    return problems, warnings


def positions_of(statement: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """종목별 기초·기말 잔고와 이름. 내역서에 행이 있는 종목만 나온다."""
    out: dict[str, dict[str, Any]] = {}
    for row in statement["rows"]:
        delta = _position_delta(row)
        if delta is None or not row["security"]:
            continue
        item = out.setdefault(row["security"], {
            "name": row["name"], "market": row["market"],
            "opening": row["position"] - delta, "traded": False,
        })
        item["closing"] = row["position"]
        item["traded"] = item["traded"] or row["type"] in TRADE_SIDE
    return out


def cash_of(statement: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in statement["rows"]:
        if _is_transfer(row["type"]):
            continue
        if row["section"] == "KRW":
            out["krw"], out["krw_as_of"] = row["balance"], row["date"]
        else:
            out["usd"], out["usd_as_of"] = row["balance_usd"], row["date"]
    return out


# ── 원장 행 ─────────────────────────────────────────────────────────────

def _side(row: dict[str, Any]) -> str | None:
    if row["type"] in TRADE_SIDE:
        return TRADE_SIDE[row["type"]]
    if row["type"].endswith("입고"):
        return "RECEIVE"
    return None


def ledger_row(row: dict[str, Any], label: str) -> dict[str, str] | None:
    side = _side(row)
    if side is None:
        return None
    note = f"{row['type']} (무상입고)" if side == "RECEIVE" else ""
    if row["market"] == "KR":
        return {"settle_date": row["date"], "side": side, "quantity": plain(row["quantity"]),
                "price_krw": plain(row["price"]), "fee_krw": plain(row["fee"]),
                "tax_krw": plain(row["tax"]), "note": note, "_new_note": label,
                "_fixed_note": label.replace("로 추가", "로 보정")}
    extra = f"제세금 {row['tax']:,}원 별도" if side == "SELL" and row["tax"] else ""
    return {"settle_date": row["date"], "side": side, "quantity": plain(row["quantity"]),
            "price_krw": plain(row["price"]), "price_usd": plain(row["price_usd"]),
            "fx_rate": plain(row["fx"]), "fee_krw": plain(row["fee"]), "note": note,
            "_new_note": " / ".join(part for part in (extra, label) if part),
            "_fixed_note": label.replace("로 추가", "로 보정")}


def closed_row(row: dict[str, Any]) -> dict[str, str] | None:
    side = _side(row)
    if side is None:
        return None
    usd = row["market"] == "US"
    return {"settle_date": row["date"], "name": row["name"], "market": row["market"],
            "side": side, "quantity": plain(row["quantity"]), "price_krw": plain(row["price"]),
            "fee_krw": plain(row["fee"]), "tax_krw": "0" if usd else plain(row["tax"]),
            "price_usd": plain(row["price_usd"]) if usd else "",
            "balance": plain(row["position"])}


def read_ledger_file(path: Path) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """(머리말 주석, 열 이름, 행). note 안의 따옴표 없는 쉼표를 견딘다.

    옛 원장은 note 에 "4,500원" 같은 쉼표를 따옴표 없이 적었다. DictReader 로 읽으면
    첫 쉼표에서 잘려 나머지가 사라지므로, 열 수를 넘는 조각은 마지막 열에 다시 붙인다.
    """
    header: list[str] = []
    body: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").split("\n"):
        if line.lstrip().startswith("#"):
            header.append(line)
        elif line.strip():
            body.append(line)
    reader = csv.reader(body)
    fields = next(reader, [])
    rows = []
    for cells in reader:
        if len(cells) > len(fields):
            cells = cells[:len(fields) - 1] + [",".join(cells[len(fields) - 1:])]
        rows.append(dict(zip(fields, cells + [""] * (len(fields) - len(cells)))))
    return header, fields, rows


def reconcile(old: list[dict[str, str]], new: list[dict[str, str]], fields: list[str],
              start: str, end: str) -> dict[str, Any]:
    """조회 기간 안의 옛 행과 내역서 행을 짝짓는다. 기간 밖의 행은 그대로 둔다."""
    before = [row for row in old if row["settle_date"] < start]
    after = [row for row in old if row["settle_date"] > end]
    inside = [row for row in old if start <= row["settle_date"] <= end]
    used = [False] * len(inside)
    compare = [name for name in fields
               if name not in ("settle_date", "side", "quantity", "price_krw", "note",
                               "name", "market")]
    added, changed, merged = [], [], []
    for row in new:
        hit = next(
            (i for i, cand in enumerate(inside)
             if not used[i] and cand["settle_date"] == row["settle_date"]
             and cand["side"] == row["side"] and _same(cand["quantity"], row["quantity"])
             and _same(cand["price_krw"], row["price_krw"])), None)
        out = {name: row.get(name, "") for name in fields}
        if hit is None:
            if "note" in out:
                out["note"] = " / ".join(
                    part for part in (row.get("note", ""), row.get("_new_note", "")) if part)
            added.append(out)
        else:
            used[hit] = True
            if "note" in out:
                out["note"] = inside[hit].get("note", "")
            diffs = {name: (inside[hit].get(name, ""), out[name]) for name in compare
                     if not _same(inside[hit].get(name), out[name])}
            if diffs:
                # 옛 note 의 "수수료 미확인" 같은 말이 값이 고쳐진 뒤에도 남는다.
                # 지우지는 않고, 이 행이 내역서로 보정됐다는 사실을 덧붙인다.
                if "note" in out and row.get("_fixed_note"):
                    out["note"] = " / ".join(
                        part for part in (out["note"], row["_fixed_note"]) if part)
                changed.append((out, diffs))
        merged.append(out)
    removed = [inside[i] for i in range(len(inside)) if not used[i]]
    return {"rows": before + merged + after, "added": added, "changed": changed,
            "removed": removed, "matched": sum(used)}


def _folded_quantity(rows: list[dict[str, str]], through: str | None = None) -> Decimal:
    total = Decimal(0)
    for row in rows:
        if through is not None and row["settle_date"] > through:
            continue
        count = Decimal(str(row["quantity"]).replace(",", ""))
        total += -count if row["side"] == "SELL" else count
    return total


def asset_mapping(extra: dict[str, str] | None = None) -> dict[str, str]:
    mapping = dict(ISIN_TO_ASSET)
    try:
        registry = json.loads(ASSETS.read_text(encoding="utf-8"))
        for asset_id, asset in registry.items():
            if isinstance(asset, dict) and asset.get("isin"):
                mapping[asset["isin"]] = asset_id
    except (OSError, ValueError):
        pass
    mapping.update(extra or {})
    return mapping


def plan(statement: dict[str, Any], mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """내역서를 원장에 적용하면 무엇이 바뀌는지 계산한다. 아무것도 쓰지 않는다."""
    mapping = asset_mapping() if mapping is None else mapping
    start, end = statement["period_start"], statement["period_end"]
    label = f"거래내역서 {start}~{end} 로 추가"
    securities = positions_of(statement)
    by_security: dict[str, list[dict[str, Any]]] = {}
    for row in statement["rows"]:
        if row["security"] and _side(row):
            by_security.setdefault(row["security"], []).append(row)

    assets: dict[str, dict[str, Any]] = {}
    closed_new: list[dict[str, str]] = []
    untracked: list[str] = []
    problems: list[str] = []
    asset_of: dict[str, str] = {}
    for key, info in securities.items():
        asset_id = key if info["market"] == "KR" else mapping.get(key)
        path = TRADES_DIR / f"{asset_id}.csv" if asset_id else None
        rows = by_security.get(key, [])
        if path is not None and path.exists():
            asset_of[key] = asset_id
            header, fields, old = read_ledger_file(path)
            new = [ledger_row(row, label) for row in rows]
            result = reconcile(old, [row for row in new if row], fields, start, end)
            result.update({"path": path, "header": header, "fields": fields, "exists": True,
                           "name": info["name"], "closing": info["closing"], "problems": []})
            opening = _folded_quantity([r for r in old if r["settle_date"] < start])
            if opening != info["opening"]:
                result["problems"].append(
                    f"기간 전 원장 수량 {plain(opening)} 이 내역서 기초잔고 "
                    f"{plain(info['opening'])} 와 다르다 — 그 이전 내역서가 필요하다")
            elif _folded_quantity(result["rows"], end) != info["closing"]:
                result["problems"].append(
                    f"고친 원장을 접어도 내역서 잔고 {plain(info['closing'])} 가 되지 않는다")
            assets[asset_id] = result
        elif not rows or not info["traded"] and info["closing"] == 0:
            untracked.append(info["name"] or key)
        elif info["closing"] != 0:
            if asset_id is None:
                problems.append(
                    f"{info['name']} ({key}): 잔고 {plain(info['closing'])} 가 있는데 원장 파일명을 "
                    f"모른다 — `--map {key}=티커` 로 알려 준다")
                continue
            asset_of[key] = asset_id
            fields = KR_FIELDS if info["market"] == "KR" else US_FIELDS
            new = [ledger_row(row, label) for row in rows]
            result = reconcile([], [row for row in new if row], fields, start, end)
            result.update({"path": path, "header": _new_header(asset_id, info, start, end),
                           "fields": fields, "exists": False, "name": info["name"],
                           "closing": info["closing"], "problems": []})
            if info["opening"] != 0:
                result["problems"].append(
                    f"원장이 없는데 내역서 기초잔고가 {plain(info['opening'])} 다 — "
                    "그 이전 내역서가 필요하다")
            assets[asset_id] = result
        else:
            closed_new.extend(row for row in map(closed_row, rows) if row)

    closed_path = TRADES_DIR / f"{CLOSED_FILE}.csv"
    closed: dict[str, Any] | None = None
    if closed_new or closed_path.exists():
        header, fields, old = (read_ledger_file(closed_path) if closed_path.exists()
                               else ([], CLOSED_FIELDS, []))
        closed_new.sort(key=lambda row: row["settle_date"])
        closed = reconcile(old, closed_new, fields or CLOSED_FIELDS, start, end)
        closed.update({"path": closed_path, "header": header,
                       "fields": fields or CLOSED_FIELDS, "exists": closed_path.exists(),
                       "problems": []})

    return {"period_start": start, "period_end": end, "assets": assets, "closed": closed,
            "untracked": sorted(set(untracked)), "problems": problems,
            "holdings": _compare_holdings(securities, asset_of),
            "cash": cash_of(statement)}


def _new_header(asset_id: str, info: dict[str, Any], start: str, end: str) -> list[str]:
    lines = [
        f"# {info['name']} ({asset_id}) 매매 내역",
        f"# 출처: 토스증권 거래내역서 {start}~{end} (collectors.toss_statement).",
        "#",
        "# ⚠️ settle_date = 내역서의 '거래일자' = 결제 기준일이다. 체결일이 아니다.",
    ]
    if info["market"] == "KR":
        lines.append("# price_krw = 체결 단가(원). fee_krw = 수수료, tax_krw = 거래세 + 제세금.")
    else:
        lines.append("# price_krw = 원화 환산 단가, price_usd = 달러 단가, fx_rate = 적용 환율,")
        lines.append("# fee_krw = 수수료. 미국 매도의 제세금은 열이 없어 note 에 적는다.")
    return lines


def _compare_holdings(securities: dict[str, dict[str, Any]],
                      asset_of: dict[str, str]) -> dict[str, Any]:
    """내역서 기말 잔고와 holdings.csv 수량. 거래가 없던 종목은 내역서에 없어 알 수 없다."""
    if not HOLDINGS.exists():
        return {"available": False, "mismatches": [], "unseen": []}
    _header, _fields, rows = read_ledger_file(HOLDINGS)
    held = {row["asset_id"]: Decimal(row["quantity"]) for row in rows
            if row.get("quantity", "").strip()}
    seen: dict[str, Decimal] = {}
    for key, info in securities.items():
        asset_id = asset_of.get(key) or (key if info["market"] == "KR" else None)
        if asset_id:
            seen[asset_id] = info["closing"]
    mismatches = [
        {"asset_id": asset_id, "statement": closing, "holdings": held.get(asset_id, Decimal(0))}
        for asset_id, closing in sorted(seen.items())
        if held.get(asset_id, Decimal(0)) != closing
    ]
    return {"available": True, "mismatches": mismatches,
            "unseen": sorted(asset_id for asset_id in held if asset_id not in seen)}


# ── 쓰기 ────────────────────────────────────────────────────────────────

def _changed(result: dict[str, Any] | None) -> bool:
    return bool(result and (result["added"] or result["removed"] or result["changed"]
                            or not result["exists"]))


def render_ledger(result: dict[str, Any], stamp: str | None) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=result["fields"], extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(result["rows"])
    header = list(result["header"]) + ([stamp] if stamp else [])
    return "\n".join(header) + ("\n" if header else "") + buffer.getvalue()


def apply(change: dict[str, Any], today: dt.date | None = None) -> list[Path]:
    """바뀌는 원장만 다시 쓴다. 문제가 있는 종목은 건너뛴다. 쓴 파일을 돌려준다."""
    today = today or dt.date.today()
    targets = [result for result in list(change["assets"].values()) + [change["closed"]]
               if _changed(result) and not result["problems"]]
    if not targets:
        return []
    backup = BACKUP_DIR / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []
    for result in targets:
        path: Path = result["path"]
        if path.exists():
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup / path.name)
        closing = f" / 내역서 잔고 {plain(result['closing'])}주" if "closing" in result else ""
        stamp = (
            f"# [toss_statement {today.isoformat()}] 거래내역서 {change['period_start']}~"
            f"{change['period_end']} 로 보정: 추가 {len(result['added'])} · 삭제 "
            f"{len(result['removed'])} · 값 변경 {len(result['changed'])}{closing}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp")
        with handle:
            handle.write(render_ledger(result, stamp))
        os.replace(handle.name, path)
        written.append(path)
    return written


# ── 화면 ────────────────────────────────────────────────────────────────

def _show_rows(result: dict[str, Any]) -> None:
    def brief(row: dict[str, str]) -> str:
        name = f"{row['name']} " if row.get("name") else ""
        return (f"{row['settle_date']} {name}{row['side']} {row['quantity']} "
                f"@{Decimal(row['price_krw']):,}")
    for row in result["added"]:
        print(f"    + {brief(row)}")
    for row in result["removed"]:
        print(f"    - {brief(row)}  (내역서에 없다)")
    for row, diffs in result["changed"]:
        detail = ", ".join(f"{name} {old or '0'} → {new}" for name, (old, new) in diffs.items())
        print(f"    ~ {brief(row)}  {detail}")
    for problem in result["problems"]:
        print(f"    ⚠️ {problem}")


def render(statement: dict[str, Any], change: dict[str, Any], warnings: list[str]) -> None:
    rows = statement["rows"]
    print(f"거래내역서 {change['period_start']} ~ {change['period_end']} · "
          f"원화 {sum(r['section'] == 'KRW' for r in rows)}행 · "
          f"달러 {sum(r['section'] == 'USD' for r in rows)}행")
    print("파싱 검증: 예수금 잔액과 종목 잔고가 모든 행에서 이어진다")
    for warning in warnings:
        print(f"  ※ {warning}")
    cash = change["cash"]
    if "krw" in cash:
        print(f"원화 예수금 {cash['krw']:,}원 ({cash['krw_as_of']} 마지막 거래 뒤)")
    if "usd" in cash:
        print(f"달러 예수금 ${cash['usd']:,} ({cash['usd_as_of']} 마지막 거래 뒤)")

    print("\n원장 대조")
    for asset_id, result in sorted(change["assets"].items()):
        state = ("변경 없음" if not _changed(result) else
                 f"추가 {len(result['added'])} · 삭제 {len(result['removed'])} · "
                 f"값 변경 {len(result['changed'])}")
        fresh = "" if result["exists"] else " (새 원장)"
        print(f"  {asset_id:<8} 내역서 잔고 {plain(result['closing']):>10}  "
              f"일치 {result['matched']} · {state}{fresh}")
        _show_rows(result)
    closed = change["closed"]
    if closed is not None:
        state = ("변경 없음" if not _changed(closed) else
                 f"추가 {len(closed['added'])} · 삭제 {len(closed['removed'])} · "
                 f"값 변경 {len(closed['changed'])}")
        print(f"  {'_closed':<8} 청산 종목                일치 {closed['matched']} · {state}")
        _show_rows(closed)
    for problem in change["problems"]:
        print(f"  ⚠️ {problem}")
    if change["untracked"]:
        print("  원장에 넣지 않은 종목(매매 행 없음): " + ", ".join(change["untracked"]))

    holdings = change["holdings"]
    print("\nholdings.csv 수량 대조 (이 파일은 고치지 않는다)")
    if not holdings["available"]:
        print("  holdings.csv 가 없다")
    elif not holdings["mismatches"]:
        print("  내역서에 나온 종목은 전부 일치한다")
    for item in holdings["mismatches"]:
        print(f"  {item['asset_id']:<8} 내역서 {plain(item['statement'])} ≠ "
              f"holdings {plain(item['holdings'])}")
    if holdings.get("unseen"):
        print("  기간 안에 거래가 없어 확인하지 못한 행: " + ", ".join(holdings["unseen"]))
    print("-" * 76)
    print("※ 대조와 산술이다. 무엇을 사고팔지에 대한 값은 여기 없다")


def in_sync(change: dict[str, Any]) -> bool:
    results = list(change["assets"].values()) + ([change["closed"]] if change["closed"] else [])
    return not (
        any(_changed(result) or result["problems"] for result in results)
        or change["problems"] or change["holdings"]["mismatches"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="토스증권 거래내역서로 거래 원장을 대조·보정")
    parser.add_argument("command", choices=["check", "apply"],
                        help="check 는 읽기 전용이다. apply 는 조회 기간의 원장 행을 내역서로 바꾼다")
    parser.add_argument("statement", help="거래내역서 PDF, 또는 pdftotext -layout 으로 뽑은 .txt")
    parser.add_argument("--map", action="append", default=[], metavar="ISIN=TICKER",
                        help="내역서의 ISIN 을 원장 파일명으로 옮기는 대응 (반복 가능)")
    args = parser.parse_args()
    try:
        extra = dict(item.split("=", 1) for item in args.map)
        statement = parse_text(read_text(Path(args.statement)))
        problems, warnings = verify(statement)
        if problems:
            print("내역서를 믿을 수 없다 — 잔액이 이어지지 않는다. 원장은 그대로 뒀다", file=sys.stderr)
            for problem in problems[:20]:
                print(f"  {problem}", file=sys.stderr)
            raise SystemExit(2)
        change = plan(statement, asset_mapping(extra))
    except (StatementError, ValueError, OSError) as error:
        print(f"오류: {getattr(error, 'message', error)}", file=sys.stderr)
        raise SystemExit(2)
    render(statement, change, warnings)
    if args.command == "apply":
        written = apply(change)
        if written:
            print("\n고친 원장: " + ", ".join(path.name for path in written))
            print(f"직전 파일은 {BACKUP_DIR.relative_to(ROOT)}/ 에 남겼다")
        else:
            print("\n고칠 원장이 없다")
        change = plan(statement, asset_mapping(extra))
    raise SystemExit(0 if in_sync(change) else 1)


if __name__ == "__main__":
    main()
