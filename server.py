#!/usr/bin/env python3
"""WeChat Local: private loopback-only, read-only archive browser.

Only normalized, explicitly imported archives are served. This module never
extracts keys, attaches to WeChat, sends messages, or opens the original DB.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import http.cookies
import http.server
import json
import mimetypes
import os
from pathlib import Path
import secrets
import socketserver
import sqlite3
import stat
import sys
import threading
import time
import tempfile
import urllib.parse

from app_config import AppError, APP_ID, VERSION, DEFAULT_PORT, ACCESS_FILE, default_state, state_id, private_dir as ensure_private_dir
from world_model import WorldModel, SourceChangedError, DraftConflictError
from personal_profile import PersonalProfile, ProfileConflictError
from personal_agenda import PersonalAgenda, AgendaConflictError

APP_DIR = Path(__file__).resolve().parent
DEFAULT_STATE = default_state()

SCHEMA = """
CREATE TABLE conversations (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, is_group INTEGER NOT NULL DEFAULT 0,
 avatar_url TEXT, last_message TEXT NOT NULL DEFAULT '',
 last_message_at INTEGER NOT NULL DEFAULT 0, message_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
 is_self INTEGER, type TEXT NOT NULL DEFAULT 'text',
 text TEXT NOT NULL DEFAULT '', timestamp INTEGER NOT NULL DEFAULT 0,
 raw_type TEXT, media_url TEXT, file_name TEXT, file_size INTEGER, media_status TEXT
);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def private_dir(path: Path) -> None:
    ensure_private_dir(path)


def numeric_time(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
            return int(parsed.timestamp())
        except (ValueError, TypeError):
            raise ValueError("消息时间格式无效，原有索引没有改变")
    value = int(float(value))
    return value // 1000 if value > 10**12 else value


def normalized_boolean(value, allow_unknown=False):
    if value is None and allow_unknown:
        return None
    if value is True or value == 1 or value in ("true", "1"):
        return True
    if value is False or value == 0 or value in ("false", "0"):
        return False
    raise ValueError("消息方向或群聊标记无效；原有索引没有改变")


def source_records(path: Path):
    """Read a normalized JSONL archive or a user-selected JSON export."""
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        with path.open(encoding="utf-8-sig") as f:
            for line_no, line in enumerate(f, 1):
                if line.strip():
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise ValueError(f"第 {line_no} 行不是记录对象")
                    yield obj
        return
    with path.open(encoding="utf-8-sig") as f:
        document = json.load(f)
    if not isinstance(document, dict):
        raise ValueError("JSON 需要包含 conversations/messages 或 conversation/messages")
    manifest = document.get("manifest", document.get("metadata", {}))
    if manifest:
        yield {"record_type": "manifest", **manifest}
    conversations = document.get("conversations", [])
    if document.get("conversation"):
        conversations = [document["conversation"], *conversations]
    for item in conversations:
        yield {**item, "record_type": "conversation"}
    default_id = document.get("conversation", {}).get("id")
    for item in document.get("messages", document.get("items", [])):
        yield {"conversation_id": default_id, **item, "record_type": "message"}


def media_reference(value):
    """Only generated files within the private media cache may be referenced."""
    if not isinstance(value, str) or not value.startswith("/api/media/"):
        return None
    suffix = urllib.parse.unquote(value[len("/api/media/"):])
    if not suffix or any(c in suffix for c in ("/", "\\", "\x00")) or suffix in (".", ".."):
        return None
    return "/api/media/" + urllib.parse.quote(suffix, safe="._-")


def import_archive(source: Path, state: Path, *, build_search: bool = True) -> dict:
    """Build an independent index atomically; invalid input never replaces it."""
    private_dir(state)
    tmp = state / ("index-building-" + secrets.token_hex(6) + ".sqlite")
    conn = sqlite3.connect(tmp)
    try:
        tmp.chmod(0o600)
        message_count = 0
        manifest = {}
        conn.executescript(SCHEMA)
        conn.execute("BEGIN")
        for rec in source_records(source):
            kind = rec.get("record_type") or rec.get("record") or rec.get("kind")
            if kind == "manifest":
                manifest.update({k: v for k, v in rec.items() if k not in ("record_type", "record", "kind")})
                continue
            if kind == "conversation":
                cid = str(rec.get("id") or "")
                if not cid:
                    raise ValueError("会话缺少稳定 ID")
                conn.execute(
                    "INSERT INTO conversations(id,name,is_group,avatar_url) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,is_group=excluded.is_group,avatar_url=excluded.avatar_url",
                    (cid, str(rec.get("name") or cid), normalized_boolean(rec.get("is_group", cid.endswith("@chatroom"))), media_reference(rec.get("avatar_url"))),
                )
                continue
            if kind != "message":
                raise ValueError("未知记录类型；导入已停止，原有索引没有改变")
            mid, cid = str(rec.get("id") or ""), str(rec.get("conversation_id") or "")
            if not mid or not cid:
                raise ValueError("消息缺少稳定 ID 或会话 ID")
            conn.execute("INSERT OR IGNORE INTO conversations(id,name,is_group) VALUES(?,?,?)", (cid, cid, cid.endswith("@chatroom")))
            conn.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, cid, str(rec.get("sender_name") or ""), normalized_boolean(rec.get("is_self"), allow_unknown=True),
                 str(rec.get("type") or "unknown"), str(rec.get("text") or ""), numeric_time(rec.get("timestamp")),
                 str(rec.get("raw_type", "")), media_reference(rec.get("media_url")),
                 str(rec["file_name"]) if rec.get("file_name") is not None else None, rec.get("file_size"),
                 str(rec["media_status"]) if rec.get("media_status") is not None else None),
            )
            message_count += 1
        if message_count == 0:
            raise ValueError("文件里没有消息，保留现有索引。加密数据库尚未读取不能算成空聊天")
        if manifest.get("complete") is False or manifest.get("failed_databases") or manifest.get("failed_tables"):
            raise ValueError("全库导出存在未通过的数据库或表，未替换现有完整索引")
        expected = manifest.get("message_count", manifest.get("total_messages"))
        if expected is not None and int(expected) != message_count:
            raise ValueError(f"消息数不一致：清单 {int(expected)}，实际 {message_count}；原有索引没有改变")
        conn.executescript("""
        CREATE INDEX messages_chat_order ON messages(conversation_id,timestamp,id);
        CREATE INDEX messages_time ON messages(timestamp,id);
        UPDATE conversations SET message_count=(SELECT count(*) FROM messages WHERE conversation_id=conversations.id),
          last_message_at=COALESCE((SELECT timestamp FROM messages WHERE conversation_id=conversations.id ORDER BY timestamp DESC,id DESC LIMIT 1),0),
          last_message=COALESCE((SELECT text FROM messages WHERE conversation_id=conversations.id ORDER BY timestamp DESC,id DESC LIMIT 1),'');
        CREATE INDEX conversations_order ON conversations(last_message_at DESC,id);
        """)
        search_engine = "substring"
        try:
            if not build_search:
                raise sqlite3.OperationalError("Search intentionally omitted for input validation")
            conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(text, content='messages', content_rowid='rowid', tokenize='trigram')")
            conn.execute("INSERT INTO messages_fts(rowid,text) SELECT rowid,text FROM messages")
            search_engine = "trigram"
        except sqlite3.OperationalError:
            conn.execute("DROP TABLE IF EXISTS messages_fts")
        manifest.update({"message_count": message_count, "imported_at": int(time.time()), "search_engine": search_engine})
        conn.execute("INSERT INTO metadata VALUES('manifest',?)", (json.dumps(manifest, ensure_ascii=False),))
        conn.commit()
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise ValueError("独立索引完整性检查失败")
        conversations = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
        conn.close()
        os.replace(tmp, state / "index.sqlite")
        return {"ok": True, "conversation_count": conversations, "message_count": message_count, "search_engine": search_engine}
    except BaseException:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def read_index(state: Path):
    if (state / "archive-publication.json").exists():
        raise OSError("Archive publication needs recovery")
    path = state / "index.sqlite"
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def public_row(row):
    item = dict(row)
    for k in ("is_self", "is_group"):
        if k in item and item[k] is not None:
            item[k] = bool(item[k])
    return item


def cursor_encode(timestamp, mid):
    return base64.urlsafe_b64encode(json.dumps([timestamp, mid], separators=(",", ":")).encode()).decode().rstrip("=")


def cursor_decode(cursor):
    if len(cursor) > 4096:
        raise ValueError("分页参数过长")
    value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], int) or not isinstance(value[1], str):
        raise ValueError("分页参数无效")
    return value


def first_arg(params, key, default=""):
    return params.get(key, [default])[0]


def bound_int(params, key, default, maximum):
    return max(0, min(maximum, int(first_arg(params, key, str(default)))))


def status_data(state):
    notes = ["范围仅限已经成功导入本机的记录；不能证明已覆盖手机或全部历史。"]
    if not (state / "index.sqlite").exists():
        probe = {}
        try:
            probe = json.loads((state / "source-status.json").read_text())
        except (OSError, ValueError):
            pass
        return {"ok": True, "ready": False, "source": "本机微信数据库", "account_label": "本地微信",
                "conversation_count": 0, "message_count": 0, "first_message_at": None, "last_message_at": None,
                "last_sync_at": None, "notes": probe.get("notes", ["尚未导入可读取的聊天记录。可在本页选择已导出的 JSON 文件，或完成本机数据库的解密接入。"]),
                "local_database_count": probe.get("database_count"), "access_state": probe.get("access_state", "not_connected"),
                "capabilities": {"text": False, "search": False, "media": False, "read_only": True, "local_only": True}}
    with read_index(state) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
        manifest = json.loads(row[0]) if row else {}
        n, start, end = conn.execute("SELECT count(*),min(nullif(timestamp,0)),max(timestamp) FROM messages").fetchone()
        chats = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
        media = conn.execute("SELECT count(*) FROM messages WHERE media_url IS NOT NULL").fetchone()[0]
        notes += [str(x) for x in manifest.get("notes", [])]
        return {"ok": True, "ready": True, "source": str(manifest.get("source", "本地导入的聊天归档")),
                "account_label": str(manifest.get("account_label", "本地微信")), "conversation_count": chats,
                "message_count": n, "first_message_at": start, "last_message_at": end,
                "last_sync_at": manifest.get("imported_at"), "notes": notes,
                "capabilities": {"text": True, "search": True, "media": bool(media), "read_only": True, "local_only": True}}


class ViewerServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer normally calls getfqdn() here. This loopback-only service
        # has a fixed local name and must start even when reverse DNS stalls.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def __init__(self, port, state, token):
        self.state, self.token = state, token
        self.state_id = state_id(state)
        self.cookie_name = "personal_world_session_" + self.state_id
        self.world = WorldModel(state)
        self.profile = PersonalProfile(state)
        self.agenda = PersonalAgenda(state)
        super().__init__(("127.0.0.1", port), ViewerHandler)


class ViewerHandler(http.server.BaseHTTPRequestHandler):
    server_version = "PersonalWorld/0.1"

    def log_message(self, *_):
        # URLs may contain search terms or the local bootstrap token.
        pass

    def allowed_host(self):
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def authenticated(self):
        try:
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            value = jar.get(self.server.cookie_name)
            return value is not None and hmac.compare_digest(value.value, self.server.token)
        except (http.cookies.CookieError, TypeError):
            return False

    def headers_common(self, status, content_type, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        is_history = urllib.parse.urlsplit(self.path).path in ("/history", "/history/", "/index.html")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if is_history else "DENY")
        ancestors = "'self'" if is_history else "'none'"
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors " + ancestors)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def json_response(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        self.headers_common(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def do_GET(self):
        if not self.allowed_host():
            return self.json_response({"error": "仅接受本机地址"}, 403)
        split = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        if split.path == "/healthz":
            return self.json_response({"ok": True, "app": APP_ID, "version": VERSION, "state_id": self.server.state_id})
        access = first_arg(params, "access")
        if split.path == "/" and access:
            if not hmac.compare_digest(access, self.server.token):
                return self.json_response({"error": "本地启动凭据无效，请重新双击启动文件"}, 403)
            self.headers_common(303, "text/plain; charset=utf-8", 0, {"Location": "/", "Set-Cookie": f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
            return
        if split.path in ("/", "/world.html", "/history", "/history/", "/index.html", "/profile", "/profile.html", "/agenda", "/agenda.html", "/agenda.js"):
            name = "world.html" if split.path in ("/", "/world.html") else "index.html"
            if split.path in ("/profile", "/profile.html"): name = "profile.html"
            if split.path in ("/agenda", "/agenda.html"): name = "agenda.html"
            if split.path == "/agenda.js": name = "agenda.js"
            payload = (APP_DIR / name).read_bytes()
            self.headers_common(200, "text/javascript; charset=utf-8" if name == "agenda.js" else "text/html; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        if not self.authenticated():
            return self.json_response({"error": "请双击桌面「打开微信聊天查看器」完成本机连接，或使用离线 JSON 导入。"}, 401)
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            # A different local port is also a different origin.
            return self.json_response({"error": "请在查看器自身页面内访问"}, 403)
        origin = self.headers.get("Origin")
        expected = f"http://{self.headers.get('Host')}"
        if origin and origin != expected:
            return self.json_response({"error": "来源不匹配"}, 403)
        try:
            if split.path == "/api/agenda":
                return self.json_response(self.server.agenda.snapshot())
            if split.path == "/api/profile":
                return self.json_response(self.server.profile.snapshot())
            if split.path == "/api/status":
                return self.json_response(status_data(self.server.state))
            if split.path == "/api/world/overview":
                return self.json_response(self.server.world.overview())
            if split.path == "/api/world/imports":
                from history_import import catalog
                return self.json_response(catalog(self.server.state))
            if split.path == "/api/world/drafts":
                return self.json_response(self.server.world.drafts())
            if split.path == "/api/world/draft":
                before=first_arg(params,"before")
                return self.json_response(self.server.world.draft(first_arg(params,"id"),int(before) if before else None,max(1,bound_int(params,"limit",20,100))))
            if split.path == "/api/world/reviews":
                return self.json_response(self.server.world.review_ledger(max(1,bound_int(params,"limit",100,500)),bound_int(params,"offset",0,10**8)))
            if split.path.startswith("/api/media/"):
                return self.serve_media(split.path)
            if not (self.server.state / "index.sqlite").exists():
                return self.json_response({"error": "真实聊天数据库尚未接通，请查看连接状态。", "ready": False}, 409)
            if split.path.startswith("/api/world/"):
                return self.world_get(split.path, params)
            with read_index(self.server.state) as conn:
                if split.path == "/api/conversations":
                    return self.conversations(conn, params)
                if split.path == "/api/messages":
                    return self.messages(conn, params)
                if split.path == "/api/search":
                    return self.search(conn, params)
                if split.path == "/api/export":
                    return self.export(conn, params)
            self.json_response({"error": "页面不存在"}, 404)
        except AppError:
            self.json_response({"error":"私有记录不可读取，请检查文件权限或本机数据状态。"},503)
        except SourceChangedError:
            self.json_response({"error":"来源已变化，请重新读取画像","code":"source_changed"},409)
        except KeyError:
            self.json_response({"error": "证据或候选不存在，请刷新后重试"}, 404)
        except (ValueError, TypeError, json.JSONDecodeError):
            self.json_response({"error": "请求参数无效"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (sqlite3.Error, OSError):
            self.json_response({"error": "本地索引暂时无法读取，请检查连接状态或重新导入。"}, 503)

    def world_get(self, path, params):
        world = self.server.world
        query = first_arg(params, "q")
        start, end = first_arg(params, "from"), first_arg(params, "to")
        if path == "/api/world/opportunities":
            result = world.opportunities(query, start, end, max(1, bound_int(params, "limit", 50, 500)), first_arg(params, "status"))
        elif path == "/api/world/timeline":
            result = world.timeline(query, start, end, max(1, bound_int(params, "limit", 100, 300)), first_arg(params, "cursor"))
        elif path == "/api/world/graph":
            result = world.graph(query, max(8, bound_int(params, "limit", 80, 160)))
        elif path == "/api/world/evidence":
            result = world.evidence(first_arg(params, "id"), max(1, bound_int(params, "radius", 4, 25)))
        elif path == "/api/world/coverage":
            result = world.coverage()
        else:
            return self.json_response({"error": "接口不存在"}, 404)
        return self.json_response(result)

    def do_POST(self):
        # These endpoints alter only personal review state, never WeChat,
        # the archive, the index, or an external account. Every browser write
        # needs both the private cookie and the exact same Origin.
        if not self.allowed_host():
            return self.json_response({"error": "仅接受本机地址"}, 403)
        if not self.authenticated():
            return self.json_response({"error": "请通过本机启动入口连接"}, 401)
        expected = f"http://{self.headers.get('Host')}"
        if self.headers.get("Origin") != expected or self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            return self.json_response({"error": "本地工作台修改需要同源请求"}, 403)
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/world/import/preview":
            return self.import_preview()
        if path not in ("/api/world/review", "/api/world/draft", "/api/world/draft/edit", "/api/world/import/apply", "/api/world/import/cancel", "/api/profile/update", "/api/profile/export", "/api/agenda/preview", "/api/agenda/update", "/api/agenda/export"):
            return self.json_response({"error": "仅支持本地人工标记和草稿；没有发送或写入微信的接口"}, 405)
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                return self.json_response({"error": "需要 JSON 请求"}, 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 131072:
                return self.json_response({"error": "请求体过大或为空"}, 413)
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("body")
            if path in ("/api/agenda/preview", "/api/agenda/update", "/api/agenda/export"):
                method={"/api/agenda/preview":self.server.agenda.preview,"/api/agenda/update":self.server.agenda.update,"/api/agenda/export":self.server.agenda.export}[path]
                return self.json_response(method(body))
            if path == "/api/profile/update":
                return self.json_response(self.server.profile.mutate(body))
            if path == "/api/profile/export":
                return self.json_response(self.server.profile.export(body))
            if path in ("/api/world/import/apply","/api/world/import/cancel"):
                from history_import import commit,discard
                action=commit if path.endswith("/apply") else discard
                return self.json_response(action(body.get("plan_id"),self.server.state))
            fingerprint = body.get("source_fingerprint")
            if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
                raise ValueError("source_fingerprint")
            if path == "/api/world/review":
                result = self.server.world.save_review(body.get("id"), body.get("status"), body.get("note", ""), fingerprint)
            elif path == "/api/world/draft/edit":
                result = self.server.world.save_draft(body.get("id"),body.get("title"),body.get("body"),body.get("expected_revision"),fingerprint)
            else:
                result = self.server.world.create_draft(body.get("id"), fingerprint)
            return self.json_response(result)
        except AppError as error:
            return self.json_response({"error":error.message,"code":error.code},409 if error.code in ("plan_stale","plan_expired","identity_conflict","state_busy","publish_recovery_needed") else 400)
        except AgendaConflictError as error:
            return self.json_response({"error":str(error),"code":"agenda_conflict"},409)
        except ProfileConflictError:
            return self.json_response({"error":"画像已在其他窗口修改；请重新读取再合并，未保存文字仍保留。","code":"profile_conflict"},409)
        except DraftConflictError:
            return self.json_response({"error":"草稿已在其他窗口更新；未覆盖最新版本，你的修改仍保留。请重新读取后合并。","code":"draft_conflict"},409)
        except SourceChangedError:
            return self.json_response({"error": "依据已变化，请重新检索并复核", "code": "source_changed"}, 409)
        except KeyError:
            return self.json_response({"error": "候选已变化，请重新检索并核对证据"}, 404)
        except (ValueError, TypeError) as error:
            return self.json_response({"error":str(error) if path.startswith("/api/agenda/") and type(error) is ValueError else "请求参数无效"}, 400)
        except (sqlite3.Error, OSError):
            return self.json_response({"error": "本地工作台暂时无法写入，原始归档未改变"}, 503)

    def import_preview(self):
        from history_import import prepare,MAX_UPLOAD
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type","").split(";",1)[0].strip()!="application/octet-stream":
                return self.json_response({"error":"请选择本机JSON/JSONL文件，使用原始文件上传。"},415)
            lengths=self.headers.get_all("Content-Length",[])
            if len(lengths)!=1 or not lengths[0].isdigit():
                return self.json_response({"error":"文件长度无效。"},400)
            length=int(lengths[0])
            if not 0<length<=MAX_UPLOAD:return self.json_response({"error":"请分批导入，每个文件上限512 MiB。"},413)
            params=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            fmt=first_arg(params,"format")
            if fmt not in ("json","jsonl"):raise ValueError("format")
            self.connection.settimeout(30)
            with tempfile.TemporaryDirectory(prefix=".upload-",dir=self.server.state) as temporary:
                source=Path(temporary)/("upload."+fmt)
                with source.open("xb") as output:
                    source.chmod(0o600);remaining=length
                    while remaining:
                        chunk=self.rfile.read(min(1024*1024,remaining))
                        if not chunk:raise ValueError("incomplete upload")
                        output.write(chunk);remaining-=len(chunk)
                preview=prepare(source,self.server.state,source_id=first_arg(params,"source_id","primary"),
                                policy=first_arg(params,"policy","keep_existing"),label=first_arg(params,"label","手动归档"))
            return self.json_response(preview)
        except AppError as error:
            return self.json_response({"error":error.message,"code":error.code},409 if error.code in ("state_busy","preview_source_changed","too_many_previews") else 400)
        except (ValueError,TypeError):
            return self.json_response({"error":"文件或导入参数无效，当前历史没有改变。"},400)
        except (OSError,sqlite3.Error):
            return self.json_response({"error":"本机预览未完成，当前历史保持不变。"},503)

    def unsupported_method(self):
        self.json_response({"error": "不支持此操作"}, 405)

    do_PUT = unsupported_method
    do_DELETE = unsupported_method
    do_PATCH = unsupported_method
    do_OPTIONS = unsupported_method

    def conversations(self, conn, params):
        limit = max(1, bound_int(params, "limit", 100, 500))
        offset = bound_int(params, "offset", 0, 10**8)
        q, kind = first_arg(params, "q")[:500], first_arg(params, "kind", "all")
        clauses, args = [], []
        if q:
            clauses.append("(instr(lower(name),lower(?))>0 OR instr(lower(id),lower(?))>0)")
            args += [q, q]
        if kind in ("group", "direct"):
            clauses.append("is_group=?")
            args.append(kind == "group")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = conn.execute("SELECT count(*) FROM conversations" + where, args).fetchone()[0]
        rows = conn.execute("SELECT * FROM conversations" + where + " ORDER BY last_message_at DESC,id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        self.json_response({"items": [public_row(r) for r in rows], "total": total, "has_more": offset + len(rows) < total})

    def messages(self, conn, params):
        cid = first_arg(params, "conversation_id")
        conversation = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if conversation is None:
            return self.json_response({"error": "会话不存在"}, 404)
        limit = max(1, bound_int(params, "limit", 100, 500))
        clauses, args = ["conversation_id=?"], [cid]
        date = first_arg(params, "date")
        if date:
            day = dt.date.fromisoformat(date)
            zone = dt.timezone(dt.timedelta(hours=8))
            start = int(dt.datetime.combine(day, dt.time.min, zone).timestamp())
            clauses += ["timestamp>=?", "timestamp<?"]
            args += [start, start + 86400]
        before = first_arg(params, "before")
        around = first_arg(params, "around")
        if before:
            t, mid = cursor_decode(before)
            clauses.append("(timestamp<? OR (timestamp=? AND id<?))")
            args += [t, t, mid]
        where = " AND ".join(clauses)
        if around and not before:
            anchor = conn.execute("SELECT timestamp,id FROM messages WHERE id=? AND conversation_id=?", (around, cid)).fetchone()
            if anchor is None:
                return self.json_response({"error": "定位的消息不存在"}, 404)
            t, mid = anchor
            older = conn.execute("SELECT * FROM messages WHERE " + where + " AND (timestamp<? OR (timestamp=? AND id<?)) ORDER BY timestamp DESC,id DESC LIMIT ?", [*args, t, t, mid, limit // 2 + 1]).fetchall()
            newer = conn.execute("SELECT * FROM messages WHERE " + where + " AND (timestamp>? OR (timestamp=? AND id>=?)) ORDER BY timestamp,id LIMIT ?", [*args, t, t, mid, limit - min(len(older), limit // 2)]).fetchall()
            has_more = len(older) > limit // 2
            rows = list(reversed(older[:limit // 2])) + list(newer)
            latest = False
        else:
            desc = conn.execute("SELECT * FROM messages WHERE " + where + " ORDER BY timestamp DESC,id DESC LIMIT ?", [*args, limit + 1]).fetchall()
            has_more = len(desc) > limit
            rows = list(reversed(desc[:limit]))
            latest = not before and not date
        next_before = cursor_encode(rows[0]["timestamp"], rows[0]["id"]) if rows and has_more else None
        self.json_response({"items": [public_row(r) for r in rows], "has_more": has_more,
                            "next_before": next_before, "conversation": public_row(conversation), "latest": latest})

    def search(self, conn, params):
        q = first_arg(params, "q").strip()[:500]
        if not q:
            return self.json_response({"items": [], "total": 0, "has_more": False})
        cid = first_arg(params, "conversation_id")
        offset, limit = bound_int(params, "offset", 0, 10**8), max(1, bound_int(params, "limit", 50, 200))
        meta = json.loads(conn.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
        args, clauses = [], []
        if meta.get("search_engine") == "trigram" and len(q) >= 3:
            clauses.append("m.rowid IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            args.append('"' + q.replace('"', '""') + '"')
        clauses.append("instr(lower(m.text),lower(?))>0")
        args.append(q)
        if cid:
            clauses.append("m.conversation_id=?")
            args.append(cid)
        where = " WHERE " + " AND ".join(clauses)
        total = conn.execute("SELECT count(*) FROM messages m" + where, args).fetchone()[0]
        rows = conn.execute("SELECT m.*,c.name AS conversation_name FROM messages m LEFT JOIN conversations c ON c.id=m.conversation_id" + where + " ORDER BY m.timestamp DESC,m.id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        self.json_response({"items": [public_row(r) for r in rows], "total": total, "has_more": offset + len(rows) < total})

    def export(self, conn, params):
        cid = first_arg(params, "conversation_id")
        chat = conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if chat is None:
            return self.json_response({"error": "会话不存在"}, 404)
        fmt = first_arg(params, "format", "json")
        if fmt not in ("json", "txt"):
            raise ValueError("export format")
        filename = "wechat-conversation." + fmt
        self.headers_common(200, "application/json; charset=utf-8" if fmt == "json" else "text/plain; charset=utf-8", extra={"Content-Disposition": f'attachment; filename="{filename}"'})
        rows = conn.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY timestamp,id", (cid,))
        if fmt == "json":
            self.wfile.write(("{\"conversation\":" + json.dumps(public_row(chat), ensure_ascii=False) + ',"messages":[').encode())
            first = True
            for row in rows:
                self.wfile.write((("" if first else ",") + json.dumps(public_row(row), ensure_ascii=False)).encode())
                first = False
            self.wfile.write(b"]}")
        else:
            self.wfile.write((chat["name"] + "\n本机微信聊天归档 · 时间为北京时间\n\n").encode())
            zone = dt.timezone(dt.timedelta(hours=8))
            for row in rows:
                stamp = dt.datetime.fromtimestamp(row["timestamp"], zone).strftime("%Y-%m-%d %H:%M:%S") if row["timestamp"] else "时间未知"
                sender = "我" if row["is_self"] else row["sender_name"]
                self.wfile.write((f"[{stamp}] {sender}\n{row['text']}\n\n").encode())

    def serve_media(self, url_path):
        rel = urllib.parse.unquote(url_path[len("/api/media/"):])
        if not rel or any(c in rel for c in ("/", "\\", "\x00")) or rel in (".", ".."):
            return self.json_response({"error": "附件路径无效"}, 400)
        root = self.server.state / "media"
        target = root / rel
        if root.is_symlink() or target.is_symlink() or not target.is_file():
            return self.json_response({"error": "附件尚未保存到本机可读缓存"}, 404)
        # Hold the verified descriptor throughout streaming. A replaced path
        # must not redirect a later open to an unrelated private file.
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            os.close(descriptor)
            return self.json_response({"error": "附件类型无效"}, 404)
        try:
            return self.stream_media(descriptor, target.name, opened.st_size)
        finally:
            os.close(descriptor)

    def stream_media(self, descriptor, filename, size):
        target = Path(filename)
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        inline = mime.startswith(("image/", "audio/", "video/")) and mime not in ("image/svg+xml",)
        start, end, status = 0, size - 1, 200
        extra = {"Accept-Ranges": "bytes", "Content-Disposition": "inline" if inline else "attachment"}
        range_header = self.headers.get("Range", "")
        if range_header:
            try:
                unit, span = range_header.split("=", 1)
                lo, hi = span.split("-", 1)
                if unit != "bytes" or "," in span:
                    raise ValueError()
                if lo:
                    start, end = int(lo), min(int(hi), size - 1) if hi else size - 1
                else:
                    start = max(0, size - int(hi))
                if start < 0 or start > end or start >= size:
                    raise ValueError()
                status = 206
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            except ValueError:
                self.headers_common(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                return
        self.headers_common(status, mime, max(0, end - start + 1), extra)
        with os.fdopen(os.dup(descriptor), "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = f.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)


def open_local(port, token):
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{port}/?access={urllib.parse.quote(token)}", new=2)


def main():
    parser = argparse.ArgumentParser(description="AIMAN Personal World · 本地证据与人工行动工作台")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--import-file", type=Path)
    parser.add_argument("--import-only", action="store_true")
    parser.add_argument("--skip-search",action="store_true",help="Validation-only input index; do not build a search index")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    state = args.data_dir.expanduser().resolve()
    private_dir(state)
    if args.import_file:
        print(json.dumps(import_archive(args.import_file.expanduser().resolve(), state,build_search=not args.skip_search), ensure_ascii=False))
        if args.import_only:
            return
    token = secrets.token_urlsafe(32)
    server = ViewerServer(args.port, state, token)
    access_file = state / ACCESS_FILE
    pending = state / (".local-access-" + secrets.token_hex(8))
    try:
        with pending.open("x", encoding="utf-8") as output:
            pending.chmod(0o600)
            json.dump({"port": server.server_address[1], "token": token, "pid": os.getpid(), "state_id": server.state_id}, output)
        os.replace(pending, access_file)
    finally:
        pending.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "listening": f"http://127.0.0.1:{server.server_address[1]}", "ready": (state / "index.sqlite").exists()}, ensure_ascii=False), flush=True)
    if args.open:
        threading.Timer(0.4, open_local, args=(server.server_address[1], token)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
