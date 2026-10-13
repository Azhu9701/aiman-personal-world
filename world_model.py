"""Local, evidence-backed retrieval. No model calls and no network clients.

Observations are messages; candidates are review queues, never business facts.
The source index is always opened read-only. Only explicit review/draft actions
write to a separate private workspace database.
"""
from __future__ import annotations

import base64
from collections import Counter, defaultdict
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import draft_store
from draft_store import DraftConflictError

ZONE = dt.timezone(dt.timedelta(hours=8))
DAY = 86400
MAX_MATCHES = 50000
MAX_CANDIDATES = 5000
TOPICS = {
    "机械臂与末端": ("机械臂", "协作臂", "灵巧手", "抓手", "末端执行器", "机械手"),
    "关节与传动": ("关节", "减速器", "无框电机", "力矩电机", "伺服", "谐波"),
    "控制与软件": ("运动控制", "控制器", "驱动器", "电机控制", "ros2", "ros 2", "控制方案"),
    "制造与打样": ("3d打印", "3d 打印", "增材制造", "cnc", "机加工", "注塑", "打样", "量产"),
    "机器人": ("机器人", "人形", "机器狗", "具身", "robot", "四足"),
}
ROBOT_TERMS = tuple(dict.fromkeys(t for group in TOPICS.values() for t in group
                                if t not in ("打样", "量产", "控制器", "驱动器", "注塑")))
SIGNALS = {
    "demand": ("需求线索", re.compile(r"需求|需要|采购|订购|求购|询价|报价|找.{0,12}(供应商|厂家|工厂|方案)|想.{0,12}(做|买|打样)|有没有.{0,15}(供|厂家|方案|推荐)")),
    "supply": ("供给线索", re.compile(r"产能|可以提供|可提供|能提供|可以做|能做|能供|供应|承接|加工能力|有现货|提供.{0,10}方案")),
    "proposal": ("合作或项目讨论", re.compile(r"合作|一起做|联合开发|项目|试产|打样|订单|估算|成本")),
}
PROGRESS = re.compile(r"已(?:经)?.{0,8}(?:交付|完成|验收|付款|签约|采购|下单|量产)|项目.{0,8}(?:结束|完成|取消)|不做了|暂不推进|取消了")
CONTEXT_TERMS = ("交付", "完成", "验收", "付款", "签约", "下单", "取消", "不做了", "不需要", "暂不推进", "排满", "电话", "线下")
PROMOTION = re.compile(r"扫码|转发抽奖|关注公众号|直播预告|限时优惠|点击领取")
QUANTITY = re.compile(r"\d+\s*(?:套|台|个|件|万元|元|批)")
STATUSES = ("needs_review", "follow_up", "dismissed", "completed")
SCOPE_NOTE = ("只在已导入的本地记录中按关键词和规则检索。候选是待核实的讨论片段；"
              "未找到后续同主题记录不等于项目没有推进，也不代表联系人当前仍具备相关能力。")


class SourceChangedError(ValueError):
    """The user is acting on evidence different from the current snapshot."""


def digest(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def public_message(row):
    result = dict(row)
    if result.get("is_self") is not None:
        result["is_self"] = bool(result["is_self"])
    return result


def topic_tags(text):
    text = text.casefold()
    return [name for name, terms in TOPICS.items() if any(t in text for t in terms)]


def query_terms(query, default_robot=False):
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("搜索词过长")
    query = query.strip().casefold()
    if query in ("机器人", "robotics", "robot", "机器人商业机会") or (not query and default_robot):
        return ROBOT_TERMS
    # A natural-language question mentioning the robotics use case can still
    # enter the transparent vocabulary search. This is not semantic search.
    if len(query) > 18 and "机器人" in query and any(t in query for t in ("机会", "聊天", "过去")):
        return ROBOT_TERMS
    return tuple(dict.fromkeys(re.split(r"[\s,，;；]+", query)))[:12] if query else ()


def date_bounds(start="", end=""):
    def parse(value, plus=0):
        return int(dt.datetime.combine(dt.date.fromisoformat(value), dt.time.min, ZONE).timestamp()) + plus if value else None
    lo, hi = parse(start), parse(end, DAY)
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError("日期范围无效")
    return lo, hi


def clauses_for(terms=(), start="", end="", alias="m"):
    clauses, args = [], []
    if terms:
        clauses.append("(" + " OR ".join(f"instr(lower({alias}.text),?)>0" for _ in terms) + ")")
        args.extend(terms)
    lo, hi = date_bounds(start, end)
    if lo is not None:
        clauses.append(f"{alias}.timestamp>=?")
        args.append(lo)
    if hi is not None:
        clauses.append(f"{alias}.timestamp<?")
        args.append(hi)
    return clauses, args


def encode_cursor(timestamp, mid):
    return base64.urlsafe_b64encode(json.dumps([timestamp, mid], separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("分页参数无效")
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except Exception as exc:
        raise ValueError("分页参数无效") from exc
    if not isinstance(data, list) or len(data) != 2 or type(data[0]) is not int or not isinstance(data[1], str):
        raise ValueError("分页参数无效")
    return data


class WorldModel:
    def __init__(self, state):
        self.state = Path(state)
        self._lock = threading.RLock()
        self._cache = {}

    @contextlib.contextmanager
    def index(self):
        if (self.state / "archive-publication.json").exists():
            raise OSError("Archive publication needs completion or recovery")
        path = (self.state / "index.sqlite").resolve()
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")  # One consistent source snapshot per operation.
            yield conn
        finally:
            conn.close()

    def source_signature(self):
        if (self.state / "archive-publication.json").exists():
            raise OSError("Archive publication needs recovery")
        info = (self.state / "index.sqlite").stat()
        return (info.st_ino, info.st_mtime_ns, info.st_size)

    @contextlib.contextmanager
    def workspace(self, write=False):
        path = self.state / "workspace.sqlite"
        if not path.exists() and not write:
            yield None
            return
        if path.is_symlink():
            raise ValueError("工作台状态不能是符号链接")
        if write:
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ValueError("工作台文件权限必须仅限本人")
            conn = sqlite3.connect(path, timeout=10)
        else:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        with contextlib.closing(conn):
            if write:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS reviews (
                        id TEXT PRIMARY KEY, status TEXT NOT NULL, note TEXT NOT NULL,
                        updated_at INTEGER NOT NULL, anchor_id TEXT NOT NULL,
                        source_fingerprint TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS drafts (
                        id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL,
                        title TEXT NOT NULL, body TEXT NOT NULL,
                        evidence_ids TEXT NOT NULL, created_at INTEGER NOT NULL,
                        source_fingerprint TEXT NOT NULL);
                """)
                draft_store.ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("PRAGMA query_only=ON")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                if write:
                    conn.commit()
            except Exception:
                if write:
                    conn.rollback()
                raise

    def reviews(self):
        with self._lock, self.workspace() as conn:
            if conn is None:
                return {}
            return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM reviews")}

    def overview(self):
        ready = (self.state / "index.sqlite").is_file()
        counts = {"messages": 0, "conversations": 0, "media": 0, "first_timestamp": None, "last_timestamp": None}
        manifest = {}
        if ready:
            with self.index() as conn:
                counts["messages"], counts["first_timestamp"], counts["last_timestamp"] = conn.execute(
                    "SELECT count(*),min(nullif(timestamp,0)),max(timestamp) FROM messages").fetchone()
                counts["conversations"] = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
                counts["media"] = conn.execute("SELECT count(*) FROM messages WHERE media_url IS NOT NULL").fetchone()[0]
                meta = conn.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
                manifest = json.loads(meta[0]) if meta else {}
        reviews = Counter(r["status"] for r in self.reviews().values())
        return {"ready": ready, "demo": manifest.get("synthetic") is True,
                "counts": counts, "reviews": dict(reviews), "source": manifest.get("source", "本地归档"),
                "analysis": {"method": "local_rules", "semantic_search": False, "cloud_upload": False,
                             "automatic_actions": False, "at_rest_encryption": False},
                "notes": [SCOPE_NOTE, "归档、索引与工作台使用本机访问权限保护；当前版本尚未提供应用级静态加密。"]}

    def coverage(self):
        with self.index() as conn:
            total, first, last = conn.execute("SELECT count(*),min(nullif(timestamp,0)),max(timestamp) FROM messages").fetchone()
            types = [dict(r) for r in conn.execute("SELECT type,count(*) AS count,sum(media_url IS NOT NULL) AS available_media FROM messages GROUP BY type ORDER BY count(*) DESC,type")]
            statuses = [dict(r) for r in conn.execute("SELECT coalesce(media_status,'not_reported') AS status,count(*) AS count FROM messages GROUP BY media_status ORDER BY count(*) DESC")]
        return {"total_messages": total, "by_type": types, "media_status": statuses,
                "date_range": {"first": first, "last": last},
                "notes": ["这是已导入归档的范围，不能证明已覆盖手机或全部历史。",
                          "可用附件计数不等于原图或原文件完整恢复；缩略图、首帧和未解码状态分别保留。",
                          "available_media 表示归档有本地引用；使用 verify 命令核对实际文件。"]}

    def evidence(self, mid, radius=4):
        if not isinstance(mid, str) or not mid or len(mid) > 2048:
            raise ValueError("证据编号无效")
        radius = max(1, min(int(radius), 25))
        with self.index() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            if row is None:
                raise KeyError("证据不存在于当前归档")
            message = public_message(row)
            cid, stamp = row["conversation_id"], row["timestamp"]
            before = conn.execute("SELECT * FROM messages WHERE conversation_id=? AND (timestamp<? OR (timestamp=? AND id<?)) ORDER BY timestamp DESC,id DESC LIMIT ?", (cid, stamp, stamp, mid, radius)).fetchall()
            after = conn.execute("SELECT * FROM messages WHERE conversation_id=? AND (timestamp>? OR (timestamp=? AND id>?)) ORDER BY timestamp,id LIMIT ?", (cid, stamp, stamp, mid, radius)).fetchall()
            chat = dict(conn.execute("SELECT id,name,is_group FROM conversations WHERE id=?", (cid,)).fetchone())
        return {"message": message, "conversation": chat,
                "context": [public_message(r) for r in reversed(before)] + [message] + [public_message(r) for r in after],
                "fingerprint": digest(message), "scope_note": "原始证据取自本次已导入的归档；摘要指纹只能核对内容一致性，不能证明原消息的真实性。"}

    def timeline(self, query="", start="", end="", limit=100, cursor=""):
        clauses, args = clauses_for(query_terms(query), start, end)
        if cursor:
            stamp, mid = decode_cursor(cursor)
            clauses.append("(m.timestamp<? OR (m.timestamp=? AND m.id<?))")
            args += [stamp, stamp, mid]
        limit = max(1, min(int(limit), 300))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.index() as conn:
            rows = conn.execute("SELECT m.*,c.name AS conversation_name FROM messages m JOIN conversations c ON c.id=m.conversation_id" + where + " ORDER BY m.timestamp DESC,m.id DESC LIMIT ?", [*args, limit + 1]).fetchall()
        items = []
        for row in rows[:limit]:
            item = public_message(row)
            item["evidence_id"] = item["id"]
            item["tags"] = topic_tags(item["text"])
            items.append(item)
        return {"items": items, "has_more": len(rows) > limit,
                "next_cursor": encode_cursor(items[-1]["timestamp"], items[-1]["id"]) if len(rows) > limit and items else None,
                "scope_note": "按原消息时间排列，日期使用北京时间。主题标签是词语匹配，不代表已确认的项目、事件或本人立场。"}

    def _base_candidates(self, query):
        terms = query_terms(query, default_robot=True)
        signature = self.source_signature()
        cache_key = (signature, terms)
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
            clauses, args = clauses_for(terms)
            where = " WHERE " + " AND ".join(clauses)
            with self.index() as conn:
                scanned, as_of = conn.execute("SELECT count(*),coalesce(max(timestamp),0) FROM messages").fetchone()
                matched = conn.execute("SELECT count(*) FROM messages m" + where, args).fetchone()[0]
                rows = [public_message(r) for r in conn.execute(
                    "SELECT m.*,c.name AS conversation_name,c.is_group FROM messages m JOIN conversations c ON c.id=m.conversation_id" + where + " ORDER BY m.timestamp DESC,m.id DESC LIMIT ?", [*args, MAX_MATCHES])]
                # Follow-up messages often omit the original product name.
                # Read these in the same SQLite snapshot and keep their
                # uncertain project attribution visible alongside exact hits.
                context_where = " OR ".join("instr(m.text,?)>0" for _ in CONTEXT_TERMS)
                contextual = defaultdict(list)
                relevant_chats = {r["conversation_id"] for r in rows}
                context_seen = 0
                for r in conn.execute("SELECT m.* FROM messages m WHERE " + context_where + " ORDER BY m.timestamp DESC,m.id DESC LIMIT ?", (*CONTEXT_TERMS, MAX_MATCHES + 1)):
                    context_seen += 1
                    if context_seen > MAX_MATCHES:
                        break
                    if r["conversation_id"] in relevant_chats:
                        contextual[r["conversation_id"]].append(public_message(r))
                for value in contextual.values():
                    value.reverse()
                context_truncated = context_seen > MAX_MATCHES
            # Fixed 30-day windows keep identifiers stable across later imports.
            # Grouping is explicitly a discussion fragment, never a project ID.
            groups, later = defaultdict(list), defaultdict(list)
            for row in reversed(rows):
                tags = topic_tags(row["text"])
                topic = tags[0] if tags else "搜索主题"
                later[(row["conversation_id"], topic)].append(row)
                if row["timestamp"] <= 0 or row["type"] in ("system", "sticker", "image", "voice", "video"):
                    continue
                text = row["text"][:16000]
                signals = [kind for kind, (_, pattern) in SIGNALS.items() if pattern.search(text)]
                if not signals or PROMOTION.search(text) or len(text) > 6000:
                    continue
                # Link/file metadata and unidentified senders can supply context
                # but cannot independently anchor a candidate.
                if row["type"] != "text" or (not row["sender_name"] and row["is_self"] is None):
                    continue
                row = {**row, "_signals": signals, "_tags": tags}
                key = (row["conversation_id"], topic, row["timestamp"] // (30 * DAY))
                groups[key].append(row)
            candidates = []
            for (cid, topic, window), evidence in groups.items():
                evidence.sort(key=lambda r: (r["timestamp"], r["id"]))
                anchor = next((r for r in evidence if "demand" in r["_signals"]), evidence[0])
                first, last = evidence[0]["timestamp"], evidence[-1]["timestamp"]
                # Require demand/supply or at least an explicit collaborative
                # phrase. A generic mention of 'project' alone is too weak.
                kinds = {k for r in evidence for k in r["_signals"]}
                if not kinds.intersection(("demand", "supply")) and not any(re.search(r"合作|一起做|联合开发|打样|订单", r["text"]) for r in evidence):
                    continue
                later_rows = [r for r in later[(cid, topic)] if r["timestamp"] > last]
                context_later = [r for r in contextual[cid] if r["timestamp"] > last]
                age = max(0, (as_of - last) // DAY)
                if later_rows:
                    follow = {"state": "later_related_evidence", "label": "存在后续同主题记录，需比对是否属于同一项目", "last_related_timestamp": later_rows[-1]["timestamp"]}
                elif context_later:
                    follow = {"state": "later_related_evidence", "label": "存在同会话后续进展或线下沟通措辞，项目归属待核对", "last_related_timestamp": context_later[-1]["timestamp"]}
                elif context_truncated:
                    follow = {"state": "recent", "label": "后续语境扫描达到上限，无法判断后续记录范围", "last_related_timestamp": None}
                elif age >= 90:
                    follow = {"state": "no_later_match", "label": "归档中未找到后续同主题记录，实际进展待核实", "last_related_timestamp": None}
                else:
                    follow = {"state": "recent", "label": "距归档末尾不足 90 天，进展仍待观察", "last_related_timestamp": None}
                # Show both subsequent evidence and possible closure, never
                # hide it to make an abandoned-opportunity narrative fit.
                closure = [r for r in evidence + later_rows + context_later if PROGRESS.search(r["text"])]
                chosen = [anchor] + [r for r in evidence if r["id"] != anchor["id"]][:8]
                extras = closure[:2] + later_rows[-2:] + context_later[-2:]
                evidence_ids = list(dict.fromkeys(r["id"] for r in chosen + extras))[:14]
                referenced = {r["id"]: r for r in chosen + extras}
                fingerprint = digest(
                    [(mid, {k: referenced[mid][k] for k in sorted(referenced[mid]) if not k.startswith("_")}) for mid in evidence_ids],
                    [(r["id"], r["text"]) for r in evidence], follow, bool(closure))
                signals = [{"kind": k, "label": SIGNALS[k][0], "message_id": r["id"]}
                           for r in chosen for k in r.get("_signals", [])]
                if closure:
                    signals.append({"kind": "possible_progress", "label": "出现完成、取消或推进措辞，请核对", "message_id": closure[0]["id"]})
                quantity = bool(QUANTITY.search("\n".join(r["text"] for r in chosen)))
                score = min(100, 20 + 18 * ("demand" in kinds) + 12 * ("supply" in kinds) + 7 * ("proposal" in kinds)
                            + min(12, len(evidence) * 2) + 8 * quantity + 5 * (not anchor["is_group"])
                            + 8 * (follow["state"] == "no_later_match") - 15 * bool(closure))
                title_kind = "需求讨论" if "demand" in kinds else "供给讨论" if "supply" in kinds else "合作讨论"
                item = {"id": "opportunity:" + digest(cid, topic, window)[:32], "title": topic + " · " + title_kind,
                        "conversation_id": cid, "conversation_name": anchor["conversation_name"],
                        "first_timestamp": first, "last_timestamp": last, "anchor_id": anchor["id"],
                        "evidence_ids": evidence_ids, "signals": signals, "topics": list(dict.fromkeys(t for r in evidence for t in r["_tags"])),
                        "score": score, "score_label": "线索相关度（规则排序，不是成功率或个人评分）",
                        "status": "needs_review", "review_note": "", "follow_up": follow,
                        "excerpt": anchor["text"][:700], "source_fingerprint": fingerprint,
                        "missing": ["实际项目进展及是否仍有需求", "当前规格、预算、交付时间与产能", "对方是否愿意继续联系"],
                        "possible_progress": bool(closure), "grouping_note": "同会话、同主题、固定 30 天窗口的讨论片段；不确认它们属于同一项目。"}
                candidates.append(item)
            candidates.sort(key=lambda r: (-r["score"], -r["last_timestamp"], r["id"]))
            # Cross-conversation links remain proposals grounded in separate
            # anchors. Shared vocabulary is not compatibility or availability.
            pools = defaultdict(list)
            base_fingerprints = {r["id"]: r["source_fingerprint"] for r in candidates}
            for item in candidates:
                kinds = {s["kind"] for s in item["signals"]}
                for tag in item["topics"]:
                    for kind in kinds.intersection(("demand", "supply")):
                        if len(pools[(tag, kind)]) < 30:
                            pools[(tag, kind)].append(item)
            for item in candidates:
                kinds = {s["kind"] for s in item["signals"]}
                target_kind = "supply" if "demand" in kinds else "demand"
                pool = {}
                for tag in item["topics"]:
                    for other in pools[(tag, target_kind)]:
                        shared_specific = (set(other["topics"]) & set(item["topics"])) - {"机器人"}
                        if shared_specific and other["conversation_id"] != item["conversation_id"] and not other["possible_progress"]:
                            pool[other["id"]] = other
                ranked = sorted(pool.values(), key=lambda r: (-len(set(r["topics"]) & set(item["topics"])), -r["score"], -r["last_timestamp"], r["id"]))
                seen_chats, leads = set(), []
                for other in ranked:
                    if other["conversation_id"] in seen_chats:
                        continue
                    seen_chats.add(other["conversation_id"])
                    leads.append({"id": other["id"], "title": other["title"], "conversation_name": other["conversation_name"],
                                  "anchor_id": other["anchor_id"], "evidence_ids": [other["anchor_id"]],
                                  "excerpt": other["excerpt"][:350], "timestamp": other["last_timestamp"], "topics": other["topics"],
                                  "relation": "共享主题的需求/供给线索，兼容性及当前状态未核实"})
                    if len(leads) == 3:
                        break
                item["related_leads"] = leads
                item["source_fingerprint"] = digest(base_fingerprints[item["id"]], [(x["id"], base_fingerprints[x["id"]]) for x in leads])
            result = {"items": candidates[:MAX_CANDIDATES], "method": "local_rules", "scanned_messages": scanned,
                      "matched_messages": matched, "considered_messages": len(rows), "analysis_as_of": as_of,
                      "total_candidates": len(candidates), "truncated": matched > MAX_MATCHES or len(candidates) > MAX_CANDIDATES or context_truncated,
                      "scope_note": SCOPE_NOTE + (f" 单次最多分析最近 {MAX_MATCHES:,} 条主题命中及 {MAX_MATCHES:,} 条后续语境命中；当前范围已截断。" if matched > MAX_MATCHES or context_truncated else "")}
            # Bound search-derived caches so free-form queries cannot accumulate
            # arbitrary amounts of private content in process memory.
            self._cache = {k: v for k, v in self._cache.items() if k[0] == signature}
            if len(self._cache) >= 3:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = result
            return result

    @staticmethod
    def _present_candidate(original, reviews, candidates):
        # Derived visibility is never written into the evidence cache and never
        # changes its source fingerprint. Human edits are not new source facts.
        item = {**original, "review_stale": False}
        review = reviews.get(item["id"])
        if review:
            item.update(status=review["status"], review_note=review["note"],
                        review_stale=review["source_fingerprint"] != item["source_fingerprint"])
        item["effective_status"] = "needs_review" if item["review_stale"] else item["status"]
        visible=[]
        for lead in original.get("related_leads", []):
            peer=reviews.get(lead["id"])
            source=candidates.get(lead["id"])
            if source is None:
                continue
            # Closed or stale peers require an explicit new review before
            # being suggested as a current counterpart again.
            if peer and (peer["status"] in ("dismissed", "completed") or peer["source_fingerprint"] != source["source_fingerprint"]):
                continue
            visible.append(dict(lead))
        item["related_leads"] = visible
        item["hidden_related_leads"] = len(original.get("related_leads", [])) - len(visible)
        return item

    def opportunities(self, query="机器人", start="", end="", limit=50, status=""):
        if status not in ("", "active", *STATUSES):
            raise ValueError("状态筛选无效")
        lo, hi = date_bounds(start, end)
        result = self._base_candidates(query)
        reviews, items = self.reviews(), []
        candidates={x["id"]:x for x in result["items"]}
        for original in result["items"]:
            if lo is not None and original["last_timestamp"] < lo:
                continue
            if hi is not None and original["first_timestamp"] >= hi:
                continue
            item=self._present_candidate(original,reviews,candidates)
            if status == "active" and item["effective_status"] not in ("needs_review","follow_up"):
                continue
            if status and status != "active" and item["effective_status"] != status:
                continue
            items.append(item)
        return {**result, "items": items[:max(1, min(int(limit), 500))], "filtered_candidates": len(items)}

    def _candidate_map(self):
        signature=self.source_signature()
        result={}
        for (sig,_),cached in list(self._cache.items()):
            if sig==signature:
                result.update((x["id"],x) for x in cached["items"])
        return result

    def review_ledger(self, limit=100, offset=0):
        limit=max(1,min(int(limit),500))
        offset=max(0,int(offset))
        reviews=self.reviews()
        ordered=sorted(reviews.values(),key=lambda r:(-r["updated_at"],r["id"]))
        items=[]
        for review in ordered[offset:offset+limit]:
            item=dict(review)
            try:
                source=self._find_candidate(review["id"])
                item.update(title=source["title"],source_state="unchanged" if source["source_fingerprint"]==review["source_fingerprint"] else "changed")
            except (KeyError,OSError,sqlite3.Error):
                item.update(title="此前保存的复核记录",source_state="unavailable")
            item["effective_status"]=review["status"] if item["source_state"]=="unchanged" else "needs_review"
            items.append(item)
        return {"items":items,"total":len(ordered),"has_more":offset+len(items)<len(ordered),
                "next_offset":offset+len(items) if offset+len(items)<len(ordered) else None,
                "scope_note":"保留本人复核和备注；来源无法定位不等于项目消失。未能重现的自定义搜索请先重新检索。"}

    def _find_candidate(self, oid):
        if not isinstance(oid, str) or not re.fullmatch(r"opportunity:[0-9a-f]{32}", oid):
            raise ValueError("候选编号无效")
        # Arbitrary-query candidates are already available only after an
        # authenticated read. Recompute the default set after a cold restart.
        signature = self.source_signature()
        for (sig, _), result in list(self._cache.items()):
            if sig == signature:
                for item in result["items"]:
                    if item["id"] == oid:
                        return item
        result = self._base_candidates("机器人")
        for item in result["items"]:
            if item["id"] == oid:
                return item
        raise KeyError("候选已变化，请重新检索并核对证据")

    def save_review(self, oid, status, note="", expected_fingerprint=None):
        if status not in STATUSES or not isinstance(note, str) or len(note) > 4000:
            raise ValueError("人工状态或备注无效")
        with self._lock:
            candidate = self._find_candidate(oid)
            if expected_fingerprint is not None and expected_fingerprint != candidate["source_fingerprint"]:
                raise SourceChangedError("依据已变化，请重新检索并复核")
            record = {"id": oid, "status": status, "note": note, "updated_at": int(time.time()),
                      "anchor_id": candidate["anchor_id"], "source_fingerprint": candidate["source_fingerprint"]}
            with self.workspace(write=True) as conn:
                conn.execute("INSERT INTO reviews VALUES (:id,:status,:note,:updated_at,:anchor_id,:source_fingerprint) ON CONFLICT(id) DO UPDATE SET status=excluded.status,note=excluded.note,updated_at=excluded.updated_at,anchor_id=excluded.anchor_id,source_fingerprint=excluded.source_fingerprint", record)
        return {"ok": True, "review": record}

    def create_draft(self, oid, expected_fingerprint=None):
        with self._lock:
            candidate = self._find_candidate(oid)
            if expected_fingerprint is not None and expected_fingerprint != candidate["source_fingerprint"]:
                raise SourceChangedError("依据已变化，请重新检索并复核")
            review_snapshot=self.reviews()
            original_candidate=candidate
            candidate=self._present_candidate(candidate,review_snapshot,self._candidate_map())
            stamp = dt.datetime.fromtimestamp(candidate["first_timestamp"], ZONE).strftime("%Y-%m-%d")
            lines = ["本地跟进准备稿 · 待本人核对", "", candidate["title"],
                     f"历史线索：{candidate['conversation_name']}，{stamp}。", "",
                     "可编辑的联系草稿：", f"你好，想跟进一下我们之前关于「{'、'.join(candidate['topics']) or '这项合作'}」的讨论。",
                     "请问这个方向现在还有需求或合作空间吗？当时的方案是否已经推进，或者有了新的变化？",
                     "如果方便，我们可以先确认规格、数量、预算和时间安排，再讨论是否继续。", "",
                     "核对要点：", *["- " + x for x in candidate["missing"]], "",
                     "后续记录提示：" + candidate["follow_up"]["label"], "",
                     "原始证据编号：", *["- " + mid for mid in candidate["evidence_ids"]], "",
                     "这份草稿由固定模板生成，尚未验证当前需求或产能，不包含报价或履约承诺。仅保存本地，未发送。"]
            related = candidate.get("related_leads", [])
            if related:
                lines += ["", "跨会话候选（仅共享主题，不能确认技术兼容、当前产能或合作意愿）："]
                lines += ["- " + lead["conversation_name"] + " · " + lead["title"] + "；证据：" + lead["anchor_id"] for lead in related]
            evidence_ids = list(dict.fromkeys(candidate["evidence_ids"] + [lead["anchor_id"] for lead in related]))
            draft_key=digest(oid,candidate["source_fingerprint"])
            if candidate["hidden_related_leads"]:
                draft_key=digest(oid,candidate["source_fingerprint"],evidence_ids)
            record = {"id": "draft:" + draft_key[:32],
                      "opportunity_id": oid, "title": candidate["title"] + " · 跟进草稿", "body": "\n".join(lines),
                      "evidence_ids": evidence_ids, "created_at": int(time.time()),
                      "source_fingerprint": candidate["source_fingerprint"], "status": "draft"}
            with self.workspace(write=True) as conn:
                current_reviews={r["id"]:dict(r) for r in conn.execute("SELECT * FROM reviews")}
                relevant={oid}|{lead["id"] for lead in original_candidate.get("related_leads",[])}
                if any(current_reviews.get(k)!=review_snapshot.get(k) for k in relevant):
                    raise SourceChangedError("关联线索状态已变化，请重新核对")
                conn.execute("INSERT OR IGNORE INTO drafts VALUES (?,?,?,?,?,?,?)", (record["id"], oid, record["title"], record["body"], json.dumps(record["evidence_ids"]), record["created_at"], record["source_fingerprint"]))
                saved = draft_store.latest(conn,record["id"])
            return self._annotate_draft(saved)

    def _annotate_draft(self, row):
        row=dict(row)
        try:
            original=self._find_candidate(row["opportunity_id"])
            current=self._present_candidate(original,self.reviews(),self._candidate_map())
            row["source_stale"]=original["source_fingerprint"] != row["source_fingerprint"]
            expected=list(dict.fromkeys(current["evidence_ids"]+[x["anchor_id"] for x in current.get("related_leads",[])]))
            row["workflow_stale"]=not row["source_stale"] and expected != row["evidence_ids"]
        except (KeyError,OSError,sqlite3.Error):
            row["source_stale"]=None
            row["workflow_stale"]=None
        row["source_note"]="原始生成版及修改版本均保留；依据或关联状态变化后需重新核对并生成新草稿。"
        row["editable"]=row["source_stale"] is False and row["workflow_stale"] is False
        return row

    def draft(self, did, before=None, limit=20):
        draft_store.validate_id(did)
        with self._lock,self.workspace() as conn:
            row=draft_store.latest(conn,did)
            versions=draft_store.history(conn,did,before,limit)
        return {"draft":self._annotate_draft(row),"history":versions}

    def save_draft(self, did, title, body, expected_revision, expected_fingerprint):
        draft_store.validate_id(did)
        draft_store.validate_edit(title,body,expected_revision)
        if not isinstance(expected_fingerprint,str) or not re.fullmatch(r"[0-9a-f]{64}",expected_fingerprint):
            raise ValueError("来源指纹无效")
        with self._lock:
            # Validate existence and provenance without creating a workspace.
            with self.workspace() as conn:
                row=draft_store.latest(conn,did)
            if row["source_fingerprint"]!=expected_fingerprint:
                raise SourceChangedError("草稿依据不一致，请重新核对")
            signature=self.source_signature()
            review_snapshot=self.reviews()
            state=self._annotate_draft(row)
            if not state["editable"]:
                raise SourceChangedError("依据或关联状态已变化，请先核对后生成新草稿；现有文字不会丢失。")
            with self.workspace(write=True) as conn:
                # Recheck while all workspace writers are serialized.
                if signature!=self.source_signature():
                    raise SourceChangedError("来源已更新，请重新核对")
                current_reviews={r["id"]:dict(r) for r in conn.execute("SELECT * FROM reviews")}
                if current_reviews!=review_snapshot:
                    raise SourceChangedError("复核记录已变化，请重新核对")
                saved=draft_store.save(conn,did,title,body,expected_revision)
                if signature!=self.source_signature():
                    raise SourceChangedError("保存期间来源已更新，请重新核对")
            return self._annotate_draft(saved)

    def drafts(self):
        with self._lock,self.workspace() as conn:
            ids=[r[0] for r in conn.execute("SELECT id FROM drafts ORDER BY created_at DESC,id LIMIT 500")] if conn else []
            rows=[draft_store.latest(conn,did) for did in ids]
        rows=[self._annotate_draft(r) for r in rows]
        rows.sort(key=lambda r:(-r["updated_at"],r["id"]))
        return {"items":rows,"scope_note":"草稿修改按版本保存在本机；历史生成版不覆盖，没有消息发送接口。"}

    def graph(self, query="机器人", limit=80):
        limit = max(8, min(int(limit), 160))
        terms = query_terms(query, default_robot=True)
        clauses, args = clauses_for(terms)
        where = " WHERE " + " AND ".join(clauses)
        with self.index() as conn:
            matched = conn.execute("SELECT count(*) FROM messages m" + where, args).fetchone()[0]
            rows = [public_message(r) for r in conn.execute("SELECT m.*,c.name AS conversation_name FROM messages m JOIN conversations c ON c.id=m.conversation_id" + where + " ORDER BY m.timestamp DESC,m.id DESC LIMIT 2000", args)]
        nodes, edges = {}, {}
        omitted = 0
        for row in rows:
            cid = "conversation:" + digest(row["conversation_id"])[:20]
            # Display names are not identity keys. Scope every non-self person
            # to their conversation, and retain that limitation in the UI.
            sender = row["sender_name"] or "未知发送者"
            pid = "person:" + digest(row["conversation_id"], sender, row["is_self"])[:20]
            labels = topic_tags(row["text"]) or ["搜索主题"]
            needed = {cid: {"id": cid, "label": row["conversation_name"], "kind": "conversation", "count": 0},
                      pid: {"id": pid, "label": ("我" if row["is_self"] else sender), "kind": "person", "count": 0,
                            "scope": "conversation", "conversation_name": row["conversation_name"]}}
            for label in labels:
                tid = "topic:" + digest(label)[:20]
                needed[tid] = {"id": tid, "label": label, "kind": "topic", "count": 0}
            if len(set(needed) | set(nodes)) > limit:
                omitted += 1
                continue
            for nid, value in needed.items():
                if nid not in nodes:
                    nodes[nid] = value
                nodes[nid]["count"] += 1
            pairs = [(pid, cid, "在此会话发言")]
            pairs += [(pid, "topic:" + digest(label)[:20], "在消息中提及") for label in labels]
            for source, target, label in pairs:
                eid = "edge:" + digest(source, target, label)[:20]
                edge = edges.setdefault(eid, {"id": eid, "source": source, "target": target, "label": label, "evidence_ids": [], "count": 0})
                edge["count"] += 1
                if len(edge["evidence_ids"]) < 8:
                    edge["evidence_ids"].append(row["id"])
        return {"nodes": list(nodes.values()), "edges": list(edges.values()), "matched_messages": matched,
                "considered_messages": len(rows), "truncated": matched > len(rows) or omitted > 0,
                "scope_note": "这是关键词命中消息的证据子图，最多展示最近 2,000 条命中与指定节点数。同名发送者按会话隔离；同会话内的同名仍可能有歧义，尚未做真实身份消歧。连线只表示发言或主题提及，不证明雇佣、合作关系或实际能力。"}
