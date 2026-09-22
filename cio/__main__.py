from __future__ import annotations

import argparse
import json
import sys

from calculators.contracts import ContractError, load_json
from collectors.web import snapshot
from cio.assets import all_assets, get_asset, register_asset
from cio.context import INTENTS, build_context
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
from cio.paths import ROOT
from cio.policy import (
    FIELDS,
    POLICY_PATH,
    deliberate_gaps,
    load_policy,
    unset_fields,
)
from cio.refresh import plan_json, run_refresh
from cio.requests import (
    RESOLUTION_STATES,
    format_requests,
    request_state,
    resolve_request,
)
from cio.pipeline import PipelineError, format_pipeline, record_program
from cio.report import evaluations_for, format_evaluations, format_report
from cio.retire import RetireError, format_survey, retire, survey

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


def cmd_assumptions(args) -> int:
    from cio.assumptions import REPORT_ID, create_analysis, format_analysis, load_analysis, report_path
    if REPORT_ID.fullmatch(args.input):
        if not args.asset:
            raise ValueError("저장된 가정 분석 조회에는 --asset이 필요하다")
        header = load_json(report_path(args.asset, args.input))
        context, _ = find_context(header["context_id"])
        report = load_analysis(args.asset, args.input, context)
    else:
        if not args.context:
            raise ValueError("가정 계산에는 --context CTX-...가 필요하다")
        context, _ = find_context(args.context)
        report = create_analysis(load_json(args.input), context)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_analysis(report))
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
        ir_sources=args.ir_source,
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
        intent=args.intent,
        review_quantity=args.quantity,
        target_quantity=args.target_quantity,
        held_quantity=args.held_quantity,
        average_cost=args.average_cost,
        average_cost_currency=args.average_cost_currency,
        verbatim=args.verbatim,
        claims=args.claim,
        save=not args.no_save,
    )
    if args.json:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
    else:
        print(f"context_id: {packet['context_id']}")
        print(f"validation: {packet['validation']['status']}")
        readiness = packet["validation"].get("readiness") or {}
        position_ready = readiness.get("position_action") or {}
        if position_ready:
            print(f"position_action: {position_ready['status']}")
            for problem in position_ready.get("problems") or []:
                print(f"  - {problem}")
        if (packet.get("request") or {}).get("position_input"):
            print("position_basis: USER_INPUT (이번 질문의 조건이며 실제 잔고 확인이 아님)")
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


def cmd_pipeline(args) -> int:
    if not args.name:
        print(format_pipeline(args.asset_id))
        return 0
    try:
        path = record_program(
            args.asset_id,
            name=args.name,
            stage=args.stage,
            indication=args.indication,
            next_milestone=args.milestone,
            expected_readout=args.readout,
            source=args.source,
            note=args.note,
        )
    except PipelineError as exc:
        print(f"오류: {exc.message}", file=sys.stderr)
        return 2
    print(f"saved: {path}")
    print(format_pipeline(args.asset_id))
    return 0


def cmd_show(args) -> int:
    print(format_report(args.asset_id, assessment_id=args.assessment_id))
    return 0


def cmd_evals(args) -> int:
    if args.json:
        print(
            json.dumps(
                evaluations_for(args.asset_id), ensure_ascii=False, indent=2
            )
        )
        return 0
    print(format_evaluations(args.asset_id, with_plan=args.plan))
    return 0


def cmd_retire(args) -> int:
    if not args.reason:
        print(format_survey(survey(args.asset_id)))
        return 0
    try:
        result = retire(
            args.asset_id,
            reason=args.reason,
            include_live=args.all,
            confirm=args.confirm,
            assessment_id=args.id,
        )
    except RetireError as exc:
        print(f"오류: {exc.message}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not result["confirmed"]:
        print(format_survey(result))
        print()
        print(f"폐기 대상 {len(result['would_retire'])}건 — 사유: {args.reason}")
        for row in result["would_retire"]:
            print(f"  {row['created_at']}  {row['assessment_id']}")
        print(result["_note"])
        return 0
    print(f"폐기 {len(result['moved'])}건 — {result['reason']}")
    for row in result["moved"]:
        print(f"  {row['created_at']}  {row['assessment_id']}  → {row['to']}")
    print(f"남은 유효 판단 {len(result['live_remaining'])}건")
    return 0


def cmd_policy(args) -> int:
    """투자정책을 보여주고 무엇을 아직 정하지 않았는지 알린다.

    값을 여기서 고치지 않는다. 사용자가 파일을 직접 편집한다 — 자본 제약을
    프로그램이 대신 정하기 시작하면 그것은 더 이상 사용자의 정책이 아니다.
    """
    data, errors = load_policy()
    if data is None and not errors:
        print(f"투자정책이 없다: {POLICY_PATH.relative_to(ROOT)}")
        print("파일을 만들면 수량·분할매도 판단에 한도가 실린다.")
        return 4
    if errors:
        print("투자정책 계약 검증 실패:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 2
    missing = unset_fields(data)
    chosen = deliberate_gaps(data)
    if args.json:
        print(
            json.dumps(
                {
                    "policy": data,
                    "unset": missing,
                    "intentionally_unset": sorted(chosen),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not missing else 3
    print(f"policy_version {data['policy_version']} / 기준통화 {data['base_currency']}")
    # 세 상태를 절대 합치지 않는다. '정했다'와 '안 두기로 했다'와 '아직
    # 모른다'는 모델이 서로 다르게 행동해야 하는 상태다.
    decided = len(FIELDS) - len(missing) - len(chosen)
    print(
        f"정한 한도 {decided} / 일부러 두지 않음 {len(chosen)} / "
        f"아직 정하지 않음 {len(missing)}  (전체 {len(FIELDS)})"
    )
    if chosen:
        print("\n일부러 두지 않음 (결정이므로 UNDETERMINED 사유가 되지 않는다):")
        for path in sorted(chosen):
            print(f"  - {path}\n      {chosen[path]}")
    if missing:
        print("\n아직 정하지 않음 (모델은 이 공백에서 UNDETERMINED를 낼 수 있다):")
        for path in missing:
            print(f"  - {path}  [{FIELDS[path][1] or FIELDS[path][0]}]")
    return 0 if not missing else 3


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

    assumptions = sub.add_parser("assumptions", help="가정의 근거·대체 계산·수익률 경계값 저장과 조회")
    assumptions.add_argument("input", help="가정 spec JSON 파일 또는 저장된 ASM-... ID")
    assumptions.add_argument("--context", help="계산이 참조할 저장된 CTX-... ID")
    assumptions.add_argument("--asset", help="저장된 가정 분석을 조회할 종목")
    assumptions.add_argument("--json", action="store_true")
    assumptions.set_defaults(func=cmd_assumptions)

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
    register.add_argument("--ir-source", action="append", default=[],
                          help="미국 발행사의 공식 IR 일정/뉴스 HTTPS URL (반복 가능)")
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
    prepare.add_argument(
        "--amount",
        type=float,
        help=(
            "최대 투입 예산. NEW_BUY·ADD 에만 준다 — 매도 검토에 주면 "
            "묻지 않은 매수 계획이 packet 에 실린다"
        ),
    )
    prepare.add_argument("--amount-currency", choices=["KRW", "USD"])
    prepare.add_argument(
        "--intent",
        choices=sorted(INTENTS),
        help="무엇을 묻는가. 신규매수와 분할매도 검토는 같은 질문이 아니다",
    )
    prepare.add_argument(
        "--quantity",
        type=float,
        help=(
            "검토 중인 매도 수량. HOLD·TRIM·EXIT_REVIEW 에만 준다 "
            "(정하지 않았으면 생략한다 — PC 가 대신 정하지 않는다)"
        ),
    )
    prepare.add_argument(
        "--target-quantity",
        type=float,
        help="목표 잔여 수량. --quantity 대신 쓴다",
    )
    prepare.add_argument(
        "--held-quantity", type=float,
        help="이번 보유분 검토의 보유수량. 사용자 입력으로 보존하며 원장은 변경하지 않는다",
    )
    prepare.add_argument(
        "--average-cost", type=float,
        help="이번 질문의 평단. 통화 기본값은 자산 통화이며 --average-cost-currency로 지정한다",
    )
    prepare.add_argument(
        "--average-cost-currency", choices=["KRW", "USD"],
        help="평단의 실제 표시 통화. 원화 평단을 현재 환율로 달러 취득평단으로 바꾸지 않는다",
    )
    prepare.add_argument(
        "--verbatim",
        help="사용자가 실제로 한 말. 분석 전제가 아니라 검증 대상으로 실린다",
    )
    prepare.add_argument(
        "--claim",
        action="append",
        help=(
            "사용자가 사실이라고 말한 것 (반복 가능). 각 주장은 판단에서 "
            "확인·반박·확인불가 중 하나로 닫혀야 한다"
        ),
    )
    prepare.add_argument("--no-save", action="store_true")
    prepare.add_argument("--json", action="store_true")
    prepare.set_defaults(func=cmd_prepare)

    policy_parser = sub.add_parser(
        "policy", help="사용자 투자정책과 아직 정하지 않은 한도 보기"
    )
    policy_parser.add_argument("--json", action="store_true")
    policy_parser.set_defaults(func=cmd_policy)

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

    pipeline_parser = sub.add_parser(
        "pipeline", help="임상 파이프라인 기록 (이름 없이 부르면 조회)"
    )
    pipeline_parser.add_argument("asset_id")
    pipeline_parser.add_argument("--name", help="프로그램 이름")
    pipeline_parser.add_argument("--stage", default="PHASE1")
    pipeline_parser.add_argument("--indication", help="적응증")
    pipeline_parser.add_argument("--milestone", help="다음 이벤트")
    pipeline_parser.add_argument("--readout", help="예상 발표일 YYYY-MM-DD")
    pipeline_parser.add_argument("--source", default="", help="출처 (필수)")
    pipeline_parser.add_argument("--note", help="한 줄 메모")
    pipeline_parser.set_defaults(func=cmd_pipeline)

    retire_parser = sub.add_parser(
        "retire", help="판단을 폐기 보관소로 옮긴다 (사유 없이 부르면 상태만 표시)"
    )
    retire_parser.add_argument("asset_id")
    retire_parser.add_argument(
        "--reason", help="왜 못 믿는지 한 줄. 없으면 상태만 보여준다"
    )
    retire_parser.add_argument(
        "--all",
        action="store_true",
        help="계약을 통과하는 판단까지 전부 폐기",
    )
    retire_parser.add_argument(
        "--id",
        help=(
            "판단 하나만 지목해 폐기한다 (살아 있는 것 포함). 새 판단이 "
            "이미 저장된 뒤 옛 판단만 치울 때 쓴다"
        ),
    )
    retire_parser.add_argument(
        "--confirm", action="store_true", help="실제로 옮긴다 (기본은 조회만)"
    )
    retire_parser.add_argument("--json", action="store_true")
    retire_parser.set_defaults(func=cmd_retire)

    score = sub.add_parser("score", help="저장된 decision을 가격으로 사후평가")
    score.add_argument("asset_id")
    score.set_defaults(func=cmd_score)

    evals = sub.add_parser(
        "evals", help="저장된 채점 결과를 읽는다 (packet 에 없는 track record)"
    )
    evals.add_argument("asset_id")
    evals.add_argument(
        "--plan",
        action="store_true",
        help="그때의 진입·손절·분할매도 문턱까지 함께 본다",
    )
    evals.add_argument("--json", action="store_true")
    evals.set_defaults(func=cmd_evals)

    show = sub.add_parser("show", help="기록된 판단을 읽는다")
    show.add_argument("asset_id")
    show.add_argument(
        "--id",
        dest="assessment_id",
        default=None,
        help="특정 판단 (기본: 살아 있는 것 중 최신)",
    )
    show.set_defaults(func=cmd_show)
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
