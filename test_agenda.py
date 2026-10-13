"""Handwritten scheduling fixtures only. No owner data or external accounts."""
import datetime as dt
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import server
from app_config import AppError, StateLock
from world_model import SourceChangedError
from agenda_dates import DAY, date_hints, extract, local_epoch, iso
from agenda_calendar import calendar_text, conflicts, event_fields
from personal_agenda import PersonalAgenda, AgendaConflictError, STATE_FILE

NOW=local_epoch('2030-06-11T08:00')
ANCHOR=local_epoch('2030-06-10T10:00')


def fixture_rows(anchor=ANCHOR):
    def row(mid,content,self=True,chat='a',offset=0):
        return dict(id='demo:agenda:'+mid,conversation_id=chat,sender_name='人工示例人物',is_self=self,type='text',text=content,timestamp=anchor+offset)
    return [row('clear','我明天下午三点到四点来开会，地点：测试会议室。'),
            row('invite','下周三14:00-15:00来参观，可以吗？',False,'b'),
            row('vague','下周有空来吃饭吧。',False,'c'),
            row('group','明天下午三点到四点开会。',False,'group'),
            row('old','2029年6月10日15:00-16:00开会。'),
            row('cancel','明天下午三点开会取消了。',False,'cancel'),
            row('change','明天会议改到下午五点到六点。',False,'change'),
            row('forward','转发：明天下午三点开会。',False,'forward'),
            row('foreign','明天美国时间下午三点开会。',False,'foreign'),
            row('unknown','明天15:00-16:00开会。',None,'unknown'),
            row('pw:other','明天下午三点开会。',True,'pw:other:a')]


class DateTests(unittest.TestCase):
    def test_relative_date_is_anchored_to_message(self):
        h=date_hints('明天下午三点半到四点半开会',ANCHOR)
        self.assertEqual(h['date'],'2030-06-11')
        self.assertEqual(h['start'],local_epoch('2030-06-11T15:30'))
        self.assertEqual(h['end']-h['start'],3600)
        self.assertEqual(date_hints('后天15:00开会',ANCHOR)['date'],'2030-06-12')

    def test_explicit_dates_and_week(self):
        for s in ('2030年6月15日','2030-06-15','2030/6/15'):
            self.assertEqual(date_hints(s+' 09:00-10:00开会',ANCHOR)['date'],'2030-06-15')
        self.assertEqual(date_hints('下周三14:00开会',ANCHOR)['date'],'2030-06-19')
        self.assertEqual(date_hints('本周三14:00开会',ANCHOR)['date'],'2030-06-12')
        self.assertEqual(date_hints('周三14:00开会',ANCHOR)['date'],'2030-06-12')

    def test_ambiguous_or_multiple_times_not_silently_chosen(self):
        for s in ('明天三点开会','下周开会','明天下午开会','明天15:00或者16:00开会','明天15:00后天16:00开会','明天美国时间下午三点开会','2030年2月30日15:00开会'):
            self.assertIsNone(date_hints(s,ANCHOR)['start'],s)
        self.assertIsNone(date_hints('明天23:00-01:00开会',ANCHOR)['start'])
        self.assertIsNone(date_hints('明天15:00开会',ANCHOR)['end'])

    def test_cross_year_not_relative_to_scan_year(self):
        anchor=local_epoch('2029-12-31T20:00')
        self.assertEqual(date_hints('明天15:00开会',anchor)['date'],'2030-01-01')
        h=date_hints('1月2日15:00开会',anchor)
        self.assertEqual(h['date'],'2029-01-02');self.assertTrue(h['warnings'])

    def test_recurring_and_midnight_not_silently_guessed(self):
        for text in ('每周三15:00-16:00开会','明天晚上十二点开会','明天上午十二点开会'):
            self.assertIsNone(date_hints(text,ANCHOR)['start'])
        self.assertEqual(date_hints('明年1月2日15:00开会',ANCHOR)['date'],'2031-01-02')

    def test_dst_ambiguity_and_gap_rejected(self):
        for t in ('2026-03-08T02:30','2026-11-01T01:30'):
            with self.assertRaises(ValueError):local_epoch(t,'America/New_York')
        self.assertIsInstance(local_epoch('2026-11-01T03:30','America/New_York'),int)

    def test_literal_role_and_tentative_not_confirmed(self):
        rows=fixture_rows()
        self.assertEqual(extract(rows[0],NOW)['status'],'review')
        self.assertEqual(extract(rows[1],NOW)['participation'],'incoming_invitation')
        self.assertTrue(extract(rows[1],NOW)['warnings'])
        self.assertEqual(extract(dict(rows[3],is_group=1),NOW)['participation'],'group_reference')
        self.assertEqual(extract(rows[4],NOW)['status'],'expired')
        self.assertEqual(extract(rows[5],NOW)['status'],'cancel_signal')
        self.assertIsNone(extract(rows[7],NOW))


class AgendaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.state=Path(self.tmp.name);self.rows=fixture_rows();self.import_rows()
        self.agenda=PersonalAgenda(self.state,clock=lambda:NOW)
    def tearDown(self):self.tmp.cleanup()
    def import_rows(self):
        source=self.state/'source.json'
        chats=[dict(id=i,name='人工会话',is_group=i=='group') for i in sorted({r['conversation_id'] for r in self.rows})]
        source.write_text(json.dumps(dict(manifest=dict(synthetic=True,complete=True),messages=self.rows,conversations=chats)))
        server.import_archive(source,self.state)
    def candidate(self,mid='clear'):
        return next(c for c in self.agenda.compute(NOW)['candidates'].values() if c['message_id']=='demo:agenda:'+mid)
    def body(self,candidate=True,**extra):
        c=self.candidate() if candidate else None
        return dict(operation='confirm',source_id=c['id'] if c else None,source_fingerprint=c['source_fingerprint'] if c else None,
                    expected_revision=self.agenda.controls()['revision'],reviewed=True,ack_scope=True,
                    title='人工评审会议',location='测试地点',timezone='Asia/Shanghai',all_day=False,
                    start='2030-06-11T15:00',end='2030-06-11T16:00',before_minutes=20,after_minutes=10,**extra)

    def test_read_is_private_and_side_effect_free(self):
        files=set(self.state.iterdir());old=(self.state/'index.sqlite').read_bytes()
        d=self.agenda.snapshot();self.assertTrue(d['synthetic']);self.assertFalse(d['events'])
        self.assertNotIn('demo:agenda:pw:other',{c['message_id'] for c in d['candidates']})
        self.assertGreater(d['counts']['expired'],0);self.assertEqual(files,set(self.state.iterdir()))
        self.assertEqual(old,(self.state/'index.sqlite').read_bytes());self.assertFalse((self.state/STATE_FILE).exists())

    def test_confirmation_requires_complete_time_and_owner_review(self):
        b=self.body();b['reviewed']=False
        with self.assertRaises(ValueError):self.agenda.update(b)
        b=self.body();b['end']=''
        with self.assertRaises(ValueError):self.agenda.update(b)
        self.assertFalse((self.state/STATE_FILE).exists())

    def test_confirm_preview_restart_and_source_preserved(self):
        b=self.body();old=(self.state/'index.sqlite').read_bytes()
        self.assertTrue(self.agenda.preview(b)['ok']);self.assertFalse((self.state/STATE_FILE).exists())
        result=self.agenda.update(b);d=PersonalAgenda(self.state,lambda:NOW).snapshot()
        self.assertEqual(len(d['events']),1);self.assertEqual(d['events'][0]['source_state'],'unchanged')
        self.assertEqual(d['events'][0]['reserved_from'],iso(local_epoch('2030-06-11T14:40')))
        self.assertEqual(old,(self.state/'index.sqlite').read_bytes())
        self.assertEqual(d['events'][0]['id'],result['id'])
        self.assertEqual((self.state/STATE_FILE).stat().st_mode&0o777,0o600)

    def test_same_source_upserts_not_duplicate(self):
        b=self.body();self.agenda.update(b);b=self.body();self.agenda.update(b)
        self.assertEqual(len(self.agenda.snapshot()['events']),1)

    def test_version_conflict_cannot_overwrite(self):
        b=self.body();self.agenda.update(b)
        with self.assertRaises(AgendaConflictError):self.agenda.update(b)

    def test_time_and_buffer_conflicts_and_alternatives(self):
        self.agenda.update(self.body());b=self.body(False);b.update(start='2030-06-11T16:15',end='2030-06-11T17:00')
        p=self.agenda.preview(b);self.assertFalse(p['ok']);self.assertEqual(p['conflicts'][0]['kind'],'准备/交通预留不足');self.assertTrue(p['alternatives'])
        with self.assertRaises(AgendaConflictError):self.agenda.update(b)
        b.update(start='2030-06-11T16:30',end='2030-06-11T17:00');self.assertTrue(self.agenda.preview(b)['ok'])

    def test_follow_up_change_invalidates_confirmed_source(self):
        self.agenda.update(self.body());self.rows.append(dict(self.rows[0],id='demo:agenda:followup',text='时间改期了，另约。',timestamp=ANCHOR+60));self.import_rows()
        e=self.agenda.snapshot()['events'][0];self.assertEqual(e['source_state'],'changed_or_unavailable')
        self.assertEqual(e['current_source']['possible_change_count'],1)
        with self.assertRaises(SourceChangedError):self.agenda.export(dict(ids=[e['id']],expected_revision=1,ack_scope=True))
        b=self.body();self.agenda.update(b);self.assertEqual(self.agenda.snapshot()['events'][0]['source_state'],'unchanged')

    def test_unrelated_conversation_change_does_not_invalidate(self):
        self.agenda.update(self.body());self.rows.append(dict(self.rows[0],id='demo:agenda:other-change',conversation_id='b',text='取消了。',timestamp=ANCHOR+60));self.import_rows()
        self.assertEqual(self.agenda.snapshot()['events'][0]['source_state'],'unchanged')

    def test_cancel_signal_cannot_be_confirmed_and_tentative_not_exported(self):
        b=self.body();c=self.candidate('cancel');b.update(source_id=c['id'],source_fingerprint=c['source_fingerprint'])
        with self.assertRaises(ValueError):self.agenda.update(b)
        with self.assertRaises(SourceChangedError):self.agenda.export(dict(ids=[c['id']],expected_revision=0,ack_scope=True))

    def test_cancellation_releases_local_slot_not_external(self):
        result=self.agenda.update(self.body());self.agenda.update(dict(operation='cancel',id=result['id'],expected_revision=1))
        d=self.agenda.snapshot();self.assertEqual(d['events'][0]['status'],'cancelled')
        with self.assertRaises(SourceChangedError):self.agenda.export(dict(ids=[result['id']],expected_revision=2,ack_scope=True))
        self.assertTrue(self.agenda.preview(self.body(False))['ok'])

    def test_source_cannot_be_removed_to_bypass_verification(self):
        r=self.agenda.update(self.body());b=self.body(False);b['id']=r['id']
        with self.assertRaises(ValueError):self.agenda.update(b)

    def test_export_uid_private_fields_and_utf8_folding(self):
        b=self.body();b['title']='人工测试'+('长文本;逗号,反斜线\\'*12);b['title']=b['title'][:160]
        result=self.agenda.update(b);request=dict(ids=[result['id']],expected_revision=1,ack_scope=True,include_buffers=True)
        out=self.agenda.export(request);content=out['text'];self.assertEqual(content.count('BEGIN:VEVENT'),3)
        self.assertNotIn('demo:agenda:',content);self.assertNotIn('下午三点',content);self.assertNotIn('ATTENDEE',content);self.assertNotIn('VALARM',content);self.assertNotIn('METHOD:REQUEST',content)
        self.assertTrue(all(len(line.encode())<=75 for line in content.split('\r\n')))
        self.assertIn('DTSTART:20300611T070000Z',content)
        again=self.agenda.export(request)['text'];self.assertEqual(content,again)
        self.assertIn('\\;',content);self.assertIn('\\,',content)

    def test_all_day_exclusive_end_and_expiration(self):
        b=self.body(False);b.update(all_day=True,start='2030-06-12',end='2030-06-13',before_minutes=0,after_minutes=0)
        r=self.agenda.update(b);out=self.agenda.export(dict(ids=[r['id']],expected_revision=1,ack_scope=True))['text']
        self.assertIn('DTSTART;VALUE=DATE:20300612',out);self.assertIn('DTEND;VALUE=DATE:20300613',out)
        later=PersonalAgenda(self.state,lambda:NOW+4*DAY)
        with self.assertRaises(SourceChangedError):later.export(dict(ids=[r['id']],expected_revision=1,ack_scope=True))

    def test_external_busy_snapshot_and_staleness_are_explicit(self):
        data=dict(operation='busy_snapshot',source='人工日历快照',captured_at=NOW,expected_revision=0,
                  **{'from':NOW,'to':NOW+7*DAY},windows=[dict(start=local_epoch('2030-06-11T15:30'),end=local_epoch('2030-06-11T17:00'))])
        self.agenda.update(data);self.assertTrue(self.agenda.snapshot()['busy_scope']['available'])
        self.assertFalse(self.agenda.preview(self.body())['ok'])
        self.assertTrue(PersonalAgenda(self.state,lambda:NOW+7200).snapshot()['busy_scope']['stale'])
        with self.assertRaises(ValueError):self.agenda.update(dict(data,expected_revision=1,captured_at=NOW+1))

    def test_lock_and_source_replacement_protect_writes(self):
        with StateLock(self.state):
            with self.assertRaises(AppError):self.agenda.update(self.body())
        b=self.body()
        with mock.patch.object(self.agenda.world,'source_signature',side_effect=[(1,),(1,),(1,),(2,)]):
            with self.assertRaises(SourceChangedError):self.agenda.update(b)
        self.assertFalse((self.state/STATE_FILE).exists())

    def test_html_newlines_and_empty_export_rejected(self):
        b=self.body(False);b['title']='test\nBEGIN:VEVENT'
        with self.assertRaises(ValueError):self.agenda.update(b)
        with self.assertRaises(ValueError):self.agenda.export(dict(ids=[],expected_revision=0,ack_scope=True))

    def test_truncation_and_no_index_are_explicit(self):
        import personal_agenda
        with mock.patch.object(personal_agenda,'MAX_MESSAGES',2):self.assertTrue(self.agenda.snapshot()['coverage']['truncated'])
        (self.state/'index.sqlite').unlink();self.assertFalse(self.agenda.snapshot()['ready'])
        self.agenda.update(self.body(False));self.assertEqual(self.agenda.snapshot()['events'][0]['source_state'],'manual')

    def test_http_auth_origin_preview_and_export(self):
        host=server.ViewerServer(0,self.state,'synthetic-agenda-session');host.agenda=self.agenda
        worker=threading.Thread(target=host.serve_forever,daemon=True);worker.start()
        def call(method,path,body=None,auth=True,origin=True):
            port=host.server_address[1];headers={}
            if auth:headers['Cookie']=host.cookie_name+'=synthetic-agenda-session'
            if method=='POST':
                headers['Content-Type']='application/json'
                if origin:headers['Origin']='http://127.0.0.1:'+str(port)
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=10)
            try:
                conn.request(method,path,json.dumps(body) if body else None,headers);r=conn.getresponse();raw=r.read()
                return r.status,json.loads(raw) if r.getheader('Content-Type','').startswith('application/json') else raw
            finally:conn.close()
        try:
            self.assertEqual(call('GET','/api/agenda',auth=False)[0],401)
            self.assertEqual(call('GET','/api/agenda')[0],200)
            self.assertEqual(call('GET','/agenda')[0],200);self.assertEqual(call('GET','/agenda.js')[0],200)
            b=self.body();self.assertEqual(call('POST','/api/agenda/update',b,origin=False)[0],403)
            self.assertEqual(call('POST','/api/agenda/preview',b)[0],200)
            result=call('POST','/api/agenda/update',b);self.assertEqual(result[0],200)
            self.assertEqual(call('POST','/api/agenda/update',b)[0],409)
            self.assertEqual(call('POST','/api/agenda/export',dict(ids=[result[1]['id']],expected_revision=1,ack_scope=True))[0],200)
            self.assertEqual(call('POST','/api/send',b)[0],405)
        finally:host.shutdown();host.server_close();worker.join(timeout=2)

if __name__=='__main__':unittest.main()
