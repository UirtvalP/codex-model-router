"""Future IDs/prices below are synthetic fixtures, not real model claims."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_model_router import lifecycle as life, router as r


def comparison():
    return {'old': 'gpt-6-sol', 'new': 'gpt-7-sol', 'tier': 'sol',
            'capability': {'verdict': 'strictly_better', 'reviewed_by': 'fixture reviewer',
                           'source': 'https://example.test/fixture', 'checked_at': '2020-01-01', 'valid_until': '2099-01-01'},
            'prices': {'basis': 'api_standard', 'currency': 'USD_per_million_tokens',
                       'source': 'https://example.test/fixture', 'reviewed_by': 'fixture reviewer',
                       'checked_at': '2020-01-01', 'valid_until': '2099-01-01', 'scope': 'standard API, same context band',
                       'old': {'input': 2, 'cached_input': 1, 'output': 10},
                       'new': {'input': 1, 'cached_input': 0.5, 'output': 9}}}


def policy():
    return {'lifecycle': {'cost_basis': 'api_standard', 'comparisons': [comparison()]}}


def models():
    return dict(r.FALLBACK_MODELS, **{'gpt-7-sol': r.FALLBACK_MODELS['gpt-6-sol']})


def snapshot(extra=False):
    names = ['gpt-6-luna', 'gpt-6-sol', 'gpt-6-astra'] + (['gpt-7-sol'] if extra else [])
    return {'client_version': '0.162.0', 'models': [{'slug': name, 'supported_reasoning_levels': [{'effort': 'medium'}],
                        'model_messages': {'instructions_template': 'synthetic fixture'},
                        'input_modalities': ['text'], 'context_window': 10000,
                        'service_tiers': ['fixture'], 'multi_agent_version': 'v2'} for name in names]}


class LifecyclePolicyTests(unittest.TestCase):
    def configured(self, config=None):
        return r.configure_catalog(r.ModelCatalog(models()), policy() if config is None else config)

    def test_future_candidates_without_evidence_do_not_imply_better_or_retire(self):
        catalog = r._catalog_from_payload(snapshot(True))
        self.assertIn('gpt-7-sol', catalog.candidate_models())
        self.assertIn('gpt-6-sol', self.configured({}).models)

    def test_strict_dominance_removes_all_automatic_paths(self):
        catalog = self.configured()
        self.assertNotIn('gpt-6-sol', catalog.models)
        self.assertEqual(catalog.preferred('sol'), 'gpt-7-sol')
        self.assertNotIn('gpt-6-sol', r._classifier_schema(catalog)['properties']['model']['enum'])
        choice = r.classify_heuristically('implement a normal feature', catalog)
        self.assertNotEqual(choice.model, 'gpt-6-sol')
        backend = r.catalog_for_parent(catalog, 'gpt-6-sol')
        with patch.object(r, '_load_router_config', return_value={**policy(), 'evaluator': {'model': 'gpt-6-sol', 'selection': 'auto'}}):
            self.assertEqual(r.load_evaluator_config(catalog=backend).model, 'gpt-7-sol')
            self.assertFalse(r.load_evaluator_config(catalog=backend).fast)

    def test_missing_expired_or_mixed_price_evidence_is_unknown(self):
        for case in ('prices', 'capability', 'expired', 'mixed', 'zero', 'nan', 'subscription', 'tier', 'unreviewed'):
            config = policy(); row = config['lifecycle']['comparisons'][0]
            if case in ('prices', 'capability'): row.pop(case)
            if case == 'expired': row['capability']['valid_until'] = '2020-01-01'
            if case == 'mixed': row['prices']['new']['output'] = 11
            if case == 'zero': row['prices']['new'] = dict(row['prices']['old'])
            if case == 'nan': row['prices']['new']['input'] = float('nan')
            if case == 'subscription': config['lifecycle']['cost_basis'] = 'subscription'
            if case == 'tier': row['tier'] = 'astra'
            if case == 'unreviewed': row['capability']['reviewed_by'] = ''
            with self.subTest(case=case):self.assertIn('gpt-6-sol', self.configured(config).models)

    def test_more_capable_more_expensive_is_available_not_forced(self):
        config = policy(); config['lifecycle']['comparisons'][0]['prices']['new']['input'] = 3
        catalog = self.configured(config)
        self.assertIn('gpt-7-sol', catalog.candidate_models())
        self.assertIn('gpt-6-sol', catalog.models)
        self.assertTrue(catalog.evidence)

    def test_disabled_successor_never_revives_and_explicit_fixed_is_preserved(self):
        config = policy(); config['routing'] = {'excluded_models': ['gpt-7-sol']}
        catalog = self.configured(config)
        self.assertIn('gpt-6-sol', catalog.models)
        self.assertNotIn('gpt-7-sol', catalog.models)
        with patch.object(r, '_load_router_config', return_value={'evaluator': {'model': 'gpt-6-sol'}}):
            self.assertEqual(r.load_evaluator_config(catalog=self.configured()).model, 'gpt-6-sol')

    def test_explicit_pin_and_provider_boundary(self):
        config=policy();config['routing']={'preferred_models':['gpt-6-sol']}
        self.assertIn('gpt-6-sol', self.configured(config).models)
        with patch.object(r,'_load_router_config',return_value={'evaluator':{'model':'openai/gpt-6-sol','selection':'auto'}}):
            with self.assertRaises(ValueError): r.load_evaluator_config(catalog=self.configured())
        with patch.object(r,'_load_router_config',return_value={}):
            self.assertEqual(r.load_evaluator_config(catalog=self.configured()).model,'gpt-7-sol')

    def test_subscription_scope_and_effort_compatibility_required(self):
        config = policy();config['lifecycle'].update(cost_basis='subscription', subscription_scope='pro-fixture')
        p=config['lifecycle']['comparisons'][0]['prices'];p.update(basis='subscription', currency='subscription_units', scope='pro-fixture', old={'usage_units': 2}, new={'usage_units': 1})
        self.assertNotIn('gpt-6-sol', self.configured(config).models)
        config['lifecycle']['subscription_scope']='different-plan'
        self.assertIn('gpt-6-sol', self.configured(config).models)
        incomplete=models();incomplete['gpt-7-sol']=('medium',)
        self.assertIn('gpt-6-sol', r.configure_catalog(r.ModelCatalog(incomplete),policy()).models)

    def test_order_cache_policy_invalidation_and_family_speed(self):
        base = r.ModelCatalog(models())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'cache.json';r._write_catalog_cache(path,base)
            self.assertEqual(list(base.models),list(r._read_catalog_cache(path).models))
        config=policy();catalog=self.configured(config)
        config['lifecycle']['comparisons'][0]['prices']['new']['input']=3
        changed=self.configured(config)
        with patch.object(r,'_load_router_config',return_value={}):
            self.assertNotEqual(r.pre_spawn_binding('task','session','name','.',catalog,30),r.pre_spawn_binding('task','session','name','.',changed,30))
        self.assertFalse(r.model_fast_setting('gpt-7-sol',{'model_speed':{'sol':False}}))


class NativeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.exe=self.root/'codex';self.exe.write_text('synthetic executable')
        self.source=self.root/'source.json';self.target=self.root/'v1.json';self.state=self.root/'state.json'
        self.source.write_text(json.dumps(snapshot()))
        self.config={'lifecycle':{'native_refresh':{'enabled':True,'catalog_path':str(self.target),'source_cache':str(self.source),'state_path':str(self.state)}}}
        patch.object(life, 'runtime_version', return_value='0.162').start()
        self.command=patch.object(life.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(snapshot()))).start()
        self.addCleanup(patch.stopall)

    def test_changes_refresh_and_unchanged_does_not_spawn_process(self):
        first=life.sync_native_catalog(str(self.exe),self.config)
        self.assertTrue(all(m['multi_agent_version']=='v1' for m in first['models']))
        self.assertEqual(first['models'][0]['service_tiers'],['fixture'])
        self.assertIsNone(life.sync_native_catalog(str(self.exe),self.config))
        self.assertEqual(self.command.call_count,1)
        self.source.write_text(json.dumps(snapshot(True)))
        new=life.sync_native_catalog(str(self.exe),self.config)
        self.assertIn('gpt-7-sol',[m['slug'] for m in new['models']])
        self.assertEqual(self.command.call_count,1)
        self.exe.write_text('new synthetic binary version')
        life.sync_native_catalog(str(self.exe),self.config)
        self.assertEqual(self.command.call_count,2)

    def test_invalid_source_and_offline_failure_preserve_last_directory(self):
        life.sync_native_catalog(str(self.exe),self.config);before=self.target.read_bytes()
        self.source.write_text('{}')
        self.assertIsNone(life.sync_native_catalog(str(self.exe),self.config));self.assertEqual(before,self.target.read_bytes())
        self.assertIsNone(life.sync_native_catalog(str(self.exe),self.config));self.assertEqual(self.command.call_count,1)
        self.source.write_text(json.dumps(snapshot()));self.exe.write_text('changed runtime')
        self.command.side_effect=OSError('offline fixture')
        self.assertIsNone(life.sync_native_catalog(str(self.exe),self.config));self.assertEqual(before,self.target.read_bytes())

    def test_bundled_new_id_does_not_expand_account_roster(self):
        self.command.return_value.stdout=json.dumps(snapshot(True))
        actual=life.sync_native_catalog(str(self.exe),self.config)
        self.assertNotIn('gpt-7-sol',[m['slug'] for m in actual['models']])

    def test_changed_source_during_refresh_and_writer_lock(self):
        self.target.write_text(json.dumps(snapshot()));before=self.target.read_bytes()
        def change(*args,**kwargs):
            self.source.write_text(json.dumps(snapshot(True)))
            return SimpleNamespace(returncode=0,stdout=json.dumps(snapshot()))
        self.command.side_effect=change
        self.assertIsNone(life.sync_native_catalog(str(self.exe),self.config));self.assertEqual(before,self.target.read_bytes())
        self.target.with_name(self.target.name+'.lock').mkdir()
        self.assertIsNone(life.sync_native_catalog(str(self.exe),self.config));self.assertEqual(before,self.target.read_bytes())

    def test_old_client_cache_and_invalid_state_preserve_verified_roster(self):
        life.sync_native_catalog(str(self.exe),self.config)
        old=snapshot(True);old['client_version']='0.154.0'
        self.source.write_text(json.dumps(old))
        actual=life.sync_native_catalog(str(self.exe),self.config)
        self.assertNotIn('gpt-7-sol',[m['slug'] for m in actual['models']])
        self.state.write_text('[]')
        self.assertIsNotNone(life.sync_native_catalog(str(self.exe),self.config))

    def test_registry_rejects_unknown_fields_and_rollback(self):
        data={'schema_version':1,'revision':2,'comparisons':[comparison()]}
        life.validate_registry(data)
        data['execute']='unsafe'
        with self.assertRaises(ValueError): life.validate_registry(data)

    def test_spawn_reads_snapshot_without_maintenance(self):
        life.sync_native_catalog(str(self.exe),self.config)
        self.source.write_text('{}')
        self.state.write_text('[]')
        self.config['routing']={'excluded_models':['gpt-6-sol']}
        with patch.object(r,'_load_router_config',return_value=self.config), patch.object(life,'sync_native_catalog',side_effect=AssertionError('spawn must not refresh')):
            actual=r.discover_catalog(codex_executable=str(self.exe))
        self.assertNotIn('gpt-6-sol',actual.models)
        self.assertIn('gpt-6-luna',actual.models)

    def test_registry_rollback_and_offline_keep_last_good(self):
        cache=self.root/'registry.json'
        good={'schema_version':1,'revision':2,'comparisons':[comparison()]}
        cache.write_text(json.dumps(good));before=cache.read_bytes()
        cfg={'lifecycle':{'registry':{'enabled':True,'url':'https://raw.githubusercontent.com/UirtvalP/codex-model-router/main/docs/model-comparisons.json','cache_path':str(cache)}}}
        class Response:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def geturl(self): return cfg['lifecycle']['registry']['url']
            def read(self,*a): return json.dumps(dict(good,revision=1)).encode()
        with patch.object(life.urllib.request,'urlopen',return_value=Response()):
            self.assertFalse(life.refresh_registry(cfg))
        self.assertEqual(before,cache.read_bytes())
        cache.with_name(cache.name+'.attempt').unlink()
        with patch.object(life.urllib.request,'urlopen',side_effect=OSError('offline')):
            self.assertFalse(life.refresh_registry(cfg))
        self.assertEqual(before,cache.read_bytes())
