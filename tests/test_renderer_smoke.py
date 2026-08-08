"""모든 렌더러가 실제로 **끝까지 실행되는가**.

이 파일이 생긴 이유가 있다. `kr_lending.cmd_show` 에 `change_pct` 를 쓰도록
고치면서 import 를 빠뜨렸는데, 모듈 import 는 통과했고 표도 정상으로 찍혔다.
마지막 요약 줄에서 `NameError` 가 나면서 그 줄만 조용히 사라졌고 종료코드도
0 이었다 — 파이프라인 뒤쪽에서 보면 아무 일도 없었던 것처럼 보인다.

렌더러의 실패는 이렇게 **부분적**이다. 앞부분이 이미 출력된 뒤에 죽으므로
"표가 나왔으니 됐다"로 읽힌다. 그러니 검증할 것은 출력 내용이 아니라
**끝까지 갔는가**이고, 그것만으로도 이 종류의 사고는 다 걸린다.

내용을 assert 하지 않는 이유는 유지비다. 숫자 하나 바뀔 때마다 깨지는 테스트는
값보다 비용이 크다. 여기서는 **결측이 섞여도 끝까지 간다**만 본다.
"""

import io
import unittest
from contextlib import redirect_stdout

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from collectors import kr_inventory, us_calendar
from calculators import normalized_earnings


def rendered(fn, *args, **kwargs):
    """렌더러를 끝까지 돌리고 출력을 돌려준다. 도중에 죽으면 예외가 그대로 오른다."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        fn(*args, **kwargs)
    return buffer.getvalue()


class TestRenderersFinish(unittest.TestCase):
    """결측이 섞인 최소 입력으로 각 렌더러를 끝까지 돌린다."""

    def test_normalized_earnings_render_with_no_revenue(self):
        rows = [{"period_end": "2025-12-31", "ocf_ttm": 1e12, "capex_ttm": 5e11,
                 "fcf_ttm": 5e11, "revenue_ttm": None, "fcf_margin_pct": None}]
        text = rendered(normalized_earnings.render, "테스트", "000660", rows,
                        normalized_earnings.normalize(rows))
        self.assertIn("정상화", text)

    def test_normalized_earnings_render_with_a_negative_trough(self):
        """저점이 음수면 진폭을 못 낸다 — 그 분기를 화면이 통과해야 한다."""
        rows = [
            {"period_end": "2023-06-30", "ocf_ttm": 1e12, "capex_ttm": 2e12,
             "fcf_ttm": -1e12, "revenue_ttm": 1e13, "fcf_margin_pct": -10.0},
            {"period_end": "2025-12-31", "ocf_ttm": 5e12, "capex_ttm": 1e12,
             "fcf_ttm": 4e12, "revenue_ttm": 2e13, "fcf_margin_pct": 20.0},
        ]
        text = rendered(normalized_earnings.render, "테스트", "000660", rows,
                        normalized_earnings.normalize(rows))
        self.assertIn("음수", text)

    def test_kr_inventory_render_without_cost_of_sales(self):
        rows = kr_inventory.derive_rows(
            {(2026, 1): (1.5e13, "report")}, {}, [2026]
        )
        text = rendered(kr_inventory.render, "000660", rows)
        self.assertIn("재고자산", text)

    def test_kr_inventory_render_with_a_comparative_source(self):
        rows = kr_inventory.derive_rows(
            {(2025, 4): (1.4e13, "comparative"), (2026, 1): (1.5e13, "report")},
            {(2026, 1): {"val": 1e13, "derived": True}},
            [2025, 2026],
        )
        text = rendered(kr_inventory.render, "000660", rows)
        self.assertIn("2026-03-31", text)

    def test_us_calendar_render_when_nothing_can_be_estimated(self):
        packet = {
            "asset_id": "TSLA", "as_of_date": "2026-08-08",
            "next_provisional_result": None,
            "next_provisional_result_missing_reason": "제도가 없다",
            "expected_periodic_report": None,
            "expected_periodic_report_missing_reason": "관측 2건 미만",
            "periodic_report_lag": {
                "observation_count": 0, "observations": [],
                "median_lag_days_by_class": {}, "sample_size_by_class": {},
                "_note": "추정이다",
            },
            "business_day_basis": "주말만 제외한 근사다",
            "_note": "추정일이다",
            "_not_a_judgment": "측정치다",
        }
        text = rendered(us_calendar.render, packet)
        self.assertIn("TSLA", text)


class TestEveryRendererIsReachable(unittest.TestCase):
    """렌더러가 참조하는 이름이 모듈에 실제로 있는가.

    `cmd_show` 안에서만 쓰는 헬퍼를 import 하지 않으면 모듈 import 는 통과하고
    실행 중에만 `NameError` 가 난다. 실제로 그랬다. 여기서는 렌더링 함수들이
    참조하는 전역 이름이 모듈에 존재하는지를 기계적으로 센다.
    """

    def missing_globals(self, module):
        """렌더링 함수가 전역으로 찾는 이름 중 모듈에 없는 것.

        스코프 판정은 손으로 하지 않는다 — keyword-only 인자, 중첩 함수,
        람다 인자, 컴프리헨션 변수까지 직접 세면 반드시 틀린다. 파이썬 자신의
        스코프 분석기(`symtable`)가 이미 정확히 아는 것을 다시 구현할 이유가
        없다.
        """
        import builtins
        import inspect
        import symtable

        source = inspect.getsource(module)
        table = symtable.symtable(source, module.__file__, "exec")
        known = set(dir(module)) | set(dir(builtins))
        missing = []

        def collect(scope, label):
            for symbol in scope.get_symbols():
                if symbol.is_global() and symbol.get_name() not in known:
                    missing.append(f"{module.__name__}.{label}: {symbol.get_name()}")
            for child in scope.get_children():
                collect(child, label)

        for child in table.get_children():
            if child.get_type() != "function":
                continue
            if child.get_name().startswith(("cmd_", "render", "show", "format_")):
                collect(child, child.get_name())
        return missing

    def test_rendering_functions_reference_only_defined_names(self):
        from calculators import (
            normalized_earnings, peer_kr, peer_us, portfolio, position,
            reverse_dcf, sector_strength, valuation_band,
        )
        from cio import documents, pipeline, report, requests, retire
        from collectors import (
            consensus, dart, edgar, kr_calendar, kr_events, kr_flow,
            kr_inventory, kr_lending, krx, price, us_calendar,
        )

        modules = [
            normalized_earnings, peer_kr, peer_us, portfolio, position,
            reverse_dcf, sector_strength, valuation_band,
            documents, pipeline, report, requests, retire,
            consensus, dart, edgar, kr_calendar, kr_events, kr_flow,
            kr_inventory, kr_lending, krx, price, us_calendar,
        ]
        found = []
        for module in modules:
            found.extend(self.missing_globals(module))
        self.assertEqual(found, [], "렌더러가 없는 이름을 참조한다")


if __name__ == "__main__":
    unittest.main()
