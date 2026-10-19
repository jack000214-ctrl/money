"""packet 이 화면만큼 알려주는가.

이 저장소에는 계산 주체가 셋 있고 경계가 있다 — 사람은 건수를 주고, PC 는
검사·계산을 하고, 모델은 판단만 한다. 그 경계가 새는 자리가 있었다.

`kr_lending` 은 packet 에 잔고와 변화 **주식수**만 실었다. 판단이 실제로 쓴
"-8.1%" 는 화면에만 있었으므로, 모델은 시작잔고를 역산해서 직접 나눠야 했다.
**모델이 하는 산술은 검증되지 않은 산술이다** — 이 저장소가 `change_pct` 를
따로 두고 테스트하는 이유가 그것인데, packet 에 안 실으면 그 함수를 우회한다.

`peer_multiples` 도 같았다. 자기 배수와 동종 중앙값을 둘 다 실으면서 괴리는
빼놓았다. 화면(`peer_kr`·`peer_us`)은 이미 그 값과 "중앙값보다 낮음/높음/근접"
라벨까지 내고 있었다.

여기서 고정하는 성질은 하나다. **화면이 내는 파생값은 packet 에도 있다.**
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators.contracts import change_pct, ratio_pct
from cio.context import (
    _forward_multiple_row,
    _margin_pct,
    _pct,
    _pct_change,
    _peer_multiples,
)


class TestPacketPercentPrecision(unittest.TestCase):
    """비율은 자릿수를 정해서 싣는다."""

    def test_a_ratio_is_rounded_to_two_places(self):
        """-8.132067439241252 는 없는 정밀도를 주장하면서 자리만 먹는다."""
        self.assertEqual(_pct(-8.132067439241252), -8.13)

    def test_absent_stays_absent_rather_than_becoming_zero(self):
        self.assertIsNone(_pct(None))

    def test_zero_is_a_value_not_an_absence(self):
        self.assertEqual(_pct(0.0), 0.0)


class TestLendingChangeReachesThePacket(unittest.TestCase):
    """대차잔고 변화율이 packet 에 실리는가."""

    def summarize(self, rows):
        from cio import context

        with patch.object(context, "_csv_rows", return_value=rows), \
                patch("pathlib.Path.exists", return_value=True):
            summaries, _ = context._optional_collection_summaries({
                "asset_id": "000660",
                "market": "KR",
                "optional_collectors": ["KR_LENDING"],
            })
        block = summaries.get("kr_lending")
        assert block is not None
        return block

    def rows(self, *balances):
        return [{"date": f"2026-08-{i + 1:02d}", "balance_shares": str(b)}
                for i, b in enumerate(balances)]

    def test_the_percentage_the_screen_shows_is_in_the_packet(self):
        block = self.summarize(self.rows(14_420_269, 13_247_603))
        self.assertEqual(block["balance_change_shares"], -1_172_666)
        self.assertEqual(block["balance_change_pct"], -8.13)

    def test_raw_shares_alone_are_not_enough(self):
        """주식수만 있으면 모델이 시작잔고를 역산해서 나눠야 한다."""
        block = self.summarize(self.rows(14_420_269, 13_247_603))
        self.assertIn("balance_change_pct", block)

    def test_a_zero_starting_balance_yields_no_rate(self):
        """0 에서 늘어난 것을 '0% 변동 없음'으로 만들면 안 된다."""
        block = self.summarize(self.rows(0, 500_000))
        self.assertEqual(block["balance_change_shares"], 500_000)
        self.assertIsNone(block["balance_change_pct"])

    def test_an_unchanged_balance_is_zero_percent_not_absent(self):
        block = self.summarize(self.rows(1_000_000, 1_000_000))
        self.assertEqual(block["balance_change_pct"], 0.0)


class TestPeerGapReachesThePacket(unittest.TestCase):
    """자기 배수와 동종 중앙값의 괴리가 packet 에 실리는가."""

    def multiples(self, subject_per, *peer_pers):
        table = {"000660": subject_per}
        peers = []
        for index, per in enumerate(peer_pers):
            asset_id = f"PEER{index}"
            table[asset_id] = per
            peers.append({"asset_id": asset_id, "name": asset_id})

        def fake_row(asset_id, name):
            per = table.get(asset_id)
            return {"asset_id": asset_id, "name": name,
                    "available": per is not None, "forward_per": per,
                    "forward_period": "2026-12"}

        with patch("cio.context._forward_multiple_row", side_effect=fake_row):
            block = _peer_multiples({"asset_id": "000660", "name": "SK하이닉스",
                                     "peers": peers})
        assert block is not None
        return block

    def test_the_gap_the_screen_shows_is_in_the_packet(self):
        block = self.multiples(4.11, 5.13)
        self.assertEqual(block["forward_per_median"], 5.13)
        self.assertEqual(block["forward_per_vs_median_pct"], -19.88)

    def test_the_subject_travels_the_same_path_as_the_peers(self):
        """자신만 다른 블록에서 오면 기준연도·주가시점이 달라진다."""
        block = self.multiples(4.11, 5.13)
        self.assertEqual(block["subject"]["asset_id"], "000660")
        self.assertEqual(block["subject"]["forward_period"],
                         block["peers"][0]["forward_period"])

    def test_a_subject_above_the_median_reads_positive(self):
        block = self.multiples(10.0, 5.0)
        self.assertEqual(block["forward_per_vs_median_pct"], 100.0)

    def test_no_usable_peer_means_no_gap_rather_than_zero(self):
        """비교 대상이 없는 것과 '중앙값과 같다'는 완전히 다른 사건이다."""
        block = self.multiples(4.11, None)
        self.assertIsNone(block["forward_per_median"])
        self.assertIsNone(block["forward_per_vs_median_pct"])

    def test_a_missing_subject_multiple_yields_no_gap(self):
        block = self.multiples(None, 5.13)
        self.assertEqual(block["forward_per_median"], 5.13)
        self.assertIsNone(block["forward_per_vs_median_pct"])

    def test_a_nonpositive_median_refuses_the_ratio(self):
        """적자 동종의 PER 이 0 이하로 들어오면 괴리는 나눗셈이 아니다."""
        block = self.multiples(4.11, 0.0)
        self.assertIsNone(block["forward_per_vs_median_pct"])

    def test_an_asset_without_peers_has_no_block_at_all(self):
        self.assertIsNone(_peer_multiples({"asset_id": "000660", "peers": []}))


class TestPeersAreComparedWithinOneForwardPeriod(unittest.TestCase):
    """같은 수집기를 썼다는 것이 같은 전망기간이라는 뜻은 아니다.

    대상 2026년 15배, 동종 A 2026년 10배, 동종 B 2027년 30배를 그대로 섞으면
    중앙값 20 대비 25% 저렴하다고 실린다. 같은 2026년끼리는 50% 비싸다 —
    상대 위치의 부호가 뒤집힌다.
    """

    def block(self, subject, *peers):
        table = {"SUBJ": subject, **{f"PEER{i}": row for i, row in enumerate(peers)}}

        def fake_row(asset_id, name):
            row = table[asset_id]
            if row is None:
                return {"asset_id": asset_id, "name": name, "available": False,
                        "forward_per": None}
            period, per, as_of = row
            return {"asset_id": asset_id, "name": name, "available": True,
                    "as_of": as_of, "forward_period": period, "forward_per": per}

        with patch("cio.context._forward_multiple_row", side_effect=fake_row):
            block = _peer_multiples({
                "asset_id": "SUBJ", "name": "대상",
                "peers": [{"asset_id": f"PEER{i}", "name": f"동종{i}"}
                          for i in range(len(peers))]})
        assert block is not None
        return block

    def test_a_peer_from_another_forward_year_does_not_move_the_median(self):
        block = self.block(("2026-12", 15.0, "2026-10-12"),
                           ("2026-12", 10.0, "2026-10-12"),
                           ("2027-12", 30.0, "2026-10-12"))
        self.assertEqual(block["forward_period"], "2026-12")
        self.assertEqual(block["forward_per_median"], 10.0)
        self.assertEqual(block["forward_per_vs_median_pct"], 50.0)
        self.assertEqual(block["forward_per_max"], 10.0)
        self.assertEqual(block["excluded_from_median"],
                         [{"asset_id": "PEER1", "forward_period": "2027-12"}])
        # 값은 지우지 않고, 어느 기간의 배수인지와 비교에서 빠졌다는 표시를 남긴다.
        self.assertEqual(block["peers"][1]["forward_per"], 30.0)
        self.assertIs(block["peers"][1]["comparable"], False)
        self.assertNotIn("comparable", block["peers"][0])
        self.assertIn("2026-12", block["_period_note"])

    def test_all_peers_in_one_period_carry_no_exclusion_note(self):
        block = self.block(("2026-12", 15.0, "2026-10-12"),
                           ("2026-12", 10.0, "2026-10-12"),
                           ("2026-12", 30.0, "2026-10-12"))
        self.assertEqual(block["forward_per_median"], 20.0)
        self.assertEqual(block["forward_per_vs_median_pct"], -25.0)
        self.assertNotIn("excluded_from_median", block)
        self.assertNotIn("as_of_range", block)

    def test_a_different_fiscal_year_end_is_a_different_period(self):
        """3월 결산의 2027-03 과 12월 결산의 2026-12 는 같은 해가 아니다."""
        block = self.block(("2026-12", 15.0, "2026-10-12"),
                           ("2027-03", 9.0, "2026-10-12"))
        self.assertIsNone(block["forward_per_median"])
        self.assertIsNone(block["forward_per_vs_median_pct"])
        self.assertEqual(len(block["excluded_from_median"]), 1)

    def test_without_a_subject_estimate_peers_need_one_shared_period(self):
        shared = self.block(None, ("2026-12", 10.0, "2026-10-12"),
                            ("2026-12", 14.0, "2026-10-12"))
        self.assertEqual(shared["forward_period"], "2026-12")
        self.assertEqual(shared["forward_per_median"], 12.0)
        self.assertIsNone(shared["forward_per_vs_median_pct"])
        split = self.block(None, ("2026-12", 10.0, "2026-10-12"),
                           ("2027-12", 14.0, "2026-10-12"))
        self.assertIsNone(split["forward_period"])
        self.assertIsNone(split["forward_per_median"])
        self.assertEqual(len(split["excluded_from_median"]), 2)

    def test_the_price_dates_behind_the_multiples_are_shown_when_they_differ(self):
        """기준일은 맞추지 않는다 — 맞췄다고 쓰지 않고 범위를 낸다."""
        block = self.block(("2026-12", 15.0, "2026-10-12"),
                           ("2026-12", 10.0, "2026-08-28"))
        self.assertEqual(block["as_of_range"],
                         {"from": "2026-08-28", "to": "2026-10-12"})
        self.assertNotIn("동일 기준일", block["basis"])

    def test_peer_order_does_not_change_the_result(self):
        rows = [("2026-12", 10.0, "2026-10-12"), ("2027-12", 30.0, "2026-10-12"),
                ("2026-12", 18.0, "2026-10-12")]
        forward = self.block(("2026-12", 15.0, "2026-10-12"), *rows)
        backward = self.block(("2026-12", 15.0, "2026-10-12"), *reversed(rows))
        for key in ("forward_per_median", "forward_per_vs_median_pct",
                    "forward_per_min", "forward_per_max"):
            self.assertEqual(forward[key], backward[key])


class TestTheFirstForwardYearIsChosenByPeriod(unittest.TestCase):
    """첫 전망연도는 표의 순서가 아니라 기간으로 고른다."""

    def row(self, annual):
        import cio.context as context
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "consensus").mkdir()
            (Path(tmp) / "consensus" / "X.json").write_text(json.dumps({
                "as_of": "2026-10-12",
                "estimates": {"annual": annual, "per_band_actual": {}}}))
            with patch.object(context, "KNOWLEDGE_DIR", Path(tmp)):
                return context._forward_multiple_row("X", "x")

    def annual(self):
        return [
            {"period": period, "actual": actual, "per": per,
             "revenue_yoy_pct": None, "roe_pct": None}
            for period, actual, per in (("2025-12", True, 20.0),
                                        ("2026-12", False, 15.0),
                                        ("2027-12", False, 12.0))]

    def test_row_order_does_not_change_the_forward_year(self):
        ordered = self.row(self.annual())
        shuffled = self.row(list(reversed(self.annual())))
        self.assertEqual(ordered["forward_period"], "2026-12")
        self.assertEqual(shuffled["forward_period"], "2026-12")
        self.assertEqual(shuffled["forward_per"], 15.0)


class TestForwardMultipleRow(unittest.TestCase):
    """자신과 동종이 같은 함수를 지나는가."""

    def test_a_missing_consensus_file_is_unavailable_not_a_crash(self):
        row = _forward_multiple_row("NOSUCH", "없는 회사")
        self.assertFalse(row["available"])
        self.assertIsNone(row["forward_per"])


class TestPacketArithmeticIsNotASecondImplementation(unittest.TestCase):
    """packet 의 비율이 공용 산술과 같은 규칙을 쓰는가.

    `_margin_pct` 와 `_pct_change` 는 `ratio_pct`·`change_pct` 와 같은 규칙을
    각자 구현하고 있었다. 확인해 보니 동작은 일치했지만, **일치한다는 것은
    확인해 봐야 아는 사실이지 유지되는 성질이 아니다** — 한쪽만 고치면 화면과
    packet 이 다른 답을 내고 아무도 모른다. 지금은 위임하므로 이 테스트는
    다시 갈라지는 것을 막는다.
    """

    CASES = [(100, 200), (100, 0), (100, -50), (-100, 200), (0, 200),
             (None, 200), (100, None), (None, None)]

    def test_margin_matches_the_shared_ratio(self):
        for value, base in self.CASES:
            with self.subTest(value=value, base=base):
                shared = ratio_pct(value, base)
                self.assertEqual(
                    _margin_pct(value, base),
                    None if shared is None else round(shared, 1),
                )

    def test_change_matches_the_shared_change(self):
        for value, base in self.CASES:
            with self.subTest(value=value, base=base):
                shared = change_pct(value, base)
                self.assertEqual(
                    _pct_change(value, base),
                    None if shared is None else round(shared, 1),
                )

    def test_a_zero_base_produces_no_margin_in_the_packet(self):
        """0 으로 나눈 자리를 0.0 으로 채우면 '마진 없음'으로 읽힌다."""
        self.assertIsNone(_margin_pct(100, 0))
        self.assertIsNone(_pct_change(100, 0))

    def test_a_negative_base_produces_no_rate_in_the_packet(self):
        """적자에서 흑자로 간 분기의 '+150%' 는 숫자일 뿐 뜻이 없다."""
        self.assertIsNone(_pct_change(50, -100))


class TestRevisionMagnitudeReachesThePacket(unittest.TestCase):
    """목표가 개정의 **크기**가 packet 에 실리는가.

    예전에는 상향·하향 건수만 실었다. 근거는 "목표가 수준보다 방향이 정보를
    담는다" 였는데, 그것은 **어느 값이 판단에 중요한가**에 대한 주장이고 그
    판단은 모델의 몫이다. 개정폭은 계산하면 나오는 사실이므로 PC 가 빼는 것은
    모델의 판단을 대신 내리는 일이다.

    실제로 000660 은 건수로는 "약간 하향 우위"(상향 6·하향 8) 인데 개정폭은
    -35.7% 와 +171.4% 였다 — 컨센서스의 완만한 이동이 아니라 애널리스트들이
    갈라진 상태다. 건수만으로는 8×-1% 와 1×-40%+7×-2% 가 같아 보인다.
    """

    def packet(self, revisions):
        payload = {
            "schema_version": "consensus-packet/1.0.0",
            "asset_id": "000660",
            "as_of": "2026-08-03",
            "summary": {"eps": 345914.0, "per": 4.32, "rating": 4.0,
                        "institution_count": 24, "target_price": 3322083.0},
            "dispersion": {
                "recent": {"count": 20, "min": 1480000.0, "max": 4700000.0,
                           "median": 3250000.0, "spread_pct": 217.57},
                "consensus_window": {"count": 24, "min": 1480000.0,
                                     "max": 4700000.0, "spread_pct": 217.57},
            },
            "revisions": revisions,
            "estimates": {"annual": []},
            "history": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "consensus").mkdir()
            (root / "consensus" / "000660.json").write_text(
                json.dumps(payload), encoding="utf-8")
            with patch("cio.context.KNOWLEDGE_DIR", root), \
                    patch("cio.context.ROOT", root):
                summaries, _ = __import__(
                    "cio.context", fromlist=["x"]
                )._optional_collection_summaries({
                    "asset_id": "000660", "market": "KR",
                    "optional_collectors": [],
                })
        return summaries["consensus"]["revisions"]

    def revisions(self, *, largest_raise=None, largest_cut=None, **counts):
        base = {"window_days": 90, "observed": 24, "raised": 6,
                "lowered": 8, "unchanged": 10,
                "largest_raise": largest_raise, "largest_cut": largest_cut}
        base.update(counts)
        return base

    def broker(self, name, prior, target, change):
        return {"broker": name, "date": "2026-07-30",
                "prior_target_price": prior, "target_price": target,
                "change_pct": change, "rating": "BUY"}

    def test_the_largest_cut_is_carried(self):
        block = self.packet(self.revisions(
            largest_cut=self.broker("신한투자", 4200000.0, 2700000.0, -35.71)))
        self.assertEqual(block["largest_cut"]["change_pct"], -35.71)

    def test_the_largest_raise_is_carried(self):
        block = self.packet(self.revisions(
            largest_raise=self.broker("상상인", 1400000.0, 3800000.0, 171.43)))
        self.assertEqual(block["largest_raise"]["change_pct"], 171.43)

    def test_counts_alone_cannot_separate_a_mild_drift_from_a_split(self):
        """같은 건수, 다른 세계 — packet 이 둘을 구분해야 한다."""
        mild = self.packet(self.revisions(
            largest_cut=self.broker("A", 100.0, 99.0, -1.0)))
        split = self.packet(self.revisions(
            largest_cut=self.broker("B", 100.0, 60.0, -40.0)))
        self.assertEqual(mild["lowered"], split["lowered"])
        self.assertNotEqual(mild["largest_cut"]["change_pct"],
                            split["largest_cut"]["change_pct"])

    def test_no_revision_in_a_direction_is_absent_not_zero(self):
        """하향이 없는 것과 '가장 큰 하향이 0%' 는 다른 사건이다."""
        block = self.packet(self.revisions(lowered=0, largest_cut=None))
        self.assertIsNone(block["largest_cut"])

    def test_the_counts_are_still_there(self):
        block = self.packet(self.revisions())
        for key in ("window_days", "observed", "raised", "lowered",
                    "unchanged"):
            with self.subTest(key):
                self.assertIn(key, block)


if __name__ == "__main__":
    unittest.main()
