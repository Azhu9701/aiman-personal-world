"""Conservative chat date hints: message-anchored, never automatic bookings."""
from __future__ import annotations
import datetime as dt
import re
from zoneinfo import ZoneInfo

SOURCE_ZONE = 'Asia/Shanghai'
UTC = dt.timezone.utc
DAY = 86400
DATE = re.compile(r'(?<!\d)(?P<y>20\d{2})[-/年](?P<m>\d{1,2})[-/月](?P<d>\d{1,2})[日号]?|(?<!\d)(?P<md>\d{1,2})月(?P<dd>\d{1,2})[日号]?|(?P<rel>大后天|后天|明天|今天|明日|今日)|(?P<w>下下|下|本|这)?(?:周|星期|礼拜)(?P<wd>[一二三四五六日天])')
NUM = r'[零〇一二两三四五六七八九十\d]{1,3}'
CLOCK = re.compile(r'(?P<part>凌晨|早上|上午|中午|下午|晚上|晚间)?\s*(?:(?P<h>\d{1,2})[:：](?P<min>\d{2})|(?P<ch>'+NUM+r')[点时](?P<cm>半|一刻|三刻|'+NUM+r'分?)?)')
CUE = re.compile(r'开会|会议|见面|碰面|面谈|拜访|参观|调研|出差|出发|到达|抵达|航班|高铁|火车|飞机|面试|体检|就诊|预约|看医生|吃饭|午饭|晚饭|聚餐|约饭|到访|签约|交付|截止|提交|演示|路演|参展|展会|对接|碰一下|聊一下|聊一聊|讨论|视频|通话|电话|接你|接我|过来|过去|来访|来一趟|去一趟')
CANCEL = re.compile(r'取消|不去了|不来了|不用来了|别来了|不参加|不出席|无法参加|无法出席|去不了|来不了|作废|停止举办')
CHANGE = re.compile(r'改期|改到|改成|改为|延期|延后|推迟|提前到|换个时间|另约|再约|重新约|时间变了')
TENTATIVE = re.compile(r'可能|暂定|待定|再看|再说|有空|方便吗|行吗|可以吗|要不要|打算|计划|如果|假如|假设|或许|不一定|不确定|是否|[?？]')
QUOTE = re.compile(r'转发|引用|他说|她说|据说|报道|公告|通知大家|报名链接|[“”「」『』<>]|https?://|www\.|```|(?:^|\n)\s*>')
THIRD = re.compile(r'他(?:们)?|她(?:们)?|同事|朋友|客户说|领导说')
VAGUE = re.compile(r'下周|下个月|这周|周末|改天|近期|最近|有空|以后|到时候|月底|月初|春节|国庆')
FOREIGN = re.compile(r'美国时间|纽约时间|洛杉矶时间|伦敦时间|日本时间|当地时间|美西|美东|UTC|GMT|PST|PDT|EST|EDT', re.I)
LOCATION = re.compile(r'(?:地点|地址|位置)[:：]\s*([^\n，,。；;]{2,100})')


def integer(value):
    if value.isdigit():
        return int(value)
    digits = dict(zip('零〇一二两三四五六七八九', (0,0,1,2,2,3,4,5,6,7,8,9)))
    if '十' in value:
        a,b = value.split('十', 1)
        return (digits[a] if a else 1)*10 + (digits[b] if b else 0)
    return digits[value]


def local_epoch(value, zone=SOURCE_ZONE):
    """Reject ambiguous/nonexistent DST wall times rather than guessing fold."""
    naive = dt.datetime.fromisoformat(value)
    if naive.tzinfo is not None:
        raise ValueError('使用当地时间，并明确选择时区')
    tz, times = ZoneInfo(zone), set()
    for fold in (0,1):
        candidate = naive.replace(tzinfo=tz, fold=fold)
        if candidate.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive:
            times.add(int(candidate.timestamp()))
    if len(times) != 1:
        raise ValueError('夏令时切换导致时间不存在或有歧义，请核对')
    return times.pop()


def iso(value, zone=SOURCE_ZONE):
    return dt.datetime.fromtimestamp(value, ZoneInfo(zone)).isoformat(timespec='minutes')


def date_hints(text, timestamp, zone=SOURCE_ZONE):
    base = dt.datetime.fromtimestamp(timestamp, ZoneInfo(zone))
    matches = list(DATE.finditer(text))
    result = dict(date=None, start=None, end=None, timezone=zone,
                  anchor=base.isoformat(timespec='minutes'), date_expression=None,
                  time_expression=None, warnings=[])
    if len(matches) != 1:
        result['warnings'].append('没有唯一明确日期' if not matches else '多个日期须分别核对')
        return result
    match = matches[0]; g = match.groupdict()
    result['date_expression'] = match.group()
    try:
        if g['y']:
            day = dt.date(int(g['y']), int(g['m']), int(g['d']))
        elif g['md']:
            year=base.year+1 if text[max(0,match.start()-2):match.start()]=='明年' else base.year
            day = dt.date(year, int(g['md']), int(g['dd']))
            result['warnings'].append('缺年份，暂按消息当年解释；跨年须核对')
        elif g['rel']:
            day = base.date()+dt.timedelta(days={'今天':0,'今日':0,'明天':1,'明日':1,'后天':2,'大后天':3}[g['rel']])
        else:
            wd = '一二三四五六日'.index(g['wd'].replace('天','日'))
            if g['w'] is None:
                day = base.date()+dt.timedelta(days=(wd-base.weekday())%7)
                result['warnings'].append('未写哪一周，暂按消息日起最近该星期解释')
            else:
                day = base.date()+dt.timedelta(days=-base.weekday()+{'本':0,'这':0,'下':7,'下下':14}[g['w']]+wd)
        result['date'] = day.isoformat()
    except (ValueError, OverflowError):
        result['warnings'].append('日期无效，须补全')
        return result
    if FOREIGN.search(text):
        result['warnings'].append('涉及其他或当地时区，须手动确认时区')
        return result
    if re.search(r'每(?:周|月|天|个星期)|隔周|每逢',text):
        result['warnings'].append('周期安排需要另行确认，本版不自动展开重复日程')
        return result
    clocks = list(CLOCK.finditer(text))
    if not clocks or len(clocks)>2:
        result['warnings'].append('没有唯一明确起止钟点；不猜测上午/下午')
        return result
    inherited, times = None, []
    try:
        for clock in clocks:
            h = integer(clock['h'] or clock['ch'])
            m = clock['min'] or clock['cm'] or '0'
            m = {'半':30,'一刻':15,'三刻':45}.get(m,m)
            m = integer(m.rstrip('分')) if isinstance(m,str) else m
            part = clock['part'] or inherited
            if not part and clock['h'] is None and 1 <= h <= 12:
                result['warnings'].append('钟点未说明上午/下午')
                return result
            if part:
                if h>12: raise ValueError('hour')
                if part in ('下午','晚上','晚间'):
                    if h==12 and part!='下午':raise ValueError('midnight date uncertain')
                    h = h%12+12
                elif part in ('凌晨','早上','上午'):
                    if h==12: raise ValueError('ambiguous twelve')
                elif part=='中午':
                    if h<=2: h+=12
                    elif h not in (11,12): raise ValueError('noon')
            if not 0<=h<=23 or not 0<=m<=59: raise ValueError('clock')
            times.append(local_epoch(day.isoformat()+f'T{h:02d}:{m:02d}',zone))
            inherited = part
        result['time_expression'] = ' / '.join(c.group().strip() for c in clocks)
        if len(times)==2 and not re.fullmatch(r'\s*[-—–~～至到]\s*', text[clocks[0].end():clocks[1].start()]):
            result['warnings'].append('多个钟点不是明确起止区间')
            return result
        result['start'] = times[0]
        if len(times)==2:
            if times[1]<=times[0]:
                result['start'] = None
                result['warnings'].append('结束早于开始，跨日须核对')
            else: result['end'] = times[1]
        else: result['warnings'].append('缺结束时间；不默填一小时')
    except (ValueError, KeyError, OverflowError):
        result['start'] = None
        result['warnings'].append('钟点或夏令时有歧义，须补全')
    return result


def extract(row, now, zone=SOURCE_ZONE):
    text = row['text']
    if not isinstance(text,str) or not 4<=len(text)<=2000 or not CUE.search(text): return None
    if not (DATE.search(text) or VAGUE.search(text)) or QUOTE.search(text): return None
    hints = date_hints(text,row['timestamp'],zone)
    status = 'review'
    if CANCEL.search(text): status = 'cancel_signal'
    elif CHANGE.search(text): status = 'change_signal'
    elif hints['date'] and dt.date.fromisoformat(hints['date'])<dt.datetime.fromtimestamp(now,ZoneInfo(zone)).date(): status = 'expired'
    elif hints['start'] is not None and hints['start']<=now: status = 'expired'
    elif hints['start'] is None or hints['end'] is None: status = 'needs_details'
    who = 'self_statement' if row.get('is_self')==1 else 'group_reference' if row.get('is_group') else 'incoming_invitation'
    warnings = list(hints['warnings'])
    if row.get('is_self') is None:
        who = 'unknown_sender'; warnings.append('发送方向未知，须确认本人参加')
    if who=='group_reference': warnings.append('群聊安排，须核对是否与你有关')
    if THIRD.search(text): warnings.append('有第三人称，可能是他人的安排')
    if TENTATIVE.search(text): warnings.append('有询问或不确定措辞，未证明约定成立')
    kind = '截止任务' if re.search('截止|提交',text) else '出行' if re.search('出发|抵达|高铁|航班|飞机|出差',text) else '约定'
    place = LOCATION.search(text)
    if hints['date'] and dt.date.fromisoformat(hints['date'])<dt.datetime.fromtimestamp(now,ZoneInfo(zone)).date():
        status='expired'
    return {**hints,'status':status,'kind':kind,'participation':who,'warnings':warnings,
            'quote':text,'location_hint':place.group(1).strip() if place else '',
            'title_hint':kind+'（请核对并命名）'}
