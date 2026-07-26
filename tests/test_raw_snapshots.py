"""DART·EDGAR 원본 스냅샷의 내용 해시 회귀 테스트."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import dart_fetch
import edgar_fetch


def expected_hash(payload):
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestRawSnapshots(unittest.TestCase):
    def test_collectors_store_payload_sha256(self):
        payload = {"list": [{"amount": "123", "name": "매출"}], "status": "000"}
        for module in (dart_fetch, edgar_fetch):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                original = module.RAW_DIR
                module.RAW_DIR = Path(tmp)
                try:
                    path = module.save_raw("fixture", payload)
                finally:
                    module.RAW_DIR = original
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["payload"], payload)
                self.assertEqual(saved["payload_sha256"], expected_hash(payload))


if __name__ == "__main__":
    unittest.main()
