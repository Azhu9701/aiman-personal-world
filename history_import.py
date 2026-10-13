"""Local history merge: preview, explicit commit, immutable normalized inputs.

Stable IDs are used within an explicit source namespace. Missing rows are not
removals. No content-similarity deduplication and no automatic account guessing.
The canonical collection is a normalized view, not a fabricated source DB dump.
"""
from __future__ import annotations
from collections import Counter
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
import time

from app_config import AppError, StateLock, atomic_json, private_dir, private_file, publish_archive, resolve_state
from archive_publish import digest_file

FORMAT = 'personal-world.collection.v1'
MAX_UPLOAD = 512 * 1024 * 1024
PLAN_TTL = 24 * 3600
POLICIES = ('keep_existing', 'prefer_incoming')
MERGE_RULES_VERSION = 2


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _source_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',value):
        raise AppError('source_id_invalid','来源标识须为1–40位小写字母、数字、下划线或短横线。primary 仅用于当前账号和相同稳定ID体系。')
    return value


def _mapped(value,source_id):
    if not isinstance(value,str) or not value or len(value)>(2048 if source_id=='primary' else 1900):
        raise AppError('record_id_invalid','记录缺少可用的稳定编号，或编号过长。')
    return value if source_id=='primary' else 'pw:'+source_id+':'+value


def _message(rec,source_id='primary'):
    from verify_archive import expected_row,FIELDS
    from server import normalized_boolean,numeric_time
    row=dict(rec,id=_mapped(rec.get('id'),source_id),conversation_id=_mapped(rec.get('conversation_id'),source_id))
    row['timestamp']=numeric_time(row.get('timestamp'))
    row['is_self']=normalized_boolean(row.get('is_self'),allow_unknown=True)
    values=expected_row(row)
    return dict(zip(FIELDS,values))


def _conversation(rec,source_id='primary'):
    from server import normalized_boolean,media_reference
    cid=_mapped(rec.get('id'),source_id)
    return {'id':cid,'name':str(rec.get('name') or rec['id']),
            'is_group':normalized_boolean(rec.get('is_group',rec['id'].endswith('@chatroom'))),
            'avatar_url':media_reference(rec.get('avatar_url'))}


def _normal(rec,source_id='primary'):
    kind=rec.get('record_type')
    return _message(rec,source_id) if kind=='message' else _conversation(rec,source_id)


def pair_digest(state):
    return {name:digest_file(state/name) if (state/name).is_file() else None for name in ('archive.jsonl','index.sqlite')}


def _readonly_index(state):
    conn=sqlite3.connect((state/'index.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _batch(path,namespace,label,validation,index_state):
    sha=digest_file(path)
    with contextlib.closing(_readonly_index(index_state)) as conn:
        count,first,last=conn.execute('SELECT count(*),min(nullif(timestamp,0)),max(timestamp) FROM messages').fetchone()
    return {'id':_digest([namespace,sha]),'archive_sha256':sha,'source_id':namespace,
            'label':label,'validation':validation,'input_messages':count,
            'first_timestamp':first,'last_timestamp':last,'created_at':int(time.time())}


def _plan_dir(state,pid):
    if not isinstance(pid,str) or not re.fullmatch(r'[0-9a-f]{32}',pid):
        raise AppError('plan_id_invalid','导入预览编号无效。')
    parent=state/'import-plans';path=parent/pid
    if parent.is_symlink() or path.is_symlink() or not path.is_dir():
        raise AppError('plan_missing','导入预览不存在，请重新选择文件。')
    return path


def _public_plan(plan):
    return {'ok':True,'plan_id':plan['id'],'source_id':plan['source_id'],'policy':plan['policy'],
            'status':plan.get('status','preview'),'summary':plan['summary'],
            'can_apply':plan['summary']['identity_conflicts']==0 and plan['summary'].get('media_content_conflicts',0)==0,'expires_at':plan['expires_at'],
            'scope_note':'只按明确来源中的稳定ID去重；缺少记录不代表删除，空白月份不代表没有聊天。原始规范来源保留在本机，不发送消息。'}


def prepare(source,state,*,source_id='primary',policy='keep_existing',label='手动归档'):
    state=resolve_state(state)
    with StateLock(state):
        return _prepare_locked(Path(source),state,source_id=source_id,policy=policy,label=label)


def _prepare_locked(source,state,*,source_id='primary',policy='keep_existing',label='手动归档',validated_dir=None):
    from personal_world import canonical_archive,archive_manifest,verify_state,generic_audit
    import server
    namespace=_source_id(source_id)
    if policy not in POLICIES:raise AppError('merge_policy_invalid','合并策略无效。')
    if not isinstance(label,str) or not label.strip() or len(label)>120 or any(ord(c)<32 for c in label):
        raise AppError('source_label_invalid','来源名称应为1–120个可显示字符。')
    source=source.expanduser().resolve()
    if not source.is_file() or source.stat().st_size>MAX_UPLOAD:
        raise AppError('import_size','请选择不超过512 MiB的规范聊天文件；较大历史请分批导入。')
    if bool((state/'archive.jsonl').exists()) != bool((state/'index.sqlite').exists()):
        raise AppError('source_pair_incomplete','当前归档和索引不成对，请先恢复，不会用新文件覆盖。')
    base=pair_digest(state)
    before_manifest=archive_manifest(state/'archive.jsonl') if base['archive.jsonl'] else {}
    if base['archive.jsonl'] and not verify_state(state)['ok']:
        raise AppError('import_verification_failed','现有归档未通过校验；请先检查，避免在错误基础上合并。')
    plans=state/'import-plans';private_dir(plans)
    pending=[]
    for path in plans.iterdir():
        if path.is_dir() and not path.is_symlink() and re.fullmatch(r'[0-9a-f]{32}',path.name):
            meta=path/'plan.json'
            if meta.is_file():
                doc=json.loads(meta.read_text())
                if doc.get('status')=='preview':pending.append(path)
    if len(pending)>=4:
        raise AppError('too_many_previews','已有4份待确认预览，请先确认或取消已有预览。')
    pid=secrets.token_hex(16);folder=plans/pid;private_dir(folder)
    try:
        incoming=folder/'input';private_dir(incoming)
        candidate=incoming/'archive.jsonl'
        if validated_dir is None:
            manifest=canonical_archive(source,candidate)
            server.import_archive(candidate,incoming,build_search=False)
            # An exported collection is accepted only as supplied normalized
            # data, never as a proof that its old batch claims are authentic.
            if manifest.get('archive_format')==FORMAT:
                verified=generic_audit(incoming,media_dir=state/'media')
                validation='provided_collection_export'
                valid=verified['archive_index_ok']==1
            else:
                verified=verify_state(incoming,media_dir=state/'media')
                validation=verified['validation'];valid=verified['ok']
            if not valid:raise AppError('import_verification_failed','输入归档未通过逐条核对；当前历史不变。')
        else:
            manifest=archive_manifest(source)
            shutil.copyfile(source,candidate);candidate.chmod(0o600)
            with contextlib.closing(_readonly_index(Path(validated_dir))) as src,contextlib.closing(sqlite3.connect(incoming/'index.sqlite')) as dst:
                src.backup(dst)
            (incoming/'index.sqlite').chmod(0o600)
            validation='wechat_source_manifest'
        if base['archive.jsonl'] and (before_manifest.get('synthetic') is True)!=(manifest.get('synthetic') is True):
            raise AppError('synthetic_mix','人工演示和真实归档必须分开，不能混入同一个工作空间。')
        input_batch=_batch(candidate,namespace,label.strip(),validation,incoming)
        batches=list(before_manifest.get('source_batches',[])) if before_manifest.get('archive_format')==FORMAT else []
        paths={}
        for batch in batches:
            sha=batch.get('archive_sha256','')
            if not isinstance(sha,str) or not re.fullmatch(r'[a-f0-9]{64}',sha):
                raise AppError('collection_sources','已有来源清单的校验值无效。')
            paths[sha]=state/'import-sources'/(sha+'.jsonl')
        bootstrap=None
        if base['archive.jsonl'] and not batches:
            bootstrap=_batch(state/'archive.jsonl','primary','升级前的本机归档',
                             'wechat_source_manifest' if 'source_tables' in before_manifest else 'normalized_archive',state)
            batches.append(bootstrap);paths[bootstrap['archive_sha256']]=state/'archive.jsonl'
        paths[input_batch['archive_sha256']]=candidate
        stats=Counter(messages_added=0,messages_changed=0,messages_unchanged=0,conflicts=0,
                      kept_existing=0,identity_conflicts=0,conversations_added=0,conversations_changed=0,
                      media_downgrades_prevented=0,media_content_conflicts=0)
        scratch=folder/'records.sqlite'
        with contextlib.closing(sqlite3.connect(scratch)) as records:
            scratch.chmod(0o600)
            records.execute('CREATE TABLE records(kind TEXT,id TEXT,cid TEXT,stamp INTEGER,signature TEXT,value TEXT,PRIMARY KEY(kind,id))')
            def add(rec,origin,ns):
                kind=rec['record_type'];normal=_normal(rec,ns);signature=_digest(normal)
                normal['origin']=origin
                records.execute('INSERT INTO records VALUES(?,?,?,?,?,?)',(kind,normal['id'],normal.get('conversation_id'),normal.get('timestamp',0),signature,_json(normal)))
            if base['archive.jsonl']:
                for rec in server.source_records(state/'archive.jsonl'):
                    if rec['record_type'] not in ('message','conversation'):continue
                    origin=rec.get('origin') if before_manifest.get('archive_format')==FORMAT else {'batch_id':bootstrap['id'],'original_id':rec['id']}
                    add(rec,origin,'primary')
            prior_count=records.execute("SELECT count(*) FROM records WHERE kind='message'").fetchone()[0]
            for rec in server.source_records(candidate):
                kind=rec['record_type']
                if kind not in ('message','conversation'):continue
                normal=_normal(rec,namespace);signature=_digest(normal)
                old=records.execute('SELECT cid,stamp,signature,value FROM records WHERE kind=? AND id=?',(kind,normal['id'])).fetchone()
                if old is None:
                    add(rec,{'batch_id':input_batch['id'],'original_id':rec['id']},namespace)
                    stats['messages_added' if kind=='message' else 'conversations_added']+=1
                elif old[2]==signature:
                    if kind=='message':stats['messages_unchanged']+=1
                else:
                    if kind=='message' and (old[0]!=normal['conversation_id'] or old[1]!=normal['timestamp']):
                        stats['identity_conflicts']+=1;continue
                    if kind=='message':stats['conflicts']+=1
                    if kind=='message' and policy=='prefer_incoming':
                        previous=json.loads(old[3])
                        if previous.get('media_url') and not normal.get('media_url'):
                            # Retain the whole old record and its exact origin;
                            # do not create an untraceable field-level hybrid.
                            if previous.get('type')==normal.get('type') and previous.get('text')==normal.get('text'):
                                stats['media_downgrades_prevented']+=1
                                stats['kept_existing']+=1
                            else:
                                stats['media_content_conflicts']+=1
                            continue
                    if policy=='keep_existing':
                        if kind=='message':stats['kept_existing']+=1
                        continue
                    records.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,normal['id']))
                    add(rec,{'batch_id':input_batch['id'],'original_id':rec['id']},namespace)
                    stats['messages_changed' if kind=='message' else 'conversations_changed']+=1
            total,first,last=records.execute("SELECT count(*),min(nullif(stamp,0)),max(stamp) FROM records WHERE kind='message'").fetchone()
            changed=bool(stats['messages_added'] or stats['messages_changed'] or stats['conversations_added'] or stats['conversations_changed'])
            stats.update(before_messages=prior_count,after_messages=total,first_timestamp=first or 0,last_timestamp=last or 0)
            if changed:
                if input_batch['id'] not in {b['id'] for b in batches}:batches.append(input_batch)
                metadata={'complete':True,'archive_format':FORMAT,'coverage_scope':'union_of_explicit_local_inputs',
                          'source':'本机历史归档集合','source_tables_verified':False,
                          'synthetic':manifest.get('synthetic') is True,'message_count':total,
                          'conversation_count':records.execute("SELECT count(*) FROM records WHERE kind='conversation'").fetchone()[0],
                          'source_batches':batches,'import_history':(list(before_manifest.get('import_history',[])) if before_manifest.get('archive_format')==FORMAT else [])+[
                              {'id':pid,'source_id':namespace,'label':label.strip(),'policy':policy,'prepared_at':int(time.time()),'summary':dict(stats)}]}
                counts=dict(records.execute("SELECT cid,count(*) FROM records WHERE kind='message' GROUP BY cid"))
                output=folder/'output';private_dir(output)
                with (output/'archive.jsonl').open('x',encoding='utf-8') as stream:
                    (output/'archive.jsonl').chmod(0o600)
                    for kind in ('conversation','message'):
                        for value, in records.execute('SELECT value FROM records WHERE kind=? ORDER BY id',(kind,)):
                            rec=json.loads(value)
                            if kind=='conversation':rec['message_count']=counts.get(rec['id'],0)
                            stream.write(_json({'record_type':kind,**rec})+'\n')
                    stream.write(_json({'record_type':'manifest',**metadata})+'\n');stream.flush();os.fsync(stream.fileno())
                delta=build_delta(output,state if base['index.sqlite'] else None)
                if not verify_collection(output,media_dir=state/'media',source_paths=paths)['ok']:
                    raise AppError('merged_verification_failed','合并后的归档未通过来源与索引校验，当前历史不变。')
            else:delta={'mode':'unchanged','rows_inserted':0,'rows_updated':0,'search_rows_written':0}
        scratch.unlink(missing_ok=True)
        if pair_digest(state)!=base:raise AppError('preview_source_changed','预览期间当前历史已变化，请重新检查。')
        plan={'id':pid,'merge_rules_version':MERGE_RULES_VERSION,'source_id':namespace,'policy':policy,'label':label.strip(),'summary':dict(stats),
              'status':'preview','created_at':int(time.time()),'expires_at':int(time.time())+PLAN_TTL,
              'base':base,'input_batch':input_batch,'bootstrap_batch':bootstrap,'changed':changed,'delta':delta,
              'output':pair_digest(folder/'output') if changed else None}
        atomic_json(folder/'plan.json',plan)
        return _public_plan(plan)
    except BaseException:
        shutil.rmtree(folder)
        raise


def build_delta(output,existing=None):
    """Reuse a consistent index copy; write only changed rows and FTS entries."""
    import server
    from verify_archive import FIELDS,expected_row
    archive=output/'archive.jsonl';dest=output/'index.sqlite'
    if existing is None:
        result=server.import_archive(archive,output)
        return {'mode':'initial_build','rows_inserted':result['message_count'],'rows_updated':0,
                'search_rows_written':result['message_count'] if result['search_engine']=='trigram' else 0}
    with contextlib.closing(_readonly_index(existing)) as source,contextlib.closing(sqlite3.connect(dest)) as conn:
        dest.chmod(0o600);source.backup(conn);conn.row_factory=sqlite3.Row
        metadata=json.loads(conn.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
        fts=metadata.get('search_engine')=='trigram'
        conn.execute('BEGIN IMMEDIATE');inserted=updated=search_writes=0;affected=set();manifest={}
        for rec in server.source_records(archive):
            if rec['record_type']=='manifest':manifest={k:v for k,v in rec.items() if k!='record_type'};continue
            if rec['record_type']=='conversation':
                values=(rec['id'],rec['name'],bool(rec['is_group']),server.media_reference(rec.get('avatar_url')))
                old=conn.execute('SELECT id,name,is_group,avatar_url FROM conversations WHERE id=?',(rec['id'],)).fetchone()
                if old is None:
                    conn.execute('INSERT INTO conversations(id,name,is_group,avatar_url) VALUES(?,?,?,?)',values);affected.add(rec['id'])
                elif tuple(old)!=values:
                    conn.execute('UPDATE conversations SET name=?,is_group=?,avatar_url=? WHERE id=?',(*values[1:],values[0]))
                continue
            values=expected_row(rec)
            old=conn.execute('SELECT rowid,'+','.join(FIELDS)+' FROM messages WHERE id=?',(values[0],)).fetchone()
            if old is None:
                cur=conn.execute('INSERT INTO messages('+','.join(FIELDS)+') VALUES('+','.join('?' for _ in FIELDS)+')',values)
                if fts:conn.execute('INSERT INTO messages_fts(rowid,text) VALUES(?,?)',(cur.lastrowid,values[5]));search_writes+=1
                inserted+=1;affected.add(values[1])
            elif tuple(old)[1:]!=values:
                if fts and old['text']!=values[5]:
                    conn.execute("INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',?,?)",(old['rowid'],old['text']))
                conn.execute('UPDATE messages SET '+','.join(k+'=?' for k in FIELDS[1:])+' WHERE id=?',(*values[1:],values[0]))
                if fts and old['text']!=values[5]:
                    conn.execute('INSERT INTO messages_fts(rowid,text) VALUES(?,?)',(old['rowid'],values[5]));search_writes+=1
                updated+=1;affected.update((old['conversation_id'],values[1]))
        for cid in affected:
            conn.execute("""UPDATE conversations SET message_count=(SELECT count(*) FROM messages WHERE conversation_id=?),
              last_message_at=coalesce((SELECT timestamp FROM messages WHERE conversation_id=? ORDER BY timestamp DESC,id DESC LIMIT 1),0),
              last_message=coalesce((SELECT text FROM messages WHERE conversation_id=? ORDER BY timestamp DESC,id DESC LIMIT 1),'') WHERE id=?""",(cid,cid,cid,cid))
        manifest.update(search_engine=metadata.get('search_engine','substring'),imported_at=int(time.time()))
        conn.execute("UPDATE metadata SET value=? WHERE key='manifest'",(_json(manifest),));conn.commit()
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise AppError('delta_index_invalid','增量索引完整性检查失败。')
        if fts:conn.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES('integrity-check',1)")
    return {'mode':'copy_on_write_delta','rows_inserted':inserted,'rows_updated':updated,'search_rows_written':search_writes}


def verify_collection(state,media_dir=None,source_paths=None):
    from personal_world import generic_audit,archive_manifest
    from server import source_records
    state=Path(state);manifest=archive_manifest(state/'archive.jsonl')
    if manifest.get('archive_format')!=FORMAT:raise AppError('collection_format','不是可校验的历史集合。')
    result=generic_audit(state,media_dir=media_dir);issues=Counter(result['issues'])
    batches=manifest.get('source_batches',[])
    if not isinstance(batches,list) or not batches:raise AppError('collection_sources','历史集合缺少来源。')
    with tempfile.TemporaryDirectory(prefix='.origin-audit-',dir=state) as temp,contextlib.closing(sqlite3.connect(Path(temp)/'origins.sqlite')) as conn:
        (Path(temp)/'origins.sqlite').chmod(0o600)
        conn.execute('CREATE TABLE origins(batch TEXT,kind TEXT,id TEXT,signature TEXT,PRIMARY KEY(batch,kind,id))')
        seen=set()
        for batch in batches:
            sha=batch.get('archive_sha256','');bid=batch.get('id','');ns=_source_id(batch.get('source_id'))
            if not re.fullmatch(r'[a-f0-9]{64}',sha) or bid!=_digest([ns,sha]) or bid in seen:
                raise AppError('collection_sources','历史来源清单格式无效或重复。')
            seen.add(bid)
            path=Path(source_paths[sha]) if source_paths is not None and sha in source_paths else state/'import-sources'/(sha+'.jsonl')
            private_file(path)
            if digest_file(path)!=sha:raise AppError('collection_source_changed','保留的来源快照发生变化，请检查本机文件。')
            for rec in source_records(path):
                kind=rec['record_type']
                if kind in ('conversation','message'):
                    conn.execute('INSERT INTO origins VALUES(?,?,?,?)',(bid,kind,rec['id'],_digest(_normal(rec,ns))))
        conn.commit()
        for rec in source_records(state/'archive.jsonl'):
            if rec['record_type'] not in ('conversation','message'):continue
            origin=rec.get('origin',{})
            row=conn.execute('SELECT signature FROM origins WHERE batch=? AND kind=? AND id=?',
                             (origin.get('batch_id'),rec['record_type'],origin.get('original_id'))).fetchone()
            issues['origin_record_mismatch']+=row is None or row[0]!=_digest(_normal(rec))
    result.update(validation='local_collection_with_retained_inputs',issues=dict(issues),archive_index_ok=int(not any(issues.values())))
    return {'ok':result['archive_index_ok']==1,**result}


def _load_plan(state,pid):
    folder=_plan_dir(state,pid);private_file(folder/'plan.json',limit=2*1024*1024)
    plan=json.loads((folder/'plan.json').read_text())
    if plan.get('id')!=pid:raise AppError('plan_invalid','预览文件与编号不符。')
    return folder,plan


def commit(pid,state):
    state=resolve_state(state)
    with StateLock(state):return _commit_locked(pid,state)


def _commit_locked(pid,state):
    from personal_world import archive_manifest
    folder,plan=_load_plan(state,pid)
    if plan.get('status')=='committed':return plan['result']
    # If a process died after pair publication but before marking its plan,
    # the committed manifest is the authority; never reapply an old plan.
    if (state/'archive.jsonl').exists():
        meta=archive_manifest(state/'archive.jsonl')
        if meta.get('archive_format')==FORMAT and any(item.get('id')==pid for item in meta.get('import_history',[])):
            result={'ok':True,'status':'merged','summary':plan['summary'],'delta':plan['delta'],'validation':plan['input_batch']['validation'],'recovered_commit':True}
            plan.update(status='committed',result=result);atomic_json(folder/'plan.json',plan);return result
    if plan.get('merge_rules_version')!=MERGE_RULES_VERSION:
        raise AppError('plan_stale','合并规则已升级，请重新预览，避免旧预览覆盖已有可用附件。')
    if time.time()>plan['expires_at']:raise AppError('plan_expired','预览已过期，请重新检查文件。')
    if pair_digest(state)!=plan['base']:raise AppError('plan_stale','当前历史已更新；旧预览不会覆盖，请重新检查。')
    if plan['summary']['identity_conflicts']:
        raise AppError('identity_conflict','相同消息编号指向不同会话或时间。请核实来源，必要时使用独立来源标识；不会覆盖原记录。')
    if plan['summary'].get('media_content_conflicts',0):
        raise AppError('identity_conflict','已有附件变缺失且正文或类型变化，请先核查，不能自动决定覆盖。')
    if plan['changed']:
        if pair_digest(folder/'output')!=plan['output']:raise AppError('preview_changed','预览产物已变化，请重新检查。')
        sources=state/'import-sources';private_dir(sources)
        for batch,input_path in ((plan.get('bootstrap_batch'),state/'archive.jsonl'),(plan['input_batch'],folder/'input/archive.jsonl')):
            if batch is None:continue
            sha=batch['archive_sha256']
            if not re.fullmatch(r'[a-f0-9]{64}',sha) or batch.get('id')!=_digest([_source_id(batch.get('source_id')),sha]):
                raise AppError('plan_invalid','预览来源编号无效，请重新检查。')
            target=sources/(sha+'.jsonl')
            if digest_file(input_path)!=sha:raise AppError('input_changed','输入快照已变化，请重新预览。')
            if target.exists() or target.is_symlink():
                private_file(target)
                if digest_file(target)!=sha:raise AppError('retained_source_changed','已有来源快照内容不一致；未覆盖。')
            else:
                temporary=sources/('.incoming-'+secrets.token_hex(12))
                try:
                    with input_path.open('rb') as src,temporary.open('xb') as dst:
                        temporary.chmod(0o600);shutil.copyfileobj(src,dst,1024*1024);dst.flush();os.fsync(dst.fileno())
                    if digest_file(temporary)!=sha:raise AppError('input_changed','复制期间输入发生变化。')
                    os.replace(temporary,target)
                finally:temporary.unlink(missing_ok=True)
        if pair_digest(state)!=plan['base']:
            raise AppError('plan_stale','确认期间历史已变化，请重新预览。')
        publish_archive(folder/'output',state)
    result={'ok':True,'status':'merged' if plan['changed'] else 'unchanged','summary':plan['summary'],'delta':plan['delta'],'validation':plan['input_batch']['validation'],
            'message_count':plan['summary']['after_messages']}
    plan.update(status='committed',result=result);atomic_json(folder/'plan.json',plan)
    shutil.rmtree(folder/'input',ignore_errors=True)
    shutil.rmtree(folder/'output',ignore_errors=True)
    return result


def discard(pid,state):
    state=resolve_state(state)
    with StateLock(state):
        folder,plan=_load_plan(state,pid)
        # Removing a preview is not removal of a retained historical input.
        if plan.get('status')=='committed':raise AppError('plan_committed','已确认的来源归档不能通过取消预览删除。')
        shutil.rmtree(folder)
    return {'ok':True,'status':'preview_discarded'}


def merge_locked(source,state,*,source_id='primary',policy='keep_existing',label='本机来源',validated_dir=None):
    plan=_prepare_locked(Path(source),state,source_id=source_id,policy=policy,label=label,validated_dir=validated_dir)
    return _commit_locked(plan['plan_id'],state)


def pending_previews(state):
    """Expose only safe local plan summaries so interrupted UI work is cancelable."""
    parent=Path(state)/'import-plans'
    if not parent.exists():return []
    if parent.is_symlink() or not parent.is_dir():raise OSError('Invalid preview directory')
    items=[]
    for folder in parent.iterdir():
        if not re.fullmatch(r'[0-9a-f]{32}',folder.name) or folder.is_symlink() or not folder.is_dir():continue
        path=folder/'plan.json'
        if not path.exists():continue
        try:
            private_file(path,limit=2*1024*1024)
            plan=json.loads(path.read_text())
            if plan.get('id')!=folder.name or plan.get('status')!='preview':continue
            items.append({'plan_id':folder.name,'label':str(plan.get('label','本机导入'))[:120],
                          'created_at':plan.get('created_at'), 'expires_at':plan.get('expires_at'),
                          'expired':time.time()>plan.get('expires_at',0)})
        except (AppError,ValueError,TypeError):
            # Invalid files must not be automatically deleted or represented as
            # a verified import; controlled recovery can inspect them locally.
            continue
    return sorted(items,key=lambda p:(p['created_at'] or 0,p['plan_id']),reverse=True)


def catalog(state):
    state=Path(state)
    if not (state/'index.sqlite').is_file():return {'ready':False,'batches':[],'imports':[],'months':[],'pending_previews':pending_previews(state)}
    if (state/'archive-publication.json').exists():raise OSError('Archive publication pending')
    with contextlib.closing(_readonly_index(state)) as conn:
        meta=json.loads(conn.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
        rows=conn.execute("SELECT strftime('%Y-%m',timestamp,'unixepoch','+8 hours') AS month,count(*) FROM messages WHERE timestamp>0 GROUP BY month ORDER BY month").fetchall()
        counts={r[0]:r[1] for r in rows if r[0] is not None}
        unknown=conn.execute("SELECT count(*) FROM messages WHERE timestamp<=0 OR strftime('%Y-%m',timestamp,'unixepoch') IS NULL").fetchone()[0]
    months=[];truncated=False
    if counts:
        low,high=min(counts),max(counts);y,m=map(int,low.split('-'));end=tuple(map(int,high.split('-')))
        while (y,m)<=end and len(months)<600:
            key=f'{y:04d}-{m:02d}';months.append({'month':key,'messages':counts.get(key,0)})
            y,m=(y+1,1) if m==12 else (y,m+1)
        truncated=(y,m)<=end
    return {'ready':True,'format':meta.get('archive_format','legacy_snapshot'),'pending_previews':pending_previews(state),
            'batches':meta.get('source_batches',[]),'imports':list(reversed(meta.get('import_history',[])))[0:100],
            'months':months,'months_truncated':truncated,'unknown_time_messages':unknown,
            'notes':['这是已导入记录的覆盖，不是完整微信历史。','空白月份只表示尚无已导入记录，不证明没有聊天。',
                     '来源批次记录数含重叠，不能直接相加当作去重后的总数。','保留的规范输入快照可能包含私人内容；当前尚无来源删除和自动到期清理。']}
