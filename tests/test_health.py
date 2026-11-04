"""Health reports remain read-only and useful with missing or broken components."""

from unittest.mock import patch

from test_installer import BaseTest
from ptero.core import write_json
from ptero.health import inspect


class HealthStateTests(BaseTest):
    def test_fresh_host_reports_next_step_without_service_commands(self):
        rows = inspect(self.installer)
        self.assertTrue(any('Host checks are available' in row for row in rows))
        self.assertEqual(self.runner.calls, [])

    def test_corrupt_panel_does_not_hide_working_wings(self):
        self.paths.state.parent.mkdir(parents=True)
        self.paths.state.write_text('{invalid-json')
        self.wings()
        rows = inspect(self.installer)
        self.assertTrue(any('Needs attention: Panel' in row for row in rows))
        self.assertIn('Healthy: Wings', rows)
        self.assertFalse(any(args[0] in ('docker', 'php', 'runuser') for args, _ in self.runner.calls))

    def test_foreign_panel_is_not_contacted_or_adopted(self):
        foreign = {**self.state, 'owner': 'another-tool', 'url': 'https://foreign.example.test'}
        write_json(self.paths.state, foreign)
        original = self.paths.state.read_bytes()
        with patch('ptero.health.urllib.request.urlopen', side_effect=AssertionError('foreign network access')):
            rows = inspect(self.installer)
        self.assertTrue(any('Needs attention: Panel' in row for row in rows))
        self.assertEqual(self.paths.state.read_bytes(), original)
        self.assertEqual(self.runner.calls, [])

    def test_corrupt_wings_still_produces_host_checks(self):
        self.paths.wings_state.parent.mkdir(parents=True, exist_ok=True)
        self.paths.wings_state.write_text('[]')
        rows = inspect(self.installer)
        self.assertTrue(any('Needs attention: Wings' in row for row in rows))
        self.assertTrue(any('Disk space' in row for row in rows))
        self.assertEqual(self.runner.calls, [])

    def test_malformed_phase_metadata_does_not_hide_wings_health(self):
        self.wings()
        state = self.installer.load(self.paths.wings_state)
        state['phases'] = {'configuration': 'invalid'}
        write_json(self.paths.wings_state, state)
        rows = inspect(self.installer)
        self.assertIn('Healthy: Wings', rows)
        self.assertIn('Saved installation phases need attention.', rows)
