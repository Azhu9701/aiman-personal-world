"""Local itinerary validation, conflict checks and RFC5545 export. No network."""
from __future__ import annotations
import datetime as dt
import re
from zoneinfo import ZoneInfo
from agenda_dates import SOURCE_ZONE, UTC, DAY, local_epoch, iso


def text(value, limit, required=False):
    if not isinstance(value,str) or len(value)>limit or re.search(r'[\x00-\x1f\x7f]',value):
        raise ValueError('字段包含控制字符或超过长度限制')
    value=value.strip()
    if required and not value: raise ValueError('请填写日程标题')
    return value


def event_fields(body, now):
    title=text(body.get('title',''),160,True)
    place=text(body.get('location',''),250)
    zone=text(body.get('timezone',SOURCE_ZONE),60,True)
    ZoneInfo(zone)
    all_day=body.get('all_day',False)
    if type(all_day) is not bool: raise ValueError('全天标记无效')
    start=text(body.get('start',''),32,True); end=text(body.get('end',''),32,True)
    if all_day:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',start) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',end):
            raise ValueError('全天日程需要开始日期及不包含在内的结束日期')
        a=local_epoch(start+'T00:00',zone); b=local_epoch(end+'T00:00',zone)
    else:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',start) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',end):
            raise ValueError('请补全开始和结束时间，不会自动填写时长')
        a=local_epoch(start,zone); b=local_epoch(end,zone)
    if b<=a or b-a>31*DAY: raise ValueError('结束须晚于开始；一次日程上限31天')
    if b<=now or (not all_day and a<=now) or a>now+730*DAY:
        raise ValueError('只接受未来两年内尚未开始的日程；全天事项须尚未结束')
    pre=body.get('before_minutes',0); post=body.get('after_minutes',0)
    if any(type(x) is not int or not 0<=x<=1440 for x in (pre,post)): raise ValueError('预留时间须为0到1440分钟')
    if all_day and (pre or post): raise ValueError('全天事项不设置分钟级交通预留')
    return dict(title=title,location=place,timezone=zone,all_day=all_day,start_local=start,end_local=end,
                start=a,end=b,before_minutes=pre,after_minutes=post)


def window(event):
    return event['start']-event.get('before_minutes',0)*60,event['end']+event.get('after_minutes',0)*60


def overlap(a,b):
    return a[0]<b[1] and b[0]<a[1]


def conflicts(event, events, busy=None):
    result=[]
    for other in events:
        if other.get('status')!='confirmed' or other.get('id')==event.get('id'): continue
        if overlap(window(event),window(other)):
            kind='时间重叠' if overlap((event['start'],event['end']),(other['start'],other['end'])) else '准备/交通预留不足'
            result.append(dict(kind=kind,id=other['id'],title=other['title'],start=other['start'],end=other['end']))
    for i,item in enumerate((busy or {}).get('windows',[])):
        if overlap(window(event),(item['start'],item['end'])):
            result.append(dict(kind='已有日历忙碌时段',id='busy:'+str(i),title='导入的忙碌时段',**item))
    return result


def suggestions(event,events,busy,now):
    """Later possible local slots, NOT permission to reschedule an appointment."""
    if event['all_day']:return []
    duration=event['end']-event['start']; result=[]
    if duration>8*3600:return result
    start_dt=dt.datetime.fromtimestamp(max(now,event['start']),ZoneInfo(event['timezone']))
    for d in range(7):
        day=start_dt.date()+dt.timedelta(days=d)
        for minute in range(9*60,18*60,15):
            a=local_epoch(day.isoformat()+f'T{minute//60:02d}:{minute%60:02d}',event['timezone'])
            limit=local_epoch(day.isoformat()+'T18:00',event['timezone'])
            trial={**event,'start':a,'end':a+duration}
            if a<=event['start'] or a<=now or window(trial)[1]>limit:continue
            if not conflicts(trial,events,busy):
                result.append({'start':iso(a,event['timezone']),'end':iso(a+duration,event['timezone'])})
                if len(result)==3:return result
    return result


def escape(value):
    return value.replace('\\','\\\\').replace('\r\n','\n').replace('\r','\n').replace('\n','\\n').replace(';','\\;').replace(',','\\,')


def fold(line):
    # RFC5545 3.1: fold at <=75 UTF-8 octets without splitting a codepoint.
    out=[]; current=''
    for character in line:
        if len((current+character).encode('utf-8'))>75:
            out.append(current);current=' '+character
        else:current+=character
    return '\r\n'.join(out+[current])


def utc_stamp(value):
    return dt.datetime.fromtimestamp(value,UTC).strftime('%Y%m%dT%H%M%SZ')


def calendar_text(events,now,include_buffers=False):
    lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//AIMAN//Personal World//ZH','CALSCALE:GREGORIAN']
    def add(e,uid,title,start,end,all_day=False):
        lines.extend(['BEGIN:VEVENT','UID:'+uid+'@personal-world.local','DTSTAMP:'+utc_stamp(now),
                      'SEQUENCE:'+str(e.get('sequence',0)),'CLASS:PRIVATE','STATUS:CONFIRMED','TRANSP:OPAQUE',
                      'SUMMARY:'+escape(title)])
        if all_day:
            lines.extend(['DTSTART;VALUE=DATE:'+e['start_local'].replace('-',''),
                          'DTEND;VALUE=DATE:'+e['end_local'].replace('-','')])
        else:lines.extend(['DTSTART:'+utc_stamp(start),'DTEND:'+utc_stamp(end)])
        if e['location']:lines.append('LOCATION:'+escape(e['location']))
        lines.append('DESCRIPTION:'+escape('本人确认的个人安排；不附带聊天原文。准备/交通预留为本人填写，并非导航估时。'))
        lines.append('END:VEVENT')
    for e in sorted(events,key=lambda x:(x['start'],x['uid'])):
        add(e,e['uid'],e['title'],e['start'],e['end'],e['all_day'])
        if include_buffers and not e['all_day']:
            a,b=window(e)
            if a<e['start']:add(e,e['uid']+'-before','准备/交通预留 · '+e['title'],a,e['start'])
            if b>e['end']:add(e,e['uid']+'-after','结束后预留 · '+e['title'],e['end'],b)
    lines.append('END:VCALENDAR')
    return '\r\n'.join(fold(line) for line in lines)+'\r\n'
