"""Evidence-linked agenda inbox and owner-confirmed local itinerary.
No calendar account, external messages, bookings or model calls. Reads never
persist candidates. Source/archive/profile/draft files are never modified.
"""
from __future__ import annotations
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from app_config import AppError, StateLock, atomic_json, read_private_json
from world_model import WorldModel, SourceChangedError
from agenda_dates import SOURCE_ZONE, DAY, UTC, CANCEL, CHANGE, extract, iso
from agenda_calendar import event_fields, conflicts, suggestions, calendar_text, text, window

STATE_FILE='agenda-state.json'
MAX_MESSAGES=50000
LOOKBACK_DAYS=90
MAX_CANDIDATES=200
VERSION=1


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class AgendaConflictError(ValueError):
    pass


class PersonalAgenda:
    def __init__(self,state,clock=None):
        self.state=Path(state);self.world=WorldModel(state);self.clock=clock or time.time

    def controls(self):
        path=self.state/STATE_FILE
        if not path.exists() and not path.is_symlink():
            return {'revision':0,'events':{},'dismissed':[],'busy':None}
        data=read_private_json(path,limit=4*1024*1024)
        if (type(data.get('revision')) is not int or not isinstance(data.get('events'),dict)
                or not isinstance(data.get('dismissed'),list)):
            raise ValueError('本机行程文件格式无效')
        return data

    def write(self,controls):
        if len(controls['events'])>1000 or len(controls['dismissed'])>10000:
            raise ValueError('本版行程数量已达上限')
        controls['revision']+=1
        if len(json.dumps(controls,ensure_ascii=False,indent=2).encode())>4*1024*1024:
            raise ValueError('本机行程文件超出容量，未覆盖原记录')
        atomic_json(self.state/STATE_FILE,controls)

    def compute(self,now):
        data={'ready':False,'synthetic':False,'candidates':{},'counts':{},'coverage':{}}
        if not (self.state/'index.sqlite').exists():return data
        signature=self.world.source_signature()
        with self.world.index() as conn:
            manifest=conn.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
            manifest=json.loads(manifest[0]) if manifest else {}
            rows=[dict(r) for r in conn.execute("""SELECT m.id,m.conversation_id,m.timestamp,m.is_self,m.text,c.is_group
                FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE m.type='text' AND m.timestamp>=? AND m.timestamp<=?
                AND m.id NOT GLOB 'pw:*' AND m.conversation_id NOT GLOB 'pw:*'
                ORDER BY m.timestamp DESC,m.id DESC LIMIT ?""",(now-LOOKBACK_DAYS*DAY,now,MAX_MESSAGES+1))]
            latest=conn.execute('SELECT max(timestamp) FROM messages').fetchone()[0]
        truncated=len(rows)>MAX_MESSAGES;rows=rows[:MAX_MESSAGES]
        signals=defaultdict(list)
        for row in rows:
            if isinstance(row['text'],str) and (CANCEL.search(row['text']) or CHANGE.search(row['text'])):
                signals[row['conversation_id']].append(row)
        counts=Counter();items={}
        for row in rows:
            candidate=extract(row,now)
            if candidate is None:continue
            identifier='agenda:'+digest(['chat-event',row['id']])[:32]
            deadline=min(candidate.get('start') or now+DAY,row['timestamp']+30*DAY)+DAY
            changes=[s for s in signals[row['conversation_id']] if (s['timestamp'],s['id'])>(row['timestamp'],row['id']) and s['timestamp']<=deadline]
            changes.sort(key=lambda s:(s['timestamp'],s['id']))
            evidence=[{'id':row['id'],'timestamp':row['timestamp'],'role':'source'}]
            evidence += [{'id':s['id'],'timestamp':s['timestamp'],'role':'possible_change'} for s in changes[-12:]]
            if changes:
                candidate['warnings'].append('后续同会话有取消/改期措辞，可能属于其他约定，须查看证据')
            candidate.update(id=identifier,message_id=row['id'],conversation_id=row['conversation_id'],
                             source_timestamp=row['timestamp'],evidence=evidence,possible_change_count=len(changes),
                             source_fingerprint=digest([VERSION,SOURCE_ZONE,row,changes]))
            counts[candidate['status']]+=1;items[identifier]=candidate
        if signature!=self.world.source_signature():raise SourceChangedError('来源读取期间发生变化')
        return {'ready':True,'synthetic':manifest.get('synthetic') is True,'candidates':items,'counts':dict(counts),
                'coverage':{'source_timezone':SOURCE_ZONE,'lookback_days':LOOKBACK_DAYS,'limit':MAX_MESSAGES,
                            'scanned':len(rows),'truncated':truncated,'latest_archive_timestamp':latest,
                            'last_archive_age_hours':round((now-latest)/3600,1) if latest else None}}

    def events(self,controls,candidates,now):
        out=[]
        for identifier,saved in controls['events'].items():
            e={**saved,'id':identifier};source=candidates.get(e.get('source_id'))
            e['source_state']='manual' if not e.get('source_id') else 'unchanged' if source and source['source_fingerprint']==e['source_fingerprint'] else 'changed_or_unavailable'
            e['expired']=e['end']<=now
            e['evidence']=source['evidence'] if source else [{'id':e['source_message_id'],'role':'saved_source'}] if e.get('source_message_id') else []
            e['current_source']=source
            e['start_label']=iso(e['start'],e['timezone']);e['end_label']=iso(e['end'],e['timezone'])
            e['reserved_from']=iso(window(e)[0],e['timezone']);e['reserved_until']=iso(window(e)[1],e['timezone'])
            out.append(e)
        out.sort(key=lambda x:(x['start'],x['id']))
        for e in out:e['conflicts']=conflicts(e,out,controls.get('busy')) if e['status']=='confirmed' else []
        return out

    def busy_scope(self,controls,now):
        busy=controls.get('busy')
        if not busy:return {'available':False,'note':'未导入外部日历；冲突仅检查本机确认行程，不能代表你有空。'}
        return {'available':True,'source':busy['source'],'captured_at':busy['captured_at'],
                'from':busy['from'],'to':busy['to'],'count':len(busy['windows']),
                'stale':now-busy['captured_at']>3600,
                'note':'外部日历是只读快照，不会自动同步；超过一小时须重新核对。忙碌为空不代表其他日历也空闲。'}

    def snapshot(self):
        now=int(self.clock());controls=self.controls();data=self.compute(now)
        events=self.events(controls,data['candidates'],now)
        items=[i for key,i in data['candidates'].items() if key not in controls['dismissed'] and key not in controls['events'] and i['status']!='expired']
        items.sort(key=lambda x:(x['status'] in ('cancel_signal','change_signal'),x['participation'] in ('group_reference','unknown_sender'),x['start'] or now+365*DAY,-x['source_timestamp'],x['id']))
        return {**data,'candidates':items[:MAX_CANDIDATES],'candidate_count':len(items),'candidate_limit':MAX_CANDIDATES,
                'events':events,'revision':controls['revision'],'now':now,'now_label':iso(now),
                'busy_scope':self.busy_scope(controls,now),
                'scope_note':'最近90天主来源文本，最多五万条；含本人、私聊对方及群聊，均仅为待核对线索。相对日期锚定消息时间。规则不会证明约定成立，也不能排除电话或线下取消。'}

    def revision(self,body,controls):
        value=body.get('expected_revision')
        if type(value) is not int or value!=controls['revision']:
            raise AgendaConflictError('行程已在其他窗口改变，请刷新后合并；未覆盖已有安排')

    def validate_event(self,body,controls,data,now):
        if body.get('reviewed') is not True:
            raise ValueError('请确认本人参加、时间地点及取消改期信息均已核对')
        if body.get('ack_scope') is not True:
            raise ValueError('请确认冲突检查只覆盖本机行程及导入的日历快照')
        e=event_fields(body,now)
        source_id=body.get('source_id');identifier=body.get('id')
        if source_id:
            if not isinstance(source_id,str):raise ValueError('来源编号无效')
            item=data['candidates'].get(source_id)
            if not item or item['source_fingerprint']!=body.get('source_fingerprint'):
                raise SourceChangedError('来源已变化，请重新查看证据')
            if item['status'] in ('expired','cancel_signal'):
                raise ValueError('已过期或含取消措辞的消息不能直接成为有效日程；请选用最新约定或手动创建')
            if identifier and identifier!=source_id:raise ValueError('不能更换已保存日程的来源')
            identifier=source_id
            e.update(source_id=source_id,source_message_id=item['message_id'],source_fingerprint=item['source_fingerprint'])
        else:
            if identifier and identifier not in controls['events']:raise KeyError('日程不存在')
            if identifier and controls['events'][identifier].get('source_id'):raise ValueError('不能清除日程来源来绕过来源复核')
            e.update(source_id=None,source_message_id=None,source_fingerprint=None)
        e['id']=identifier
        return e

    def preview(self,body):
        now=int(self.clock());controls=self.controls();self.revision(body,controls)
        data=self.compute(now);e=self.validate_event(body,controls,data,now)
        events=self.events(controls,data['candidates'],now)
        found=conflicts(e,events,controls.get('busy'))
        return {'ok':not found,'event':e,'conflicts':found,'alternatives':suggestions(e,events,controls.get('busy'),now) if found else [],
                'busy_scope':self.busy_scope(controls,now),'note':'候选时间仅表示本机及快照未占用，改期仍须与你约定的人重新确认；未保存。'}

    def update(self,body):
        now=int(self.clock())
        with StateLock(self.state):
            controls=self.controls();self.revision(body,controls);operation=body.get('operation')
            if operation=='cancel':
                identifier=body.get('id')
                if identifier not in controls['events']:raise KeyError('日程不存在')
                controls['events'][identifier]['status']='cancelled'
                controls['events'][identifier]['sequence']+=1
                controls['events'][identifier]['updated_at']=now
            elif operation=='dismiss':
                identifier=body.get('id')
                if not isinstance(identifier,str) or not re.fullmatch(r'agenda:[0-9a-f]{32}',identifier):raise ValueError('候选编号无效')
                controls['dismissed']=sorted(set(controls['dismissed'])|{identifier})
            elif operation=='confirm':
                signature=self.world.source_signature() if (self.state/'index.sqlite').exists() else None
                data=self.compute(now);e=self.validate_event(body,controls,data,now)
                events=self.events(controls,data['candidates'],now)
                if conflicts(e,events,controls.get('busy')):
                    raise AgendaConflictError('时间或准备/交通预留有冲突，请先检查并调整；没有保存')
                identifier=e.pop('id') or 'manual:'+uuid.uuid4().hex
                previous=controls['events'].get(identifier)
                e.update(uid=previous['uid'] if previous else uuid.uuid4().hex,
                         sequence=previous['sequence']+1 if previous else 0,status='confirmed',updated_at=now)
                if signature is not None and signature!=self.world.source_signature():raise SourceChangedError('来源在保存时更新')
                controls['events'][identifier]=e
            elif operation=='busy_snapshot':
                # Explicit external read results only. Never accepts OAuth/account credentials.
                start=body.get('from');end=body.get('to');captured=body.get('captured_at')
                if any(type(v) is not int for v in (start,end,captured)) or not start<end or end-start>93*DAY or not 0<=now-captured<=86400:
                    raise ValueError('日历快照的覆盖范围或读取时间无效')
                source=text(body.get('source','手动导入的忙碌快照'),100,True)
                windows=body.get('windows')
                if not isinstance(windows,list) or len(windows)>2000:raise ValueError('忙碌时段数量无效')
                checked=[]
                for w in windows:
                    a=w.get('start');b=w.get('end')
                    if type(a) is not int or type(b) is not int or not start<=a<b<=end:raise ValueError('忙碌时段超出覆盖范围')
                    checked.append({'start':a,'end':b})
                controls['busy']={'source':source,'from':start,'to':end,'captured_at':captured,'windows':checked}
            else:raise ValueError('不支持此行程操作')
            self.write(controls)
            return {'ok':True,'revision':controls['revision'],'id':locals().get('identifier')}

    def export(self,body):
        with StateLock(self.state):
            controls=self.controls();self.revision(body,controls);now=int(self.clock())
            ids=body.get('ids');buffers=body.get('include_buffers',False)
            if not isinstance(ids,list) or not ids or len(ids)>100 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
                raise ValueError('请明确选择1到100条已确认行程')
            if type(buffers) is not bool or body.get('ack_scope') is not True:raise ValueError('请确认导出及日历覆盖范围')
            data=self.compute(now);events=self.events(controls,data['candidates'],now);mapping={e['id']:e for e in events};chosen=[]
            for identifier in ids:
                e=mapping.get(identifier)
                if not e or e['status']!='confirmed' or e['expired'] or e['source_state'] not in ('unchanged','manual'):
                    raise SourceChangedError('仅能导出未过期、本人确认且来源仍匹配的日程')
                if e['conflicts']:raise AgendaConflictError('请先解决重叠或预留时间冲突')
                chosen.append(e)
            return {'ok':True,'count':len(chosen),'text':calendar_text(chosen,now,buffers),'mime':'text/calendar;charset=utf-8',
                    'busy_scope':self.busy_scope(controls,now),'note':'仅生成日历文件，未写入Google/Apple日历、未设置提醒或邀请任何人。外部导入可能产生副本，取消本机日程不会删除外部副本。'}
