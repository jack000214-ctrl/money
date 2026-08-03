"""임상 파이프라인 기록.

임상단계 바이오에서 논지 전체는 **어떤 프로그램이 어느 단계에 있고 다음
데이터가 언제 나오는가**에 걸려 있다. 그런데 그 정보를 주는 공개 API 가 없다 —
SEC 공시에는 흩어져 있고, ClinicalTrials.gov 는 등록 정보일 뿐 회사의 우선순위를
말하지 않는다. 그래서 `PIPELINE_RNPV` 가 registry 에 있어도 채울 수집기가 없었고,
`CLINICAL_BIOTECH` 렌즈가 연결하는 것은 "조사하라"는 지시문 한 줄뿐이었다.

여기는 **모델이 조사한 결과를 남기는 자리**다. 수집기가 아니라 대장이다.
`cio requests` 가 모델의 요청을 사용자가 처리하는 대기열인 것처럼, 이쪽은
모델이 조사한 파이프라인 사실을 다음 판단으로 넘기는 통로다.

한 번 조사하면 다음 판단의 packet 에 실려 나오므로 같은 검색을 반복하지
않는다. 대신 **낡는다** — `as_of` 를 함께 저장하고 packet 이 경과일을 보여준다.
readout 예정일이 지났는데 결과가 기록되지 않았으면 그것 자체가 신호다.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from calculators.contracts import ContractError, load_json, write_json
from cio.assets import get_asset
from cio.paths import KNOWLEDGE_DIR

SCHEMA_VERSION = "pipeline-ledger/1.0.0"

# 임상 단계. 성공확률은 단계마다 크게 다르고 그 값은 모델이 정한다 — 여기서
# 기본 확률을 주면 계산기가 판정을 내는 것이 된다.
STAGES = (
    "DISCOVERY", "PRECLINICAL", "PHASE1", "PHASE1_2", "PHASE2", "PHASE2_3",
    "PHASE3", "REGISTRATION", "APPROVED", "DISCONTINUED",
)


class PipelineError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _path(asset_id: str) -> Path:
    return KNOWLEDGE_DIR / "pipeline" / f"{asset_id}.json"


def load_pipeline(asset_id: str) -> dict[str, Any]:
    path = _path(asset_id)
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "asset_id": asset_id,
            "programs": [],
        }
    try:
        return load_json(path)
    except (ContractError, OSError) as exc:
        raise PipelineError(f"{path}: 읽을 수 없다 — {exc}") from exc


def record_program(
    asset_id: str,
    *,
    name: str,
    stage: str,
    indication: str | None = None,
    next_milestone: str | None = None,
    expected_readout: str | None = None,
    source: str,
    note: str | None = None,
) -> Path:
    """프로그램 하나를 기록하거나 갱신한다.

    `source` 는 필수다. 출처 없는 파이프라인 항목은 기억이지 근거가 아니고,
    이 프로젝트에서 기억은 근거가 될 수 없다.
    """
    asset = get_asset(asset_id)
    stage = stage.upper()
    if stage not in STAGES:
        raise PipelineError(
            f"stage 는 {' | '.join(STAGES)} 중 하나여야 한다 (받은 값: {stage})"
        )
    if not (source or "").strip():
        raise PipelineError(
            "source 가 필요하다. 출처 없는 파이프라인 항목은 근거로 쓸 수 없다"
        )
    if expected_readout:
        try:
            dt.date.fromisoformat(expected_readout)
        except ValueError as exc:
            raise PipelineError(
                "expected_readout 은 YYYY-MM-DD 형식이어야 한다"
            ) from exc
    data = load_pipeline(asset["asset_id"])
    programs = [
        row for row in data.get("programs", [])
        if str(row.get("name")).lower() != name.lower()
    ]
    programs.append({
        "name": name,
        "stage": stage,
        "indication": indication,
        "next_milestone": next_milestone,
        "expected_readout": expected_readout,
        "source": source.strip(),
        "note": note,
        "as_of": dt.date.today().isoformat(),
    })
    programs.sort(key=lambda row: (str(row.get("expected_readout") or "9999"),
                                   str(row.get("name"))))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "asset_id": asset["asset_id"],
        "updated_at": dt.datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "programs": programs,
        "_role": (
            "모델이 조사해 남긴 파이프라인 사실이다. 수집기가 만든 것이 아니라 "
            "판단 과정의 산출물이므로, 인용하기 전에 as_of 와 source 를 본다"
        ),
    }
    path = _path(asset["asset_id"])
    write_json(path, payload)
    return path


def pipeline_for_context(
    asset_id: str, today: dt.date | None = None
) -> dict[str, Any] | None:
    """packet 용 요약. 경과일과 지난 readout 을 함께 낸다."""
    today = today or dt.date.today()
    try:
        data = load_pipeline(asset_id)
    except PipelineError:
        return None
    programs = data.get("programs") or []
    if not programs:
        return None
    rows = []
    overdue = []
    for program in programs:
        readout = program.get("expected_readout")
        row = {
            key: program.get(key)
            for key in ("name", "stage", "indication", "next_milestone",
                        "expected_readout", "source", "as_of")
        }
        if readout:
            try:
                due = dt.date.fromisoformat(str(readout))
                row["days_until_readout"] = (due - today).days
                if due < today:
                    overdue.append(program.get("name"))
            except ValueError:
                pass
        as_of = program.get("as_of")
        if as_of:
            try:
                row["days_since_researched"] = (
                    today - dt.date.fromisoformat(str(as_of))
                ).days
            except ValueError:
                pass
        rows.append(row)
    summary: dict[str, Any] = {
        "updated_at": data.get("updated_at"),
        "program_count": len(rows),
        "programs": rows,
        "_role": (
            "이전 판단에서 조사해 남긴 파이프라인이다. 수집기 산출물이 아니라 "
            "모델의 조사 결과이므로 그 시점 이후는 반영돼 있지 않다"
        ),
    }
    if overdue:
        summary["overdue_readouts"] = {
            "programs": overdue,
            "_warning": (
                "예상 readout 일이 지났는데 결과가 기록되지 않았다. 발표가 "
                "미뤄졌거나 조사가 갱신되지 않은 것이며, 어느 쪽인지는 이 "
                "기록만으로 구분되지 않는다"
            ),
        }
    return summary


def format_pipeline(asset_id: str, today: dt.date | None = None) -> str:
    summary = pipeline_for_context(asset_id, today)
    if not summary:
        return (
            f"{asset_id}: 기록된 파이프라인이 없다.\n"
            "  임상단계 바이오에서 이것이 비어 있으면 논지의 핵심이 비어 있다.\n"
            "  조사한 뒤 `cio pipeline <asset_id> --name ... --stage ... "
            "--source ...` 로 남긴다"
        )
    lines = [f"파이프라인 — {asset_id}  ({summary['program_count']}건, "
             f"갱신 {summary['updated_at']})"]
    for row in summary["programs"]:
        due = row.get("expected_readout") or "-"
        days = row.get("days_until_readout")
        left = f" ({days:+d}일)" if days is not None else ""
        lines.append(
            f"  {row['name']}  [{row['stage']}]  {row.get('indication') or '-'}"
        )
        lines.append(
            f"      다음 이벤트  {row.get('next_milestone') or '-'}  "
            f"예상 {due}{left}"
        )
        lines.append(
            f"      출처 {row.get('source')}  (조사 {row.get('as_of')}, "
            f"{row.get('days_since_researched', '?')}일 경과)"
        )
    if summary.get("overdue_readouts"):
        lines.append("")
        lines.append(f"⚠️ {summary['overdue_readouts']['_warning']}")
        lines.append(f"   {', '.join(summary['overdue_readouts']['programs'])}")
    return "\n".join(lines)
