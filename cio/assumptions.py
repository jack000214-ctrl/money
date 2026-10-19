"""Persist and bind the model's assumption experiment to a frozen context."""

import datetime as dt
import math
import re

from calculators.assumptions import AssumptionError, VERSION, analyze, calculate, keys, number, rows, text
from calculators.contracts import ContractError, canonical_payload_hash, content_hash, load_json, stable_hash, write_json
from cio.assets import ASSET_ID_RE
from cio.paths import KNOWLEDGE_DIR, ROOT

REPORT_SCHEMA = "cio-assumption-analysis/1.0.0"
REPORT_ID = re.compile(r"^ASM-[0-9a-f]{20}$")
SUPPORTED_FAMILIES = {"DCF", "PEER_MULTIPLE", "NORMALIZED_EARNINGS_MULTIPLE", "EARNINGS_MULTIPLE", "EQUITY_BRIDGE", "EVENT_REPRICING"}


def comparison(spec, context):
    selection = spec["comparison"]
    keys(selection, {"basis"}, {"total_return_pct", "rationale"}, "comparison")
    if selection["basis"] == "POLICY_TARGET":
        if set(selection) != {"basis"}:
            raise AssumptionError("POLICY_TARGET의 값은 packet에서만 읽는다")
        policy = (context.get("context") or {}).get("investor_policy") or {}
        annual = (policy.get("objective") or {}).get("target_annual_return_pct")
        annual = number(annual, "packet의 연 목표수익률", low=-99.999)
        months = spec["inputs"]["horizon_months"]
        months = number(months, "horizon_months", low=1, high=120)
        try:
            total = ((1 + annual / 100) ** (months / 12) - 1) * 100
        except OverflowError as exc:
            raise AssumptionError("목표수익률 환산 결과가 유한하지 않다") from exc
        result = {"basis": "POLICY_TARGET", "annual_return_pct": annual, "total_return_pct": total,
                  "horizon_months": months, "source": "context.investor_policy.objective.target_annual_return_pct"}
        # 목표는 정책 기준통화의 수익률이고 이 계산의 수익률은 자산 통화다. 다르면
        # 이 비교는 환율이 그대로라는 가정을 품는다. packet 의 다른 계산은 통화가
        # 다르면 값을 비우는데 이 자리만 아무 말 없이 견줬다.
        policy_currency = policy.get("base_currency")
        return_currency = (spec.get("inputs") or {}).get("currency")
        if policy_currency and return_currency and policy_currency != return_currency:
            result["currency_basis"] = {
                "policy_currency": policy_currency,
                "return_currency": return_currency,
                "matched": False,
                "_warning": (
                    f"목표수익률은 {policy_currency} 기준인데 이 계산의 수익률은 "
                    f"{return_currency} 기준이다. 이 비교는 환율이 변하지 않는다고 "
                    "가정한 것과 같다 — 환율 변동이 결론을 바꿀 수 있으면 그 가정을 "
                    "해석에 적고, 환율을 반영하려면 MODEL 기준선으로 직접 정한다"
                ),
            }
        return result
    if selection["basis"] != "MODEL":
        raise AssumptionError("comparison.basis: POLICY_TARGET 또는 MODEL")
    number(selection.get("total_return_pct"), "comparison.total_return_pct", low=-100)
    text(selection.get("rationale"), "comparison.rationale")
    return dict(selection)


def _source_path(locator):
    if not isinstance(locator, str) or not locator:
        raise AssumptionError("근거 파일 경로가 없다")
    path = (ROOT / locator).resolve()
    if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
        raise AssumptionError("근거 파일이 없거나 저장소 밖을 가리킨다")
    return path


def evidence_for(spec, context):
    """Fetch only referenced local documents; full bodies never enter the report."""
    refs = {ref for basis in spec["bases"] for ref in basis["evidence_refs"]}
    catalog = {row["source_id"]: row for row in context.get("evidence_index", [])}
    if refs - set(catalog):
        from cio.documents import documents_for
        for row in documents_for(context["request"]["asset_id"])["available"]:
            catalog.setdefault(row["doc_id"], {"source_id": row["doc_id"], "path": row["locator"], "kind": row["kind"]})
    evidence = []
    for ref in sorted(refs):
        if ref not in catalog:
            raise AssumptionError(f"보존된 근거가 없다: {ref}. snapshot 또는 공시 원문 수집이 필요하다")
        row = catalog[ref]
        path = _source_path(row.get("path"))
        digest = content_hash(path)
        if row.get("content_sha256") and row["content_sha256"] != digest:
            raise AssumptionError(f"근거 지문이 다르다: {ref}")
        # Use the existing content-addressed evidence store (same bytes, no cleanup).
        from cio.context import _snapshot_evidence
        target = _snapshot_evidence(path, digest)
        evidence.append({"source_id": ref, "path": str(target.relative_to(ROOT)),
                         "content_sha256": digest, "kind": row.get("kind")})
    return evidence


def _bound_outputs(spec, context):
    keys(spec, {"schema_version", "inputs", "comparison", "bases", "tests", "boundaries"}, {"earnings_basis"}, label="spec")
    calculate(spec["inputs"])
    request = context["request"]
    inputs = spec.get("inputs") or {}
    if inputs.get("price") != request.get("price") or inputs.get("currency") != request.get("price_currency"):
        raise AssumptionError("계산 가격·통화가 context와 다르다")
    validate_earnings_basis(spec, context)
    reference = comparison(spec, context)
    outputs = analyze(spec, reference["total_return_pct"])
    return reference, outputs


def validate_earnings_basis(spec, context):
    """The model supplies profit and the denominator; PC only checks the division."""
    basis = spec.get("earnings_basis")
    inputs = spec["inputs"]
    required = (context.get("model_contract") or {}).get("earnings_basis_required") and inputs["method"] == "EARNINGS_MULTIPLE"
    if basis is None:
        if required:
            raise AssumptionError("증자 공시가 있는 새 EPS 판단은 earnings_basis로 이익·주식수·가격 단위를 연결해야 한다")
        return
    if inputs["method"] in {"EQUITY_BRIDGE", "EVENT_PRICE"}:
        raise AssumptionError("이 방법은 시나리오별 bases를 사용하며 earnings_basis를 섞지 않는다")
    keys(basis, {"shares", "basis", "source_id", "net_income", "price_basis", "price_unit_multiplier", "rationale"}, label="earnings_basis")
    number(basis["shares"], "shares", positive=True)
    number(basis["price_unit_multiplier"], "price_unit_multiplier", positive=True)
    text(basis["rationale"], "earnings_basis.rationale")
    refs = {ref for item in spec["bases"] for ref in item["evidence_refs"]}
    if basis["source_id"] is not None and basis["source_id"] not in refs:
        raise AssumptionError("주식수 근거를 bases.evidence_refs에도 포함해 봉인해야 한다")
    capital = (context.get("context") or {}).get("capital_changes") or {}
    if basis["price_basis"] not in {"PRE_BONUS", "POST_BONUS", "UNCHANGED"}:
        raise AssumptionError("price_basis: PRE_BONUS|POST_BONUS|UNCHANGED")
    bonus = next((e for e in capital.get("events", []) if e.get("kind") == "BONUS"), None)
    if basis["basis"] != "MODEL_SCENARIO":
        observed = (capital.get("share_bases") or {}).get(basis["basis"])
        if basis["source_id"] != capital.get("source_id") or observed is None or not math.isclose(basis["shares"], observed, rel_tol=0, abs_tol=1e-6):
            raise AssumptionError("선택한 주식수 기준이 packet의 증자 공시 산술과 다르다")
        denominator_after_bonus = basis["basis"] == "POST_BONUS_ELIGIBLE"
        if bonus:
            if basis["price_basis"] == "UNCHANGED":
                raise AssumptionError("무상증자 공시가 있으면 가격이 무상 전/후 어느 단위인지 명시한다")
            expected_multiplier = (bonus["share_multiplier"] if denominator_after_bonus else 1) / (bonus["share_multiplier"] if basis["price_basis"] == "POST_BONUS" else 1)
            if not math.isclose(basis["price_unit_multiplier"], expected_multiplier, rel_tol=1e-8):
                raise AssumptionError("주식수 기준과 가격 단위의 무상 환산이 맞지 않는다")
    elif basis["source_id"] is not None and basis["source_id"] not in {r["source_id"] for r in context.get("evidence_index", [])}:
        raise AssumptionError("모델 주식수 시나리오의 근거 ID가 없다")
    if basis["price_unit_multiplier"] != 1:
        ratios = {r for e in capital.get("events", []) if e.get("kind") == "BONUS" and e.get("share_multiplier") for r in (e["share_multiplier"], 1 / e["share_multiplier"])}
        if basis["price_unit_multiplier"] not in ratios:
            raise AssumptionError("가격 단위 환산배수가 공시된 무상비율과 다르다")
    if inputs["method"] == "EARNINGS_MULTIPLE":
        keys(basis["net_income"], {"bear", "base", "bull"}, label="net_income")
        for name, income in basis["net_income"].items():
            number(income, "모델 순이익", positive=True)
            eps = income / basis["shares"] * basis["price_unit_multiplier"]
            if not math.isclose(eps, inputs["scenarios"][name]["eps"], rel_tol=1e-8, abs_tol=0.01):
                raise AssumptionError("EPS가 모델 순이익/선택 주식수·가격단위 계산과 다르다")
    elif basis["net_income"] is not None or basis["shares"] != inputs["dcf"]["shares"] or basis["price_unit_multiplier"] != 1:
        raise AssumptionError("DCF 분모 연결은 같은 주식 단위·null net_income이어야 한다")


def _check_extra_source(source, *, not_after):
    """packet 밖에서 더한 보존 원문이 자기 내용·ID·시각과 맞는지 본다.

    packet 의 근거는 context 무결성 검사가 기준일 순서를 본다. 이 경로는 지문과
    ID 만 봤고, 그래서 회수시각이 2030년인 원문을 2026년 판단의 근거로 묶어도
    통과했다. 분석을 만든 뒤에 받은 자료로 그 분석의 가정을 뒷받침할 수 없다.
    """
    document = load_json(_source_path(source["path"]))
    payload = document.get("payload")
    digest = canonical_payload_hash(payload)
    if digest != document.get("payload_sha256") or not isinstance(payload, dict):
        raise AssumptionError("보존 원문의 payload 지문이 다르다")
    identifier = source["source_id"]
    if identifier.startswith("DARTDOC:"):
        if identifier != f"DARTDOC:{payload.get('rcept_no')}":
            raise AssumptionError("공시 접수번호와 가정 근거 ID가 다르다")
    elif identifier != f"WEBSNAP:{digest[:12]}":
        raise AssumptionError("보존 웹 원문과 가정 근거 ID가 다르다")
    try:
        fetched = dt.datetime.fromisoformat(str(document.get("fetched_at")).replace("Z", "+00:00"))
        boundary = dt.datetime.fromisoformat(str(not_after).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssumptionError(f"보존 원문 또는 가정 분석의 시각을 읽을 수 없다: {identifier}") from exc
    if fetched.tzinfo is None or boundary.tzinfo is None:
        raise AssumptionError(f"보존 원문 또는 가정 분석의 시각에 시간대가 없다: {identifier}")
    if fetched > boundary:
        raise AssumptionError(
            f"{identifier}: 원문 회수시각 {document.get('fetched_at')} 이 가정 분석 시각 "
            f"{not_after} 보다 뒤다. 그때 없던 자료를 가정의 근거로 쓸 수 없다")


def _check_basis_evidence(spec, evidence):
    from cio.decision import _evidence_family
    families = {r["source_id"]: _evidence_family(r["source_id"], r.get("kind")) for r in evidence}
    for basis in spec["bases"]:
        financial = any(p.endswith((".eps", ".fcf", ".net_debt", ".shares", ".cash", ".debt", ".enterprise_value")) for p in basis["parameters"])
        if financial and basis["origin"] != "MODEL_CHOICE":
            if not any(families[ref] in {"FINANCIALS", "PRIMARY_DOCUMENT", "SECONDARY", "CALCULATION"} for ref in basis["evidence_refs"]):
                raise AssumptionError("재무 가정의 근거로 가격·공시 색인만 사용할 수 없다")


def analysis_id(report):
    return stable_hash({k: v for k, v in report.items() if k != "analysis_id"}, "ASM-")


def create_analysis(spec, context):
    from cio.decision import validate_context_integrity
    errors = validate_context_integrity(context)
    if errors:
        raise AssumptionError("; ".join(errors))
    reference, outputs = _bound_outputs(spec, context)
    report = {
        "schema_version": REPORT_SCHEMA, "engine_version": VERSION,
        "context_id": context["context_id"], "asset_id": context["request"]["asset_id"],
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "spec": spec, "comparison": reference, "outputs": outputs,
        "evidence_index": evidence_for(spec, context),
    }
    report["analysis_id"] = analysis_id(report)
    _check_basis_evidence(spec, report["evidence_index"])
    context_ids = {r["source_id"] for r in context.get("evidence_index") or []}
    for source in report["evidence_index"]:
        if source["source_id"] not in context_ids:
            _check_extra_source(source, not_after=report["created_at"])
    write_json(report_path(report["asset_id"], report["analysis_id"]), report)
    return report


def report_path(asset_id, identifier):
    if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id) or not isinstance(identifier, str) or not REPORT_ID.fullmatch(identifier):
        raise AssumptionError("잘못된 종목 또는 가정 분석 ID")
    path = (KNOWLEDGE_DIR / "assumptions" / asset_id / f"{identifier}.json").resolve()
    if not path.is_relative_to((KNOWLEDGE_DIR / "assumptions").resolve()):
        raise AssumptionError("가정 분석 경로가 허용 범위 밖이다")
    return path


def load_analysis(asset_id, identifier, context):
    report = load_json(report_path(asset_id, identifier))
    if report.get("analysis_id") != identifier or analysis_id(report) != identifier:
        raise AssumptionError("가정 분석 내용 지문이 다르다")
    if report.get("schema_version") != REPORT_SCHEMA or report.get("engine_version") not in {"1.0.0", "1.1.0", VERSION}:
        raise AssumptionError("지원하지 않는 가정 계산 계약")
    if report.get("asset_id") != asset_id or report.get("context_id") != context.get("context_id"):
        raise AssumptionError("다른 종목·context의 가정 분석을 인용했다")
    reference, outputs = _bound_outputs(report["spec"], context)
    # currency_basis 는 나중에 붙은 설명 필드다. 그 전에 봉인된 보고서에는 없고,
    # 그것만으로 이미 기록된 판단을 계약 미달로 만들지 않는다. 숫자는 그대로 대조한다.
    if "currency_basis" not in report["comparison"]:
        reference = {k: v for k, v in reference.items() if k != "currency_basis"}
    if reference != report["comparison"] or outputs != report["outputs"]:
        raise AssumptionError("가정 분석의 산출값이 재계산과 다르다")
    expected_refs = {ref for basis in report["spec"]["bases"] for ref in basis["evidence_refs"]}
    given_refs = [row["source_id"] for row in report["evidence_index"]]
    if set(given_refs) != expected_refs or len(given_refs) != len(expected_refs):
        raise AssumptionError("가정의 근거와 봉인된 파일 목록이 다르다")
    context_sources = {r["source_id"]: r for r in context.get("evidence_index") or []}
    for source in report["evidence_index"]:
        original = context_sources.get(source["source_id"])
        if original and original.get("content_sha256") != source.get("content_sha256"):
            raise AssumptionError("context의 출처 ID를 다른 내용으로 재정의했다")
        if content_hash(_source_path(source.get("path"))) != source.get("content_sha256"):
            raise AssumptionError("가정 근거의 파일 지문이 다르다")
        if not original:
            _check_extra_source(source, not_after=report["created_at"])
    _check_basis_evidence(report["spec"], report["evidence_index"])
    return report


def validate_review(decision, context):
    """Require the model to answer its experiments, never choose those answers."""
    review = decision.get("assumption_review")
    required = (context.get("model_contract") or {}).get("assumption_review_required") and decision.get("verdict") != "INSUFFICIENT"
    if review is None:
        return ["assumption_review: 가정 검토를 기록해야 한다"] if required else []
    try:
        keys(review, {"status", "analysis_id", "interpretation", "responses"}, label="assumption_review")
        text(review["interpretation"], "interpretation")
        if review["status"] == "UNSUPPORTED_METHOD":
            family = (decision.get("method") or {}).get("valuation_family", "").upper()
            if family in SUPPORTED_FAMILIES or review["analysis_id"] is not None or review["responses"] != []:
                raise AssumptionError("지원하는 방법을 미지원으로 우회하거나 계산 ID를 함께 쓸 수 없다")
            return []
        if review["status"] != "EVALUATED":
            raise AssumptionError("status: EVALUATED|UNSUPPORTED_METHOD")
        report = load_analysis(decision["asset_id"], review["analysis_id"], context)
        before = dt.datetime.fromisoformat(context["created_at"])
        at = dt.datetime.fromisoformat(report["created_at"])
        after = dt.datetime.fromisoformat(decision["created_at"])
        if any(value.tzinfo is None for value in (before, at, after)) or not before <= at <= after:
            raise AssumptionError("context <= 가정 분석 <= 판단 시각이어야 한다")
        inputs, outputs = report["spec"]["inputs"], report["outputs"]
        family = (decision.get("method") or {}).get("valuation_family", "").upper()
        expected_method = "EVENT_PRICE" if family == "EVENT_REPRICING" else family if family in {"DCF", "EQUITY_BRIDGE"} else "EARNINGS_MULTIPLE"
        if family in SUPPORTED_FAMILIES and inputs["method"] != expected_method:
            raise AssumptionError("판단의 가치평가 방법과 계산 방법이 다르다")
        if decision.get("horizon_months") != inputs["horizon_months"] or decision.get("win_return_threshold_pct") != inputs["win_return_threshold_pct"]:
            raise AssumptionError("계산과 판단의 기간·승리 문턱이 다르다")
        pairs = [(decision.get("win_probability_pct"), outputs["baseline"]["win_probability_pct"])]
        for name in ("bear", "base", "bull"):
            pairs += [((decision.get("expected_return_pct") or {}).get(name), outputs["baseline"]["scenarios"][name]["total_return_pct"]),
                      ((decision.get("scenario_probability_pct") or {}).get(name), inputs["scenarios"][name]["probability_pct"])]
        pairs.append(((decision.get("expected_return_pct") or {}).get("probability_weighted"), outputs["baseline"]["expected_return_pct"]))
        for written, computed in pairs:
            number(written, "판단의 확률·수익률")
            if not math.isclose(written, computed, abs_tol=0.02):
                raise AssumptionError("판단의 확률·수익률이 인용한 가정 계산과 다르다. 가정을 바꿨으면 다시 계산한다")
        expected = {row["id"] for row in [*outputs["tests"], *outputs["boundaries"]]}
        answered = set()
        from cio.decision import ACTION_DIRECTIONS, VERDICTS
        for response in rows(review["responses"], "responses", maximum=20):
            keys(response, {"id", "effect", "verdict", "action", "rationale"}, label="response")
            identifier = response["id"]
            if not isinstance(identifier, str) or identifier not in expected or identifier in answered:
                raise AssumptionError("없거나 중복된 가정 검사에 답했다")
            answered.add(identifier)
            text(response["rationale"], "response.rationale")
            effect = response["effect"]
            if effect not in {"KEEP", "CHANGE", "REASSESS"}:
                raise AssumptionError("response.effect: KEEP|CHANGE|REASSESS")
            if effect == "REASSESS":
                if response["verdict"] is not None or response["action"] is not None:
                    raise AssumptionError("재평가가 필요하면 행동을 확정하지 않는다")
            else:
                if response["verdict"] not in VERDICTS or response["action"] not in ACTION_DIRECTIONS:
                    raise AssumptionError("대체 가정에서의 verdict·action이 필요하다")
                same = response["verdict"] == decision.get("verdict") and response["action"] == (decision.get("action") or {}).get("direction")
                if same != (effect == "KEEP"):
                    raise AssumptionError("KEEP/CHANGE와 모델이 쓴 행동이 서로 다르다")
        if answered != expected:
            raise AssumptionError("답하지 않은 대체 가정·경계값: " + ", ".join(sorted(expected - answered)))
    except (AssumptionError, ContractError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return [f"assumption_review: {exc}"]
    return []


def format_analysis(report, *, include_bases=True):
    out = report["outputs"]
    comparison_basis = "사용자 정책의 기간 환산" if report["comparison"]["basis"] == "POLICY_TARGET" else "모델이 정한 누적 비교선"
    lines = [f"가정 분석 {report['analysis_id']} · {report['spec']['inputs']['method']}",
             f"기준 기대수익 {out['baseline']['expected_return_pct']:.2f}% · 비교선 {out['comparison_total_return_pct']:.2f}%",
             f"비교선 출처: {comparison_basis}",
             f"조건부 확률을 합한 승리확률 {out['baseline']['win_probability_pct']:.2f}% (모델 가정)"]
    if include_bases:
        from calculators.assumptions import parameter_values
        values = parameter_values(report["spec"]["inputs"])
        for basis in report["spec"]["bases"]:
            lines += [f"  가정 {basis['id']} ({basis['origin']})",
                      "    " + ", ".join(f"{p}={values[p]:g}" for p in basis["parameters"]),
                      f"    이유: {basis['rationale']}",
                      "    근거: " + (", ".join(basis["evidence_refs"]) or "직접 근거 없음 — 모델 선택"),
                      f"    재검토: {basis['revisit_when']}"]
    for row in out["tests"]:
        lines.append(f"  {row['id']}: 기대수익 {row['expected_return_pct']:.2f}% ({row['change_pp']:+.2f}%p), 비교선 {row['comparison_relation']}")
    for row in out["boundaries"]:
        value = row["required_value"]
        lines.append(f"  {row['id']}: {row['parameter']} = {value:.6g}" if value is not None else f"  {row['id']}: {row['status']}")
    lines.append("※ 비교선 통과 여부는 산술이다. 가정 선택과 행동 판단은 모델이 한다.")
    return "\n".join(lines)


def carried_review(decision):
    """A small pointer and boundary summary; full assumptions stay on disk."""
    review = decision.get("assumption_review")
    if not review:
        return None
    summary = {"status": review["status"], "analysis_id": review["analysis_id"],
               "interpretation": review["interpretation"][:240]}
    if review["status"] == "EVALUATED":
        report = load_json(report_path(decision["asset_id"], review["analysis_id"]))
        summary["comparison_total_return_pct"] = report["outputs"]["comparison_total_return_pct"]
        summary["boundaries"] = [{"id": r["id"], "parameter": r["parameter"], "required_value": r["required_value"], "status": r["status"]} for r in report["outputs"]["boundaries"][:4]]
        summary["responses"] = [{"id": r["id"], "effect": r["effect"], "action": r["action"]} for r in review["responses"][:4]]
    summary["_role"] = "직전 모델의 가정과 계산이다. 현재 사실이 아니다. 전체 근거·대체 가정·응답은 cio assumptions ASM-... --asset ASSET로 읽고 바뀐 가정과 이유를 이번 해석에 남긴다"
    return summary
