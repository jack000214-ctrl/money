"""보존한 원문 안을 찾는다.

`dart document`가 사업보고서를 받으면 897만 자짜리 파일이 생긴다. 그 안에는
부문별 실적도, 주요 매출처(Alphabet·Apple)도, 파운드리 수주잔고도 다 들어 있다.
그런데 읽을 수가 없다 — 25만 토큰이다. 그래서 모델은 로컬에 있는 사실을 두고
웹을 검색하고, 2분기 실적을 DART 원문 대신 언론 보도로 인용하는 일이 생겼다.

이 모듈이 그 사이를 메운다. 원문을 통째로 넣는 대신 물어본 자리만 잘라 준다.
토픽에 종속되지 않는 것이 핵심이다 — 폴드8을 찾든 관세를 찾든 HBM을 찾든 같은
도구이고, 다음에 무엇이 중요해져도 새로 만들 것이 없다.

원문은 손대지 않는다. 검색은 태그를 걷어낸 **사본**에서 하고, 그 사본은 내용
지문에서 결정되므로 언제든 다시 만들 수 있다.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from calculators.contracts import ContractError, load_json
from cio.assets import get_asset
from cio.paths import DATA_DIR, KNOWLEDGE_DIR, ROOT

INDEX_PATH = DATA_DIR / "raw" / "document-index.json"
TEXT_CACHE_DIR = DATA_DIR / "raw" / "text"
INDEX_SCHEMA = "document-index/1.0.0"
# _strip_markup 을 고칠 때마다 올린다. 캐시된 사본의 신원에 들어간다.
TEXT_EXTRACT_VERSION = "v2"

OBJECT_DIRS = (
    ("DART", DATA_DIR / "raw" / "dart" / "objects"),
    ("WEB", DATA_DIR / "raw" / "web" / "objects"),
)

DEFAULT_CONTEXT_CHARS = 320
DEFAULT_MAX_MATCHES = 12
MAX_TOTAL_EXCERPT_CHARS = 14_000
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
DOCUMENT_TITLE_RE = re.compile(
    r"<(?:DOCUMENT-NAME|TITLE)[^>]*>(.*?)</(?:DOCUMENT-NAME|TITLE)>",
    re.IGNORECASE | re.DOTALL,
)


class DocumentError(ValueError):
    pass


# ---------------------------------------------------------------- 텍스트 추출


def _strip_markup(raw: str) -> str:
    """태그를 걷어내되 표의 행·칸 구분은 남긴다.

    재무 표를 공백 하나로 이어 붙이면 어느 숫자가 어느 항목인지 알 수 없게
    된다. 행과 칸의 경계만 살려도 읽을 수 있는 표가 된다.
    """
    text = re.sub(
        r"<(script|style)\b.*?</\1>", " ", raw, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"</(?:tr|p|div|h[1-6]|li|table)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:td|th)\s*>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # 태그를 걷어낸 뒤에 푼다. 먼저 풀면 본문에 적힌 &lt;script&gt; 가 진짜
    # 태그가 되어 그 다음 단계에서 사라진다.
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _payload_text(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """저장된 payload에서 검색 대상 본문과 구성 목록을 만든다."""
    parts: list[dict[str, Any]] = []
    chunks: list[str] = []
    documents = payload.get("documents")
    if isinstance(documents, dict):
        for name in sorted(documents):
            body = documents[name]
            if not isinstance(body, str):
                continue
            text = _strip_markup(body)
            parts.append({"name": name, "chars": len(text)})
            chunks.append(f"[[문서 {name}]]\n{text}")
        return "\n\n".join(chunks), parts
    body = payload.get("body")
    if isinstance(body, str):
        text = _strip_markup(body)
        parts.append({"name": payload.get("final_url") or "body", "chars": len(text)})
        return text, parts
    return "", parts


def _cached_text(digest: str, locator: str) -> str:
    """태그를 걷어낸 사본을 지문별로 한 번만 만든다.

    원문은 내용 주소라 절대 바뀌지 않는다. 그러나 **추출 규칙은 바뀐다** —
    표 구분을 넣거나 엔티티 처리를 고치면 같은 원문에서 다른 텍스트가 나온다.
    그래서 사본의 신원은 원문 지문만이 아니라 추출 규칙 판까지 포함한다.
    이걸 빼면 규칙을 고쳐도 낡은 사본이 계속 쓰인다.
    """
    TEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = TEXT_CACHE_DIR / f"{digest}.{TEXT_EXTRACT_VERSION}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    document = load_json(ROOT / locator)
    text, _ = _payload_text(document.get("payload") or {})
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=TEXT_CACHE_DIR, delete=False
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, cache)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return text


# ------------------------------------------------------------------- 색인


def _describe(digest: str, source: str, path: Path) -> dict[str, Any] | None:
    document = load_json(path)
    payload = document.get("payload")
    if not isinstance(payload, dict):
        return None
    text, parts = _payload_text(payload)
    if not text:
        return None
    entry = {
        "digest": digest,
        "locator": str(path.relative_to(ROOT)),
        "fetched_at": document.get("fetched_at"),
        "text_chars": len(text),
        "parts": parts,
    }
    rcept_no = payload.get("rcept_no")
    if source == "DART" and isinstance(rcept_no, str):
        entry["kind"] = "DART_DOCUMENT"
        entry["doc_id"] = f"DARTDOC:{rcept_no}"
        entry["rcept_no"] = rcept_no
        match = DOCUMENT_TITLE_RE.search(
            next(iter((payload.get("documents") or {}).values()), "")
        )
        entry["title"] = (
            re.sub(r"\s+", " ", match.group(1)).strip()[:120] if match else None
        )
        return entry
    if source == "WEB":
        match = TITLE_RE.search(payload.get("body") or "")
        entry["kind"] = "WEB_SNAPSHOT"
        entry["doc_id"] = f"WEBSNAP:{digest[:12]}"
        entry["url"] = payload.get("final_url")
        entry["title"] = (
            re.sub(r"\s+", " ", match.group(1)).strip()[:120] if match else None
        )
        return entry
    return None


def build_index(*, rebuild: bool = False) -> dict[str, Any]:
    """새로 생긴 원문만 훑어 색인을 갱신한다.

    내용 주소형 저장이라 이미 색인된 지문은 다시 열 필요가 없다. 사업보고서
    하나가 13MB라 이 증분 성질이 없으면 매 검색이 느려진다.
    """
    known: dict[str, dict[str, Any]] = {}
    if INDEX_PATH.exists() and not rebuild:
        try:
            existing = load_json(INDEX_PATH)
            # text_chars 같은 값도 추출 규칙에서 나온다. 규칙이 바뀌면 색인도
            # 다시 만든다.
            if (
                existing.get("schema_version") == INDEX_SCHEMA
                and existing.get("extract_version") == TEXT_EXTRACT_VERSION
            ):
                for entry in existing.get("documents") or []:
                    if isinstance(entry, dict) and entry.get("digest"):
                        known[entry["digest"]] = entry
        except (ContractError, OSError):
            known = {}
    seen: set[str] = set()
    for source, directory in OBJECT_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            digest = path.stem
            seen.add(digest)
            if digest in known:
                continue
            try:
                entry = _describe(digest, source, path)
            except (ContractError, OSError, ValueError):
                continue
            if entry:
                known[digest] = entry
    # 지워진 원문은 색인에서도 뺀다.
    documents = [entry for digest, entry in known.items() if digest in seen]
    documents.sort(key=lambda item: str(item.get("fetched_at") or ""), reverse=True)
    index = {
        "schema_version": INDEX_SCHEMA,
        "extract_version": TEXT_EXTRACT_VERSION,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "documents": documents,
    }
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=INDEX_PATH.parent, delete=False
        ) as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, INDEX_PATH)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return index


# --------------------------------------------------------------- 자산 연결


def _filing_rows(asset_id: str) -> list[dict[str, Any]]:
    path = KNOWLEDGE_DIR / "filings" / f"{asset_id}.json"
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except (ContractError, OSError):
        return []
    rows = data.get("filings")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _web_digests(asset_id: str) -> dict[str, dict[str, Any]]:
    path = KNOWLEDGE_DIR / "evidence" / "web" / f"{asset_id}.json"
    if not path.exists():
        return {}
    try:
        data = load_json(path)
    except (ContractError, OSError):
        return {}
    result = {}
    for item in data.get("snapshots") or []:
        if isinstance(item, dict) and isinstance(item.get("content_sha256"), str):
            result[item["content_sha256"]] = item
    return result


def fetch_hint(asset: dict[str, Any], row: dict[str, Any] | None = None) -> str:
    """이 자산의 공시 원문을 받는 방법. 시장마다 다르다.

    DART 접수번호는 OpenDART 로 받고, SEC 문서는 그냥 URL 이라 스냅샷으로 받는다.
    한쪽 방법을 양쪽에 안내하면 모델이 반드시 실패하는 명령을 시도하게 된다.

    **ETF 에는 `filing_source` 가 없다** (발행사 문서를 스냅샷으로 받으므로
    등록부에 `etf` 만 있다). 예전에는 여기서 KeyError 가 나 `cio prepare` 가
    통째로 죽었고, 보유 중인 ETF 를 판단할 수 없었다. 없으면 스냅샷 안내가 맞다.
    """
    if asset.get("filing_source") == "DART":
        return (
            "`python3.13 -m collectors.dart document <접수번호>` 로 받는다"
        )
    url = (row or {}).get("document_url") or "<document_url>"
    return (
        f"`python3.13 -m cio snapshot {url} "
        f"--asset {asset['asset_id']} --label <이름>` 로 받는다"
    )


def documents_for(asset_id: str) -> dict[str, Any]:
    """이 자산에서 검색 가능한 원문과, 색인에는 있으나 아직 받지 않은 공시."""
    asset = get_asset(asset_id)
    index = build_index()
    by_rcept = {
        entry["rcept_no"]: entry
        for entry in index["documents"]
        if entry.get("kind") == "DART_DOCUMENT"
    }
    web = _web_digests(asset_id)
    # SEC 문서는 스냅샷으로 들어오므로 접수번호가 아니라 URL 로 맞춘다.
    # 이걸 안 하면 이미 받은 공시가 계속 "안 받음" 으로 남는다.
    by_url = {}
    for entry in index["documents"]:
        if entry.get("kind") == "WEB_SNAPSHOT" and entry["digest"] in web:
            if entry.get("url"):
                by_url[entry["url"]] = entry
    available: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    linked_digests: set[str] = set()
    for row in _filing_rows(asset_id):
        accession = row.get("accession_number")
        entry = by_rcept.get(accession) or by_url.get(row.get("document_url"))
        if entry:
            linked_digests.add(entry["digest"])
            available.append({
                **entry,
                "description": row.get("description"),
                "filing_date": row.get("filing_date"),
                "form": row.get("form"),
            })
        else:
            missing.append({
                "accession_number": accession,
                "form": row.get("form"),
                "filing_date": row.get("filing_date"),
                "description": row.get("description"),
                "document_url": row.get("document_url"),
                "fetch_with": fetch_hint(asset, row),
            })
    for entry in index["documents"]:
        if entry.get("kind") != "WEB_SNAPSHOT" or entry["digest"] in linked_digests:
            continue
        record = web.get(entry["digest"])
        if record:
            available.append({
                **entry,
                "description": record.get("label"),
                "filing_date": str(record.get("fetched_at") or "")[:10],
                "form": "WEB_SNAPSHOT",
            })
    available.sort(key=lambda item: str(item.get("filing_date") or ""), reverse=True)
    return {
        "asset_id": asset_id,
        "available": available,
        "missing": missing,
        "fetch_hint": fetch_hint(asset),
    }


# ------------------------------------------------------------------- 검색


def compile_pattern(pattern: str, *, fixed: bool = False) -> tuple[re.Pattern, bool]:
    """정규식으로 읽되 깨지면 문자 그대로 찾는다.

    '연결재무제표기준영업(잠정)실적' 처럼 괄호가 든 공시 이름을 그대로 넘기는
    일이 흔하다. 그때 정규식 오류로 실패시키는 대신 리터럴로 처리하고, 그렇게
    했다는 사실을 결과에 남긴다.
    """
    if not pattern or not pattern.strip():
        raise DocumentError("검색어가 비어 있다")
    if not fixed:
        try:
            return re.compile(pattern, re.IGNORECASE | re.MULTILINE), False
        except re.error:
            pass
    return re.compile(re.escape(pattern), re.IGNORECASE | re.MULTILINE), True


METACHARACTERS = set(".^$*+?{}[]\\|()")


def _run(
    compiled: re.Pattern,
    documents: list[dict[str, Any]],
    *,
    context_chars: int,
    max_matches: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    hits: list[dict[str, Any]] = []
    total_chars = 0
    total_found = 0
    truncated = False
    for entry in documents:
        try:
            text = _cached_text(entry["digest"], entry["locator"])
        except (ContractError, OSError):
            continue
        matches = list(compiled.finditer(text))
        total_found += len(matches)
        for match in matches:
            if len(hits) >= max_matches or total_chars >= MAX_TOTAL_EXCERPT_CHARS:
                truncated = True
                break
            start = max(0, match.start() - context_chars)
            end = min(len(text), match.end() + context_chars)
            excerpt = text[start:end].strip()
            total_chars += len(excerpt)
            hits.append({
                "doc_id": entry["doc_id"],
                "title": entry.get("title") or entry.get("description"),
                "filing_date": entry.get("filing_date"),
                "form": entry.get("form"),
                "offset": match.start(),
                "matched": match.group(0)[:120],
                "excerpt": excerpt,
                "locator": entry["locator"],
            })
        if truncated:
            break
    return hits, total_found, truncated


def search_asset(
    asset_id: str,
    pattern: str,
    *,
    context_chars: int = DEFAULT_CONTEXT_CHARS,
    max_matches: int = DEFAULT_MAX_MATCHES,
    fixed: bool = False,
    doc_id: str | None = None,
) -> dict[str, Any]:
    compiled, fell_back = compile_pattern(pattern, fixed=fixed)
    catalog = documents_for(asset_id)
    documents = catalog["available"]
    if doc_id:
        documents = [item for item in documents if item["doc_id"] == doc_id]
        if not documents:
            raise DocumentError(
                f"{asset_id}에 {doc_id} 원문이 없다. "
                "`python3.13 -m cio docs <asset_id>`로 확인한다"
            )
    hits, total_found, truncated = _run(
        compiled,
        documents,
        context_chars=context_chars,
        max_matches=max_matches,
    )
    # '연결재무제표기준영업(잠정)실적' 처럼 괄호가 든 공시 이름은 정규식으로도
    # 유효해서 오류가 나지 않는다. 대신 (잠정)을 그룹으로 읽어 엉뚱하게 0건이
    # 된다. 한국 공시 이름은 괄호투성이라 이 침묵하는 0건이 가장 흔한 함정이다.
    if (
        total_found == 0
        and not fixed
        and not fell_back
        and any(char in METACHARACTERS for char in pattern)
    ):
        literal, _ = compile_pattern(pattern, fixed=True)
        hits, total_found, truncated = _run(
            literal,
            documents,
            context_chars=context_chars,
            max_matches=max_matches,
        )
        if total_found:
            fell_back = True
    return {
        "asset_id": asset_id,
        "pattern": pattern,
        "treated_as_literal": fell_back,
        "searched_documents": len(documents),
        "total_matches": total_found,
        "returned": len(hits),
        "truncated": truncated,
        "hits": hits,
        "not_fetched": catalog["missing"][:8],
        "not_fetched_total": len(catalog["missing"]),
        "_note": (
            "발췌는 태그를 걷어낸 사본에서 잘라낸 것이다. 숫자가 결론을 좌우하면 "
            "locator의 원문을 직접 확인한다. not_fetched는 공시 색인에 있으나 "
            f"아직 받지 않은 문서이며 {catalog['fetch_hint']}"
        ),
    }


MAX_CONTEXT_DOCUMENTS = 6


def documents_for_context(asset_id: str) -> dict[str, Any] | None:
    """packet 에 실을 원문 목록. 본문은 넣지 않고 '무엇을 찾을 수 있는지'만 알린다.

    사업보고서 하나가 100만 자다. packet 에 넣을 수도 없고 넣어서도 안 된다.
    모델이 알아야 하는 것은 내용이 아니라 **여기에 있으니 찾아보라**는 사실이다.
    """
    try:
        catalog = documents_for(asset_id)
    except (ContractError, ValueError, OSError):
        return None
    available = catalog["available"]
    if not available and not catalog["missing"]:
        return None
    return {
        "searchable": [
            _context_entry(entry) for entry in available[:MAX_CONTEXT_DOCUMENTS]
        ],
        "searchable_total": len(available),
        "not_fetched_total": len(catalog["missing"]),
        "_role": (
            "로컬에 보존된 원문이다. 웹 스냅샷과 공시 원문이 한 목록에 있으며 "
            "url 이 있으면 네가 이전에 보존한 웹 원문이다 — 같은 것을 다시 "
            "검색하지 말고 유효한지만 확인하며, 인용할 때 그 url 을 locator 로 "
            "쓴다. 본문은 packet 에 없다(사업보고서 하나가 100만 자다). 필요한 "
            "자리만 `python3.13 -m cio search <asset_id> <검색어>` 로 찾는다. "
            "부문별 매출, 주요 매출처, 수주잔고, 생산능력, 원재료 공급처처럼 "
            "회사 구조에 관한 사실은 웹을 검색하기 전에 여기서 먼저 찾는다. "
            "not_fetched_total 은 공시 색인에는 있으나 아직 원문을 받지 않은 "
            "문서 수이며 `python3.13 -m cio docs <asset_id>` 로 목록을 본다"
        ),
    }


def _context_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """packet 한 줄. 웹 스냅샷이면 원본 url 까지 싣는다.

    예전에는 웹 스냅샷이 `web_snapshots` 블록에 한 번 더 실렸다. 같은 문서가
    두 목록에 나오면서 `source_id`/`doc_id` 와 `label`/`description` 이 매 판단마다
    두 번씩 전송됐다. url 만 여기로 흡수하면 그 블록이 통째로 필요 없어진다.
    """
    row = {
        "doc_id": entry["doc_id"],
        "filing_date": entry.get("filing_date"),
        "description": entry.get("description") or entry.get("title"),
        "chars": entry["text_chars"],
    }
    if entry.get("url"):
        row["url"] = entry["url"]
    return row


def format_search(result: dict[str, Any]) -> str:
    lines = [
        f"{result['asset_id']}  '{result['pattern']}'  "
        f"문서 {result['searched_documents']}건에서 {result['total_matches']}건 일치"
        + ("  (정규식이 아니라 문자 그대로 찾았다)" if result["treated_as_literal"] else "")
    ]
    if result["truncated"]:
        lines.append(f"※ {result['returned']}건만 표시했다. 검색어를 좁히면 더 정확해진다")
    lines.append("")
    for hit in result["hits"]:
        head = f"{hit['doc_id']}  {hit.get('filing_date') or ''}  {hit.get('title') or ''}"
        lines.append(head.strip())
        lines.append(f"  …{hit['excerpt']}…")
        lines.append("")
    if result["not_fetched"]:
        lines.append(
            f"아직 받지 않은 공시 {result['not_fetched_total']}건 중 최근 "
            f"{len(result['not_fetched'])}건:"
        )
        for item in result["not_fetched"]:
            lines.append(
                f"  {item['accession_number']}  {item.get('filing_date') or ''}  "
                f"{item.get('description') or ''}"
            )
    return "\n".join(lines)


def format_documents(catalog: dict[str, Any]) -> str:
    lines = [f"{catalog['asset_id']} 검색 가능한 원문 {len(catalog['available'])}건", ""]
    for entry in catalog["available"]:
        lines.append(
            f"  {entry['doc_id']:<28} {entry.get('filing_date') or '':<12}"
            f"{entry['text_chars']:>9,}자  {entry.get('description') or entry.get('title') or ''}"
        )
    if catalog["missing"]:
        lines.append("")
        lines.append(f"색인에는 있으나 받지 않은 공시 {len(catalog['missing'])}건:")
        for item in catalog["missing"][:15]:
            lines.append(
                f"  {item['accession_number']}  {item.get('filing_date') or '':<12}"
                f"{item.get('description') or ''}"
            )
        lines.append("")
        lines.append(f"받는 법: {catalog['fetch_hint']}")
    return "\n".join(lines)


__all__ = [
    "DocumentError",
    "build_index",
    "compile_pattern",
    "documents_for",
    "format_documents",
    "format_search",
    "search_asset",
]
