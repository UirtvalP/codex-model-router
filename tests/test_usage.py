import json
import sqlite3
from datetime import datetime, timedelta
import tempfile
import unittest
from pathlib import Path
from codex_model_router.usage import build_usage_payload


def counts(n):
    return {'input_tokens': n, 'cached_input_tokens': n//2, 'output_tokens': 10, 'reasoning_output_tokens': 2}


def event(kind, payload, stamp='2026-09-09T12:00:00+08:00'):
    return {'type': kind, 'timestamp': stamp, 'payload': payload}


def modern(n, response='r1', thread='s1'):
    return event('token_usage_record', {'thread_id': thread, 'response_id': response, 'usage': counts(n), 'thread_token_usage': counts(n)})


def legacy(total, last=None, stamp='2026-09-09T12:00:00+08:00'):
    return event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': total, 'last_token_usage': last or total}}, stamp)


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.log = self.home/'decisions.jsonl'

    def write(self, name, rows):
        p=self.home/'sessions'/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        return p

    def base(self):
        return [event('session_meta', {'id':'s1'}), event('turn_context', {'model':'terra'})]

    def read(self, **kwargs):
        return build_usage_payload(self.home,self.log,**kwargs)

    def test_modern_global_dedup_and_router_supplement(self):
        self.write('a.jsonl',self.base()+[modern(100)])
        self.write('b.jsonl',self.base()+[modern(100)])
        self.log.write_text('\n'.join(json.dumps({'timestamp':'2026-09-09T12:00:00+08:00','evaluator':{'thread_id':t,'model':'terra','usage':counts(50)}}) for t in ('s1','eval')))
        d=self.read();self.assertEqual(d['summary']['input_tokens'],150)
        self.assertEqual(d['summary']['uncached_input_tokens'],75)
        self.assertEqual(len(d['by_session']),2)
        self.assertEqual(d,self.read())

    def test_legacy_delta_duplicates_reset(self):
        a=counts(100);b=counts(150);b['output_tokens']=20
        self.write('a.jsonl',self.base()+[legacy(a),legacy(a),legacy(b),legacy(counts(20))])
        self.assertEqual(self.read()['summary']['input_tokens'],170)

    def test_mixed_dual_write_both_orders(self):
        for rows in ([modern(100),legacy(counts(100))],[legacy(counts(100)),modern(100)]):
            self.write('a.jsonl',self.base()+rows)
            self.assertEqual(self.read()['summary']['input_tokens'],100)

    def test_date_and_model_changes_and_cache_invalidation(self):
        p=self.write('a.jsonl',self.base()+[legacy(counts(100),stamp='2026-09-08T12:00:00+08:00'),event('turn_context',{'model':'sol'}),modern(200,'r2')])
        d=self.read(start='2026-09-09',end='2026-09-09')
        self.assertEqual(d['summary']['input_tokens'],200)
        self.assertEqual(d['by_model'][0]['model'],'sol')
        p.write_text(p.read_text()+json.dumps(modern(300,'r3'))+'\n')
        self.assertEqual(self.read()['summary']['input_tokens'],600)
        with self.assertRaises(ValueError):self.read(start='2026-09-10',end='2026-09-09')

    def test_parallel_sessions_with_identical_counters_are_not_merged(self):
        self.write('a.jsonl', self.base()+[legacy(counts(100))])
        self.write('b.jsonl', [event('session_meta', {'id':'s2'})]+[legacy(counts(100))])
        self.assertEqual(self.read()['summary']['input_tokens'],200)

    def test_dual_write_with_different_cumulative_scopes(self):
        new=modern(100)
        new['payload']['thread_token_usage']=counts(900)
        self.write('a.jsonl',self.base()+[new,legacy(counts(100))])
        self.assertEqual(self.read()['summary']['input_tokens'],100)

    def test_dual_write_is_one_to_one_and_thread_scoped(self):
        cumulative=counts(200);cumulative['output_tokens']=20;cumulative['reasoning_output_tokens']=4
        self.write('a.jsonl',self.base()+[modern(100),legacy(counts(100)),legacy(cumulative,counts(100))])
        self.assertEqual(self.read()['summary']['input_tokens'],200)
        self.write('a.jsonl',self.base()+[modern(100,thread='other'),legacy(counts(100))])
        self.assertEqual(self.read()['summary']['input_tokens'],200)

    def test_daily_model_breakdown_preserves_totals(self):
        self.write('a.jsonl', self.base()+[modern(100), event('turn_context', {'model':'sol'}), modern(200,'r2')])
        d=self.read();day=d['by_day'][0]
        self.assertEqual({m['model']:m['input_tokens'] for m in day['models']}, {'terra':100,'sol':200})
        for field in ['input_tokens','output_tokens','cached_input_tokens','uncached_input_tokens','total_tokens']:
            self.assertEqual(sum(m[field] for m in day['models']),day[field])

    def test_titles_and_top_week_ignore_selected_date_filter(self):
        now=datetime.now().astimezone()
        for i in range(4):
            row=modern((i+1)*100,'rank'+str(i),'s'+str(i))
            row['timestamp']=now.isoformat()
            self.write(str(i)+'.jsonl',[event('session_meta',{'id':'s'+str(i)}),row])
        with sqlite3.connect(self.home/'state_5.sqlite') as db:
            db.execute('create table threads (id text, title text, name text)')
            db.execute('insert into threads values (?,?,?)',('s3','Original prompt','Conversation title'))
        future=(now+timedelta(days=2)).date().isoformat()
        d=self.read(start=future,end=future)
        self.assertEqual(d['summary']['total_tokens'],0)
        self.assertEqual([r['session_id'] for r in d['top_week_sessions']],['s3','s2','s1'])
        self.assertEqual(d['top_week_sessions'][0]['title'],'Conversation title')
        self.assertEqual(self.read()['by_session'][0]['title'],'Conversation title')

    def test_archived_and_broken_line(self):
        p=self.write('a.jsonl',self.base()+[modern(100)])
        archived=self.home/'archived_sessions';archived.mkdir();p.rename(archived/'a.jsonl')
        self.assertEqual(self.read()['summary']['input_tokens'],100)
        with (archived/'a.jsonl').open('a') as f:f.write('{"type":"token_usage_record",bad}\n')
        self.assertEqual(self.read()['coverage']['errors'],1)
