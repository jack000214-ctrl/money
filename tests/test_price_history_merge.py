"""가격 이력이 재수집으로 잘리지 않는지 지키는 회귀 테스트.

`collectors.price` 는 `--range` 만큼만 받아 오는데 저장이 통째로 덮어쓰기였다.
`cio refresh` 는 `--range 5y` 로 고정돼 있으므로 파일이 **롤링 5년 창**이 됐고,
돌릴 때마다 가장 오래된 하루가 조용히 떨어졌다. 실제로 000660 은 2016 년부터
2,448 일이 있었는데 refresh 한 번에 1,220 일이 됐다 — 1,229 일이 사라졌고 어떤
경고도 없었다.

가격은 "다시 받으면 되는" 데이터처럼 보이지만 아니다. 3년 실측 PER 밴드,
12개월 수익률, 고점 대비 낙폭이 전부 여기서 나오고, 무엇보다 **과거 판단의
채점**이 이 시계열 위에 선다. 창이 깎이면 track record 가 앞에서부터 지워진다.

두 번째 테스트가 특히 중요하다. 병합을 처음 만들 때 겹침 대조에 미확정 봉을
포함시켰고, 장중이라 그 봉의 종가가 달라지는 바람에 "분할이 있었다"로 오판해
이력이 그대로 날아갔다. **고쳤다고 생각한 것이 같은 결과를 냈다.**
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from collectors import price


def bar(day: str, close: float, *, factor: float = 1.0) -> dict:
    return {
        "date": day,
        "close": close,
        "adjusted_close": close,
        "split_adjustment_factor": factor,
        "high": close,
        "low": close,
        "volume": 100,
    }


SETTLED = "SETTLED"
UNSETTLED = "UNSETTLED"


class TestHistoryIsNotTruncated(unittest.TestCase):
    def _save(self, tmp, rows, state):
        midnight = int(datetime.fromisoformat(rows[-1]["date"]).replace(tzinfo=timezone.utc).timestamp())
        meta = {
            "regularMarketTime": midnight + (16 if state == SETTLED else 14) * 3600,
            "currentTradingPeriod": {"regular": {
                "start": midnight + 9 * 3600, "end": midnight + 16 * 3600}},
            "gmtoffset": 0,
        }
        with patch("collectors.price.PRICE_DIR", Path(tmp)):
            return price.save("TEST", {"currency": "KRW", **meta}, rows)

    def test_short_range_keeps_the_older_history(self):
        long_history = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        short_pull = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 13)]

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, long_history, SETTLED)
            path = self._save(tmp, short_pull, SETTLED)
            saved = price._existing_rows(path)

        self.assertEqual(saved[0]["date"], "2020-01-01", "옛 이력이 잘렸다")
        self.assertEqual(saved[-1]["date"], "2020-01-12", "새 봉이 안 붙었다")
        self.assertEqual(len(saved), 12)

    def test_moving_unsettled_bar_is_not_mistaken_for_a_split(self):
        """장중 봉의 종가 변화로 병합이 거부되면 안 된다."""
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        # 마지막 봉이 미확정인 채로 저장된 상태에서, 값이 움직인 뒤 재수집.
        moved = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 11)]
        moved[-1]["close"] = 999.0
        moved[-1]["adjusted_close"] = 999.0

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, UNSETTLED)
            path = self._save(tmp, moved, UNSETTLED)
            saved = price._existing_rows(path)

        self.assertEqual(saved[0]["date"], "2020-01-01", "장중 봉 때문에 잘렸다")
        self.assertEqual(float(saved[-1]["close"]), 999.0)

    def test_a_real_split_refuses_to_stitch(self):
        """확정된 겹침 구간이 어긋나면 옛 기준을 이어 붙이지 않는다."""
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        adjusted = [
            bar(f"2020-01-{d:02d}", (100 + d) / 2, factor=2.0)
            for d in range(6, 13)
        ]

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, SETTLED)
            path = self._save(tmp, adjusted, SETTLED)
            saved = price._existing_rows(path)

        self.assertEqual(
            saved[0]["date"],
            "2020-01-06",
            "분할 뒤에도 옛 기준을 붙이면 두 기준이 한 파일에 섞인다",
        )

    def test_next_session_corrects_the_previously_unsettled_bar_without_truncation(self):
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        moved = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 12)]
        moved[-2]["close"] = 999.0
        moved[-2]["adjusted_close"] = 999.0
        with tempfile.TemporaryDirectory() as tmp:
            old_path = self._save(tmp, stored, UNSETTLED)
            self.assertEqual(price._unsettled_date(old_path), "2020-01-10")
            path = self._save(tmp, moved, SETTLED)
            saved = price._existing_rows(path)
        self.assertEqual(saved[0]["date"], "2020-01-01")
        self.assertEqual(float(saved[-2]["close"]), 999.0)

    def test_disjoint_pull_does_not_stitch_across_a_gap(self):
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 6)]
        far = [bar(f"2021-06-{d:02d}", 200 + d) for d in range(1, 4)]

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, SETTLED)
            path = self._save(tmp, far, SETTLED)
            saved = price._existing_rows(path)

        self.assertEqual(saved[0]["date"], "2021-06-01", "빈 구간을 이어 붙였다")


class TestResponseHolesDoNotDeleteStoredBars(unittest.TestCase):
    """응답에 없는 날짜는 '그날이 없었다'가 아니라 '이번에 안 실었다'다.

    2026-10-13 수집에서 Yahoo 는 214450.KQ 와 ^KQ11 의 2026-10-12 를 빼고 정상
    응답을 줬다. 병합은 **수집 범위보다 앞선** 행만 보존했으므로 범위 안의 그
    하루가 사라졌고, packet 의 세션 수익률은 한 세션을 건너뛴 채 실렸다. 같은 날
    ^KS11 은 10-12 종가가 사후 정정(6,399.43 → 6,384.24)된 것을 액면분할로 오판해
    가장 오래된 40거래일을 버렸다.
    """

    _save = TestHistoryIsNotTruncated._save

    def test_a_confirmed_bar_absent_from_the_response_is_kept(self):
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        pulled = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 13) if d != 8]

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, SETTLED)
            path = self._save(tmp, pulled, SETTLED)
            saved = price._existing_rows(path)

        dates = [row["date"] for row in saved]
        self.assertIn("2020-01-08", dates, "응답에서 빠진 확정 봉을 지웠다")
        self.assertEqual(dates, sorted(dates), "보존한 봉이 날짜 순서를 깼다")
        self.assertEqual(len(saved), 12)

    def test_a_late_revision_of_the_last_stored_bar_is_not_a_split(self):
        """마지막 봉은 SETTLED 로 적혔어도 다음 수집 전까지는 잠정값이다."""
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        pulled = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 13)]
        pulled[4]["close"] = pulled[4]["adjusted_close"] = 109.76  # 2020-01-10 정정

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, SETTLED)
            path = self._save(tmp, pulled, SETTLED)
            saved = {row["date"]: row for row in price._existing_rows(path)}

        self.assertIn("2020-01-01", saved, "하루의 정정을 분할로 오판해 이력을 잘랐다")
        self.assertEqual(float(saved["2020-01-10"]["close"]), 109.76)

    def test_an_unconfirmed_last_bar_is_not_frozen_as_a_close(self):
        """장중 값을 종가로 남기지 않는다. 대신 그 세션이 있었다는 사실을 남긴다."""
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        stored[-1]["close"] = stored[-1]["adjusted_close"] = 555.0  # 장중 값
        pulled = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 13) if d != 10]

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, UNSETTLED)
            path = self._save(tmp, pulled, SETTLED)
            saved = [row["date"] for row in price._existing_rows(path)]
            missing = price.missing_sessions(path)

        self.assertNotIn("2020-01-10", saved, "장중 값이 확정 종가로 굳었다")
        self.assertEqual(missing, ["2020-01-10"], "사라진 세션이 기록되지 않았다")
        self.assertEqual(saved[0], "2020-01-01")

    def test_a_settled_label_does_not_make_the_last_bar_final(self):
        """SETTLED 로 적힌 마지막 봉도 같은 규칙이다. 재확인 전에는 잠정값이다."""
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        pulled = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 13) if d != 10]

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, SETTLED)
            path = self._save(tmp, pulled, SETTLED)
            saved = [row["date"] for row in price._existing_rows(path)]
            missing = price.missing_sessions(path)

        self.assertNotIn("2020-01-10", saved)
        self.assertEqual(missing, ["2020-01-10"])

    def test_a_bar_filled_from_the_exchange_keeps_its_provenance(self):
        """KRX 로 메운 봉은 재수집 뒤에도 남고, 출처 표기도 함께 남는다."""
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        stored[7]["volume"] = None  # 2020-01-08 — 지수 백필은 거래량을 비운다
        pulled = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 13) if d != 8]

        with tempfile.TemporaryDirectory() as tmp:
            path = self._save(tmp, stored, SETTLED)
            lines = path.read_text(encoding="utf-8").splitlines()
            marker = "# backfilled 2020-01-08 / source KRX Open API (idx/kospi_dd_trd) — 거래소 원문"
            first_data_line = next(
                index for index, line in enumerate(lines) if not line.startswith("#")
            )
            lines.insert(first_data_line, marker)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            path = self._save(tmp, pulled, SETTLED)
            saved = [row["date"] for row in price._existing_rows(path)]
            header = [
                line for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("#")
            ]

        self.assertIn("2020-01-08", saved, "메운 봉이 재수집에 지워졌다")
        self.assertIn(marker, header, "메운 봉의 출처 표기가 사라졌다")

    def test_a_recorded_hole_survives_until_the_bar_arrives(self):
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        without = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 13) if d != 10]
        again = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 14) if d != 10]
        complete = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 15)]

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, UNSETTLED)
            self._save(tmp, without, SETTLED)
            path = self._save(tmp, again, SETTLED)
            still_missing = price.missing_sessions(path)
            path = self._save(tmp, complete, SETTLED)
            saved = [row["date"] for row in price._existing_rows(path)]
            cleared = price.missing_sessions(path)

        self.assertEqual(still_missing, ["2020-01-10"], "재수집 한 번에 기록이 지워졌다")
        self.assertIn("2020-01-10", saved)
        self.assertEqual(cleared, [], "봉이 들어왔는데 구멍 기록이 남았다")

    def test_the_first_four_header_lines_keep_their_positions(self):
        """packet 조립이 머리말 네 줄을 자리로 읽는다. 새 줄은 그 뒤에만 온다."""
        stored = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(1, 11)]
        pulled = [bar(f"2020-01-{d:02d}", 100 + d) for d in range(6, 13) if d != 10]

        with tempfile.TemporaryDirectory() as tmp:
            self._save(tmp, stored, UNSETTLED)
            path = self._save(tmp, pulled, SETTLED)
            head = path.read_text(encoding="utf-8").splitlines()[:4]

        self.assertTrue(head[0].startswith("# schema_version "))
        self.assertTrue(head[1].startswith("# symbol "))
        self.assertTrue(head[2].startswith("# retrieved_at "))
        self.assertTrue(head[3].startswith("# last_bar "))


if __name__ == "__main__":
    unittest.main()
