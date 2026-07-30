from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from calculators.contracts import ContractError, load_json
from collectors.web import snapshot
from cio.assets import all_assets, get_asset, register_asset
from cio.context import build_context
from cio.decision import find_context, require_valid_decision
from cio.documents import (
    DEFAULT_CONTEXT_CHARS,
    DEFAULT_MAX_MATCHES,
    DocumentError,
    documents_for,
    format_documents,
    format_search,
    search_asset,
)
from cio.memory import record_decision_file, score_asset
from cio.refresh import inspect_plan, plan_json, run_refresh
from cio.requests import (
    RESOLUTION_STATES,
    format_requests,
    request_state,
    resolve_request,
)

MIN_PYTHON = (3, 13)


def cmd_assets(_args) -> int:
    rows = [
        {
            "asset_id": asset_id,
            "name": asset["name"],
            "market": asset["market"],
            "asset_type": asset["asset_type"],
            "analysis_lens": asset["analysis_lens"],
        }
        for asset_id, asset in sorted(all_assets().items())
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_register(args) -> int:
    etf = None
    if args.asset_type == "ETF":
        etf = {
            "strategy": args.etf_strategy,
            "underlying": args.underlying,
            "derivative_use": args.derivative_use,
            "leverage": args.leverage,
            "holdings_source": "ISSUER",
            "distribution_policy": args.distribution_policy,
        }
    asset = register_asset(
        args.asset_id,
        name=args.name,
        market=args.market,
        exchange=args.exchange,
        asset_type=args.asset_type,
        price_symbol=args.price_symbol,
        sector_code=args.sector_code,
        analysis_lens=args.analysis_lens,
        valuation_family=args.valuation_family,
        benchmarks=args.benchmark,
        filing_source=args.filing_source,
        etf=etf,
    )
    print(json.dumps(asset, ensure_ascii=False, indent=2))
    return 0


def cmd_refresh(args) -> int:
    if not args.run:
        payload = plan_json(args.asset_id, include_optional=args.optional)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        for task in payload["tasks"]:
            age = (
                f"{task['age_hours']:.1f}h"
                if task["age_hours"] is not None
                else "-"
            )
            print(
                f"{task['state']:<8} {task['name']:<28} "
                f"age={age:<9} as_of={task['data_as_of'] or '-'}"
            )
        return 0
    results, code = run_refresh(
        args.asset_id,
        include_optional=args.optional,
        force=args.force,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return code


def cmd_prepare(args) -> int:
    packet, path = build_context(
        args.asset_id,
        price=args.price,
        amount=args.amount,
        amount_currency=args.amount_currency,
        save=not args.no_save,
    )
    if args.json:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
    else:
        print(f"context_id: {packet['context_id']}")
        print(f"validation: {packet['validation']['status']}")
        for problem in packet["validation"]["problems"]:
            print(f"  - {problem}")
        if path:
            print(f"saved: {path}")
        size = len(
            json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        print(f"compact_size_bytes: {size}")
    return 0 if packet["validation"]["status"] == "PASS" else 2


def cmd_validate(args) -> int:
    data = load_json(args.decision)
    context, _ = find_context(data.get("context_id", ""))
    normalized = require_valid_decision(data, context=context)
    print(
        json.dumps(
            {
                "status": "VALID",
                "assessment_id": normalized["assessment_id"],
                "asset_id": normalized["asset_id"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_record(args) -> int:
    path = record_decision_file(args.decision)
    print(f"saved: {path}")
    return 0


def cmd_docs(args) -> int:
    catalog = documents_for(args.asset_id)
    if args.json:
        print(json.dumps(catalog, ensure_ascii=False, indent=2))
    else:
        print(format_documents(catalog))
    return 0


def cmd_search(args) -> int:
    result = search_asset(
        args.asset_id,
        args.pattern,
        context_chars=args.context,
        max_matches=args.limit,
        fixed=args.fixed,
        doc_id=args.doc,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_search(result))
    return 0 if result["total_matches"] else 1


def cmd_snapshot(args) -> int:
    get_asset(args.asset_id)
    result = snapshot(
        args.url,
        asset_id=args.asset_id,
        label=args.label,
        note=args.note,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_requests(args) -> int:
    if args.resolve:
        path = resolve_request(
            args.asset_id,
            args.resolve,
            state=args.state,
            note=args.note,
        )
        print(f"saved: {path}")
        return 0
    state = request_state(args.asset_id)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print(format_requests(state))
    return 0


def cmd_score(args) -> int:
    report = score_asset(args.asset_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 건너뛴 판단이 있으면 조용히 성공으로 끝내지 않는다.
    return 0 if not report["skipped"] else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3.13 -m cio",
        description="한·미 주식/ETF용 provider-neutral CIO context and memory",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    assets = sub.add_parser("assets", help="등록된 자산 보기")
    assets.set_defaults(func=cmd_assets)

    register = sub.add_parser("register", help="새 KR/US 주식 또는 ETF 등록")
    register.add_argument("asset_id")
    register.add_argument("--name", required=True)
    register.add_argument("--market", required=True, choices=["KR", "US"])
    register.add_argument("--exchange", required=True)
    register.add_argument("--asset-type", required=True, choices=["STOCK", "ETF"])
    register.add_argument("--price-symbol", required=True)
    register.add_argument("--sector-code", required=True)
    register.add_argument("--analysis-lens", required=True)
    register.add_argument("--valuation-family", nargs="+", required=True)
    register.add_argument("--benchmark", action="append", required=True)
    register.add_argument("--filing-source", choices=["DART", "SEC_EDGAR"])
    register.add_argument("--etf-strategy", default="UNKNOWN")
    register.add_argument("--underlying")
    register.add_argument("--derivative-use", action="store_true")
    register.add_argument("--leverage", type=float, default=1.0)
    register.add_argument("--distribution-policy", default="UNKNOWN")
    register.set_defaults(func=cmd_register)

    refresh = sub.add_parser("refresh", help="선택 자산에 필요한 수집만 계획/실행")
    refresh.add_argument("asset_id")
    refresh.add_argument("--run", action="store_true")
    refresh.add_argument("--optional", action="store_true")
    refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(func=cmd_refresh)

    prepare = sub.add_parser("prepare", help="모델용 최소 context packet 생성")
    prepare.add_argument("asset_id")
    prepare.add_argument("--price", type=float, required=True)
    prepare.add_argument("--amount", type=float, required=True)
    prepare.add_argument("--amount-currency", choices=["KRW", "USD"])
    prepare.add_argument("--no-save", action="store_true")
    prepare.add_argument("--json", action="store_true")
    prepare.set_defaults(func=cmd_prepare)

    validate = sub.add_parser("validate", help="모델 decision JSON 검증")
    validate.add_argument("decision")
    validate.set_defaults(func=cmd_validate)

    record = sub.add_parser("record", help="검증된 decision을 기억에 저장")
    record.add_argument("decision")
    record.set_defaults(func=cmd_record)

    docs_parser = sub.add_parser(
        "docs", help="검색 가능한 원문과 아직 받지 않은 공시"
    )
    docs_parser.add_argument("asset_id")
    docs_parser.add_argument("--json", action="store_true")
    docs_parser.set_defaults(func=cmd_docs)

    search_parser = sub.add_parser(
        "search", help="보존한 원문 안에서 찾기"
    )
    search_parser.add_argument("asset_id")
    search_parser.add_argument("pattern", help="정규식 또는 문자열")
    search_parser.add_argument(
        "--context", type=int, default=DEFAULT_CONTEXT_CHARS, help="앞뒤 문맥 글자 수"
    )
    search_parser.add_argument(
        "--limit", type=int, default=DEFAULT_MAX_MATCHES, help="표시할 최대 건수"
    )
    search_parser.add_argument(
        "--fixed", action="store_true", help="정규식이 아니라 문자 그대로"
    )
    search_parser.add_argument("--doc", help="특정 문서 ID 안에서만")
    search_parser.add_argument("--json", action="store_true")
    search_parser.set_defaults(func=cmd_search)

    snapshot_parser = sub.add_parser(
        "snapshot", help="조사한 웹 원문을 근거로 보존"
    )
    snapshot_parser.add_argument("url")
    snapshot_parser.add_argument("--asset", dest="asset_id", required=True)
    snapshot_parser.add_argument(
        "--label", required=True, help="소문자-하이픈 짧은 이름"
    )
    snapshot_parser.add_argument("--note", help="왜 남기는지 한 줄")
    snapshot_parser.set_defaults(func=cmd_snapshot)

    requests_parser = sub.add_parser(
        "requests", help="모델이 낸 수집 요청의 미해결/해결 상태"
    )
    requests_parser.add_argument("asset_id")
    requests_parser.add_argument(
        "--resolve", metavar="REQUEST_ID", help="요청 ID 또는 그 앞부분"
    )
    requests_parser.add_argument(
        "--state", choices=list(RESOLUTION_STATES), default="DONE"
    )
    requests_parser.add_argument("--note", help="무엇을 했는지 한 줄")
    requests_parser.add_argument("--json", action="store_true")
    requests_parser.set_defaults(func=cmd_requests)

    score = sub.add_parser("score", help="저장된 decision을 가격으로 사후평가")
    score.add_argument("asset_id")
    score.set_defaults(func=cmd_score)
    return parser


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        print(
            "ERROR: Python 3.13 이상이 필요하다. "
            f"현재 {sys.version_info.major}.{sys.version_info.minor}를 사용 중이다.",
            file=sys.stderr,
        )
        return 2
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (ContractError, DocumentError, ValueError, OSError) as exc:
        messages = exc.errors if isinstance(exc, ContractError) else [str(exc)]
        for message in messages:
            print(f"ERROR: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
