#!/usr/bin/env python3
"""Append the separate WeChat AI subsystem without changing ordinary Msg_ tables.

Only copied DB/WAL files are opened through SQLCipher.  Protobuf fields are
selected by an explicit allowlist; resource identifiers, resource hashes, AES material,
trace metadata, and complete BLOBs are never emitted.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterator

from sync_wechat import (
    Database, SyncError, app_message, emit_record, fingerprints, integer,
    key_spec, load_keys, quote_identifier, xml_root, xml_text,
)

ADAPTER = "append_chatbot.v1"
SUBSYSTEM = "wechat_ai"
TABLE = "chatbot_message_v1"
SOURCE_ID = "wechat_chatbot_message_v1"
MESSAGE_PREFIX = "wechat-ai:message:"
CONVERSATION_PREFIX = "wechat-ai:dialogue:"
BEIJING = dt.timezone(dt.timedelta(hours=8))
MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_WIRE_FIELDS = 8192
NOTES = [
    "微信 AI 对话：独立数据库的记录按对话保留，未混入普通 Msg_ 表的覆盖统计。",
    "微信 AI 对话：文件卡片保留文件名与原卡片显示大小；未映射的附件实体不可预览。引用检索条目的详细内容尚未导入。",
]


class ChatbotError(Exception):
    """Messages are fixed descriptions and never include source contents."""


def number(value: Any) -> int:
    result = integer(value)
    if result is None:
        raise ChatbotError("微信 AI 消息缺少有效的整数元数据，未更新导出。")
    return result


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ChatbotError("Protobuf 数据截断。")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ChatbotError("Protobuf 整数越界。")
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
    raise ChatbotError("Protobuf 整数越界。")


def parse_wire(data: bytes) -> list[tuple[int, int, Any]]:
    if len(data) > MAX_BLOB_BYTES:
        raise ChatbotError("微信 AI 卡片超出解析大小上限。")
    result: list[tuple[int, int, Any]] = []
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        field, wire_type = tag >> 3, tag & 7
        if not 0 < field < 2**29 or len(result) >= MAX_WIRE_FIELDS:
            raise ChatbotError("Protobuf 字段结构无效。")
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type in (1, 2, 5):
            if wire_type == 2:
                size, offset = read_varint(data, offset)
            else:
                size = 8 if wire_type == 1 else 4
            if size > len(data) - offset:
                raise ChatbotError("Protobuf 字段长度越界。")
            value = data[offset:offset + size]
            offset += size
        else:
            raise ChatbotError("Protobuf 使用了当前未支持的字段类型。")
        result.append((field, wire_type, value))
    return result


def bytes_at(data: bytes, path: tuple[int, ...]) -> list[bytes]:
    values = [data]
    if len(path) > 8:
        raise ChatbotError("Protobuf 路径超出深度上限。")
    for step in path:
        values = [
            value for item in values
            for field, kind, value in parse_wire(item)
            if field == step and kind == 2
        ]
    return values


def one_text(data: bytes, path: tuple[int, ...]) -> str:
    values = bytes_at(data, path)
    if len(values) != 1:
        raise ChatbotError("微信 AI 卡片正文路径不唯一。")
    try:
        text = values[0].decode("utf-8")
    except UnicodeDecodeError:
        raise ChatbotError("微信 AI 卡片正文不是有效 UTF-8。") from None
    if not text.strip() or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
        raise ChatbotError("微信 AI 卡片正文为空或含未支持的控制字符。")
    return text


def decode_card(blob: bytes, raw_type: int) -> dict[str, Any]:
    """No generic string harvesting: only the three observed card schemas."""
    result: dict[str, Any] = {
        "type": "card", "text": "[微信 AI 卡片：内容暂未解析]",
        "decode_status": "chatbot_unmapped_content", "content_fields": [],
        "source_blob_bytes": len(blob),
        "source_blob_sha256": hashlib.sha256(blob).hexdigest(),
        "unparsed_resource_fields_preserved_in_source": True,
        "citation_entry_count": 0,
    }
    try:
        wrappers = bytes_at(blob, (1,))
        if len(wrappers) != 1:
            raise ChatbotError("微信 AI 卡片封装数量暂未支持。")
        inner_types = [value for field, kind, value in parse_wire(wrappers[0]) if field == 1 and kind == 0]
        if inner_types != [raw_type]:
            raise ChatbotError("微信 AI 卡片类型与行元数据不一致。")
        result["citation_entry_count"] = len(bytes_at(blob, (1, 14, 1)))
        if raw_type == 30:
            result.update(
                type="text", text=one_text(blob, (1, 16, 2)),
                decode_status="ok", content_fields=["1.16.2"],
            )
        elif raw_type == 2:
            xml = one_text(blob, (1, 2))
            root = xml_root(xml)
            app = app_message(root)
            if app is None or integer(xml_text(app, "type")) != 5:
                raise ChatbotError("微信 AI XML 卡片不是已验证的链接类型。")
            title, description = xml_text(app, "title"), xml_text(app, "des")
            if not title and not description:
                raise ChatbotError("微信 AI 链接卡片没有可读标题或摘要。")
            result.update(
                type="link", text="\n".join(v for v in (title, description) if v),
                decode_status="metadata_only",
                content_fields=["1.2:/msg/appmsg/title", "1.2:/msg/appmsg/des"],
                media_status="link_card_metadata_only",
            )
        elif raw_type == 18:
            filename = one_text(blob, (1, 11, 5))
            display_size = one_text(blob, (1, 11, 6))
            if not re.fullmatch(r"\s*\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|字节)\s*", display_size, re.IGNORECASE):
                raise ChatbotError("微信 AI 文件卡片的大小格式尚未验证。")
            result.update(
                type="file", file_name=filename, file_size_display=display_size,
                text="[文件] " + filename + "\n原卡片显示大小：" + display_size,
                decode_status="metadata_only", media_status="unmapped_local_attachment",
                content_fields=["1.11.5", "1.11.6"],
            )
    except (ChatbotError, ValueError, TypeError):
        # Preserve one metadata record rather than dropping an unfamiliar row.
        result.update(
            type="card", text="[微信 AI 卡片：内容暂未解析]",
            decode_status="chatbot_unmapped_content", content_fields=[],
        )
        result.pop("file_name", None)
        result.pop("file_size_display", None)
    return result


def direction_evidence(rows: list[dict[str, Any]], self_id: str | None) -> dict[str, Any]:
    """A snapshot-local mapping, supported by XML identity and dialogue order."""
    groups: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    matches = 0
    conflicts = 0
    for row in rows:
        groups[number(row["dialogue_id"])].append(row)
        if self_id and number(row["type"]) == 2:
            try:
                root = xml_root(one_text(bytes(row["card_wraplist_buffer"] or b""), (1, 2)))
                sender = xml_text(root, "fromusername") if root is not None else ""
                if sender == self_id:
                    matches += 1
                    conflicts += number(row["direction"]) != 1
            except (ChatbotError, ValueError, TypeError):
                pass
    ordered = 0
    for group in groups.values():
        inputs = [r for r in group if number(r["direction"]) == 1]
        replies = [r for r in group if number(r["direction"]) == 0]
        if inputs and replies and min(number(r["create_time"]) for r in inputs) < min(number(r["create_time"]) for r in replies):
            ordered += 1
    verified = bool(self_id and matches and not conflicts and ordered == len(groups) and groups)
    return {
        "status": "snapshot_xml_self_match_and_dialogue_order" if verified else "unknown",
        "xml_self_match_count": matches,
        "xml_direction_conflict_count": conflicts,
        "dialogue_count": len(groups),
        "dialogues_with_input_before_reply": ordered,
        "direction_1_is_self": True if verified else None,
        "direction_0_is_reply": True if verified else None,
        "scope": "this_snapshot_not_a_recovered_protocol_enum",
    }


def stable_id(prefix: str, values: list[Any]) -> str:
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()[:32]


def message_id(row: dict[str, Any]) -> str:
    # The index sorts (integer-second timestamp, id). Preserve millisecond order
    # within a second without putting the source PK or dialogue ID in the ID.
    milliseconds = number(row["create_time"])
    if not -(1 << 63) <= milliseconds < (1 << 63):
        raise ChatbotError("微信 AI 消息时间超出有效范围。")
    prefix = MESSAGE_PREFIX + f"{milliseconds + (1 << 63):020d}:"
    return stable_id(prefix, [SOURCE_ID, row["__source_identity"]])


def normalize_rows(rows: list[dict[str, Any]], self_id: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    evidence = direction_evidence(rows, self_id)
    mapped = evidence["status"] != "unknown"
    groups: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    messages: list[dict[str, Any]] = []
    identities: set[str] = set()
    for row in rows:
        dialogue = number(row["dialogue_id"])
        cid = stable_id(CONVERSATION_PREFIX, [SOURCE_ID, dialogue])
        mid = message_id(row)
        if mid in identities:
            raise ChatbotError("微信 AI 来源行标识重复，未更新导出。")
        identities.add(mid)
        direction, raw_type = number(row["direction"]), number(row["type"])
        is_self = direction == 1 if mapped and direction in (0, 1) else None
        sender = "我" if is_self is True else "微信 AI" if is_self is False else "微信 AI 对话（方向待确认）"
        role_id = self_id if is_self is True else "wechat-ai:assistant" if is_self is False else None
        decoded = decode_card(bytes(row["card_wraplist_buffer"] or b""), raw_type)
        message = {
            "record_type": "message", "id": mid, "conversation_id": cid,
            "sender_name": sender, "sender_id": role_id, "is_self": is_self,
            "sender_identity_kind": "self_account" if is_self is True else "adapter_role" if is_self is False else "unknown",
            "timestamp": number(row["create_time"]) // 1000,
            "raw_type": "chatbot:" + str(raw_type), "server_id": str(row["svrid"]),
            "source_adapter": ADAPTER, "source_subsystem": SUBSYSTEM,
            "source_table": TABLE, "source_direction": direction,
            "source_create_time_ms": number(row["create_time"]),
            "source_qa_seq": number(row["qa_seq"]),
            "direction_status": evidence["status"], **decoded,
        }
        messages.append(message)
        groups[dialogue].append(message)
    messages.sort(key=lambda m: (m["timestamp"], m["id"]))
    conversations = []
    for order, group in enumerate(sorted(groups.values(), key=lambda g: (min(m["timestamp"] for m in g), g[0]["conversation_id"])), 1):
        group.sort(key=lambda m: (m["timestamp"], m["id"]))
        date_label = dt.datetime.fromtimestamp(group[0]["timestamp"], BEIJING).strftime("%Y-%m-%d %H:%M")
        conversations.append({
            "record_type": "conversation", "id": group[0]["conversation_id"],
            "name": "微信 AI 对话 · " + date_label + " · " + str(order).zfill(2),
            "is_group": False, "message_count": len(group),
            "last_message_at": group[-1]["timestamp"], "last_message": group[-1]["text"][:400],
            "source_adapter": ADAPTER, "source_subsystem": SUBSYSTEM,
        })
    source_counts = collections.Counter(
        stable_id(CONVERSATION_PREFIX, [SOURCE_ID, number(row["dialogue_id"])]) for row in rows
    )
    details = {
        "source_id": SOURCE_ID, "source_adapter": ADAPTER,
        "database": "chatbot/chatbot_message.db", "table": TABLE,
        "expected_count": len(rows), "exported_count": len(messages),
        "expected_conversation_count": len(groups), "exported_conversation_count": len(conversations),
        "message_type_counts": dict(collections.Counter(m["type"] for m in messages)),
        "decode_status_counts": dict(collections.Counter(m["decode_status"] for m in messages)),
        "unknown_direction_count": sum(m["is_self"] is None for m in messages),
        "unmapped_attachment_count": sum(m.get("media_status") == "unmapped_local_attachment" for m in messages),
        "citation_message_count": sum(bool(m["citation_entry_count"]) for m in messages),
        "citation_entry_count": sum(m["citation_entry_count"] for m in messages),
        "citation_details_imported": False, "direction_evidence": evidence,
        "record_coverage_complete": len(rows) == len(messages),
        "content_complete": all(m["decode_status"] == "ok" and not m["citation_entry_count"] for m in messages),
        "message_identity_sha256": hashlib.sha256(json.dumps(
            sorted([[m["id"], m["conversation_id"]] for m in messages]),
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        "conversation_counts": [
            {"conversation_id": c["id"], "expected_count": source_counts[c["id"]],
             "exported_count": c["message_count"]}
            for c in sorted(conversations, key=lambda c: c["id"])
        ],
    }
    return messages, conversations, details


@contextlib.contextmanager
def chatbot_snapshot(root: Path, attempts: int = 3) -> Iterator[tuple[Path, Path, dict[str, Any]]]:
    """Copy only this subsystem's DB and WAL; SHM is rebuilt in the temp copy."""
    for attempt in range(attempts):
        candidates = list(root.rglob("chatbot_message.db"))
        if len(candidates) != 1:
            raise ChatbotError("所选来源没有唯一的微信 AI 消息数据库。")
        source = candidates[0]
        before = fingerprints(root, [source])
        with tempfile.TemporaryDirectory(prefix="wechat-ai-readonly-") as temporary:
            destination = Path(temporary)
            try:
                for relative, expected in before.items():
                    target = destination / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with (root / relative).open("rb") as inp, target.open("xb") as out:
                        os.chmod(target, 0o600)
                        first = os.fstat(inp.fileno())
                        if (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns, first.st_ctime_ns) != expected:
                            raise ChatbotError("数据库在复制期间发生变化。")
                        while chunk := inp.read(1024 * 1024):
                            out.write(chunk)
                        last = os.fstat(inp.fileno())
                        if (last.st_dev, last.st_ino, last.st_size, last.st_mtime_ns, last.st_ctime_ns) != expected or out.tell() != expected[2]:
                            raise ChatbotError("数据库在复制期间发生变化。")
                if fingerprints(root, [source]) != before:
                    raise ChatbotError("数据库或 WAL 在复制期间发生变化。")
            except (FileNotFoundError, ChatbotError, SyncError) as error:
                retryable = not isinstance(error, SyncError) or error.code == "snapshot_source_changed"
                if retryable and attempt + 1 < attempts:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                if isinstance(error, SyncError) and not retryable:
                    raise
                raise ChatbotError("未获得稳定的微信 AI 数据库副本，原有导出保持不变。") from None
            yield destination / source.relative_to(root), source, {
                "method": "stable_db_and_wal_copy_then_quick_check",
                "attempt": attempt + 1, "files": len(before),
                "bytes": sum(item[2] for item in before.values()),
                "wal_files": sum(name.endswith("-wal") for name in before),
                "source_mutated_by_importer": False,
            }
            return


def read_rows(db_root: Path, key_file: Path, snapshot_root: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = (snapshot_root or db_root).expanduser().resolve()
    keys = load_keys(key_file.expanduser().resolve())
    with chatbot_snapshot(root) as (copy, source, snapshot_info):
        with copy.open("rb") as f:
            header = f.read(16)
        spec = key_spec(keys, source, root, header)
        keys.clear()
        db = Database(copy, spec)
        try:
            db.verify()
            columns = db.columns(TABLE)
            names = {row["name"] for row in columns}
            required = {"svrid", "create_time", "dialogue_id", "qa_seq", "direction", "type", "card_wraplist_buffer"}
            if not required <= names:
                raise ChatbotError("微信 AI 数据库结构不匹配，未更新导出。")
            primary = [row["name"] for row in sorted(columns, key=lambda r: r.get("pk") or 0) if row.get("pk")]
            fields = sorted(required | set(primary))
            prefix = "" if primary else 'rowid AS "__source_rowid",'
            query = "SELECT " + prefix + ",".join(quote_identifier(name) for name in fields) + " FROM " + quote_identifier(TABLE)
            expected = int(db.one("SELECT count(*) AS n FROM " + quote_identifier(TABLE))["n"])
            rows = list(db.rows(query))
            for row in rows:
                row["__source_identity"] = [row[name] for name in primary] if primary else [row["__source_rowid"]]
            if len(rows) != expected:
                raise ChatbotError("微信 AI 数据库行数核对失败，未更新导出。")
            snapshot_info["source_mode"] = "provided_snapshot" if snapshot_root else "live_stable_copy"
            snapshot_info["integrity"] = "ok"
            return rows, snapshot_info
        finally:
            db.close()


def owned(record: dict[str, Any]) -> bool:
    return record.get("source_adapter") == ADAPTER and record.get("source_subsystem") == SUBSYSTEM


def path_outside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return False
    except ValueError:
        return True


def validate_paths(db_root: Path, archive: Path, output: Path, snapshot_root: Path | None) -> None:
    if archive == output:
        raise ChatbotError("请为追加结果指定不同的输出文件；原导出不会被原地修改。")
    account = db_root.parent
    for file in (archive, output):
        if not path_outside(file, account) or snapshot_root and not path_outside(file, snapshot_root):
            raise ChatbotError("输入归档和输出文件都必须位于微信账号与数据库副本目录之外。")
    if not archive.is_file() or archive.suffix.lower() not in {".jsonl", ".ndjson"}:
        raise ChatbotError("请提供已有的规范 JSONL 归档。")
    if output.exists():
        raise ChatbotError("输出文件已存在；请指定一个新文件以保留旧结果。")


def append_rows(archive: Path, output: Path, rows: list[dict[str, Any]], snapshot_info: dict[str, Any], self_id: str | None = None) -> dict[str, Any]:
    """Streaming rewrite, replacing only records tagged as owned by this adapter."""
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".pending", dir=output.parent)
    pending = Path(temporary)
    os.fchmod(fd, 0o600)
    manifest: dict[str, Any] = {}
    input_messages = retained_messages = removed_messages = removed_conversations = 0
    conversation_ids: set[str] = set()
    message_types: collections.Counter[str] = collections.Counter()
    decode_statuses: collections.Counter[str] = collections.Counter()
    unknown_senders = 0
    candidate_server_ids = {str(row["svrid"]) for row in rows if row.get("svrid") not in (None, 0, "", "0")}
    overlaps = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target, archive.open(encoding="utf-8-sig") as source:
            for line in source:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ChatbotError("输入归档包含非对象记录。")
                kind = record.get("record_type") or record.get("record") or record.get("kind")
                if kind == "manifest":
                    manifest.update({k: v for k, v in record.items() if k not in {"record_type", "record", "kind"}})
                    continue
                if kind not in {"message", "conversation"}:
                    raise ChatbotError("输入归档包含未知记录类型。")
                identifier = str(record.get("id") or "")
                if not identifier:
                    raise ChatbotError("输入归档存在缺少标识的记录。")
                if kind == "message":
                    input_messages += 1
                if owned(record):
                    if kind == "message":
                        removed_messages += 1
                    else:
                        removed_conversations += 1
                    continue
                if identifier.startswith((MESSAGE_PREFIX, CONVERSATION_PREFIX)):
                    raise ChatbotError("微信 AI 标识与其他来源冲突，未更新导出。")
                target.write(line if line.endswith("\n") else line + "\n")
                if kind == "message":
                    retained_messages += 1
                    conversation_ids.add(str(record["conversation_id"]))
                    message_types[str(record.get("type") or "unknown")] += 1
                    decode_statuses[str(record.get("decode_status") or "unknown")] += 1
                    unknown_senders += record.get("sender_id") is None
                    overlaps += str(record.get("server_id") or "") in candidate_server_ids
                else:
                    conversation_ids.add(identifier)
            if not manifest or manifest.get("complete") is False:
                raise ChatbotError("输入归档缺少已完成的来源清单。")
            if manifest.get("message_count") is not None and number(manifest["message_count"]) != input_messages:
                raise ChatbotError("输入归档与清单消息数不一致。")
            if overlaps:
                raise ChatbotError("微信 AI 来源与普通消息存在服务器标识重叠，需要先核查重复。")
            previous_self = manifest.get("self_id")
            if self_id and previous_self and self_id != previous_self:
                raise ChatbotError("指定的本人身份与普通归档的已核实身份不一致。")
            resolved_self = self_id or (previous_self if isinstance(previous_self, str) else None)
            messages, conversations, details = normalize_rows(rows, resolved_self)
            for message in messages:
                emit_record(target, message)
                message_types[message["type"]] += 1
                decode_statuses[message["decode_status"]] += 1
                unknown_senders += message["sender_id"] is None
            for conversation in conversations:
                emit_record(target, conversation)
                conversation_ids.add(conversation["id"])
            existing_sources = manifest.get("additional_sources", [])
            if not isinstance(existing_sources, list):
                raise ChatbotError("输入清单的额外来源字段格式不兼容。")
            details.update(snapshot=snapshot_info, completed_at=dt.datetime.now(dt.timezone.utc).isoformat(), ordinary_source_overlap_count=overlaps)
            manifest["additional_sources"] = [item for item in existing_sources if not isinstance(item, dict) or item.get("source_id") != SOURCE_ID] + [details]
            manifest.update(
                message_count=retained_messages + len(messages),
                conversation_count=len(conversation_ids),
                message_type_counts=dict(message_types),
                decode_status_counts=dict(decode_statuses),
                unknown_sender_count=unknown_senders,
                content_complete=bool(manifest.get("content_complete")) and details["content_complete"],
            )
            if "type_counts" in manifest:
                manifest["type_counts"] = dict(message_types)
            notes = manifest.get("notes", [])
            if not isinstance(notes, list):
                raise ChatbotError("输入清单的备注格式不兼容。")
            manifest["notes"] = [note for note in notes if note not in NOTES] + NOTES
            emit_record(target, {"record_type": "manifest", **manifest})
            target.flush()
            os.fsync(target.fileno())
        if output.exists():
            raise ChatbotError("输出路径在写入期间出现新文件，未覆盖该文件。")
        os.link(pending, output)  # Atomic create-if-absent; never overwrite an existing output.
        pending.unlink()
        return {
            "status": "complete_with_content_limits", "message_count": manifest["message_count"],
            "conversation_count": manifest["conversation_count"],
            "additional_source": details,
            "previous_adapter_messages_replaced": removed_messages,
            "previous_adapter_conversations_replaced": removed_conversations,
            "ordinary_records_rewritten": False,
        }
    finally:
        with contextlib.suppress(FileNotFoundError):
            pending.unlink()


def append_chatbot(db_root: Path, key_file: Path, archive: Path, output: Path, snapshot_root: Path | None = None, self_id: str | None = None) -> dict[str, Any]:
    db_root, key_file, archive, output = [p.expanduser().resolve() for p in (db_root, key_file, archive, output)]
    snapshot_root = snapshot_root.expanduser().resolve() if snapshot_root else None
    validate_paths(db_root, archive, output, snapshot_root)
    rows, snapshot_info = read_rows(db_root, key_file, snapshot_root)
    return append_rows(archive, output, rows, snapshot_info, self_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从只读副本追加微信 AI 对话，保留普通消息与媒体补全结果。")
    parser.add_argument("--db-root", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--self-id")
    args = parser.parse_args(argv)
    try:
        result = append_chatbot(args.db_root, args.key_file, args.archive, args.output, args.snapshot_root, args.self_id)
        # Identity hashes are retained in the private archive for independent
        # auditing, but terminal reports contain only aggregates and statuses.
        report = {**result, "additional_source": {
            key: value for key, value in result["additional_source"].items()
            if key not in {"conversation_counts", "message_identity_sha256"}
        }}
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except SyncError as error:
        print(json.dumps({"status": "failed", "error_code": error.code, "message": error.message}, ensure_ascii=False))
        return 1
    except ChatbotError as error:
        print(json.dumps({"status": "failed", "message": str(error)}, ensure_ascii=False))
        return 1
    except Exception as error:
        # Never print a SQL statement, account path, record, or traceback.
        print(json.dumps({"status": "failed", "message": "微信 AI 追加失败，原有归档保持不变。", "error_type": type(error).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
