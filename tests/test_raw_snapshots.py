"""DART·EDGAR 원본 스냅샷의 내용 해시 회귀 테스트."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from calculators.contracts import ContractError
from collectors import dart, edgar


def expected_hash(payload):
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestRawSnapshots(unittest.TestCase):
    def test_collectors_store_payload_sha256(self):
        payload = {"list": [{"amount": "123", "name": "매출"}], "status": "000"}
        for module in (dart, edgar):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                original = module.RAW_DIR
                module.RAW_DIR = Path(tmp)  # pyright: ignore[reportAttributeAccessIssue]
                try:
                    path = module.save_raw("fixture", payload)
                    again = module.save_raw("another_name", payload)
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(saved["payload"], payload)
                    self.assertEqual(saved["payload_sha256"], expected_hash(payload))
                    self.assertEqual(path, again)
                    self.assertEqual(len(list(Path(tmp).rglob("*.json"))), 1)
                    if module is edgar:
                        saved["payload"] = {"tampered": True}
                        path.write_text(json.dumps(saved), encoding="utf-8")
                        with self.assertRaises(ContractError):
                            module.save_raw("fixture", payload)
                finally:
                    module.RAW_DIR = original  # pyright: ignore[reportAttributeAccessIssue]


if __name__ == "__main__":
    unittest.main()
