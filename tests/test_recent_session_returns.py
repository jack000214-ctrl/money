import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import helpers  # noqa: F401
from cio.context import ContextError, _price_summary, _prompt_version


class TestRecentSessionReturns(unittest.TestCase):
    def test_prompt_version_is_read_from_canonical_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'prompt.md'
            path.write_text('# CIO prompt v2.10\n규칙\n')
            self.assertEqual(_prompt_version(path), '2.10')
            path.write_text('# Missing version\n')
            with self.assertRaises(ContextError):
                _prompt_version(path)

    def summary(self, state):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'prices' / 'TEST.csv'
            path.parent.mkdir()
            path.write_text(
                '# schema_version price-series/2.1.0\n# symbol TEST / currency USD\n'
                '# retrieved_at 2026-09-21T21:00:00+00:00 / source fixture\n'
                f'# last_bar 2026-09-21 / session {state}\n'
                'date,close,adjusted_close,split_adjustment_factor,high,low,volume\n'
                '2026-09-16,100,100,1,100,100,1\n'
                '2026-09-17,110,110,1,110,110,1\n'
                '2026-09-18,121,121,1,121,121,1\n'
                '2026-09-21,130,133.1,1,130,130,1\n')
            with patch('cio.context.KNOWLEDGE_DIR', root), patch('cio.context.ROOT', root):
                return _price_summary('TEST')[0]['settled_session_returns']

    def test_weekend_uses_sessions_and_adjusted_returns(self):
        result = self.summary('SETTLED')
        self.assertEqual(result['windows']['2'], {
            'from_date': '2026-09-17', 'through_date': '2026-09-21', 'return_pct': 21.0})
        self.assertEqual(result['windows']['3']['return_pct'], 33.1)
        self.assertIsNone(result['excluded_latest_bar'])

    def test_unknown_or_unfinished_latest_bar_is_excluded(self):
        for state in ['UNSETTLED', 'UNKNOWN']:
            with self.subTest(state=state):
                result = self.summary(state)
                self.assertEqual(result['windows']['2']['through_date'], '2026-09-18')
                self.assertEqual(result['windows']['2']['return_pct'], 21.0)
                self.assertNotIn('3', result['windows'])
                self.assertEqual(result['excluded_latest_bar'], '2026-09-21')


if __name__ == '__main__':
    unittest.main()
