"""Synthetic acceptance tests for backfill, delta updates and recovery."""
import hashlib
import contextlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode

import app_config
import archive_publish
import history_import as history
import personal_world as cli
import server
from world_model import WorldModel


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.state=self.root/'state'
        app_config.private_dir(self.state);self.serial=0
        self.chat={'id':'chat-a','name':'人工会话','is_group':False}
        self.original=[self.message('m1','我们需要机械臂20套 uniqueoldtoken',1735689600),self.message('m2','人工普通消息',1735776000)]
        cli.import_state(self.source(self.original),self.state,no_open=True)
    def tearDown(self):self.tmp.cleanup()
    def message(self,mid,text,stamp):
        return {'id':mid,'conversation_id':'chat-a','sender_name':'人工发送者','is_self':False,'type':'text','text':text,'timestamp':stamp}
    def source(self,messages,chat=None):
        self.serial+=1;p=self.root/('fixture-'+str(self.serial)+'.json')
        p.write_text(json.dumps({'manifest':{'synthetic':True,'complete':True},'conversations':[chat or self.chat],'messages':messages},ensure_ascii=False));p.chmod(0o600);return p
    def pair(self):return history.pair_digest(self.state)
    def rows(self):
        with contextlib.closing(sqlite3.connect(self.state/'index.sqlite')) as conn:return dict(conn.execute('SELECT id,text FROM messages'))
    def prepare(self,source,**kw):return history.prepare(source,self.state,**kw)
    def commit(self,plan):return history.commit(plan['plan_id'],self.state)
    def test_old_history_preview_preserves_current_and_commit_extends_coverage(self):
        original=self.pair();w=WorldModel(self.state);candidate=w.opportunities()['items'][0]
        w.save_review(candidate['id'],'follow_up','原有私人备注');draft=w.create_draft(candidate['id'])
        w.save_draft(draft['id'],'人工草稿','需要保留的人工编辑',1,draft['source_fingerprint'])
        work=archive_publish.digest_file(self.state/'workspace.sqlite')
        plan=self.prepare(self.source([self.message('old','人工旧记录',1577836800),*self.original]))
        self.assertEqual(plan['summary']['messages_added'],1);self.assertEqual(plan['summary']['messages_unchanged'],2)
        self.assertEqual(self.pair(),original)
        result=self.commit(plan);self.assertEqual(result['summary']['after_messages'],3)
        self.assertEqual(set(self.rows()),{'m1','m2','old'})
        self.assertEqual(result['delta']['rows_inserted'],1);self.assertEqual(result['delta']['rows_updated'],0)
        self.assertEqual(archive_publish.digest_file(self.state/'workspace.sqlite'),work)
        self.assertTrue(cli.verify_state(self.state)['ok'])
        months=history.catalog(self.state)['months'];self.assertEqual(months[0]['month'],'2020-01')
        self.assertEqual(months[-1]['month'],'2025-01');self.assertTrue(any(m['messages']==0 for m in months))
    def test_repeated_import_is_noop_and_does_not_reset_workflow(self):
        source=self.source(self.original);before=self.pair();plan=self.prepare(source)
        self.assertEqual(plan['summary']['messages_added'],0)
        result=self.commit(plan);self.assertEqual(result['status'],'unchanged');self.assertEqual(self.pair(),before)
        self.assertEqual(history.commit(plan['plan_id'],self.state),result)
        self.assertFalse((self.state/'workspace.sqlite').exists())
    def test_partial_snapshot_never_deletes_older_messages(self):
        plan=self.prepare(self.source([self.message('m3','人工新消息',1735862400)]),policy='prefer_incoming')
        self.commit(plan);self.assertEqual(set(self.rows()),{'m1','m2','m3'})
        repeat=self.prepare(self.source([self.original[0]]));self.commit(repeat)
        self.assertEqual(set(self.rows()),{'m1','m2','m3'})
    def test_default_keeps_old_content_explicit_update_changes_search_only(self):
        changed=dict(self.original[0],text='我们需要机械臂20套 uniquenewtoken')
        source=self.source([changed]);before=self.pair();plan=self.prepare(source)
        self.assertEqual(plan['summary']['conflicts'],1);self.assertEqual(plan['summary']['kept_existing'],1)
        self.commit(plan);self.assertEqual(self.pair(),before)
        plan=self.prepare(source,policy='prefer_incoming');result=self.commit(plan)
        self.assertEqual(result['delta']['rows_updated'],1);self.assertEqual(self.rows()['m1'],changed['text'])
        with contextlib.closing(sqlite3.connect(self.state/'index.sqlite')) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone():
                self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'uniqueoldtoken'").fetchone()[0],0)
                self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'uniquenewtoken'").fetchone()[0],1)
        self.assertTrue(cli.verify_state(self.state)['ok'])
    def test_metadata_only_update_does_not_rewrite_text_search(self):
        changed=dict(self.original[0],sender_name='人工更正显示名')
        result=self.commit(self.prepare(self.source([changed]),policy='prefer_incoming'))
        self.assertEqual(result['delta']['rows_updated'],1);self.assertEqual(result['delta']['search_rows_written'],0)
    def test_identity_collision_requires_another_namespace(self):
        changed=dict(self.original[0],timestamp=1577836800)
        plan=self.prepare(self.source([changed]),policy='prefer_incoming')
        self.assertFalse(plan['can_apply']);self.assertEqual(plan['summary']['identity_conflicts'],1)
        before=self.pair()
        with self.assertRaises(app_config.AppError):self.commit(plan)
        self.assertEqual(self.pair(),before)
        history.discard(plan['plan_id'],self.state)
        other=self.prepare(self.source([changed]),source_id='older-export')
        self.commit(other)
        self.assertIn('pw:older-export:m1',self.rows());self.assertIn('m1',self.rows())
        self.assertTrue(cli.verify_state(self.state)['ok'])
    def test_old_preview_cannot_overwrite_a_new_import(self):
        a=self.prepare(self.source([self.message('a','人工A',1735862400)]))
        b=self.prepare(self.source([self.message('b','人工B',1735862401)]))
        self.commit(a);before=self.pair()
        with self.assertRaises(app_config.AppError) as error:self.commit(b)
        self.assertEqual(error.exception.code,'plan_stale');self.assertEqual(self.pair(),before)
    def test_preview_tampering_is_rejected_without_touching_current_pair(self):
        plan=self.prepare(self.source([self.message('a','人工A',1735862400)]));before=self.pair()
        path=self.state/'import-plans'/plan['plan_id']/'output/archive.jsonl'
        with path.open('a') as f:f.write('changed')
        with self.assertRaises(app_config.AppError):self.commit(plan)
        self.assertEqual(self.pair(),before)
    def test_retained_input_is_hash_checked_and_record_origins_are_audited(self):
        self.commit(self.prepare(self.source([self.message('a','人工A',1735862400)])))
        self.assertTrue(cli.verify_state(self.state)['ok'])
        retained=next((self.state/'import-sources').glob('*.jsonl'))
        with retained.open('a') as f:f.write('corruption')
        with self.assertRaises(app_config.AppError):cli.verify_state(self.state)
    def test_empty_malformed_and_mixed_synthetic_inputs_keep_current(self):
        before=self.pair()
        for source in [self.source([])]:
            with self.assertRaises(app_config.AppError):self.prepare(source)
        source=self.source([self.message('a','人工A',1735862400)])
        doc=json.loads(source.read_text());doc['manifest']['synthetic']=False;source.write_text(json.dumps(doc))
        with self.assertRaises(app_config.AppError):self.prepare(source)
        self.assertEqual(self.pair(),before)
    def test_discard_removes_only_preview(self):
        plan=self.prepare(self.source([self.message('a','人工A',1735862400)]));before=self.pair()
        history.discard(plan['plan_id'],self.state)
        self.assertFalse((self.state/'import-plans'/plan['plan_id']).exists());self.assertEqual(self.pair(),before)
    def test_legacy_replace_entry_rejects_a_managed_collection(self):
        self.commit(self.prepare(self.source([self.message('a','人工A',1735862400)])));before=self.pair()
        with self.assertRaises(app_config.AppError):cli.import_state(self.source(self.original),self.state,no_open=True)
        self.assertEqual(self.pair(),before)
    def test_initial_import_builds_auditable_collection(self):
        other=self.root/'other'
        plan=history.prepare(self.source(self.original),other)
        result=history.commit(plan['plan_id'],other)
        self.assertEqual(result['delta']['mode'],'initial_build');self.assertTrue(cli.verify_state(other)['ok'])

    def test_interrupted_preview_can_be_found_and_canceled_after_restart(self):
        plan=self.prepare(self.source([self.message('older','人工旧数据',1577836800)]))
        pending=history.catalog(self.state)['pending_previews']
        self.assertEqual([x['plan_id'] for x in pending],[plan['plan_id']])
        self.assertNotIn(str(self.root),json.dumps(pending))
        before=self.pair();history.discard(plan['plan_id'],self.state)
        self.assertEqual(history.catalog(self.state)['pending_previews'],[])
        self.assertEqual(self.pair(),before)

    def media_source(self,changed_text=False):
        media=self.state/'media';app_config.private_dir(media)
        blob=media/'synthetic-voice.wav';blob.write_bytes(b'RIFF-synthetic-media-fixture');blob.chmod(0o600)
        row=dict(self.original[0],media_url='/api/media/synthetic-voice.wav',media_status='available',file_size=blob.stat().st_size,type='voice')
        cli.import_state(self.source([row,self.original[1]]),self.state,no_open=True)
        new=dict(row,media_url=None,media_status='silk_decoder_unavailable',file_size=None)
        if changed_text:new['text']='人工更改的语音说明'
        return self.source([new])
    def test_failed_media_reread_never_downgrades_existing_cached_record(self):
        source=self.media_source();before=self.pair();plan=self.prepare(source,policy='prefer_incoming')
        self.assertTrue(plan['can_apply']);self.assertEqual(plan['summary']['media_downgrades_prevented'],1)
        self.assertEqual(plan['summary']['messages_changed'],0);self.commit(plan)
        self.assertEqual(self.pair(),before)
    def test_media_loss_with_changed_content_requires_explicit_review(self):
        source=self.media_source(changed_text=True);before=self.pair();plan=self.prepare(source,policy='prefer_incoming')
        self.assertFalse(plan['can_apply']);self.assertEqual(plan['summary']['media_content_conflicts'],1)
        with self.assertRaises(app_config.AppError):self.commit(plan)
        self.assertEqual(self.pair(),before)
    def test_old_rule_preview_cannot_apply_after_protection_upgrade(self):
        plan=self.prepare(self.source([self.message('a','人工新记录',1735862400)]));before=self.pair()
        path=self.state/'import-plans'/plan['plan_id']/'plan.json'
        doc=json.loads(path.read_text());doc.pop('merge_rules_version');app_config.atomic_json(path,doc)
        with self.assertRaises(app_config.AppError) as error:self.commit(plan)
        self.assertEqual(error.exception.code,'plan_stale');self.assertEqual(self.pair(),before)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.state=self.root/'state';self.staging=self.root/'staging'
        for p in [self.state,self.staging]:app_config.private_dir(p)
        for name in archive_publish.NAMES:
            (self.state/name).write_bytes(('old-'+name).encode());(self.state/name).chmod(0o600)
            (self.staging/name).write_bytes(('new-'+name).encode());(self.staging/name).chmod(0o600)
        self.before=history.pair_digest(self.state)
    def tearDown(self):self.tmp.cleanup()
    def crash(self):
        code="""import os,sys
from pathlib import Path
import archive_publish
real=os.replace
def crash(src,dst):
 if Path(dst).name=='index.sqlite':os._exit(71)
 return real(src,dst)
archive_publish.os.replace=crash
archive_publish.publish(Path(sys.argv[1]),Path(sys.argv[2]))
"""
        p=subprocess.run([sys.executable,'-B','-c',code,str(self.staging),str(self.state)],cwd=Path(__file__).resolve().parent)
        self.assertEqual(p.returncode,71);self.assertTrue((self.state/archive_publish.JOURNAL).exists())
    def test_hard_exit_between_pair_replacements_recovers_old_pair(self):
        self.crash()
        with app_config.StateLock(self.state) as lock:self.assertEqual(lock.recovery['outcome'],'previous_pair_restored')
        self.assertEqual(history.pair_digest(self.state),self.before)
        self.assertFalse((self.state/archive_publish.JOURNAL).exists())
    def test_recovery_refuses_unrecognized_external_file_change(self):
        self.crash();(self.state/'archive.jsonl').write_bytes(b'not the recorded old or new value')
        with self.assertRaises(app_config.AppError):archive_publish.recover(self.state)
        self.assertEqual((self.state/'archive.jsonl').read_bytes(),b'not the recorded old or new value')
        self.assertTrue((self.state/archive_publish.JOURNAL).exists())


class ImportHTTPTests(unittest.TestCase):
    source=HistoryTests.source
    message=HistoryTests.message
    pair=HistoryTests.pair
    def setUp(self):
        HistoryTests.setUp(self)
        self.httpd=server.ViewerServer(0,self.state,'synthetic-history-session')
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
        self.port=self.httpd.server_address[1]
    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join(timeout=2);HistoryTests.tearDown(self)
    def request(self,path,body=None,auth=True,origin=True,content_type='application/json',extra=None):
        headers={'Content-Type':content_type}
        if auth:headers['Cookie']=self.httpd.cookie_name+'=synthetic-history-session'
        if origin:headers['Origin']='http://127.0.0.1:'+str(self.port)
        headers.update(extra or {})
        client=http.client.HTTPConnection('127.0.0.1',self.port,timeout=20)
        client.request('GET' if body is None else 'POST',path,body=body,headers=headers)
        response=client.getresponse();data=response.read();code=response.status;client.close()
        return code,json.loads(data)
    def test_file_preview_requires_local_auth_and_explicit_apply(self):
        source=self.source([self.message('older','人工更早记录',1577836800)])
        raw=source.read_bytes();path='/api/world/import/preview?format=json&source_id=primary&policy=keep_existing'
        before=self.pair()
        self.assertEqual(self.request('/api/world/imports',auth=False)[0],401)
        self.assertEqual(self.request(path,raw,auth=False,content_type='application/octet-stream')[0],401)
        self.assertEqual(self.request(path,raw,origin=False,content_type='application/octet-stream')[0],403)
        self.assertEqual(self.request(path,raw,content_type='application/json')[0],415)
        code,preview=self.request(path,raw,content_type='application/octet-stream')
        self.assertEqual(code,200);self.assertEqual(self.pair(),before)
        self.assertEqual(preview['summary']['messages_added'],1)
        code,done=self.request('/api/world/import/apply',json.dumps({'plan_id':preview['plan_id']}))
        self.assertEqual(code,200);self.assertEqual(done['summary']['after_messages'],3)
        code,catalog=self.request('/api/world/imports');self.assertEqual(code,200)
        self.assertEqual(catalog['months'][0]['month'],'2020-01')
        self.assertNotIn(str(self.root),json.dumps(catalog))
        self.assertEqual(self.request('/api/send',b'{}')[0],405)
    def test_oversized_or_invalid_upload_has_no_partial_import(self):
        before=self.pair();path='/api/world/import/preview?format=json'
        code,_=self.request(path,b'x',content_type='application/octet-stream',extra={'Content-Length':str(history.MAX_UPLOAD+1)})
        self.assertEqual(code,413)
        self.assertEqual(self.request(path,b'not json',content_type='application/octet-stream')[0],400)
        self.assertEqual(self.pair(),before)
        self.assertEqual(list(self.state.glob('.upload-*')),[])


class MoreHistoryTests(unittest.TestCase):
    setUp=HistoryTests.setUp
    tearDown=HistoryTests.tearDown
    source=HistoryTests.source
    message=HistoryTests.message
    prepare=HistoryTests.prepare
    commit=HistoryTests.commit
    pair=HistoryTests.pair
    rows=HistoryTests.rows
    # Additional tests use the same small fixture, not private archives.
    def test_two_backfills_preserve_prior_sources_and_reimport_is_noop(self):
        self.commit(self.prepare(self.source([self.message('old-a','人工旧甲',1577836800)])))
        self.commit(self.prepare(self.source([self.message('old-b','人工旧乙',1609459200)])))
        self.assertEqual(len(self.rows()),4);self.assertTrue(cli.verify_state(self.state)['ok'])
        before=self.pair()
        result=self.commit(self.prepare(self.source([self.message('old-a','人工旧甲',1577836800)])))
        self.assertEqual(result['status'],'unchanged');self.assertEqual(self.pair(),before)
    def test_wrong_origin_does_not_pass_collection_audit(self):
        self.commit(self.prepare(self.source([self.message('a','人工A',1735862400)])))
        path=self.state/'archive.jsonl';rows=[json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if row.get('record_type')=='message':row['origin']['original_id']='missing-id';break
        path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        # Index message data is unchanged, but origin verification must fail.
        self.assertFalse(cli.verify_state(self.state)['ok'])
