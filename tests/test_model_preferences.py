import unittest
from codex_model_router.router import (
    ModelCatalog, FALLBACK_MODELS, configure_catalog, model_fast_setting,
    _catalog_from_payload,
)


class ModelPreferencesTests(unittest.TestCase):
    def test_native_preference_and_speed(self):
        base = ModelCatalog(dict(FALLBACK_MODELS))
        models = {m: list(e) for m, e in base.models.items()}
        configured = configure_catalog(base, {'routing': {
            'models': models, 'preferred_models': list(models)}})
        from codex_model_router.dashboard import _model_family
        self.assertEqual(_model_family(configured.preferred("astra")), "astra")
        self.assertEqual(set(configured.candidate_models()), set(models))
        self.assertEqual(configured.preferred('astra'), 'gpt-6-astra')
        speeds = {'model_speed': {'default': True, 'astra': False, 'sol': False}}
        for family in ('luna', 'sol', 'astra'):
            self.assertEqual(model_fast_setting(configured.preferred(family), speeds),
                             family == 'luna')
        self.assertEqual(configure_catalog(base, {}).candidate_models(), base.candidate_models())

    def test_partial_preference_keeps_other_families(self):
        base = ModelCatalog(dict(FALLBACK_MODELS))
        configured = configure_catalog(base, {'routing': {
            'models': {'custom-sol': ['low', 'medium']},
            'preferred_models': ['custom-sol'],
        }})
        self.assertEqual(configured.candidate_models(), [
            'gpt-6-luna', 'custom-sol', 'gpt-6-astra',
        ])

    def test_catalog_rejects_non_native_or_legacy_models(self):
        with self.assertRaises(ValueError):
            _catalog_from_payload({'models': [
            {'slug': 'oxygen/GPT-6-' + family + '-joybuilder',
             'supported_reasoning_levels': [{'effort': 'medium'}]}
            for family in ('Luna', 'Sol', 'Astra')]})
        with self.assertRaises(ValueError):
            _catalog_from_payload({'models': [
                {'slug': 'gpt-5.6-' + family,
                 'supported_reasoning_levels': [{'effort': 'medium'}]}
                for family in ('luna', 'sol', 'astra')]})

    def test_invalid_configuration(self):
        for routing in ({'models': {'other': ['medium']}},
                        {'preferred_models': ['missing']},
                        {'models': {'gpt-6-astra': ['invalid']}},
                        {'models': []}):
            with self.assertRaises(ValueError):
                configure_catalog(ModelCatalog(dict(FALLBACK_MODELS)), {'routing': routing})

class BackendIsolationTests(unittest.TestCase):
    def test_parent_pool_filters_before_selection(self):
        from codex_model_router.router import catalog_for_parent
        base = dict(FALLBACK_MODELS)
        api = {'oxygen/' + m + '-joybuilder': e for m, e in base.items()}
        all_models = ModelCatalog(dict(base, **api), preferred_models=tuple(api))
        self.assertEqual(set(catalog_for_parent(all_models, 'gpt-6-astra').candidate_models()), set(base))
        self.assertEqual(set(catalog_for_parent(all_models, 'oxygen/GPT-6-Astra-joybuilder').candidate_models()), set(api))
        self.assertIsNone(catalog_for_parent(all_models, 'another/model'))

    def test_latest_turn_not_session_provider(self):
        import tempfile, json
        from pathlib import Path
        from codex_model_router.router import parent_model_from_hook
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'session.jsonl'
            p.write_text('\n'.join(json.dumps(r) for r in [
                {'type': 'session_meta', 'payload': {'model_provider': 'openai'}},
                {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}},
                {'type': 'turn_context', 'payload': {'model': 'oxygen/GPT-6-Astra-joybuilder'}},
                {'type': 'event_msg', 'payload': {'text': 'x' * 150000}},
            ]))
            self.assertEqual(parent_model_from_hook({'transcript_path': str(p)}),
                             'oxygen/GPT-6-Astra-joybuilder')

    def test_unknown_parent_inherits_and_strips_override(self):
        from unittest.mock import patch
        from codex_model_router.router import run_hook
        with patch('codex_model_router.router.parent_model_from_hook', return_value=None), patch('codex_model_router.router.route_task') as route:
            result = run_hook({'tool_name': 'spawn_agent', 'tool_input': {
                'message': 'test', 'model': 'oxygen/GPT-6-Astra-joybuilder',
                'reasoning_effort': 'high', 'fork_turns': 'all'}}, no_log=True)
            updated = result['hookSpecificOutput']['updatedInput']
            self.assertNotIn('model', updated)
            self.assertNotIn('reasoning_effort', updated)
            self.assertEqual(updated['fork_turns'], 'all')
            route.assert_not_called()

    def test_hook_routes_only_parent_backend(self):
        from unittest.mock import patch
        from codex_model_router.router import run_hook
        base = dict(FALLBACK_MODELS)
        api = {'oxygen/' + m + '-joybuilder': e for m, e in base.items()}
        catalog = ModelCatalog(dict(base, **api), preferred_models=tuple(api))
        for parent, expected_prefix in [('gpt-6-astra', 'gpt-'), ('oxygen/GPT-6-Astra-joybuilder', 'oxygen/')]:
            with patch('codex_model_router.router.parent_model_from_hook', return_value=parent), patch('codex_model_router.router.discover_catalog', return_value=catalog):
                result = run_hook({'tool_name':'spawn_agent', 'tool_input':{'message':'Compute 2+2'}}, heuristic_only=True, no_log=True)
                self.assertTrue(result['hookSpecificOutput']['updatedInput']['model'].startswith(expected_prefix))
