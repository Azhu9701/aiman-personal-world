"""Recoverable publication of the archive/index pair, with a durable intent.

Writers hold the existing per-state lock. Readers pause while intent exists.
Recovery changes only paths whose content matches the recorded old/new hashes.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

JOURNAL = "archive-publication.json"
NAMES = ("archive.jsonl", "index.sqlite")


def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def sync_directory(path):
    if os.name != "posix":
        return
    fd=os.open(path,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
    try:os.fsync(fd)
    finally:os.close(fd)


def _fail():
    from app_config import AppError
    raise AppError("publish_recovery_needed","检测到未完成的归档更新；备份仍保留，请先执行 recover，不会覆盖无法确认的文件。")


def recover(state):
    from app_config import private_file
    state=Path(state);journal=state/JOURNAL
    if not journal.exists() and not journal.is_symlink():return {"recovered":False}
    private_file(journal,limit=16384)
    try:
        doc=json.loads(journal.read_text())
        if doc.get("schema")!=1 or not re.fullmatch(r"[.]publish-backup-[A-Za-z0-9_-]+",doc.get("backup","")):_fail()
        if set(doc.get("old",{}))!=set(NAMES) or set(doc.get("new",{}))!=set(NAMES):_fail()
        if any(not isinstance(value,str) for value in doc['new'].values()):_fail()
        for value in list(doc['old'].values())+list(doc['new'].values()):
            if value is not None and not re.fullmatch(r"[a-f0-9]{64}",value):_fail()
    except (TypeError,ValueError,AttributeError):_fail()
    backup=state/doc['backup']
    if backup.is_symlink() or not backup.is_dir():_fail()
    current={}
    for name in NAMES:
        path=state/name
        if path.exists() or path.is_symlink():
            private_file(path);current[name]=digest_file(path)
        else:current[name]=None
        if current[name] not in (doc['old'][name],doc['new'][name]):_fail()
    committed=all(current[name]==doc['new'][name] for name in NAMES)
    if not committed:
        # Validate all restoration sources before changing either live file.
        for name in NAMES:
            if doc['old'][name] is not None:
                private_file(backup/name)
                if digest_file(backup/name)!=doc['old'][name]:_fail()
        for name in reversed(NAMES):
            if doc['old'][name] is None:
                (state/name).unlink(missing_ok=True)
            elif current[name]!=doc['old'][name]:
                # Keep backup originals until both files are durably restored.
                temporary=backup/(name+'.restoring')
                shutil.copyfile(backup/name,temporary);temporary.chmod(0o600)
                with temporary.open('rb') as stream:os.fsync(stream.fileno())
                os.replace(temporary,state/name)
        sync_directory(state)
    journal.unlink();sync_directory(state)
    shutil.rmtree(backup)
    return {"recovered":True,"outcome":"commit_finished" if committed else "previous_pair_restored"}


def publish(staging,state):
    from app_config import AppError,atomic_json,private_file
    staging,state=Path(staging),Path(state)
    if (state/JOURNAL).exists():recover(state)
    for name in NAMES:
        private_file(staging/name)
        if (state/name).is_symlink():raise AppError("archive_symlink","不能替换符号链接形式的归档或索引。")
        if (state/name).exists():private_file(state/name)
        with (staging/name).open('rb') as stream:os.fsync(stream.fileno())
    backup=Path(tempfile.mkdtemp(prefix='.publish-backup-',dir=state));backup.chmod(0o700)
    old={};new={};intent=False
    try:
        for name in NAMES:
            old[name]=digest_file(state/name) if (state/name).exists() else None
            new[name]=digest_file(staging/name)
            if old[name] is not None:
                try:os.link(state/name,backup/name)
                except OSError:shutil.copyfile(state/name,backup/name);(backup/name).chmod(0o600)
        sync_directory(backup)
        atomic_json(state/JOURNAL,{"schema":1,"backup":backup.name,"old":old,"new":new})
        sync_directory(state);intent=True
        for name in NAMES:os.replace(staging/name,state/name)
        sync_directory(state)
        (state/JOURNAL).unlink();sync_directory(state)
        shutil.rmtree(backup)
    except BaseException:
        if (state/JOURNAL).exists():recover(state)
        elif backup.exists():shutil.rmtree(backup)
        raise
