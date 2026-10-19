"""Model-selected assumptions, alternatives and return thresholds; never a verdict."""

from copy import deepcopy
import math
import re

from calculators.reverse_dcf import equity_value_per_share

VERSION = "1.2.0"
SCHEMA = "cio-assumption-spec/1.0.0"
NAMES = ("bear", "base", "bull")
METHODS = {"EARNINGS_MULTIPLE", "DCF", "EQUITY_BRIDGE", "EVENT_PRICE"}
ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


class AssumptionError(ValueError):
    pass


def keys(value, required, optional=(), label="object"):
    if not isinstance(value, dict):
        raise AssumptionError(f"{label}: 객체가 필요하다")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise AssumptionError(f"{label}: 필드가 다르다 (필수 {', '.join(sorted(required))})")


def number(value, label, *, low=None, high=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AssumptionError(f"{label}: 유한한 숫자가 필요하다")
    if (low is not None and value < low) or (high is not None and value > high) or (positive and value <= 0):
        raise AssumptionError(f"{label}: 허용 범위 밖이다")
    return value


def text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise AssumptionError(f"{label}: 1~2000자의 설명이 필요하다")


def rows(value, label, *, minimum=1, maximum=24):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise AssumptionError(f"{label}: {minimum}~{maximum}개의 배열이 필요하다")
    return value


def parameter_values(inputs):
    """Only numeric, named model inputs are addressable; no expression evaluation."""
    result = {
        f"scenarios.{name}.{field}": value
        for name, row in inputs["scenarios"].items() for field, value in row.items()
    }
    result.update({f"dcf.{field}": value for field, value in inputs.get("dcf", {}).items()})
    return result


def calculate(inputs):
    keys(inputs, {"method", "price", "currency", "horizon_months", "win_return_threshold_pct", "scenarios"}, {"dcf"}, "inputs")
    if not isinstance(inputs["method"], str) or inputs["method"] not in METHODS or not isinstance(inputs["currency"], str) or inputs["currency"] not in {"KRW", "USD"}:
        raise AssumptionError("inputs: 지원하지 않는 방법 또는 통화")
    price = number(inputs["price"], "price", positive=True)
    months = number(inputs["horizon_months"], "horizon_months", low=1, high=120)
    if not isinstance(months, int):
        raise AssumptionError("horizon_months: 정수여야 한다")
    number(inputs["win_return_threshold_pct"], "win_return_threshold_pct", low=-100)
    keys(inputs["scenarios"], NAMES, label="scenarios")
    dcf = inputs.get("dcf")
    if inputs["method"] == "DCF":
        keys(dcf, {"fcf", "shares", "net_debt", "discount_rate_pct", "terminal_growth_pct", "years"}, label="dcf")
        for key in ("fcf", "shares"):
            number(dcf[key], f"dcf.{key}", positive=True)
        number(dcf["net_debt"], "net_debt")
        number(dcf["discount_rate_pct"], "discount_rate_pct", positive=True)
        number(dcf["terminal_growth_pct"], "terminal_growth_pct", low=-99.999)
        if dcf["discount_rate_pct"] <= dcf["terminal_growth_pct"]:
            raise AssumptionError("DCF: 할인율이 영구성장률보다 커야 한다")
        number(dcf["years"], "years", low=1, high=100)
        if not isinstance(dcf["years"], int):
            raise AssumptionError("DCF years: 정수여야 한다")
    elif "dcf" in inputs:
        raise AssumptionError("DCF 이외의 방법에 dcf 입력을 섞을 수 없다")
    output = {}
    for name in NAMES:
        row = inputs["scenarios"][name]
        fields = {"probability_pct", "conditional_win_probability_pct"}
        if inputs["method"] == "EQUITY_BRIDGE":
            fields |= {"enterprise_value", "cash", "debt", "shares"}
        elif inputs["method"] == "EVENT_PRICE":
            fields |= {"terminal_price", "cash_distribution"}
        else:
            fields |= {"eps", "multiple", "cash_distribution"} if dcf is None else {"growth_pct"}
        keys(row, fields, label=f"scenarios.{name}")
        number(row["probability_pct"], "probability_pct", positive=True, high=100)
        number(row["conditional_win_probability_pct"], "conditional_win_probability_pct", low=0, high=100)
        raw_value = None
        try:
            if inputs["method"] == "EVENT_PRICE":
                value = number(row["terminal_price"], "terminal_price", low=0)
                terminal_wealth = value + number(row["cash_distribution"], "cash_distribution", low=0)
            elif inputs["method"] == "EQUITY_BRIDGE":
                # Model-selected horizon values, in one currency and share unit.
                # Cash is AFTER burn/funding; financing must also change shares.
                for field in ("enterprise_value", "cash", "debt"):
                    number(row[field], field, low=0)
                number(row["shares"], "shares", positive=True)
                raw_value = (row["enterprise_value"] + row["cash"] - row["debt"]) / row["shares"]
                number(raw_value, "raw_equity_value_per_share")
                value = max(0, raw_value)
                terminal_wealth = value
            elif dcf is None:
                number(row["eps"], "eps", positive=True)
                number(row["multiple"], "multiple", positive=True)
                number(row["cash_distribution"], "cash_distribution", low=0)
                value = row["eps"] * row["multiple"]
                terminal_wealth = value + row["cash_distribution"]
            else:
                number(row["growth_pct"], "growth_pct", low=-99.999)
                raw_value = equity_value_per_share(
                    dcf["fcf"], row["growth_pct"] / 100,
                    discount_rate=dcf["discount_rate_pct"] / 100,
                    terminal_growth=dcf["terminal_growth_pct"] / 100,
                    years=dcf["years"], net_debt=dcf["net_debt"], shares=dcf["shares"],
                )
                # Common equity has a zero floor; expose the raw residual too.
                value = max(0, raw_value)
                terminal_wealth = value
            total = (terminal_wealth / price - 1) * 100
            annual = ((terminal_wealth / price) ** (12 / months) - 1) * 100
            for metric in (value, total, annual):
                number(metric, "calculated output")
        except (OverflowError, ZeroDivisionError) as exc:
            raise AssumptionError("가정에서 유한한 계산 결과가 나오지 않는다") from exc
        output[name] = {"value_per_share": value, "total_return_pct": total, "annualized_return_pct": annual}
        if raw_value is not None:
            output[name]["raw_equity_value_per_share"] = raw_value
    if not math.isclose(sum(row["probability_pct"] for row in inputs["scenarios"].values()), 100, abs_tol=1e-8):
        raise AssumptionError("시나리오 확률의 합은 100이어야 한다")
    return {
        "scenarios": output,
        "expected_return_pct": sum(output[n]["total_return_pct"] * inputs["scenarios"][n]["probability_pct"] / 100 for n in NAMES),
        "win_probability_pct": sum(inputs["scenarios"][n]["conditional_win_probability_pct"] * inputs["scenarios"][n]["probability_pct"] / 100 for n in NAMES),
    }


def changed(inputs, changes):
    if not isinstance(changes, dict) or not changes:
        raise AssumptionError("changes: 바꿀 가정이 필요하다")
    allowed = parameter_values(inputs)
    result = deepcopy(inputs)
    for path, value in changes.items():
        if path not in allowed:
            raise AssumptionError(f"changes: 계산 변수로 허용되지 않는 경로 {path}")
        number(value, path)
        target = result
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return result


def relation(value, reference):
    if math.isclose(value, reference, abs_tol=1e-7):
        return "EQUAL"
    return "ABOVE" if value > reference else "BELOW"


def analyze(spec, reference):
    keys(spec, {"schema_version", "inputs", "comparison", "bases", "tests", "boundaries"}, {"earnings_basis"}, label="spec")
    if spec["schema_version"] != SCHEMA:
        raise AssumptionError("지원하지 않는 가정 spec 버전")
    number(reference, "comparison total_return_pct", low=-100)
    baseline = calculate(spec["inputs"])
    returns = [baseline["scenarios"][n]["total_return_pct"] for n in NAMES]
    if returns != sorted(returns):
        raise AssumptionError("기준 시나리오는 bear <= base <= bull 순서여야 한다")
    baseline["comparison_relation"] = relation(baseline["expected_return_pct"], reference)
    parameters = parameter_values(spec["inputs"])
    covered = {}
    basis_ids = set()
    for basis in rows(spec["bases"], "bases"):
        keys(basis, {"id", "parameters", "origin", "rationale", "evidence_refs", "revisit_when"}, label="basis")
        identifier = basis["id"]
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in basis_ids:
            raise AssumptionError("basis.id: 중복되거나 잘못된 ID")
        basis_ids.add(identifier)
        if not isinstance(basis["origin"], str) or basis["origin"] not in {"OBSERVED", "MODEL_ESTIMATE", "MODEL_CHOICE"}:
            raise AssumptionError("basis.origin: OBSERVED|MODEL_ESTIMATE|MODEL_CHOICE")
        for key in ("rationale", "revisit_when"):
            text(basis[key], f"basis.{key}")
        refs = rows(basis["evidence_refs"], "evidence_refs", minimum=0)
        for ref in refs:
            text(ref, "evidence_ref")
        if not refs and basis["origin"] != "MODEL_CHOICE":
            raise AssumptionError("관측값·추정값의 근거 파일 참조가 필요하다")
        for path in rows(basis["parameters"], "parameters", maximum=32):
            if not isinstance(path, str) or path not in parameters or path in covered:
                raise AssumptionError(f"basis.parameters: 없거나 중복된 계산 변수 {path}")
            covered[path] = identifier
    if set(covered) != set(parameters):
        raise AssumptionError("근거를 설명하지 않은 계산 변수: " + ", ".join(sorted(set(parameters) - set(covered))))
    tests = []
    identifiers = set()
    for test in rows(spec["tests"], "tests", maximum=12):
        keys(test, {"id", "changes", "rationale"}, label="test")
        identifier = test["id"]
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in identifiers:
            raise AssumptionError("test.id: 중복되거나 잘못된 ID")
        identifiers.add(identifier)
        text(test["rationale"], "test.rationale")
        alternative = changed(spec["inputs"], test["changes"])
        if alternative == spec["inputs"]:
            raise AssumptionError("test: 기준과 같은 가정은 대체 가정 검사가 아니다")
        result = calculate(alternative)
        tests.append({
            "id": identifier, "changes": test["changes"], "rationale": test["rationale"],
            "basis_ids": sorted({covered[path] for path in test["changes"]}),
            "expected_return_pct": result["expected_return_pct"],
            "change_pp": result["expected_return_pct"] - baseline["expected_return_pct"],
            "win_probability_pct": result["win_probability_pct"],
            "comparison_relation": relation(result["expected_return_pct"], reference),
        })
    boundaries = []
    for request in rows(spec["boundaries"], "boundaries", minimum=0, maximum=8):
        keys(request, {"id", "parameter", "low", "high"}, label="boundary")
        identifier, parameter = request["id"], request["parameter"]
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in identifiers:
            raise AssumptionError("boundary.id: 중복되거나 잘못된 ID")
        identifiers.add(identifier)
        if not isinstance(parameter, str) or parameter not in parameters or parameter.endswith(("probability_pct", ".years")):
            raise AssumptionError("boundary.parameter: 연속 가치평가 변수만 단독 역산한다")
        low = number(request["low"], "boundary.low")
        high = number(request["high"], "boundary.high")
        if low >= high:
            raise AssumptionError("boundary: low < high여야 한다")
        def value_at(value):
            return calculate(changed(spec["inputs"], {parameter: value}))["expected_return_pct"]
        lower_return, upper_return = value_at(low), value_at(high)
        row = {**request, "low_return_pct": lower_return, "high_return_pct": upper_return,
               "required_value": None, "status": "NOT_BRACKETED"}
        if math.isclose(lower_return, upper_return, abs_tol=1e-9):
            row["status"] = "NO_UNIQUE_BOUNDARY"
        elif min(lower_return, upper_return) <= reference <= max(lower_return, upper_return):
            increasing = upper_return > lower_return
            left, right = low, high
            for _ in range(100):
                mid = (left + right) / 2
                if (value_at(mid) < reference) == increasing:
                    left = mid
                else:
                    right = mid
            root = (left + right) / 2
            row.update(status="SOLVED", required_value=root, return_at_boundary_pct=value_at(root))
        boundaries.append(row)
    return {
        "baseline": baseline, "comparison_total_return_pct": reference,
        "tests": tests, "boundaries": boundaries,
        "_role": "가정·검사 범위·조건부 승리확률은 모델이 정했다. PC는 수익률과 비교선의 관계만 계산하며 행동을 정하지 않는다. 경계값은 나머지 가정을 고정한 역산값이다.",
    }
