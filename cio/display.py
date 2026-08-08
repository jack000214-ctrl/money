"""표를 만들 때 지켜야 할 것 하나 — **없는 값을 0 으로 바꾸지 않는다.**

화면은 사람만 보는 것이 아니다. 이 저장소의 CLI 를 부르는 것은 모델이고,
모델은 터미널 출력을 근거로 읽는다. 실제로 한 판단이 `kr_inventory` 의 DIO,
`kr_flow` 의 순매수, `normalized_earnings` 의 배수를 전부 렌더링 출력에서 가져다
썼다. 그러니 표에 찍히는 숫자는 화면 장식이 아니라 **입력**이다.

그래서 서식이 틀리는 방식 셋을 여기서 한 번에 막는다.

1. **크래시** — `f"{None:>7.1f}"` 는 TypeError 다. 실제로 적자 연도의 PER 이
   null 이라 컨센서스 표 전체가 죽었고, packet 은 정상 저장됐는데 refresh 만
   FAILED 로 보고했다. 원인을 엉뚱한 곳에서 찾게 된다.
2. **0 으로 채우기** — 더 나쁘다. "적자라 PER 이 없다" 가 "PER 0배" 로 읽히고
   아무도 이상하게 여기지 않는다. 없는 것은 없다고 보여야 한다.
3. **칸 넘침** — 값이 칸보다 길면 옆 칸과 붙어 어느 숫자가 어느 항목인지 알 수
   없게 된다. 조 단위 회사에서 실제로 그랬다. 한글은 터미널에서 두 칸을
   차지하는데 `len()` 은 1 을 세므로 헤더는 그 사실을 따로 계산해야 한다.
"""

from __future__ import annotations

import unicodedata
from typing import Any

# 값이 없다는 표시. 0·빈칸·"N/A" 와 눈으로 구분되어야 한다.
ABSENT = "—"


def display_width(text: str) -> int:
    """터미널이 실제로 쓰는 칸 수.

    한글·한자·전각기호는 두 칸을 차지하는데 `len()` 은 1 을 센다. 글자 수로
    접으면 실제 줄은 두 배로 뻗어 나가고 접는 의미가 사라진다.
    """
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )


def pad(text: str, width: int, *, align: str = ">") -> str:
    """표시 폭 기준으로 채운다. 헤더 라벨이 한글일 때 `str.rjust` 로는 어긋난다."""
    filler = " " * max(0, width - display_width(text))
    return text + filler if align == "<" else filler + text


def cell(
    value: Any,
    width: int,
    spec: str = "",
    unit: str = "",
    *,
    scale: float | None = None,
) -> str:
    """숫자 한 칸. 없으면 `—`, 있으면 서식대로.

    `scale` 은 표시 단위 환산이다(원 → 억원 등). 값이 없을 때 나눗셈을
    시도하지 않도록 여기서 함께 처리한다 — 호출부에서 `value / 1e8` 을 먼저
    계산하면 그 자리에서 None 이 터진다.
    """
    if value is None:
        return pad(ABSENT, width)
    if scale:
        value = value / scale
    return pad(format(value, spec) + unit, width)
