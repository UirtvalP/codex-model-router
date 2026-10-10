import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_model_router import cli, router as r


class PreSpawnReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.catalog = r.ModelCatalog(dict(r.FALLBACK_MODELS), 'test')
        self.task = 'Explain the private bounded request'
        self.name = 'route_check'
        self.session = 'parent-session'
        self.config = {}
        for mocker in (
            patch.object(r, '_pre_spawn_directory', return_value=self.directory / 'cache'),
            patch.object(r, 'default_log_path', return_value=self.directory / 'decisions.jsonl'),
            patch.object(r, '_load_router_config', side_effect=lambda *args: self.config),
            patch.object(r, 'discover_catalog', side_effect=lambda *args: self.catalog),
            patch.object(cli, 'discover_catalog', side_effect=lambda *args: self.catalog),
            patch.object(r, 'parent_model_from_hook', return_value='gpt-6-sol'),
            patch.object(cli, 'find_codex_executable', return_value='codex'),
        ):
            mocker.start()
            self.addCleanup(mocker.stop)
        choice = r.classify_heuristically(self.task, self.catalog)
        choice.source = 'codex-evaluator'
        choice.classifier_ms = 10
        choice.reason = self.task
        choice.evaluator_reason = self.task
        self.classifier = patch.object(r, 'classify_with_codex', return_value=choice).start()
        self.addCleanup(patch.stopall)

    def preselect(self, *extras):
        with redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(cli.main(['--spawn-route', '--session-id', self.session,
                                      '--task-name', self.name, '--cd', str(self.directory),
                                      '--log-path', str(self.directory / 'decisions.jsonl'),
                                      '--prompt', self.task, *extras]), 0)
        return json.loads(stream.getvalue())

    def payload(self, selected):
        return {'tool_name': 'spawn_agent', 'session_id': self.session,
                'cwd': str(self.directory), 'tool_input': {
                    'message': self.task, **selected['spawn_input']}}

    def test_plaintext_preselect_and_hook_evaluate_once_then_consume(self):
        selected = self.preselect()
        result = r.run_hook(self.payload(selected))['hookSpecificOutput']['updatedInput']
        self.assertEqual(result['model'], selected['model'])
        self.assertEqual(self.classifier.call_count, 1)
        records = [json.loads(line) for line in (self.directory / 'decisions.jsonl').read_text().splitlines()]
        self.assertEqual([row['event'] for row in records], ['route_decision', 'route_reused'])
        self.assertEqual(records[-1]['decision_id'], selected['decision_id'])
        r.run_hook(self.payload(selected), no_log=True)
        self.assertEqual(self.classifier.call_count, 2)

    def test_binding_changes_always_evaluate_again(self):
        cases = ('task', 'session', 'name', 'cwd', 'config', 'catalog', 'catalog_order', 'model', 'effort', 'timeout')
        for case in cases:
            with self.subTest(case=case):
                self.config = {}
                self.catalog = r.ModelCatalog(dict(r.FALLBACK_MODELS), 'test')
                selected = self.preselect()
                payload = self.payload(selected)
                kwargs = {'no_log': True}
                if case == 'task': payload['tool_input']['message'] += ' different'
                if case == 'session': payload['session_id'] += '-other'
                if case == 'name': payload['tool_input']['task_name'] += '_other'
                if case == 'cwd': payload['cwd'] = str(self.directory / 'different')
                if case == 'config': self.config = {'model_speed': {'sol': False}}
                if case == 'catalog': self.catalog.models['gpt-6-sol'] = ('low', 'medium', 'high')
                if case == 'catalog_order': self.catalog = r.ModelCatalog(dict(reversed(list(self.catalog.models.items()))), 'test')
                if case == 'model': payload['tool_input']['model'] = 'gpt-6-sol'
                if case == 'effort': payload['tool_input']['reasoning_effort'] = 'high'
                if case == 'timeout': kwargs['classifier_timeout_seconds'] = 25
                with patch.object(r, 'discover_catalog', side_effect=lambda *args: self.catalog):
                    before = self.classifier.call_count
                    r.run_hook(payload, **kwargs)
                    self.assertEqual(self.classifier.call_count, before + 1)

    def test_encrypted_path_preserves_selection_without_evaluation(self):
        selected = self.preselect()
        payload = self.payload(selected)
        payload['tool_input']['message'] = 'gAAAA' + 'A' * 80
        result = r.run_hook(payload, no_log=True)['hookSpecificOutput']['updatedInput']
        self.assertEqual(result['model'], selected['model'])
        self.assertEqual(self.classifier.call_count, 1)

    def test_catalog_order_that_changes_default_model_changes_binding(self):
        first = r.ModelCatalog({'gpt-6-luna': ('low',), 'gpt-6-sol': ('medium',),
                                'gpt-6.1-sol': ('medium',), 'gpt-6-astra': ('medium',)})
        second = r.ModelCatalog(dict(reversed(list(first.models.items()))))
        self.assertNotEqual(first.preferred('sol'), second.preferred('sol'))
        self.assertNotEqual(r.pre_spawn_binding(self.task, self.session, self.name, str(self.directory), first, 30),
                            r.pre_spawn_binding(self.task, self.session, self.name, str(self.directory), second, 30))

    def test_cross_backend_cache_never_crosses_provider(self):
        selected = self.preselect()
        with patch.object(r, 'parent_model_from_hook', return_value='openai/gpt-6-sol'):
            result = r.run_hook(self.payload(selected), no_log=True)['hookSpecificOutput']['updatedInput']
        self.assertNotIn('model', result)
        self.assertEqual(self.classifier.call_count, 1)

    def test_no_log_and_failed_evaluator_do_not_cache(self):
        self.preselect('--no-log')
        self.assertFalse((self.directory / 'cache').exists())
        self.classifier.side_effect = RuntimeError('evaluation failed')
        self.preselect()
        self.assertFalse((self.directory / 'cache').exists())

    def test_missing_binding_or_missing_forwarded_fields_do_not_reuse(self):
        selected = self.preselect()
        for key, where in (('session_id', 'payload'), ('task_name', 'input'),
                           ('model', 'input'), ('reasoning_effort', 'input')):
            with self.subTest(key=key):
                payload = self.payload(selected)
                del (payload if where == 'payload' else payload['tool_input'])[key]
                before = self.classifier.call_count
                r.run_hook(payload, no_log=True)
                self.assertEqual(self.classifier.call_count, before + 1)

    def test_expired_corrupt_or_unreadable_cache_evaluates_again(self):
        for case in ('expired', 'corrupt', 'unreadable'):
            with self.subTest(case=case):
                selected = self.preselect()
                binding = r.pre_spawn_binding(self.task, self.session, self.name,
                                              str(self.directory), self.catalog, 30.0)
                path = self.directory / 'cache' / (binding + '.json')
                if case == 'expired':
                    record = json.loads(path.read_text());record['created'] -= 301
                    path.write_text(json.dumps(record))
                if case == 'corrupt': path.write_text('{')
                context = patch.object(r.os, 'replace', side_effect=OSError('denied')) if case == 'unreadable' else patch.object(r, 'PRE_SPAWN_TTL_SECONDS', 300)
                with context:
                    before = self.classifier.call_count
                    r.run_hook(self.payload(selected), no_log=True)
                    self.assertEqual(self.classifier.call_count, before + 1)

    def test_concurrent_hooks_only_one_consumes_the_preselection(self):
        selected = self.preselect()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: r.run_hook(self.payload(selected), no_log=True), range(2)))
        self.assertEqual(self.classifier.call_count, 2)
        self.assertTrue(all(row['hookSpecificOutput']['updatedInput']['model'] == selected['model'] for row in results))

    def test_cache_contains_no_prompt_or_classifier_prose(self):
        self.preselect()
        raw = next((self.directory / 'cache').glob('*.json')).read_text()
        self.assertNotIn(self.task, raw)
        if os.name != 'nt':
            path = next((self.directory / 'cache').glob('*.json'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_no_session_environment_requires_explicit_session(self):
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            cli.main(['--spawn-route', '--task-name', self.name, '--prompt', self.task,
                      '--log-path', str(self.directory / 'decisions.jsonl')])
        self.assertFalse((self.directory / 'cache').exists())
