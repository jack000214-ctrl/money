"""과거 평가가 다음 판단에 실제로 도달하는지 지키는 회귀 테스트.

README 첫 문단이 이 프로그램의 목적을 "좋은 모델일수록 더 나은 판단을 하도록
데이터와 **과거 평가**를 로컬 자산으로 남긴다"로 적고 있다. 그런데 평가를
남기는 것과 그것이 판단 시점에 읽히는 것은 다른 일이고, 실제로 끊겨 있었다.

`cio score` 는 `evaluation_id` 를 내용 지문으로 계산해 파일명으로 쓰고,
`cio prepare` 는 그 지문을 다시 계산해 맞지 않으면 변조로 보고 건너뛴다.
그런데 라이터가 지문을 낸 **뒤에** `source`·`citable`·`contract_gap`·`retired`
넷을 객체에 붙였고, 리더는 붙은 뒤의 파일 전체로 다시 계산했다. 두 값이 영원히
어긋나 저장된 116건 중 0건이 읽혔고 packet 의 `latest_evaluation` 은 언제나
null 이었다 — 판단이 자기 직전 판단의 성적을 한 번도 못 봤다.

가장 나쁜 것은 이것이 **조용했다**는 점이다. 리더는 `continue` 로 넘어가고
`cio score` 는 자기 출력에서 지문을 검사하지 않으므로, 두 명령 모두 정상으로
보였다. 그래서 여기서 지키는 것은 "값이 맞다"가 아니라 **"두 쪽이 같은 규칙을
쓴다"** 이다.
"""

import json
import unittest

from cio.context import stable_hash
from cio.memory import EVALUATION_ID_EXCLUDED_KEYS
from cio.paths import KNOWLEDGE_DIR


def fingerprint(evaluation: dict) -> str:
    """리더(`cio/context.py`)가 하는 것과 같은 재계산."""
    return stable_hash(
        {
            key: value
            for key, value in evaluation.items()
            if key not in EVALUATION_ID_EXCLUDED_KEYS
        },
        "EVAL-",
    )


# `_score_one` 이 지문 계산 뒤에 붙이는 저장 시점 라벨들. 이 목록이 늘어나면
# EVALUATION_ID_EXCLUDED_KEYS 도 같이 늘어야 하고, 안 그러면 그 순간부터
# 모든 평가가 다시 읽히지 않는다.
LABELS_ADDED_AFTER_FINGERPRINT = {
    "source": "LIVE",
    "citable": True,
    "contract_gap": {"errors": ["예시"], "_meaning": "예시"},
    "retired": {"retired_at": "2026-08-09", "class": "USER_DECLARED"},
}


class TestExclusionSetIsShared(unittest.TestCase):
    """라이터와 리더가 같은 상수를 쓰는지 — 값이 아니라 출처를 본다."""

    def test_reader_and_writer_import_the_same_object(self):
        from cio import context as context_module
        from cio import memory as memory_module

        self.assertIs(
            context_module.EVALUATION_ID_EXCLUDED_KEYS,
            memory_module.EVALUATION_ID_EXCLUDED_KEYS,
            "지문 키 집합이 두 곳에 따로 있으면 한쪽만 고쳐질 때 다시 끊긴다",
        )

    def test_every_post_fingerprint_label_is_excluded(self):
        for key in LABELS_ADDED_AFTER_FINGERPRINT:
            with self.subTest(key=key):
                self.assertIn(
                    key,
                    EVALUATION_ID_EXCLUDED_KEYS,
                    f"{key} 는 지문 계산 뒤에 붙는데 제외 목록에 없다 — "
                    "이 상태면 모든 평가가 리더에서 조용히 사라진다",
                )


class TestFingerprintSurvivesLabels(unittest.TestCase):
    """저장 시점 라벨이 붙어도 지문이 그대로여야 한다."""

    def _base(self) -> dict:
        return {
            "schema_version": "cio-evaluation/1.1.0",
            "assessment_id": "CIO-0000000000000000dead",
            "context_id": "CTX-0000000000000000beef",
            "asset_id": "214450",
            "verdict": "WAIT",
            "outcome_metric": "TERMINAL_TOTAL_RETURN_ASSET_CURRENCY",
            "price": {"start": 378500.0, "latest": 406500.0},
            "window": {"complete": False},
        }

    def test_labels_do_not_change_the_fingerprint(self):
        evaluation = self._base()
        before = fingerprint(evaluation)

        evaluation["evaluation_id"] = before
        evaluation["evaluated_at"] = "2026-08-10T14:12:23+09:00"
        evaluation.update(LABELS_ADDED_AFTER_FINGERPRINT)

        self.assertEqual(
            fingerprint(evaluation),
            before,
            "라벨이 붙었다고 지문이 바뀌면 리더가 변조로 읽는다",
        )

    def test_changing_a_prediction_does_change_the_fingerprint(self):
        """반대 방향도 지킨다 — 예측 내용이 바뀌면 지문은 반드시 바뀐다.

        제외 목록을 넓히는 것으로 이 테스트를 통과시키려는 수정을 막는다.
        """
        evaluation = self._base()
        before = fingerprint(evaluation)

        evaluation["price"] = {"start": 378500.0, "latest": 999999.0}

        self.assertNotEqual(fingerprint(evaluation), before)


class TestStoredEvaluationsAreReadable(unittest.TestCase):
    """디스크에 쌓인 실제 평가가 지금 리더로 읽히는가."""

    def test_all_stored_evaluations_match_their_filename(self):
        directory = KNOWLEDGE_DIR / "evaluations"
        if not directory.exists():
            self.skipTest("평가가 아직 없다")

        unreadable = []
        total = 0
        for path in sorted(directory.glob("*/EVAL-*.json")):
            total += 1
            data = json.loads(path.read_text(encoding="utf-8"))
            stored = data.get("evaluation_id")
            if path.stem != stored or fingerprint(data) != stored:
                unreadable.append(path.name)

        if not total:
            self.skipTest("평가가 아직 없다")
        self.assertEqual(
            unreadable,
            [],
            f"{len(unreadable)}/{total} 건이 리더에서 건너뛰어진다. "
            "packet 의 latest_evaluation 이 그만큼 null 이 된다",
        )


if __name__ == "__main__":
    unittest.main()
