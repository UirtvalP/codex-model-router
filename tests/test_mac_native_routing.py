import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_model_router import router


class MacNativeRoutingTests(unittest.TestCase):
    def test_minor_version_catalog_survives_cache_and_exclusion(self):
        payload = {'models': [
            {'slug': model, 'supported_reasoning_levels': [{'effort': 'medium'}]}
            for model in ('gpt-6-luna', 'gpt-6-sol', 'gpt-6.1-sol', 'gpt-6-astra',
                          'openai/gpt-6.1-sol', 'gpt-5.6-sol')
        ]}
        catalog = router._catalog_from_payload(payload)
        self.assertNotIn('openai/gpt-6.1-sol', catalog.models)
        self.assertNotIn('gpt-5.6-sol', catalog.models)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            router._write_catalog_cache(path, catalog)
            cached = router._read_catalog_cache(path)
        configured = router.configure_catalog(cached, {'routing': {
            'excluded_models': ['gpt-6-sol']}})
        native = router.catalog_for_parent(configured, 'gpt-6.1-sol')
        self.assertEqual(native.preferred('sol'), 'gpt-6.1-sol')
        self.assertNotIn('gpt-6-sol', native.candidate_models())
        self.assertIsNone(router.catalog_for_parent(configured, 'openai/gpt-6.1-sol'))

    def test_invalid_exclusions_are_rejected(self):
        catalog = router.ModelCatalog(dict(router.FALLBACK_MODELS))
        for excluded in ('gpt-6-sol', [None]):
            with self.subTest(excluded=excluded), self.assertRaises(ValueError):
                router.configure_catalog(catalog, {'routing': {'excluded_models': excluded}})

    def test_desktop_runtime_precedes_stale_path_on_mac(self):
        with patch.object(router.sys, 'platform', 'darwin'), patch.object(Path, 'is_file', return_value=True), patch.object(router.os, 'access', return_value=True), patch.object(router.shutil, 'which') as lookup:
            self.assertEqual(router.find_codex_executable(), str(Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex')))
            lookup.assert_not_called()

    def test_missing_desktop_runtime_keeps_path_fallback(self):
        with patch.object(router.sys, 'platform', 'darwin'), patch.object(Path, 'is_file', return_value=False), patch.object(router.shutil, 'which', return_value='/bin/codex'):
            self.assertEqual(router.find_codex_executable(), '/bin/codex')

    def test_incomplete_backend_is_logged_as_skip_without_prompt(self):
        catalog = router.ModelCatalog({'gpt-6-luna': ('low',), 'gpt-6-astra': ('medium',), 'openai/gpt-6.1-sol': ('medium',)})
        payload = {'tool_name': 'spawn_agent', 'session_id': 'mac-test', 'tool_input': {
            'message': 'Private request must never be logged', 'model': 'gpt-6-luna',
            'reasoning_effort': 'low', 'task_name': 'native_check', 'fork_turns': 'none'}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'decisions.jsonl'
            with patch.object(router, 'parent_model_from_hook', return_value='gpt-6.1-sol'), patch.object(router, 'discover_catalog', return_value=catalog), patch.object(router, 'route_task') as route, patch.object(router, 'default_log_path', return_value=path):
                result = router.run_hook(payload)['hookSpecificOutput']['updatedInput']
            route.assert_not_called()
            self.assertNotIn('model', result)
            raw = path.read_text()
            self.assertNotIn(payload['tool_input']['message'], raw)
            record = json.loads(raw)
            self.assertEqual(record['event'], 'route_skipped')
            self.assertEqual(record['reason'], 'incomplete_backend_catalog')
            self.assertEqual(record['action'], 'inherit_parent')

    def test_no_log_skips_do_not_write(self):
        with patch.object(router, 'parent_model_from_hook', return_value=None), patch.object(router, 'default_log_path') as path:
            router.run_hook({'tool_name': 'spawn_agent', 'tool_input': {'message': 'Explain X'}}, no_log=True)
            path.assert_not_called()
