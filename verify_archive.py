#!/usr/bin/env python3
"""Read-only audit. Output is numeric summaries only; no names, IDs or content.

Default: compare private archive.jsonl and index.sqlite exactly.
--media-dir: check references against a separate published media directory.
--recount-source: also recount ordinary Msg_ tables in a NEW stable DB+WAL
snapshot using the existing sync module. A newer source may legitimately differ
from the imported snapshot. Additional AI records are checked against their
separate archived source manifest, identity digest and per-dialogue counts.
Exit 0: all requested comparisons match; 1: archive/index mismatch;
2: audit could not finish; 3: only the fresh source snapshot differs.
This script never writes an archive, index, key file, app, or source database.
"""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import urllib.parse


FIELDS = ("id", "conversation_id", "sender_name", "is_self", "type", "text",
          "timestamp", "raw_type", "media_url", "file_name", "file_size", "media_status")
STATUSES = {"ok", "metadata_only", "content_missing", "content_too_large",
            "decompression_failed", "decompressor_missing",
            "binary_content_unsupported", "xml_parse_failed", "unsupported_type",
            "xml_repaired", "chatbot_unmapped_content"}
TYPES = {"text", "image", "voice", "video", "sticker", "location", "call",
         "contact", "friend_request", "system", "card", "file", "quote",
         "transfer", "red_packet", "forwarded", "mini_program", "link",
         "unsupported", "unknown", "audio", "attachment", "emoji"}
MESSAGE_DB = re.compile(r"^(?:biz_)?message_\d+\.db$", re.I)
AI_ADAPTER = "append_chatbot.v1"
AI_SUBSYSTEM = "wechat_ai"
AI_SOURCE = "wechat_chatbot_message_v1"
AI_TABLE = "chatbot_message_v1"
AI_DATABASE = "chatbot/chatbot_message.db"
AI_MESSAGE = re.compile(r"wechat-ai:message:([0-9]{20}):[0-9a-f]{32}")
AI_CONVERSATION = re.compile(r"wechat-ai:dialogue:[0-9a-f]{32}")
AI_DIRECTION = "snapshot_xml_self_match_and_dialogue_order"
XML_REPAIRS = {"invalid_numeric_reference", "misplaced_xml_declaration", "invalid_literal_character"}


def fp(path):
    s = path.lstat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def media_reference(value):
    if not isinstance(value, str) or not value.startswith("/api/media/"):
        return None
    name = urllib.parse.unquote(value[len("/api/media/"):])
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\0")):
        return None
    return "/api/media/" + urllib.parse.quote(name, safe="._-")


def expected_row(rec):
    timestamp = 0 if rec.get("timestamp") in (None, "") else int(float(rec["timestamp"]))
    if timestamp > 10**12:
        timestamp //= 1000
    return (str(rec.get("id") or ""), str(rec.get("conversation_id") or ""),
            str(rec.get("sender_name") or ""), rec.get("is_self"),
            str(rec.get("type") or "unknown"), str(rec.get("text") or ""),
            timestamp, str(rec.get("raw_type", "")), media_reference(rec.get("media_url")),
            str(rec["file_name"]) if rec.get("file_name") is not None else None,
            rec.get("file_size"), str(rec["media_status"]) if rec.get("media_status") is not None else None)


def is_ai(rec):
    return (rec.get("source_adapter") == AI_ADAPTER or rec.get("source_subsystem") == AI_SUBSYSTEM
            or str(rec.get("id") or "").startswith("wechat-ai:")
            or str(rec.get("conversation_id") or "").startswith("wechat-ai:")
            or str(rec.get("raw_type") or "").startswith("chatbot:"))


class MediaAudit:
    """Inspect each referenced file once, without reading or hashing its body."""
    def __init__(self, directory, issues, summary):
        self.directory = directory.expanduser().absolute()
        self.issues, self.summary = issues, summary
        self.files = {}
        self.directory_fp = None
        self.directory_ok = False
        # Do not resolve away a symlink before testing the configured boundary.
        if any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
            issues["media_directory_symlink"] += 1
            return
        try:
            entry = self.directory.lstat()
        except FileNotFoundError:
            return
        self.directory_fp = fp(self.directory)
        self.directory_ok = stat.S_ISDIR(entry.st_mode)
        issues["media_directory_not_directory"] += not self.directory_ok
        if os.name == "posix":
            issues["media_directory_permissions"] += bool(stat.S_IMODE(entry.st_mode) & 0o077)
            issues["media_directory_owner"] += entry.st_uid != os.geteuid()
            issues["media_directory_not_readable"] += (entry.st_mode & 0o500) != 0o500

    def check(self, value, kind, size=None):
        url = media_reference(value)
        if value not in (None, "") and url is None:
            self.issues[kind + "_url_invalid"] += 1
        if url is None:
            return False
        self.summary[kind + "_references"] += 1
        name = urllib.parse.unquote(url[len("/api/media/"):])
        if name not in self.files:
            self.summary["unique_media_paths_checked"] += 1
            path, old, valid, actual_size = self.directory / name, None, False, None
            if self.directory_ok:
                try:
                    entry = path.lstat()
                    old = fp(path)
                    valid = stat.S_ISREG(entry.st_mode)
                    self.issues["media_file_not_regular"] += not valid
                    if valid:
                        actual_size = entry.st_size
                        self.summary["unique_media_files_existing"] += 1
                        self.summary["media_file_bytes_checked_by_stat"] += actual_size
                        if os.name == "posix":
                            self.issues["media_file_permissions"] += bool(stat.S_IMODE(entry.st_mode) & 0o077)
                            self.issues["media_file_owner"] += entry.st_uid != os.geteuid()
                            self.issues["media_file_not_readable"] += not bool(entry.st_mode & 0o400)
                except FileNotFoundError:
                    pass
            self.files[name] = (path, old, valid, actual_size)
        _, _, valid, actual_size = self.files[name]
        self.issues[kind + "_url_missing_file"] += not valid
        if valid and size is not None:
            self.issues["media_url_file_size_mismatch"] += type(size) is not int or size != actual_size
        return valid

    def finish(self):
        for path, old, _, _ in self.files.values():
            try:
                current = fp(path)
            except FileNotFoundError:
                current = None
            self.issues["media_file_changed_during_audit"] += current != old
        if self.directory_fp is not None:
            try:
                current = fp(self.directory)
            except FileNotFoundError:
                current = None
            self.issues["media_directory_changed_during_audit"] += current != self.directory_fp


def audit_chatbot(manifest, records, conversations, ordinary_server_ids, issues, summary):
    sources = manifest.get("additional_sources", [])
    if not isinstance(sources, list):
        issues["additional_sources_format"] += 1
        sources = []
    matching = []
    for source in sources:
        if isinstance(source, dict) and source.get("source_id") == AI_SOURCE:
            matching.append(source)
        else:
            issues["unknown_additional_source"] += 1
    ai_conversations = {cid: rec for cid, rec in conversations.items() if is_ai(rec)}
    summary["ai_messages"] = len(records)
    summary["ai_conversations"] = len(ai_conversations)
    summary["additional_sources"] = len(sources)
    issues["ai_manifest_source_count"] = int(len(matching) != (1 if records or ai_conversations or matching else 0))
    if not matching and not records and not ai_conversations:
        return True
    source = matching[0] if matching else {}
    issues["ai_manifest_source_identity"] += any(source.get(k) != v for k, v in (
        ("source_adapter", AI_ADAPTER), ("database", AI_DATABASE), ("table", AI_TABLE)))
    pairs, chats, statuses, types, ai_server_ids = [], Counter(), Counter(), Counter(), set()
    citations = citation_messages = unknown_direction = unmapped_attachments = 0
    evidence = source.get("direction_evidence", {})
    evidence = evidence if isinstance(evidence, dict) else {}
    direction_status = evidence.get("status")
    issues["ai_direction_evidence_status"] += direction_status not in (AI_DIRECTION, "unknown")
    for rec in records:
        mid, cid = str(rec.get("id") or ""), str(rec.get("conversation_id") or "")
        match = AI_MESSAGE.fullmatch(mid)
        issues["ai_message_id_format"] += match is None
        issues["ai_message_conversation_id_format"] += AI_CONVERSATION.fullmatch(cid) is None
        issues["ai_message_source_identity"] += any(rec.get(k) != v for k, v in (
            ("source_adapter", AI_ADAPTER), ("source_subsystem", AI_SUBSYSTEM), ("source_table", AI_TABLE)))
        milliseconds = rec.get("source_create_time_ms")
        if type(milliseconds) is not int or not -(1 << 63) <= milliseconds < (1 << 63):
            issues["ai_source_time_format"] += 1
        else:
            issues["ai_timestamp_mismatch"] += rec.get("timestamp") != milliseconds // 1000
            issues["ai_message_id_time_mismatch"] += match is None or int(match[1]) != milliseconds + (1 << 63)
        issues["ai_raw_type_format"] += re.fullmatch(r"chatbot:-?[0-9]+", str(rec.get("raw_type"))) is None
        issues["ai_source_sequence_format"] += type(rec.get("source_qa_seq")) is not int
        direction = rec.get("source_direction")
        issues["ai_source_direction_format"] += type(direction) is not int
        expected_self = (direction == 1) if direction_status == AI_DIRECTION and direction in (0, 1) else None
        issues["ai_direction_mismatch"] += rec.get("is_self") is not expected_self or rec.get("direction_status") != direction_status
        expected_sender = manifest.get("self_id") if expected_self is True else "wechat-ai:assistant" if expected_self is False else None
        issues["ai_sender_role_mismatch"] += rec.get("sender_id") != expected_sender
        pairs.append([mid, cid])
        chats[cid] += 1
        status, msgtype = rec.get("decode_status"), rec.get("type")
        statuses[status if status in STATUSES else "other"] += 1
        types[msgtype if msgtype in TYPES else "other"] += 1
        unknown_direction += rec.get("is_self") is None
        unmapped_attachments += rec.get("media_status") == "unmapped_local_attachment"
        count = rec.get("citation_entry_count")
        if type(count) is not int or count < 0:
            issues["ai_citation_count_format"] += 1
        else:
            citations += count
            citation_messages += bool(count)
        server_id = str(rec.get("server_id") or "")
        if server_id not in ("", "0"):
            ai_server_ids.add(server_id)
    for cid, rec in ai_conversations.items():
        issues["ai_conversation_id_format"] += AI_CONVERSATION.fullmatch(cid) is None
        issues["ai_conversation_source_identity"] += rec.get("source_adapter") != AI_ADAPTER or rec.get("source_subsystem") != AI_SUBSYSTEM
        issues["ai_conversation_group_flag"] += rec.get("is_group") is not False
    issues["ai_message_conversation_set"] += len(set(chats) ^ set(ai_conversations))
    digest = hashlib.sha256(json.dumps(sorted(pairs), ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    issues["ai_message_identity_digest"] += source.get("message_identity_sha256") != digest
    declared_chats = {}
    chat_manifest = source.get("conversation_counts", [])
    if not isinstance(chat_manifest, list):
        issues["ai_conversation_manifest_format"] += 1
        chat_manifest = []
    for entry in chat_manifest:
        if not isinstance(entry, dict) or not isinstance(entry.get("conversation_id"), str):
            issues["ai_conversation_manifest_format"] += 1
            continue
        cid = entry["conversation_id"]
        issues["ai_duplicate_manifest_conversations"] += cid in declared_chats
        declared_chats[cid] = entry
        issues["ai_conversation_source_count"] += not (type(entry.get("expected_count")) is int
            and type(entry.get("exported_count")) is int
            and entry["expected_count"] == entry["exported_count"] == chats[cid])
    issues["ai_declared_conversation_set"] += len(set(declared_chats) ^ set(ai_conversations))
    overlap = len(ai_server_ids & ordinary_server_ids)
    issues["ai_ordinary_server_id_overlap"] += overlap
    complete = all(s == "ok" for s in statuses) and not citations
    comparisons = {
        "expected_count": len(records), "exported_count": len(records),
        "expected_conversation_count": len(ai_conversations), "exported_conversation_count": len(ai_conversations),
        "message_type_counts": dict(types), "decode_status_counts": dict(statuses),
        "unknown_direction_count": unknown_direction, "unmapped_attachment_count": unmapped_attachments,
        "citation_message_count": citation_messages, "citation_entry_count": citations,
        "citation_details_imported": False, "record_coverage_complete": True,
        "content_complete": complete, "ordinary_source_overlap_count": overlap,
    }
    for key, actual in comparisons.items():
        issues["ai_manifest_" + key] += source.get(key) != actual or (type(actual) is int and type(source.get(key)) is not int)
    issues["ai_evidence_dialogue_count"] += evidence.get("dialogue_count") != len(chats)
    if direction_status == AI_DIRECTION:
        issues["ai_evidence_inconsistent"] += not (type(evidence.get("xml_self_match_count")) is int
            and evidence["xml_self_match_count"] > 0 and evidence.get("xml_direction_conflict_count") == 0
            and evidence.get("dialogues_with_input_before_reply") == len(chats)
            and evidence.get("direction_1_is_self") is True and evidence.get("direction_0_is_reply") is True)
    summary["ai_citation_messages"] = citation_messages
    summary["ai_citation_entries"] = citations
    summary["ai_unknown_direction"] = unknown_direction
    return complete


def archive_index(state, media_dir=None):
    archive, index = state / "archive.jsonl", state / "index.sqlite"
    before = {p: fp(p) for p in (archive, index)}
    issues, summary, field_errors = Counter(), Counter(), Counter()
    summary.update({name: 0 for name in ("messages_with_media_url", "media_urls_with_existing_file",
                    "media_messages_without_url", "metadata_only_media", "metadata_only_nonmedia",
                    "media_decode_limits", "text_decode_limits")})
    statuses, types, raw_media = Counter(), Counter(), Counter()
    tables, table_chats, chat_counts, conversations = Counter(), {}, Counter(), {}
    ids, manifests = set(), []
    ai_records, ordinary_server_ids = [], set()
    repairs, repaired_messages = Counter(), 0
    media = MediaAudit(Path(media_dir) if media_dir is not None else state / "media", issues, summary)
    if any(not stat.S_ISREG(p.lstat().st_mode) for p in (archive, index)):
        raise ValueError()
    summary["private_file_permission_failures"] = sum(bool(stat.S_IMODE(p.stat().st_mode) & 0o077) for p in (archive, index)) if os.name == "posix" else 0
    summary["permission_check"] = "posix_mode_and_owner" if os.name == "posix" else "not_checked_platform_acl"
    issues["private_file_permissions"] = summary["private_file_permission_failures"]
    with contextlib.closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        issues["index_quick_check"] = int(db.execute("PRAGMA quick_check").fetchall() != [("ok",)])
        with archive.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if not line.strip():
                    continue
                rec = json.loads(line)
                kind = rec.get("record_type") or rec.get("record") or rec.get("kind")
                if kind == "manifest":
                    manifests.append(rec)
                    continue
                if manifests:
                    issues["records_after_manifest"] += 1
                if kind == "conversation":
                    cid = rec["id"]
                    issues["duplicate_conversations"] += cid in conversations
                    conversations[cid] = rec
                    summary["conversations_with_avatar_url"] += media_reference(rec.get("avatar_url")) is not None
                    summary["avatar_urls_with_existing_file"] += media.check(rec.get("avatar_url"), "avatar")
                    continue
                if kind != "message":
                    issues["unknown_record_type"] += 1
                    continue
                expected = expected_row(rec)
                mid, cid = expected[:2]
                summary["archive_messages"] += 1
                issues["duplicate_messages"] += mid in ids
                ids.add(mid)
                chat_counts[cid] += 1
                parts = mid.split(":", 2)
                ai = is_ai(rec)
                if ai:
                    ai_records.append(rec)
                elif len(parts) == 3 and MESSAGE_DB.fullmatch(Path(parts[0]).name) and parts[1].startswith("Msg_"):
                    key = tuple(parts[:2])
                    tables[key] += 1
                    table_chats.setdefault(key, set()).add(cid)
                else:
                    issues["message_source_id_format"] += 1
                if not ai:
                    summary["ordinary_messages"] += 1
                    server_id = str(rec.get("server_id") or "")
                    if server_id not in ("", "0"):
                        ordinary_server_ids.add(server_id)
                status = rec.get("decode_status")
                statuses[status if status in STATUSES else "other"] += 1
                issues["unknown_decode_status"] += status not in STATUSES
                msgtype = rec.get("type")
                types[msgtype if msgtype in TYPES else "other"] += 1
                issues["unknown_message_type"] += msgtype not in TYPES
                if rec.get("xml_repaired") is True:
                    repaired_messages += 1
                    values = rec.get("xml_repair_counts")
                    if not isinstance(values, dict) or not values:
                        issues["xml_repair_metadata_format"] += 1
                    else:
                        for key, value in values.items():
                            if key not in XML_REPAIRS or type(value) is not int or value <= 0:
                                issues["xml_repair_metadata_format"] += 1
                            else:
                                repairs[key] += value
                elif status == "xml_repaired" or rec.get("xml_repair_counts"):
                    issues["xml_repair_flag_missing"] += 1
                summary["unknown_senders"] += rec.get("sender_id") is None
                summary["unknown_direction"] += rec.get("is_self") is None
                summary["unknown_conversation_messages"] += cid.startswith("opaque:")
                summary["empty_message_text"] += not expected[5]
                summary["missing_or_nonpositive_timestamp"] += expected[6] <= 0
                summary["missing_local_id"] += not ai and rec.get("source_local_id") in (None, "")
                summary["missing_server_id"] += rec.get("server_id") in (None, "", "0", 0)
                raw = rec.get("raw_type")
                base = (int(raw) & 0xffffffff) if raw is not None and not ai else 0
                media_kind = {3: "image", 34: "voice", 43: "video", 62: "video", 47: "sticker"}.get(base)
                if msgtype in ("file", "attachment") or (base == 49 and int(raw) >> 32 == 6):
                    media_kind = "file"
                if media_kind:
                    raw_media[media_kind] += 1
                    summary["media_messages_without_url"] += expected[8] is None
                    summary["metadata_only_media"] += status == "metadata_only"
                    summary["media_decode_limits"] += status not in ("ok", "metadata_only", "xml_repaired")
                else:
                    summary["metadata_only_nonmedia"] += status == "metadata_only"
                summary["text_decode_limits"] += base == 1 and status != "ok"
                existing_file = media.check(rec.get("media_url"), "media", rec.get("file_size"))
                if expected[8]:
                    summary["messages_with_media_url"] += 1
                    summary["media_urls_with_existing_file"] += existing_file
                actual = db.execute("SELECT " + ",".join(FIELDS) + " FROM messages WHERE id=?", (mid,)).fetchone()
                if actual is None:
                    issues["index_missing_message"] += 1
                else:
                    for name, want, got in zip(FIELDS, expected, actual):
                        field_errors[name] += want != got
        issues["manifest_record_count"] = int(len(manifests) != 1)
        manifest = manifests[-1] if manifests else {}
        ai_content_complete = audit_chatbot(manifest, ai_records, conversations, ordinary_server_ids, issues, summary)
        source_tables = manifest.get("source_tables", [])
        declared = {}
        for rec in source_tables:
            key = (rec["database"], rec["table"])
            issues["duplicate_manifest_tables"] += key in declared
            declared[key] = rec
            count = tables.get(key, 0)
            issues["table_count_mismatch"] += not (rec["expected_count"] == rec["exported_count"] == count)
            issues["table_conversation_mismatch"] += bool(table_chats.get(key, set()) - {rec["conversation_id"]})
        issues["undeclared_message_tables"] = len(set(tables) - set(declared))
        issues["ordinary_table_message_total"] = int(sum(tables.values()) != summary["ordinary_messages"])
        summary["manifest_tables"] = len(declared)
        summary["unmapped_tables"] = sum(r.get("identity_status") != "mapped" for r in source_tables)
        summary["ambiguous_tables"] = sum(r.get("identity_status") == "ambiguous" for r in source_tables)
        summary["failed_databases"] = len(manifest.get("failed_databases", []))
        summary["failed_tables"] = len(manifest.get("failed_tables", []))
        summary["manifest_complete"] = int(manifest.get("complete") is True)
        summary["manifest_content_complete"] = int(manifest.get("content_complete") is True)
        summary["archive_conversations"] = len(conversations)
        summary["empty_conversations"] = sum(chat_counts[cid] == 0 for cid in conversations)
        summary["unknown_conversations"] = sum(cid.startswith("opaque:") for cid in conversations)
        summary["unnamed_conversations"] = sum(r.get("name") == cid for cid, r in conversations.items())
        for prefix, business in (("message", False), ("biz_message", True)):
            selected = {k[0] for k in declared if Path(k[0]).name.lower().startswith("biz_") == business}
            summary[prefix + "_shards_with_tables"] = len(selected)
            summary[prefix + "_tables"] = sum(k[0] in selected for k in declared)
            summary[prefix + "_messages"] = sum(n for k, n in tables.items() if k[0] in selected)
        issues["manifest_not_complete"] = int(manifest.get("complete") is not True)
        issues["manifest_failed_sources"] = summary["failed_databases"] + summary["failed_tables"]
        for key, actual in (("message_count", summary["archive_messages"]), ("conversation_count", len(conversations)),
                            ("message_table_count", len(declared)), ("unmapped_table_count", summary["unmapped_tables"]),
                            ("unknown_sender_count", summary["unknown_senders"]), ("decode_status_counts", dict(statuses)),
                            ("message_type_counts", dict(types)), ("content_complete", all(s == "ok" for s in statuses) and ai_content_complete)):
            issues["manifest_" + key] = int(manifest.get(key) != actual)
        if repaired_messages or "xml_repaired_message_count" in manifest or "xml_repair_counts" in manifest:
            issues["manifest_xml_repaired_message_count"] = int(manifest.get("xml_repaired_message_count") != repaired_messages)
            issues["manifest_xml_repair_counts"] = int(manifest.get("xml_repair_counts") != dict(repairs))
        stored = json.loads(db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
        issues["stored_manifest_fields"] = sum(stored.get(k) != v for k, v in manifest.items() if k not in ("record_type", "record", "kind"))
        for cid, rec in conversations.items():
            issues["conversation_declared_count"] += rec.get("message_count") != chat_counts[cid]
            row = db.execute("SELECT name,is_group,message_count,avatar_url FROM conversations WHERE id=?", (cid,)).fetchone()
            want = (str(rec.get("name") or cid), bool(rec.get("is_group", cid.endswith("@chatroom"))), chat_counts[cid], media_reference(rec.get("avatar_url")))
            issues["conversation_index_fields"] += row != want
            issues["conversation_avatar_index"] += row is not None and row[3] != want[3]
        issues["message_without_declared_conversation"] = len(set(chat_counts) - set(conversations))
        summary["index_messages"] = db.execute("SELECT count(*) FROM messages").fetchone()[0]
        summary["index_conversations"] = db.execute("SELECT count(*) FROM conversations").fetchone()[0]
        issues["index_total_messages"] = int(summary["index_messages"] != len(ids))
        issues["index_total_conversations"] = int(summary["index_conversations"] != len(conversations))
        issues["index_orphan_messages"] = db.execute("SELECT count(*) FROM messages m LEFT JOIN conversations c ON c.id=m.conversation_id WHERE c.id IS NULL").fetchone()[0]
        issues["index_chat_count_sum"] = int(db.execute("SELECT COALESCE(sum(message_count),0) FROM conversations").fetchone()[0] != summary["index_messages"])
        issues["index_last_message_fields"] = db.execute("""SELECT count(*) FROM conversations c WHERE
          last_message_at IS NOT COALESCE((SELECT timestamp FROM messages WHERE conversation_id=c.id ORDER BY timestamp DESC,id DESC LIMIT 1),0)
          OR last_message IS NOT COALESCE((SELECT text FROM messages WHERE conversation_id=c.id ORDER BY timestamp DESC,id DESC LIMIT 1),'')""").fetchone()[0]
    issues["archive_or_index_changed_during_audit"] = sum(fp(p) != old for p, old in before.items())
    media.finish()
    issues["index_message_field_mismatches"] = sum(field_errors.values())
    summary["decode_ok"] = statuses["ok"]
    summary["metadata_only"] = statuses["metadata_only"]
    summary["xml_repaired"] = statuses["xml_repaired"]
    summary["xml_repaired_messages"] = repaired_messages
    summary["other_decode_states"] = sum(n for s, n in statuses.items() if s not in ("ok", "metadata_only", "xml_repaired"))
    summary["raw_media_messages"] = sum(raw_media.values())
    result = {"archive_index_ok": int(not any(issues.values())), "counts": dict(summary),
              "issues": dict(issues), "index_field_mismatches": dict(field_errors),
              "decode_status_counts": dict(statuses), "message_type_counts": dict(types),
              "raw_media_type_counts": dict(raw_media), "xml_repair_counts": dict(repairs)}
    return result, manifest


def source_recount(state, app_dir, manifest):
    sys.path.insert(0, str(app_dir.resolve()))
    sync = importlib.import_module("sync_wechat")
    config = json.loads((state / "source-config.json").read_text())
    root = Path(config["db_root"]).expanduser().resolve()
    keys = sync.load_keys(Path(config["key_file"]).expanduser())
    counts, current, schema = Counter(), {}, Counter()
    # Independent filename inventory, not merely the importer's discovered set.
    def catalog():
        return {p.relative_to(root).as_posix() for p in root.rglob("*")
                if p.is_file() and MESSAGE_DB.fullmatch(p.name)}
    inventory_before = catalog()
    with sync.snapshot(root, 3) as (copy_root, sources, snapshot_info):
        counts["databases_verified"] = 0
        counts["wal_files_in_fresh_snapshot"] = snapshot_info["wal_files"]
        selected_message_paths = {p.relative_to(root).as_posix() for p in sources if MESSAGE_DB.fullmatch(p.name)}
        counts["independent_message_database_inventory"] = len(inventory_before)
        counts["message_databases_not_in_exporter_discovery"] = len(inventory_before - selected_message_paths)
        for source in sources:
            relative = source.relative_to(root).as_posix()
            copied = copy_root / relative
            with copied.open("rb") as stream:
                header = stream.read(16)
            spec = sync.key_spec(keys, source, root, header)
            with contextlib.closing(sync.Database(copied, spec)) as db:
                db.verify()
                counts["databases_verified"] += 1
                if not MESSAGE_DB.fullmatch(source.name):
                    continue
                prefix = "biz_message" if source.name.lower().startswith("biz_") else "message"
                counts[prefix + "_databases"] += 1
                tables = db.tables()
                name_table = next((t for t in tables if t["name"] == "Name2Id"), None)
                schema["name2id_missing"] += name_table is None
                if name_table:
                    cols = {c["name"] for c in db.columns("Name2Id")}
                    schema["name2id_user_name_missing"] += "user_name" not in cols
                msg = [t for t in tables if t["name"].startswith("Msg_")]
                schema["case_variant_msg_tables"] += sum(t["name"].lower().startswith("msg_") and not t["name"].startswith("Msg_") for t in tables)
                counts["databases_without_msg_tables"] += not msg
                for table in msg:
                    name = table["name"]
                    columns = {c["name"] for c in db.columns(name)}
                    for field in ("local_id", "server_id", "create_time", "local_type", "real_sender_id"):
                        schema["tables_missing_" + field] += field not in columns
                    schema["tables_missing_both_content_columns"] += not ({"message_content", "compress_content"} & columns)
                    n = int(db.one("SELECT count(*) AS n FROM " + sync.quote_identifier(name))["n"])
                    current[(relative, name)] = n
                    counts[prefix + "_tables"] += 1
                    counts[prefix + "_messages"] += n
    counts["message_database_inventory_changed_during_recount"] = int(catalog() != inventory_before)
    previous = {(r["database"], r["table"]): r["expected_count"] for r in manifest.get("source_tables", [])}
    common = set(previous) & set(current)
    counts["source_tables_now"] = len(current)
    counts["source_messages_now"] = sum(current.values())
    counts["tables_added_since_archive"] = len(set(current) - set(previous))
    counts["tables_missing_since_archive"] = len(set(previous) - set(current))
    counts["shared_tables_with_count_changes"] = sum(current[k] != previous[k] for k in common)
    counts["additional_rows_in_fresh_source"] = sum(max(0, current.get(k, 0) - previous.get(k, 0)) for k in set(current) | set(previous))
    counts["fewer_rows_in_fresh_source"] = sum(max(0, previous.get(k, 0) - current.get(k, 0)) for k in set(current) | set(previous))
    counts["selected_database_count_changed"] = int(counts["databases_verified"] != manifest.get("database_count"))
    equal = (current == previous and not counts["selected_database_count_changed"]
             and not counts["message_databases_not_in_exporter_discovery"]
             and not counts["message_database_inventory_changed_during_recount"]
             and not schema["case_variant_msg_tables"])
    return {"fresh_source_counts_match_archive": int(equal), "counts": dict(counts), "schema_counts": dict(schema)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--app-dir", type=Path)
    parser.add_argument("--media-dir", type=Path)
    parser.add_argument("--recount-source", action="store_true")
    args = parser.parse_args()
    try:
        state = args.state.expanduser().resolve()
        result, manifest = archive_index(state, args.media_dir)
        code = 0 if result["archive_index_ok"] else 1
        if args.recount_source:
            if args.app_dir is None:
                raise ValueError()
            result["source_audit"] = source_recount(state, args.app_dir.expanduser(), manifest)
            if code == 0 and not result["source_audit"]["fresh_source_counts_match_archive"]:
                code = 3
        result["exit_code"] = code
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return code
    except Exception:
        # Exception messages can contain paths, SQL or message content.
        print(json.dumps({"audit_completed": 0, "audit_error": 1, "exit_code": 2}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
