"""Synthetic regression tests for persistent, review-aware local workflows."""
import concurrent.futures
import hashlib
import json
import sqlite3
import unittest
from unittest import mock
import draft_store
from urllib.parse import urlencode

import server
import test_world_model as fixtures
from world_model import WorldModel, SourceChangedError, DraftConflictError


class WorkflowTests(unittest.TestCase):
    setUp=fixtures.WorldTests.setUp
    tearDown=fixtures.WorldTests.tearDown
    write_source=fixtures.WorldTests.write_source
    candidates=fixtures.WorldTests.candidates

    def new_draft(self):
        item=self.candidates()['c']
        return self.world.create_draft(item['id'],item['source_fingerprint'])

    def edit(self,draft,body='人工修改的跟进草稿',world=None):
        return (world or self.world).save_draft(draft['id'],'人工标题',body,draft['revision'],draft['source_fingerprint'])

    def test_edit_survives_reload_and_original_is_immutable(self):
        before=hashlib.sha256((self.state/'index.sqlite').read_bytes()).digest()
        draft=self.new_draft();saved=self.edit(draft)
        self.assertEqual(saved['revision'],2)
        restarted=WorldModel(self.state)
        self.assertEqual(restarted.draft(draft['id'])['draft']['body'],saved['body'])
        self.assertEqual(restarted.drafts()['items'][0]['body'],saved['body'])
        history=restarted.draft(draft['id'])['history']['items']
        self.assertEqual([x['revision'] for x in history],[2,1])
        self.assertEqual(history[-1]['body'],draft['body'])
        again=restarted.create_draft(draft['opportunity_id'],draft['source_fingerprint'])
        self.assertEqual(again['body'],saved['body'])
        self.assertEqual(again['revision'],2)
        self.assertEqual(hashlib.sha256((self.state/'index.sqlite').read_bytes()).digest(),before)

    def test_conflict_and_retry_never_overwrite_newer_revision(self):
        draft=self.new_draft();first=self.edit(draft,'第一份人工修改')
        self.assertEqual(self.edit(draft,'第一份人工修改'),first)
        with self.assertRaises(DraftConflictError):self.edit(draft,'另一个窗口的旧版本')
        self.assertEqual(self.world.draft(draft['id'])['draft']['body'],'第一份人工修改')
        self.assertEqual(self.edit(first,'第一份人工修改')['revision'],2)

    def test_separate_instances_serialize_competing_writers(self):
        draft=self.new_draft()
        def write(body):
            try:return self.edit(draft,body,WorldModel(self.state))['body']
            except DraftConflictError:return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(write,['编辑甲','编辑乙']))
        self.assertEqual(results.count('conflict'),1)
        self.assertEqual(self.world.draft(draft['id'])['draft']['revision'],2)

    def test_legacy_workspace_reads_do_not_migrate(self):
        draft=self.new_draft()
        with self.world.workspace(write=True) as conn:conn.execute('DROP TABLE draft_revisions')
        path=self.state/'workspace.sqlite';before=hashlib.sha256(path.read_bytes()).digest()
        result=WorldModel(self.state).draft(draft['id'])
        self.assertEqual(result['draft']['revision'],1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),before)
        self.edit(draft)
        self.assertEqual(self.world.draft(draft['id'])['draft']['revision'],2)

    def test_revision_history_is_paginated_without_gaps(self):
        draft=self.new_draft()
        for i in range(6):draft=self.edit(draft,'版本内容 '+str(i))
        revisions=[];before=None
        while True:
            page=self.world.draft(draft['id'],before,2)['history']
            revisions += [r['revision'] for r in page['items']]
            if not page['has_more']:break
            before=page['next_before']
        self.assertEqual(revisions,list(range(7,0,-1)))

    def test_source_refresh_preserves_edits_and_review_but_rejects_old_save(self):
        draft=self.new_draft();saved=self.edit(draft)
        self.world.save_review(draft['opportunity_id'],'follow_up','线下进展由本人核对')
        before=hashlib.sha256((self.state/'workspace.sqlite').read_bytes()).digest()
        updated=[dict(m) for m in self.messages];updated[4]['text']='我们需要机器人关节 50 台，请确认报价。'
        self.write_source([dict(id=x,name='人工 '+x,is_group=False) for x in ('a','b','c','d')],updated)
        server.import_archive(self.source,self.state)
        restarted=WorldModel(self.state)
        retained=restarted.draft(saved['id'])['draft']
        self.assertEqual(retained['body'],saved['body']);self.assertTrue(retained['source_stale'])
        self.assertFalse(retained['editable'])
        with self.assertRaises(SourceChangedError):self.edit(saved,'过期修改',restarted)
        self.assertEqual(hashlib.sha256((self.state/'workspace.sqlite').read_bytes()).digest(),before)
        self.assertEqual(restarted.review_ledger()['items'][0]['note'],'线下进展由本人核对')
        self.assertEqual(restarted.review_ledger()['items'][0]['source_state'],'changed')

    def test_closed_peers_disappear_from_warm_cache_and_new_drafts(self):
        before=self.candidates();item=before['b'];peer=before['c']
        self.assertIn(peer['id'],[x['id'] for x in item['related_leads']])
        old=self.world.create_draft(item['id'])
        for status in ('dismissed','completed'):
            self.world.save_review(peer['id'],status,'人工决定',peer['source_fingerprint'])
            after=self.candidates()
            self.assertNotIn(peer['id'],[x['id'] for x in after['b']['related_leads']])
            self.assertEqual(after['b']['source_fingerprint'],item['source_fingerprint'])
            self.assertNotIn(peer['id'],[x['id'] for x in self.world.opportunities(status='active')['items']])
            fresh=self.world.create_draft(item['id'])
            self.assertNotIn(peer['anchor_id'],fresh['evidence_ids'])
            self.assertNotEqual(old['id'],fresh['id'])
            self.assertTrue(self.world.draft(old['id'])['draft']['workflow_stale'])
            with self.assertRaises(SourceChangedError):self.edit(old)
        self.world.save_review(peer['id'],'follow_up','重新核对',peer['source_fingerprint'])
        self.assertIn(peer['id'],[x['id'] for x in self.candidates()['b']['related_leads']])
        # Presentation filtering did not corrupt the source-only cache.
        self.assertIn(peer['id'],[x['id'] for x in self.world._find_candidate(item['id'])['related_leads']])

    def test_changed_closed_candidate_returns_to_needs_review_not_active_peer(self):
        peer=self.candidates()['c'];self.world.save_review(peer['id'],'completed')
        updated=[dict(m) for m in self.messages];updated[4]['text']='我们需要机器人关节 90 台，请确认报价。'
        self.write_source([dict(id=x,name='人工 '+x,is_group=False) for x in ('a','b','c','d')],updated)
        server.import_archive(self.source,self.state)
        found={x['id']:x for x in self.world.opportunities(status='needs_review')['items']}
        self.assertIn(peer['id'],found);self.assertEqual(found[peer['id']]['status'],'completed')
        self.assertEqual(found[peer['id']]['effective_status'],'needs_review')
        self.assertNotIn(peer['id'],[x['id'] for x in self.candidates()['b']['related_leads']])

    def test_orphan_review_remains_visible_without_recreating_source(self):
        item=self.candidates()['c'];self.world.save_review(item['id'],'follow_up','保留本人经验')
        self.write_source([dict(id='d',name='人工 d',is_group=False)],[self.messages[-1]])
        server.import_archive(self.source,self.state)
        before=hashlib.sha256((self.state/'workspace.sqlite').read_bytes()).digest()
        ledger=WorldModel(self.state).review_ledger()
        self.assertEqual(ledger['total'],1);self.assertEqual(ledger['items'][0]['source_state'],'unavailable')
        self.assertEqual(ledger['items'][0]['note'],'保留本人经验')
        self.assertEqual(hashlib.sha256((self.state/'workspace.sqlite').read_bytes()).digest(),before)

    def test_invalid_edit_and_unknown_read_have_no_write_side_effect(self):
        with self.assertRaises(ValueError):self.world.save_draft('bad','title','body',1,'0'*64)
        with self.assertRaises(KeyError):self.world.draft('draft:'+'a'*32)
        self.assertEqual(self.world.review_ledger()['total'],0)
        self.assertFalse((self.state/'workspace.sqlite').exists())
        draft=self.new_draft()
        for rev,title,body in [(True,'title','body'),(0,'title','body'),(1,'','body'),(1,'title',''),(1,'t','x'*16001)]:
            with self.assertRaises(ValueError):self.world.save_draft(draft['id'],title,body,rev,draft['source_fingerprint'])
        self.assertEqual(self.world.draft(draft['id'])['draft']['revision'],1)

    def test_source_swap_during_edit_rolls_back_revision(self):
        draft=self.new_draft()
        changed=[dict(m) for m in self.messages];changed[4]['text']='我们需要机器人关节 80 台，请确认报价。'
        self.write_source([dict(id=x,name='人工 '+x,is_group=False) for x in ('a','b','c','d')],changed)
        real_save=draft_store.save
        def swapping(*args,**kwargs):
            result=real_save(*args,**kwargs)
            server.import_archive(self.source,self.state)
            return result
        with mock.patch.object(draft_store,'save',side_effect=swapping):
            with self.assertRaises(SourceChangedError):self.edit(draft,'保存期间更新不能写入')
        retained=self.world.draft(draft['id'])
        self.assertEqual(retained['draft']['revision'],1)
        self.assertEqual(len(retained['history']['items']),1)

    def test_history_and_revision_bounds_fail_as_validation_errors(self):
        draft=self.new_draft()
        for before in (True,0,-1,2**63):
            with self.assertRaises(ValueError):self.world.draft(draft['id'],before)
        with self.assertRaises(ValueError):self.world.save_draft(draft['id'],'标题','正文',2**63,draft['source_fingerprint'])

    def test_review_change_during_provenance_check_cannot_be_adopted_silently(self):
        draft=self.new_draft()
        real_annotate=self.world._annotate_draft
        def changing_review(row):
            result=real_annotate(row)
            self.world.save_review(draft['opportunity_id'],'completed','人工并发测试')
            return result
        with mock.patch.object(self.world,'_annotate_draft',side_effect=changing_review):
            with self.assertRaises(SourceChangedError):self.edit(draft,'应拒绝未核对状态')
        self.assertEqual(self.world.draft(draft['id'])['draft']['revision'],1)


class WorkflowHTTPTests(unittest.TestCase):
    setUp=fixtures.WorldHTTPTests.setUp
    tearDown=fixtures.WorldHTTPTests.tearDown
    request=fixtures.WorldHTTPTests.request
    write_source=fixtures.WorldTests.write_source
    candidates=fixtures.WorldTests.candidates

    def test_edit_routes_require_auth_origin_fingerprint_and_revision(self):
        item=self.candidates()['c']
        code,draft,_=self.request('POST','/api/world/draft',{'id':item['id'],'source_fingerprint':item['source_fingerprint']})
        self.assertEqual(code,200)
        body=dict(id=draft['id'],title='人工 HTTP 标题',body='<script>只作为原文</script>',expected_revision=1,source_fingerprint=draft['source_fingerprint'])
        route='/api/world/draft/edit';url='/api/world/draft?'+urlencode({'id':draft['id']})
        self.assertEqual(self.request('GET',url,auth=False)[0],401)
        self.assertEqual(self.request('GET','/api/world/reviews',auth=False)[0],401)
        self.assertEqual(self.request('POST',route,body,auth=False)[0],401)
        self.assertEqual(self.request('POST',route,body,origin=False)[0],403)
        self.assertEqual(self.request('POST',route,{**body,'expected_revision':True})[0],400)
        self.assertEqual(self.request('POST',route,{**body,'source_fingerprint':'0'*64})[0],409)
        code,saved,_=self.request('POST',route,body)
        self.assertEqual(code,200);self.assertEqual(saved['revision'],2)
        self.assertEqual(self.request('GET',url)[1]['draft']['body'],body['body'])
        conflict=self.request('POST',route,{**body,'body':'不能覆盖'})
        self.assertEqual(conflict[0],409);self.assertEqual(conflict[1]['code'],'draft_conflict')
        self.assertEqual(self.request('POST','/api/send',body)[0],405)
