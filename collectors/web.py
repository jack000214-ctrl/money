#!/usr/bin/env python3
"""모델이 직접 조사한 원문을 로컬에 내용 주소로 보존한다.

모델은 웹에서 근거를 찾을 수 있지만 그것을 남길 수단이 없었다. 그래서 판단에는
URL만 남고, 며칠 뒤 그 페이지가 바뀌거나 사라지면 근거를 재현할 수 없었다.
프롬프트가 "전용 로컬 수집을 요청하라"고 말해도 그 요청은 사람을 거쳐야 했다.
이 수집기는 그 사이를 메운다 — 모델이 자기 근거를 스스로 박아 넣는다.

**원문을 손대지 않는다.** 현재는 손실 없이 왕복할 수 있는 텍스트 응답만 받으며
PDF·바이너리는 지원하지 않는다. 광고를 걷어내거나 본문만 뽑지 않는다. 페이지 구조는
언제든 바뀌고 그때마다 파서가 깨지지만, 받은 그대로 저장한 바이트는 깨지지
않는다. 무엇이 본문인지는 그 파일을 읽는 모델이 판단한다.

수집한 원문은 context packet 에 들어가지 않는다. packet 은 작게 두고, 필요한
판단에서만 파일을 연다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn

from calculators.contracts import ContractError, canonical_payload_hash, load_json
from cio.paths import DATA_DIR, KNOWLEDGE_DIR, ROOT

OBJECT_DIR = DATA_DIR / "raw" / "web" / "objects"
INDEX_DIR = KNOWLEDGE_DIR / "evidence" / "web"
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 45
USER_AGENT = "Money/1.0 (research snapshot; contact: local user)"
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,39}")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
KST = dt.timezone(dt.timedelta(hours=9))


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


FRAME_RE = re.compile(r"<i?frame\b", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(
    r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)


def shell_page_warning(payload: dict[str, Any]) -> str | None:
    """받은 것이 내용이 아니라 프레임 껍데기면 그렇다고 말한다.

    DART 뷰어(dsaf001/main.do)가 대표적이다. 주소는 공시를 가리키지만 응답에는
    본문이 없고 iframe 만 있다. 그것을 근거로 저장하면 "원문을 보존했다"는
    기록만 남고 내용은 없는, 가장 나쁜 종류의 근거가 된다.
    """
    body = payload.get("body") or ""
    if not FRAME_RE.search(body):
        return None
    text = TAG_RE.sub(" ", SCRIPT_RE.sub(" ", body))
    if len(re.sub(r"\s+", "", text)) >= 800:
        return None
    hint = ""
    host = urllib.parse.urlparse(payload.get("final_url") or "").netloc
    if "dart.fss.or.kr" in host:
        hint = (
            " DART 뷰어는 본문을 iframe 으로 불러온다. 공시 원문 자체가 "
            "필요하면 `python -m collectors.dart document <접수번호>` 로 "
            "OpenDART 원문을 받는다"
        )
    return (
        "이 응답에는 본문이 거의 없고 프레임만 있다 — 실제 내용은 다른 "
        "주소에서 불러온다. 이 스냅샷을 내용 근거로 인용하면 안 된다." + hint
    )


def _title(body: str) -> str | None:
    match = TITLE_RE.search(body)
    if not match:
        return None
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    return text[:200] or None


def _user_agent(host: str) -> str:
    """호스트가 요구하는 신원을 쓴다.

    SEC 는 연락처 없는 요청을 403 으로 막는다. 이 저장소에는 이미 그 목적의
    SEC_USER_AGENT 가 있으므로, 일반 UA 로 보내 실패하는 대신 그것을 쓴다.
    """
    if host == "sec.gov" or host.endswith(".sec.gov"):
        from collectors.edgar import user_agent

        return user_agent()
    return USER_AGENT


def fetch(url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        fail("http 또는 https URL만 수집한다")
    if not parsed.netloc:
        fail("호스트가 없는 URL이다")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _user_agent(parsed.netloc.split(":")[0].lower())},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_BYTES + 1)
            status = response.status
            headers = dict(response.headers.items())
            final_url = response.url
    except urllib.error.HTTPError as exc:
        fail(f"HTTP {exc.code} — {url}")
    except urllib.error.URLError as exc:
        fail(f"연결 실패 — {exc.reason}")
    except (TimeoutError, OSError) as exc:
        fail(f"수집 실패 — {exc}")
    if len(raw) > MAX_BYTES:
        fail(f"본문이 {MAX_BYTES} bytes 제한을 넘는다")
    content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    media_type = content_type.split(";", 1)[0].strip().lower()
    is_text_type = (not media_type or media_type.startswith("text/")
                    or media_type in {"application/json", "application/xml", "application/javascript"}
                    or media_type.endswith(("+json", "+xml")))
    if raw.lstrip().startswith(b"%PDF-") or not is_text_type:
        fail(f"PDF·바이너리 응답({media_type or 'unknown'})은 지원하지 않는다 — 원문을 저장하지 않았다")
    charset = "utf-8"
    match = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        body = raw.decode(charset, errors="strict")
        if body.encode(charset, errors="strict") != raw:
            fail("선언한 문자 인코딩으로 원문 바이트를 재현할 수 없다 — 저장하지 않았다")
    except (LookupError, UnicodeError):
        fail(f"본문을 {charset}로 손실 없이 읽을 수 없다 — 대체문자로 바꾸거나 저장하지 않았다")
    if "\x00" in body:
        fail("텍스트가 아닌 NUL 포함 응답은 지원하지 않는다 — 원문을 저장하지 않았다")
    return {
        "requested_url": url,
        "final_url": final_url,
        "http_status": status,
        "content_type": content_type or None,
        "content_length": len(raw),
        "body_encoding": charset,
        "body": body,
    }


def save_snapshot(payload: dict[str, Any], *, label: str) -> tuple[Path, str]:
    digest = canonical_payload_hash(payload)
    OBJECT_DIR.mkdir(parents=True, exist_ok=True)
    path = OBJECT_DIR / f"{digest}.json"
    if path.exists():
        return path, digest
    document = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "web",
        "request_name": label,
        "payload_sha256": digest,
        "payload": payload,
    }
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=OBJECT_DIR, delete=False
        ) as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path, digest


def index_path(asset_id: str) -> Path:
    return INDEX_DIR / f"{asset_id}.json"


def load_index(asset_id: str) -> dict[str, Any]:
    path = index_path(asset_id)
    if not path.exists():
        return {
            "schema_version": "web-evidence/1.0.0",
            "asset_id": asset_id,
            "snapshots": [],
        }
    data = load_json(path)
    if data.get("schema_version") != "web-evidence/1.0.0":
        raise ContractError([f"{path}: web-evidence/1.0.0이 아니다"])
    if not isinstance(data.get("snapshots"), list):
        raise ContractError([f"{path}: snapshots 배열이 없다"])
    return data


def record_index(
    asset_id: str,
    *,
    label: str,
    digest: str,
    locator: str,
    payload: dict[str, Any],
    note: str | None,
) -> Path:
    data = load_index(asset_id)
    entry = {
        "source_id": f"WEBSNAP:{digest[:12]}",
        "label": label,
        "url": payload["final_url"],
        "requested_url": payload["requested_url"],
        "title": _title(payload["body"]),
        "note": note,
        "content_sha256": digest,
        "path": locator,
        "fetched_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
        "content_length": payload["content_length"],
        "shell_page_warning": shell_page_warning(payload),
    }
    snapshots = [
        item
        for item in data["snapshots"]
        if item.get("content_sha256") != digest
    ]
    snapshots.append(entry)
    snapshots.sort(key=lambda item: str(item.get("fetched_at") or ""))
    data["snapshots"] = snapshots
    path = index_path(asset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def snapshot(
    url: str, *, asset_id: str, label: str, note: str | None = None
) -> dict[str, Any]:
    if not LABEL_RE.fullmatch(label):
        fail("label은 소문자·숫자·하이픈 2~40자여야 한다")
    payload = fetch(url)
    object_path, digest = save_snapshot(payload, label=label)
    locator = str(object_path.relative_to(ROOT))
    record_index(
        asset_id,
        label=label,
        digest=digest,
        locator=locator,
        payload=payload,
        note=note,
    )
    return {
        "source_id": f"WEBSNAP:{digest[:12]}",
        "asset_id": asset_id,
        "label": label,
        "url": payload["final_url"],
        "title": _title(payload["body"]),
        "content_sha256": digest,
        "path": locator,
        "content_length": payload["content_length"],
        "index": str(index_path(asset_id).relative_to(ROOT)),
        "shell_page_warning": shell_page_warning(payload),
    }


# `recent_snapshots` 는 없앴다. 보존한 웹 원문은 `cio.documents` 가 공시 원문과
# 한 목록으로 싣는다 — 따로 실으면 같은 문서가 packet 에 두 번 나갔다.


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collectors.web",
        description="조사한 웹 원문을 내용 주소로 보존한다",
    )
    parser.add_argument("url")
    parser.add_argument("--asset", required=True, help="자산 ID")
    parser.add_argument("--label", required=True, help="소문자-하이픈 짧은 이름")
    parser.add_argument("--note", help="왜 이 원문을 남기는지 한 줄")
    args = parser.parse_args()
    result = snapshot(
        args.url, asset_id=args.asset, label=args.label, note=args.note
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
