#!/usr/bin/env python3
"""리서치 분석 JSON을 검증한 뒤 승인 저장소에 복사한다."""

import argparse
import sys

from contracts import ContractError, load_json, save_validated_analysis


def main():
    p = argparse.ArgumentParser(description="주장·출처·스냅샷 계약 검증")
    p.add_argument("input", help="검증할 분석 JSON")
    p.add_argument("--save", help="검증 성공 시 저장할 경로")
    a = p.parse_args()

    try:
        data = load_json(a.input)
        if a.save:
            path = save_validated_analysis(a.save, data)
            print(f"VALIDATED_AND_SAVED {path}")
        else:
            from contracts import require_valid_analysis
            require_valid_analysis(data)
            print("VALID")
    except (ContractError, FileNotFoundError) as exc:
        messages = exc.errors if isinstance(exc, ContractError) else [str(exc)]
        for message in messages:
            print(f"INVALID: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
