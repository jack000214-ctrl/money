"""시장 체제 판정 회귀 테스트.

발생한 버그: 낙폭 임계의 조건 순서가 뒤집혀 있었다.
    flag = "조정" if dd <= -10 else ("약세장" if dd <= -20 else "")
-39% 는 첫 조건에서 True 가 되어 '조정'으로 찍혔다. 약세장 분기는 도달 불가였다.
같은 임계를 쓰는 유일한 경로가 이제 `classify` 뿐이므로 그 회귀는
`test_large_drawdown_is_stress` 가 지킨다 — -39% 는 '약세장'이어야 한다.
"""

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT  # noqa: F401
from calculators import market_regime
from calculators.market_regime import classify, percentile
from cio import refresh
from cio.refresh import RefreshTask, inspect_task


class TestClassify(unittest.TestCase):
    def test_calm_market_is_normal(self):
        regime, _, score, _ = classify(vix=14.0, worst_drawdown_pct=-2.0)
        self.assertEqual(regime, "NORMAL")
        self.assertEqual(score, 0)

    def test_large_drawdown_is_stress(self):
        regime, _, score, notes = classify(vix=18.58, worst_drawdown_pct=-39.0)
        self.assertEqual(regime, "STRESS")
        self.assertEqual(score, 3)
        self.assertTrue(any("약세장" in n for n in notes))

    def test_high_vix_plus_bear_is_crisis(self):
        regime, _, score, _ = classify(vix=45.0, worst_drawdown_pct=-35.0)
        self.assertEqual(regime, "CRISIS")
        self.assertGreaterEqual(score, 5)

    def test_missing_vix_still_classifies(self):
        """VIX 수집이 실패해도 낙폭만으로 판정할 수 있어야 한다."""
        regime, _, score, _ = classify(vix=None, worst_drawdown_pct=-25.0)
        self.assertEqual(regime, "STRESS")
        self.assertEqual(score, 3)

    def test_monotonic_in_severity(self):
        """상황이 나빠지면 점수가 줄어들면 안 된다."""
        scores = [classify(20.0, dd)[2] for dd in (0.0, -6.0, -12.0, -25.0)]
        self.assertEqual(scores, sorted(scores))


class TestAChangedInputMakesTheStoredRegimeStale(unittest.TestCase):
    """입력이 바뀌었는지는 수집시각이 아니라 내용으로 본다.

    거래소 백필은 가격 파일에 빠진 봉을 채우면서 원래 `retrieved_at` 을 보존한다.
    시각만 비교하던 의존성 검사는 그 변화를 못 봤다 — 채워진 지수 봉 하나로
    재계산은 NORMAL·0 에서 STRESS·3 이 되는데 저장된 시장 상태는 그대로였고
    refresh 는 SKIPPED_FRESH 였다.
    """

    HEADER = ("# schema_version price-series/2.1.0\n# symbol {symbol} / currency KRW\n"
              "# retrieved_at 2026-10-13T00:00:00+00:00 / source Yahoo Finance chart API\n"
              "# last_bar 2026-10-12 / session SETTLED\n"
              "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n")
    NOW = dt.datetime(2026, 10, 13, 1, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "knowledge" / "prices").mkdir(parents=True)
        (self.root / "knowledge" / "market").mkdir(parents=True)
        for target in (
            patch.object(market_regime, "ROOT", self.root),
            patch.object(market_regime, "PRICE_DIR", self.root / "knowledge" / "prices"),
            patch.object(refresh, "ROOT", self.root),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.write("^KS11", {"2026-10-07": 100, "2026-10-08": 100, "2026-10-12": 100})
        self.write("^KQ11", {"2026-10-07": 100, "2026-10-08": 100, "2026-10-12": 100})

    def write(self, symbol, closes):
        name = market_regime.safe_name(symbol)
        (self.root / "knowledge" / "prices" / f"{name}.csv").write_text(
            self.HEADER.format(symbol=symbol)
            + "".join(f"{day},{close},{close},1,{close},{close},1\n"
                      for day, close in sorted(closes.items())),
            encoding="utf-8")

    def compute(self):
        with patch("sys.argv", ["market_regime", "--market", "KR", "--out",
                                "knowledge/market/regime-KR.json", "--local-only"]), \
                contextlib.redirect_stdout(io.StringIO()):
            market_regime.main()
        return json.loads(
            (self.root / "knowledge/market/regime-KR.json").read_text("utf-8"))

    def inspect(self):
        return inspect_task(RefreshTask(
            name="market:KR", capability="MARKET_STATE",
            artifact="knowledge/market/regime-KR.json", max_age_hours=24,
            command=None,
            dependencies=["knowledge/prices/KS11.csv", "knowledge/prices/KQ11.csv"],
        ), now=dt.datetime.now(dt.timezone.utc))

    def test_the_artifact_records_what_it_read(self):
        snapshot = self.compute()
        self.assertEqual(set(snapshot["input_fingerprints"]),
                         {"knowledge/prices/KS11.csv", "knowledge/prices/KQ11.csv"})
        self.assertEqual(self.inspect().state, "FRESH")

    def test_a_backfilled_bar_that_keeps_the_retrieval_time_is_seen(self):
        before = self.compute()["by_market"]["KR"]
        self.assertEqual((before["regime"], before["score"]), ("NORMAL", 0))
        # 백필: 빠져 있던 10-09 의 봉(고점)이 채워진다. 머리말의 회수시각은 그대로다.
        self.write("^KS11", {"2026-10-07": 100, "2026-10-08": 100,
                             "2026-10-09": 130, "2026-10-12": 100})
        task = self.inspect()
        self.assertEqual(task.state, "STALE")
        assert task.reason is not None
        self.assertIn("knowledge/prices/KS11.csv", task.reason)
        self.assertIn("내용", task.reason)
        after = self.compute()["by_market"]["KR"]
        self.assertEqual((after["regime"], after["score"]), ("STRESS", 3))
        self.assertEqual(self.inspect().state, "FRESH")

    def test_an_unchanged_input_stays_fresh_however_often_it_is_checked(self):
        self.compute()
        self.assertEqual(self.inspect().state, "FRESH")
        self.assertEqual(self.inspect().state, "FRESH")

    def test_an_input_that_was_missing_at_compute_time_and_arrives_later_is_seen(self):
        (self.root / "knowledge/prices/KQ11.csv").unlink()
        self.compute()
        self.write("^KQ11", {"2026-10-07": 100, "2026-10-12": 70})
        task = self.inspect()
        self.assertEqual(task.state, "STALE")
        assert task.reason is not None
        self.assertIn("knowledge/prices/KQ11.csv", task.reason)

    def test_an_artifact_with_no_fingerprints_cannot_be_called_fresh(self):
        """지문 이전에 만든 산출물은 입력이 바뀌었는지 대조할 수 없다."""
        snapshot = self.compute()
        del snapshot["input_fingerprints"]
        (self.root / "knowledge/market/regime-KR.json").write_text(
            json.dumps(snapshot), encoding="utf-8")
        task = self.inspect()
        self.assertEqual(task.state, "STALE")
        assert task.reason is not None
        self.assertIn("입력 지문", task.reason)


class TestPercentile(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(percentile([1, 2, 3, 4], 3), 50.0)

    def test_empty_returns_none(self):
        self.assertIsNone(percentile([], 3))


if __name__ == "__main__":
    unittest.main()
