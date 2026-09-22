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


if __name__ == "__main__":
    unittest.main()
