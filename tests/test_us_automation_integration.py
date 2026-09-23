"""US original-source availability must survive registration, refresh and packet wiring."""

from __future__ import annotations

from copy import deepcopy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cio.assets import AssetRegistryError, get_asset, validate_asset
from cio.context import _data_coverage, _us_automated_summaries
from cio.refresh import (RefreshTask, _recent_us_earnings_failure, inspect_task,
                         run_refresh, tasks_for)


class TestIrRegistrationContract(unittest.TestCase):
    def test_registry_accepts_only_urls_the_earnings_collector_accepts(self):
        asset = deepcopy(get_asset("IBM"))
        asset["ir_sources"] = [f"https://ir.example.com/events/{i}" for i in range(6)]
        validate_asset("IBM", asset)

        asset["ir_sources"].append("https://ir.example.com/events/6")
        with self.assertRaises(AssetRegistryError):
            validate_asset("IBM", asset)

        asset["ir_sources"] = ["https://ir.example.com:8443/events"]
        with self.assertRaises(AssetRegistryError):
            validate_asset("IBM", asset)

    def test_unregistered_ir_is_not_scheduled_as_a_failing_fetch(self):
        asset = deepcopy(get_asset("IBM"))
        asset.pop("ir_sources")
        with patch("cio.refresh.get_asset", return_value=asset):
            task = next(task for task in tasks_for("IBM") if task.capability == "US_EARNINGS")
        self.assertIsNone(task.command)
        self.assertEqual(inspect_task(task).state, "NOT_REGISTERED")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("cio.refresh.tasks_for", return_value=[task]), patch("cio.refresh.DATA_DIR", Path(tmp)):
                results, exit_code = run_refresh("IBM")
        self.assertEqual(exit_code, 0)
        self.assertEqual(results[0]["status"], "NOT_REGISTERED")

    def test_packet_ignores_old_ir_artifact_after_registration_is_removed(self):
        asset = deepcopy(get_asset("IBM"))
        asset.pop("ir_sources")
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "us_earnings" / "IBM.json"
            old.parent.mkdir()
            old.write_text("{}", encoding="utf-8")
            with patch("cio.context.KNOWLEDGE_DIR", Path(tmp)):
                summaries, sources = _us_automated_summaries(asset, refresh_tasks=[])
        self.assertEqual(summaries["us_earnings"]["state"], "NOT_REGISTERED")
        self.assertFalse(any(source["source_id"] == "US_EARNINGS:IBM" for source in sources))
        self.assertEqual(_data_coverage(asset, structured_optional=summaries)["earnings_announcement"]["state"], "NOT_REGISTERED")


class TestRefreshStateInPacket(unittest.TestCase):
    def test_new_filings_make_old_capital_summary_stale_in_packet(self):
        asset = get_asset("RXRX")
        task = SimpleNamespace(capability="US_CAPITAL", state="STALE", reason="입력 산출물보다 오래됐다")
        with tempfile.TemporaryDirectory() as tmp:
            capital = Path(tmp) / "us_capital" / "RXRX.json"
            capital.parent.mkdir()
            capital.write_text(json.dumps({"collected_at": "2026-09-23T00:00:00+00:00"}), encoding="utf-8")
            with patch("cio.context.KNOWLEDGE_DIR", Path(tmp)), patch("cio.context.ROOT", Path(tmp)), \
                 patch("collectors.us_capital.validate_artifact", return_value=[]), \
                 patch("collectors.us_capital.summary", return_value={"state": "AVAILABLE", "period_end_shares": {"value": 100}}):
                summaries, _ = _us_automated_summaries(asset, refresh_tasks=[task])
        capital_summary = summaries["us_capital"]
        self.assertEqual(capital_summary["state"], "STALE")
        self.assertEqual(capital_summary["source_state"], "AVAILABLE")
        self.assertFalse(capital_summary["available"])
        self.assertEqual(_data_coverage(asset, structured_optional=summaries)["dilution"]["state"], "STALE")


class TestRecentIrFailureRetry(unittest.TestCase):
    URL = "https://www.ibm.com/investor/events"
    COLLECTED = dt.datetime(2026, 9, 23, 14, 0, tzinfo=dt.timezone.utc)

    def _artifact(self, root, *, collected_at=None, as_of_date="2026-09-23", configured_sources=None,
                  status="NETWORK_ERROR"):
        path = root / "knowledge/us_earnings/IBM.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "asset_id": "IBM", "status": status,
            "collected_at": collected_at or self.COLLECTED.isoformat(),
            "as_of_date": as_of_date,
            "configured_sources": configured_sources if configured_sources is not None else [self.URL],
        }), encoding="utf-8")
        return RefreshTask(name="earnings_announced:IBM", capability="US_EARNINGS",
                           artifact="knowledge/us_earnings/IBM.json", max_age_hours=24,
                           command=["collector"], required=False, state="STALE",
                           resolved_path="knowledge/us_earnings/IBM.json",
                           reason="Official IR request timed out")

    def test_cooldown_requires_valid_current_registration_and_times(self):
        now = self.COLLECTED + dt.timedelta(minutes=30)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self._artifact(root)
            with patch("cio.refresh.ROOT", root), \
                 patch("cio.refresh.get_asset", return_value={"ir_sources": [self.URL]}), \
                 patch("collectors.us_earnings.validate_artifact", return_value=[]):
                self.assertEqual(_recent_us_earnings_failure(task, now=now),
                                 self.COLLECTED + dt.timedelta(hours=1))
                self._artifact(root, status="PARSE_ERROR")
                self.assertEqual(_recent_us_earnings_failure(task, now=now),
                                 self.COLLECTED + dt.timedelta(hours=1))
                self._artifact(root)
                self.assertIsNone(_recent_us_earnings_failure(
                    task, now=self.COLLECTED + dt.timedelta(hours=1)))
                self._artifact(root, configured_sources=["https://www.ibm.com/investor/other"])
                self.assertIsNone(_recent_us_earnings_failure(task, now=now))
                self._artifact(root, collected_at="2026-09-23T14:00:00", as_of_date="2026-09-23")
                self.assertIsNone(_recent_us_earnings_failure(task, now=now))
                self._artifact(root, as_of_date="2026-09-22")
                self.assertIsNone(_recent_us_earnings_failure(task, now=now))
                self._artifact(root)
                with patch("collectors.us_earnings.validate_artifact", return_value=["invalid source"]):
                    self.assertIsNone(_recent_us_earnings_failure(task, now=now))

    def test_default_refresh_skips_recent_failure_but_force_runs(self):
        task = RefreshTask(name="earnings_announced:IBM", capability="US_EARNINGS",
                           artifact="knowledge/us_earnings/IBM.json", max_age_hours=24,
                           command=["collector"], required=False, state="STALE",
                           resolved_path="knowledge/us_earnings/IBM.json", reason="IR timeout")
        retry_after = self.COLLECTED + dt.timedelta(hours=1)
        with tempfile.TemporaryDirectory() as tmp, \
             patch("cio.refresh.ROOT", Path(tmp)), \
             patch("cio.refresh.DATA_DIR", Path(tmp)), \
             patch("cio.refresh.tasks_for", return_value=[task]), \
             patch("cio.refresh.inspect_task", return_value=task), \
             patch("cio.refresh._recent_us_earnings_failure", return_value=retry_after) as cooldown, \
             patch("cio.refresh.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="")) as command:
            results, exit_code = run_refresh("IBM")
            self.assertEqual(exit_code, 0)
            self.assertEqual(results[0]["status"], "SKIPPED_RECENT_FAILURE")
            self.assertEqual(results[0]["reason"], "IR timeout")
            command.assert_not_called()

            results, exit_code = run_refresh("IBM", force=True)
            self.assertEqual(exit_code, 0)
            self.assertEqual(results[0]["status"], "FAILED")
            command.assert_called_once()
            self.assertEqual(cooldown.call_count, 1)


if __name__ == "__main__":
    unittest.main()
