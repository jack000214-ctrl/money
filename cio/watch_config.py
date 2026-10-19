"""보유 종목 감시 배치의 설정과 장 시간 게이트.

언제·얼마나 자주 감시할지는 코드가 아니라 `config/watch.json` 이 정한다.
주기를 60분에서 10분으로 바꾸는 일은 `interval_minutes` 한 줄이면 된다.

`review` 의 null 은 '제한 없음'이 아니라 '아직 정하지 않음'이다. 여기서 기본값을
채우지 않는다 — 프로그램이 값을 정하는 순간 사용자의 정책이 프로그램의 가정으로
바뀌고 그 사실이 출력에서 사라진다. 정하지 않은 항목은 감시하지 않고, 감시하지
않는다는 사실을 `unset_review_fields()` 로 보고한다.

비밀번호는 이 파일에 없다. `SMTP_PASSWORD` 는 `.env` 에만 둔다.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from calculators.contracts import write_json
from cio.paths import CONFIG_DIR

CONFIG_PATH = CONFIG_DIR / "watch.json"
TEMPLATE_PATH = CONFIG_DIR / "templates" / "watch.json"

SCHEMA_VERSION = "watch-config/1.0.0"
SUPPORTED_MARKETS = ("KR",)
CHANNELS = ("console", "file", "dashboard", "email")
REVIEW_FIELDS = (
    "stale_after_days",
    "price_drift_pct",
    "earnings_within_business_days",
)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# 헤더 주입을 막으려고 공백·줄바꿈·쉼표를 주소에 허용하지 않는다.
_ADDRESS_RE = re.compile(r"^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")

RUN = "RUN"
SKIP = "SKIP"


class WatchConfigError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _fallback_defaults() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "timezone": "Asia/Seoul",
        "window": {"start": "08:00", "end": "20:00"},
        "interval_minutes": 60,
        "markets": ["KR"],
        "channels": {
            "console": True,
            "file": True,
            "dashboard": True,
            "email": False,
        },
        "email": {
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_user": "",
            "smtp_from": "",
            "recipients": [],
        },
        "review": {name: None for name in REVIEW_FIELDS},
        "briefing": {"enabled": True, "time": "08:30"},
    }


def _read(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WatchConfigError([f"{path.name}을 읽을 수 없다: {exc}"]) from exc
    if not isinstance(data, dict):
        raise WatchConfigError([f"{path.name}의 최상위는 객체여야 한다"])
    return data


def _parse_time(value: Any, label: str, errors: list[str]) -> dt.time | None:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        errors.append(f"{label}은 HH:MM 형식이어야 한다")
        return None
    return dt.time(int(value[:2]), int(value[3:]))


def _positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
        and value not in (float("inf"), float("-inf"))
        and value > 0
    )


def validate_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    zone = config.get("timezone")
    try:
        ZoneInfo(zone)  # type: ignore[arg-type]
    except (ZoneInfoNotFoundError, ValueError, TypeError, KeyError):
        errors.append("timezone은 IANA 시간대 이름이어야 한다 (예: Asia/Seoul)")

    window = config.get("window")
    if not isinstance(window, dict):
        errors.append("window는 start·end를 가진 객체여야 한다")
    else:
        start = _parse_time(window.get("start"), "window.start", errors)
        end = _parse_time(window.get("end"), "window.end", errors)
        if start and end and not start < end:
            errors.append("window.start는 window.end보다 이르다")

    interval = config.get("interval_minutes")
    if (
        not isinstance(interval, int)
        or isinstance(interval, bool)
        or not 1 <= interval <= 1440
    ):
        errors.append("interval_minutes는 1~1440 사이의 정수여야 한다")

    markets = config.get("markets")
    if (
        not isinstance(markets, list)
        or not markets
        or any(m not in SUPPORTED_MARKETS for m in markets)
    ):
        errors.append(
            f"markets는 {list(SUPPORTED_MARKETS)} 중에서 고른다 (미국 주식은 감시하지 않는다)"
        )

    channels = config.get("channels")
    if not isinstance(channels, dict) or set(channels) != set(CHANNELS):
        errors.append(f"channels는 {list(CHANNELS)} 모두를 true/false로 가져야 한다")
    elif any(not isinstance(v, bool) for v in channels.values()):
        errors.append("channels의 값은 true/false여야 한다")

    email = config.get("email")
    if not isinstance(email, dict):
        errors.append("email은 객체여야 한다")
    else:
        host = email.get("smtp_host", "")
        if not isinstance(host, str) or (host and not _HOST_RE.fullmatch(host)):
            errors.append("email.smtp_host는 호스트 이름이어야 한다")
        port = email.get("smtp_port")
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            errors.append("email.smtp_port는 1~65535 사이의 정수여야 한다")
        for field in ("smtp_user", "smtp_from"):
            value = email.get(field, "")
            if not isinstance(value, str) or re.search(r"[\r\n]", value):
                errors.append(f"email.{field}는 한 줄 문자열이어야 한다")
        recipients = email.get("recipients")
        if not isinstance(recipients, list):
            errors.append("email.recipients는 배열이어야 한다")
        else:
            for index, row in enumerate(recipients, start=1):
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("address"), str)
                    or not _ADDRESS_RE.fullmatch(row["address"])
                ):
                    errors.append(f"email.recipients[{index}]의 address가 올바르지 않다")
                elif not isinstance(row.get("enabled", True), bool):
                    errors.append(f"email.recipients[{index}]의 enabled는 true/false여야 한다")

    review = config.get("review")
    if not isinstance(review, dict) or set(review) != set(REVIEW_FIELDS):
        errors.append(f"review는 {list(REVIEW_FIELDS)} 를 모두 가져야 한다 (미정은 null)")
    else:
        for name, value in review.items():
            if value is not None and not _positive_number(value):
                errors.append(f"review.{name}은 null(미정) 또는 0보다 큰 숫자여야 한다")

    briefing = config.get("briefing")
    if not isinstance(briefing, dict):
        errors.append("briefing은 객체여야 한다")
    else:
        if not isinstance(briefing.get("enabled"), bool):
            errors.append("briefing.enabled는 true/false여야 한다")
        _parse_time(briefing.get("time"), "briefing.time", errors)
    return errors


def default_config() -> dict[str, Any]:
    """템플릿 파일이 있으면 그것을, 없으면 같은 값의 내장 기본 구조를 낸다."""
    try:
        template = _read(TEMPLATE_PATH)
    except WatchConfigError:
        template = None
    base = _fallback_defaults()
    if template:
        template = {k: v for k, v in template.items() if not k.startswith("_")}
        base = _merge(base, template)
    return base


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """객체는 깊게 합치고 배열·스칼라는 통째로 교체한다."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    """설정을 읽는다. 파일이 없으면 템플릿 값이며 오류가 아니다."""
    target = Path(path) if path else CONFIG_PATH
    stored = _read(target)
    config = default_config()
    if stored:
        config = _merge(config, {k: v for k, v in stored.items() if not k.startswith("_")})
    errors = validate_config(config)
    if errors:
        raise WatchConfigError(errors)
    return config


def save_config(changes: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """일부 항목을 바꿔 저장한다. 검증에 실패하면 아무것도 쓰지 않는다."""
    if not isinstance(changes, dict):
        raise WatchConfigError(["설정 본문은 객체여야 한다"])
    target = Path(path) if path else CONFIG_PATH
    current = load_config(target)
    allowed = {k: v for k, v in changes.items() if k in current and k != "schema_version"}
    merged = _merge(current, allowed)
    # 수신자 목록은 부분 병합이 아니라 통째로 교체해야 삭제가 반영된다.
    if "email" in allowed and "recipients" in (allowed["email"] or {}):
        merged["email"]["recipients"] = copy.deepcopy(allowed["email"]["recipients"])
    errors = validate_config(merged)
    if errors:
        raise WatchConfigError(errors)
    merged["schema_version"] = SCHEMA_VERSION
    write_json(target, merged)
    return merged


def unset_review_fields(config: dict[str, Any]) -> list[str]:
    """아직 정하지 않아 감시하지 않는 항목."""
    return [name for name in REVIEW_FIELDS if config["review"].get(name) is None]


def _zone(config: dict[str, Any]) -> ZoneInfo:
    return ZoneInfo(config["timezone"])


def now_in_zone(config: dict[str, Any]) -> dt.datetime:
    return dt.datetime.now(_zone(config))


def gate(now: dt.datetime | None, config: dict[str, Any]) -> dict[str, Any]:
    """지금 감시해도 되는 시각인가. 시작은 포함, 종료는 미포함이다.

    평일 휴장일은 여기서 알 수 없다. 휴장일은 시세 시각이 갱신되지 않는 것으로
    `collectors/quote.py` 가 감지한다.
    """
    zone = _zone(config)
    local = now.astimezone(zone) if now and now.tzinfo else (
        now.replace(tzinfo=zone) if now else dt.datetime.now(zone)
    )
    if local.weekday() >= 5:
        return {"decision": SKIP, "reason": "WEEKEND", "message": "주말이라 감시하지 않는다", "at": local.isoformat(timespec="seconds")}
    window = config["window"]
    start = dt.time(int(window["start"][:2]), int(window["start"][3:]))
    end = dt.time(int(window["end"][:2]), int(window["end"][3:]))
    clock = local.time().replace(second=0, microsecond=0)
    if clock < start:
        return {"decision": SKIP, "reason": "BEFORE_WINDOW", "message": f"감시 시작({window['start']}) 전이다", "at": local.isoformat(timespec="seconds")}
    if clock >= end:
        return {"decision": SKIP, "reason": "AFTER_WINDOW", "message": f"감시 종료({window['end']}) 이후다", "at": local.isoformat(timespec="seconds")}
    return {"decision": RUN, "reason": None, "message": "감시 시간이다", "at": local.isoformat(timespec="seconds")}
