"""Append-only local draft edits; the generated v1 row remains unchanged.

Call save() inside the workspace BEGIN IMMEDIATE transaction. Reads recognize
old workspaces without migrating them. No network, source-index or WeChat writes.
"""
from __future__ import annotations
import json
import re
import time


class DraftConflictError(ValueError):
    """Another editor saved a newer revision; never overwrite that revision."""


def validate_id(did):
    if not isinstance(did, str) or not re.fullmatch(r"draft:[0-9a-f]{32}", did):
        raise ValueError("草稿编号无效")


def validate_edit(title, body, revision):
    if not isinstance(title, str) or not title.strip() or len(title) > 240:
        raise ValueError("草稿标题无效")
    if not isinstance(body, str) or not body.strip() or len(body) > 16000:
        raise ValueError("草稿正文无效")
    if type(revision) is not int or not 1 <= revision < 2**31:
        raise ValueError("草稿版本无效")
    if "\x00" in title or "\x00" in body:
        raise ValueError("草稿包含无效字符")


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS draft_revisions (
        draft_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=2),
        title TEXT NOT NULL, body TEXT NOT NULL, updated_at INTEGER NOT NULL,
        PRIMARY KEY(draft_id,revision),
        FOREIGN KEY(draft_id) REFERENCES drafts(id))""")


def has_revisions(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='draft_revisions'").fetchone() is not None


def original(conn, did):
    validate_id(did)
    row=conn.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone() if conn else None
    if row is None:
        raise KeyError("草稿不存在")
    row=dict(row)
    row['evidence_ids']=json.loads(row['evidence_ids'])
    row.update(revision=1, updated_at=row['created_at'], status='draft')
    return row


def latest(conn, did):
    row=original(conn, did)
    if has_revisions(conn):
        edit=conn.execute("SELECT revision,title,body,updated_at FROM draft_revisions WHERE draft_id=? ORDER BY revision DESC LIMIT 1", (did,)).fetchone()
        if edit:
            row.update(dict(edit))
    return row


def history(conn, did, before=None, limit=20):
    base=original(conn, did)
    limit=max(1,min(int(limit),100))
    if before is not None and (type(before) is not int or not 1 <= before < 2**63):
        raise ValueError("版本分页无效")
    rows=[]
    if has_revisions(conn):
        rows=[dict(r) for r in conn.execute("SELECT revision,title,body,updated_at FROM draft_revisions WHERE draft_id=? AND revision<? ORDER BY revision DESC LIMIT ?", (did,before if before is not None else 2**63-1,limit+1))]
    if (before is None or before>1) and len(rows)<limit+1:
        rows.append({k:base[k] for k in ('revision','title','body','updated_at')})
    more=len(rows)>limit
    return {'items':rows[:limit], 'has_more':more,
            'next_before':rows[limit-1]['revision'] if more else None}


def save(conn, did, title, body, expected_revision):
    validate_edit(title,body,expected_revision)
    current=latest(conn,did)
    # A retried successful write is a no-op only when the exact saved content
    # and immediate successor version match. An old edit can never overwrite.
    if current['revision'] != expected_revision:
        if current['revision']==expected_revision+1 and (title,body)==(current['title'],current['body']):
            return current
        raise DraftConflictError("草稿已在其他窗口更新；你的修改仍保留，请重新读取后合并。")
    if (title,body)==(current['title'],current['body']):
        return current
    conn.execute("INSERT INTO draft_revisions VALUES (?,?,?,?,?)", (did,expected_revision+1,title,body,int(time.time())))
    return latest(conn,did)
