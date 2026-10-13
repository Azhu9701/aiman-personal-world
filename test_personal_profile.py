"""Only handwritten synthetic self-portrait data. Never load an owner's archive."""
import concurrent.futures
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
import server
import personal_profile as P
from app_config import AppError, StateLock
from world_model import SourceChangedError


def synthetic_messages():
    def row(mid,text,self=True,days=0,chat='c',kind='text'):
        return dict(id=mid,conversation_id=chat,sender_name='人工示例',is_self=self,type=kind,text=text,timestamp=1704067200+days*86400)
    return [row('self-one','我坚持研究过程保留原始证据。'),
            row('self-two','我坚持研究过程保留原始证据。',days=40),
            row('self-project','我正在制作一套开源机械臂。',days=41),
            row('self-job','我是机器人项目的工程师。',days=42),
            row('self-identity','我是开源贡献者。',days=44),
            row('self-pref','我更喜欢先做本地验证。',days=45),
            row('self-neg','我不喜欢直接跳过测试。',days=46),
            row('not-self','我喜欢隔壁城市的独特产品。',self=False),
            row('unknown','我希望把这个秘密项目做完。',self=None),
            row('forward','他说：我喜欢炒股。'),
            row('quote','我喜欢“代入角色的机器人故事”。'),
            row('question','我应该喜欢自动化研究吗？'),
            row('hypothetical','如果我是机器人专家，我喜欢这条路线。'),
            row('not-text','我负责一个开源项目。',kind='file'),
            row('foreign','我喜欢别的账号的内容。',chat='pw:other:c'),
            row('pw:other:self','我希望这是另一个人的项目。'),
            row('sensitive','我希望保留我的病史和密码。'),
            row('contact','我希望你联系 test@example.invalid。'),
            row('long-number','我希望保留 12345678901 这个测试编号。')]


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.state=Path(self.tmp.name);self.source=self.state/'synthetic.json'
        self.rows=synthetic_messages();self.import_rows();self.profile=P.PersonalProfile(self.state)

    def tearDown(self):self.tmp.cleanup()

    def import_rows(self):
        chats=[dict(id=cid,name='人工测试会话',is_group=False) for cid in sorted({r['conversation_id'] for r in self.rows})]
        self.source.write_text(json.dumps({'manifest':{'synthetic':True,'complete':True},'conversations':chats,'messages':self.rows},ensure_ascii=False))
        server.import_archive(self.source,self.state)

    def candidate(self,quote=None):
        data=self.profile.snapshot()
        return next(i for i in data['candidates'] if quote is None or i['quote']==quote)

    def confirm(self,item=None,text=None,revision=None,profile=None):
        item=item or self.candidate();profile=profile or self.profile
        return profile.mutate(dict(operation='confirm',id=item['id'],text=text or item['quote'],category=item['category'],source_fingerprint=item['source_fingerprint'],expected_revision=profile.controls()['revision'] if revision is None else revision))

    def test_only_primary_explicit_self_text_contributes(self):
        data=self.profile.snapshot();all_ids={e['id'] for i in data['candidates'] for e in i['evidence']}
        self.assertEqual(all_ids,{'self-one','self-two','self-project','self-job','self-identity','self-pref','self-neg'})
        self.assertTrue(data['synthetic']);self.assertEqual(data['revision'],0)
        for topic in data['topics']:
            self.assertFalse({'not-self','unknown','not-text','foreign','pw:other:self'} & {e['id'] for e in topic['evidence']})

    def test_reads_have_no_persistent_profile_side_effect(self):
        before=set(self.state.iterdir());fingerprint=hashlib.sha256((self.state/'index.sqlite').read_bytes()).hexdigest()
        self.profile.snapshot();self.profile.snapshot()
        self.assertEqual(set(self.state.iterdir()),before);self.assertFalse((self.state/P.STATE_FILE).exists())
        self.assertEqual(hashlib.sha256((self.state/'index.sqlite').read_bytes()).hexdigest(),fingerprint)

    def test_exact_quotes_negation_dates_and_repetition(self):
        item=self.candidate('我坚持研究过程保留原始证据。')
        self.assertEqual(item['mentions'],2);self.assertEqual(item['distinct_days'],2)
        self.assertEqual(item['evidence'][0]['id'],'self-one');self.assertEqual(item['category'],'指令')
        self.assertIn('不喜欢',self.candidate('我不喜欢直接跳过测试。')['quote'])
        self.assertEqual({i['category'] for i in self.profile.snapshot()['candidates']},set(P.CATEGORIES))

    def test_many_repetitions_keep_earliest_and_recent_evidence(self):
        quote='我坚持每次实验保留原始证据。'
        self.rows=[dict(self.rows[0],id='repeat-'+str(i),text=quote,timestamp=1700000000+i*86400) for i in range(12)]
        self.import_rows();item=self.candidate(quote)
        self.assertEqual(item['mentions'],12);self.assertEqual(len(item['evidence']),8)
        ids={e['id'] for e in item['evidence']}
        self.assertIn('repeat-0',ids);self.assertIn('repeat-11',ids)

    def test_monthly_rates_use_effective_self_text_denominator(self):
        months={m['month']:m for m in self.profile.snapshot()['months']}
        self.assertEqual(months['2024-01']['topics']['研究与学习']['messages'],2)
        for month in months.values():
            for v in month['topics'].values():self.assertEqual(v['per_100_messages'],round(100*v['messages']/month['messages'],1))

    def test_confirm_correct_restart_export_selected_only(self):
        item=self.candidate();self.confirm(item,text='我坚持先核对证据再作决定。')
        restarted=P.PersonalProfile(self.state);data=restarted.snapshot()
        self.assertEqual(data['items'][0]['text'],'我坚持先核对证据再作决定。')
        export=restarted.export(dict(ids=[item['id']],expected_revision=data['revision']))
        self.assertIn('我坚持先核对证据再作决定。',export['text']);self.assertNotIn(item['id'],export['text'])
        self.assertNotIn('人工测试会话',export['text']);self.assertFalse(export['includes_raw_chat'])
        self.assertIn('原句始于',export['text']);self.assertIn('本人确认',export['text'])
        self.assertEqual(data['items'][0]['quote'],item['quote'])
        if __import__('os').name=='posix':self.assertEqual((self.state/P.STATE_FILE).stat().st_mode & 0o777,0o600)

    def test_unconfirmed_and_dismissed_are_not_exportable(self):
        item=self.candidate()
        with self.assertRaises(SourceChangedError):self.profile.export(dict(ids=[item['id']],expected_revision=0))
        self.profile.mutate(dict(operation='dismiss',id=item['id'],source_fingerprint=item['source_fingerprint'],expected_revision=0))
        with self.assertRaises(SourceChangedError):self.profile.export(dict(ids=[item['id']],expected_revision=1))

    def test_revision_conflict_preserves_latest_edit(self):
        item=self.candidate();self.confirm(item,revision=0)
        with self.assertRaises(P.ProfileConflictError):self.confirm(item,text='我希望覆盖另一个编辑。',revision=0)
        self.assertEqual(self.profile.snapshot()['items'][0]['text'],item['quote'])

    def test_two_instances_cannot_overwrite_each_other(self):
        item=self.candidate()
        def writer(text):
            try:self.confirm(item,text=text,revision=0,profile=P.PersonalProfile(self.state));return 'saved'
            except (P.ProfileConflictError,AppError):return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(writer,['我希望保留甲的编辑。','我希望保留乙的编辑。']))
        self.assertEqual(sorted(results),['conflict','saved'])

    def test_source_changes_pause_export_but_preserve_manual_text(self):
        item=self.candidate('我更喜欢先做本地验证。');self.confirm(item)
        self.rows[5]['text']='我现在更喜欢先核对公开资料。';self.import_rows()
        stored=self.profile.snapshot()['items'][0];self.assertNotEqual(stored['source_state'],'unchanged')
        self.assertEqual(stored['text'],item['quote'])
        with self.assertRaises(SourceChangedError):self.profile.export(dict(ids=[item['id']],expected_revision=1))

    def test_added_evidence_requires_fresh_confirmation(self):
        item=self.candidate('我坚持研究过程保留原始证据。');self.confirm(item)
        self.rows.append(dict(self.rows[0],id='new-evidence',timestamp=1720000000));self.import_rows()
        stored=self.profile.snapshot()['items'][0];self.assertEqual(stored['source_state'],'changed')
        with self.assertRaises(SourceChangedError):self.confirm(item,revision=1)
        self.confirm(stored['current_candidate'],revision=1)
        self.assertEqual(self.profile.snapshot()['items'][0]['source_state'],'unchanged')

    def test_exclude_and_include_conversation_changes_profile_not_messages(self):
        item=self.candidate();self.confirm(item)
        old=(self.state/'index.sqlite').read_bytes()
        self.profile.mutate(dict(operation='exclude_conversation',conversation_id='c',expected_revision=1))
        data=self.profile.snapshot();self.assertFalse(data['candidates']);self.assertFalse(data['topics']);self.assertEqual(data['items'][0]['source_state'],'excluded')
        with self.assertRaises(SourceChangedError):self.profile.export(dict(ids=[item['id']],expected_revision=2))
        self.profile.mutate(dict(operation='include_conversation',conversation_id='c',expected_revision=2))
        self.assertEqual(self.profile.snapshot()['items'][0]['source_state'],'unchanged');self.assertEqual((self.state/'index.sqlite').read_bytes(),old)

    def test_delete_removes_saved_prose_and_suppresses_exact_candidate(self):
        item=self.candidate();self.confirm(item,text='这是一段删除测试的人工修订。')
        self.profile.mutate(dict(operation='delete',id=item['id'],expected_revision=1))
        raw=(self.state/P.STATE_FILE).read_text();self.assertNotIn('删除测试',raw);self.assertNotIn(item['quote'],raw)
        data=P.PersonalProfile(self.state).snapshot();self.assertEqual(data['items'],[]);self.assertNotIn(item['id'],{i['id'] for i in data['candidates']})
        self.assertEqual(data['suppressed_count'],1)
        self.assertTrue(server.WorldModel(self.state).evidence(item['evidence'][0]['id']))

    def test_delete_works_when_original_source_is_gone(self):
        item=self.candidate();self.confirm(item);(self.state/'index.sqlite').unlink()
        self.profile.mutate(dict(operation='delete',id=item['id'],expected_revision=1));self.assertFalse(self.profile.controls()['items'])

    def test_rules_filter_contacts_sensitive_quotes_and_hypotheticals(self):
        for text in ('我的密码是 abcdefg','我喜欢金融政治研究。','我希望你联系 test@example.invalid。','我喜欢“这句话”。','如果我是老师，我喜欢写作。','我喜欢 12345678901。','我正在粘贴 <script>文本</script>。'):
            self.assertFalse(P.statements(text),text)
        self.assertEqual(P.statements('我更喜欢先核对原始证据。')[0][1],'我更喜欢先核对原始证据。')

    def test_no_data_and_truncation_are_explicit(self):
        with mock.patch.object(P,'MAX_MESSAGES',2):self.assertTrue(self.profile.snapshot()['coverage']['truncated'])
        (self.state/'index.sqlite').unlink();self.assertFalse(self.profile.snapshot()['ready'])

    def test_invalid_and_sensitive_edits_do_not_write(self):
        item=self.candidate()
        for text in ('','我希望保留 password secret。','x'*401):
            with self.assertRaises(ValueError):self.confirm(item,text=text if text else ' ',revision=0)
        with self.assertRaises(ValueError):self.profile.mutate(dict(operation='confirm',expected_revision=True))
        self.assertFalse((self.state/P.STATE_FILE).exists())

    def test_source_replacement_during_read_or_save_is_rejected(self):
        item=self.candidate();before=set(self.state.iterdir())
        with mock.patch.object(self.profile.world,'source_signature',side_effect=[(1,1,1),(1,1,1),(1,1,1),(2,2,2)]):
            with self.assertRaises(SourceChangedError):self.confirm(item)
        self.assertFalse((self.state/P.STATE_FILE).exists())
        self.assertTrue(before<=set(self.state.iterdir()))

    def test_supported_archive_writer_and_profile_edit_share_lock(self):
        item=self.candidate()
        with StateLock(self.state):
            with self.assertRaises(AppError):self.confirm(item)


class ProfileHTTPTests(unittest.TestCase):
    setUp=ProfileTests.setUp
    tearDown=ProfileTests.tearDown
    import_rows=ProfileTests.import_rows

    def test_authenticated_same_origin_routes_and_literal_edit(self):
        host=server.ViewerServer(0,self.state,'synthetic-profile-access')
        worker=threading.Thread(target=host.serve_forever,daemon=True);worker.start()
        def call(method,path,body=None,auth=True,origin=True):
            port=host.server_address[1];headers={}
            if auth:headers['Cookie']=host.cookie_name+'=synthetic-profile-access'
            if method=='POST':
                headers['Content-Type']='application/json'
                if origin:headers['Origin']='http://127.0.0.1:'+str(port)
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=10)
            try:
                conn.request(method,path,json.dumps(body) if body is not None else None,headers)
                response=conn.getresponse();raw=response.read();return response.status,json.loads(raw)
            finally:conn.close()
        try:
            self.assertEqual(call('GET','/api/profile',auth=False)[0],401)
            code,data=call('GET','/api/profile');self.assertEqual(code,200);self.assertTrue(data['synthetic'])
            item=data['candidates'][0];body=dict(operation='confirm',id=item['id'],source_fingerprint=item['source_fingerprint'],expected_revision=0)
            self.assertEqual(call('POST','/api/profile/update',body,auth=False)[0],401)
            self.assertEqual(call('POST','/api/profile/update',body,origin=False)[0],403)
            self.assertEqual(call('POST','/api/profile/update',body)[0],200)
            conflict=call('POST','/api/profile/update',body);self.assertEqual(conflict[0],409);self.assertEqual(conflict[1]['code'],'profile_conflict')
            self.assertEqual(call('POST','/api/profile/export',dict(ids=[item['id']],expected_revision=1))[0],200)
            self.assertEqual(call('POST','/api/profile/export',dict(ids=[],expected_revision=1))[0],400)
            self.assertEqual(call('POST','/api/send',body)[0],405)
        finally:host.shutdown();host.server_close();worker.join(timeout=2)
