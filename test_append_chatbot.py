"""Synthetic records only; no account discovery, network, or real messages."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import append_chatbot as chatbot


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def wire(field, value):
    if isinstance(value, int):
        return varint(field << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint((field << 3) | 2) + varint(len(value)) + value


SECRETS = ["RESOURCE_SECRET_NEVER_EXPORT", "a" * 194, "b" * 32, "c" * 32]
SELF = "synthetic_self"


def card(kind, content="合成正文", citations=False):
    wrapper = wire(1, kind)
    if kind == 30:
        wrapper += wire(16, wire(1, SECRETS[0]) + wire(2, content))
    elif kind == 18:
        wrapper += wire(11, wire(1, SECRETS[1]) + wire(2, SECRETS[2]) +
                        wire(5, "合成文件.pdf") + wire(6, "1.25 MB") + wire(10, SECRETS[3]))
    elif kind == 2:
        wrapper += wire(2, '<msg><fromusername>' + SELF + '</fromusername><appmsg>'
                        '<type>5</type><title>合成标题</title><des>合成摘要</des>'
                        '<url>https://example.invalid/' + SECRETS[0] + '</url>'
                        '<aeskey>' + SECRETS[2] + '</aeskey></appmsg></msg>')
    if citations:
        wrapper += wire(14, wire(1, wire(1, SECRETS[0])) + wire(1, wire(2, SECRETS[2])))
    return wire(1, wrapper)


def source_rows():
    samples = [
        (11, 1001, 1700000000000, 1, 2, False),
        (12, 1001, 1700000000300, 0, 30, True),
        (13, 1001, 1700000000100, 0, 30, False),
        (21, 1002, 1700000005000, 1, 18, False),
        (22, 1002, 1700000005100, 0, 30, False),
        (31, 1003, 1700000010000, 1, 30, False),
        (32, 1003, 1700000010200, 0, 30, False),
    ]
    return [dict(svrid=identity, dialogue_id=dialogue, create_time=milliseconds,
                 direction=direction, type=kind, qa_seq=0,
                 card_wraplist_buffer=card(kind, citations=citations),
                 __source_identity=[identity])
            for identity, dialogue, milliseconds, direction, kind, citations in samples]


class AppendChatbotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechat-ai-synthetic-")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "account/db_storage"
        self.db_path = self.root / "chatbot/chatbot_message.db"
        self.db_path.parent.mkdir(parents=True)
        self.state = self.base / "state"
        self.state.mkdir()
        self.archive = self.state / "ordinary.jsonl"
        self.output = self.state / "with-ai.jsonl"
        self.keys = self.base / "keys.json"
        self.keys.write_text("{}")
        self.keys.chmod(0o600)
        self.rows = source_rows()
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA wal_autocheckpoint=0")
        self.connection.execute("CREATE TABLE chatbot_message_v1 (svrid INTEGER PRIMARY KEY, create_time INTEGER, dialogue_id INTEGER, qa_seq INTEGER, direction INTEGER, type INTEGER, card_wraplist_buffer BLOB)")
        self.connection.executemany("INSERT INTO chatbot_message_v1 VALUES (:svrid,:create_time,:dialogue_id,:qa_seq,:direction,:type,:card_wraplist_buffer)", self.rows)
        self.connection.commit()
        self.ordinary = [
            {"record_type": "message", "id": "ordinary:1", "conversation_id": "ordinary-dialogue",
             "sender_name": "合成联系人", "sender_id": "synthetic_peer", "is_self": False,
             "timestamp": 1700000000, "text": "普通消息保持原样", "type": "text", "decode_status": "ok", "server_id": "99999"},
            {"record_type": "conversation", "id": "ordinary-dialogue", "name": "合成普通会话", "is_group": False, "message_count": 1},
        ]
        self.manifest = {
            "record_type": "manifest", "complete": True, "content_complete": False,
            "message_count": 1, "conversation_count": 1, "self_id": SELF,
            "source_tables": [{"database": "message/message_0.db", "table": "Msg_synthetic", "expected_count": 1, "exported_count": 1}],
            "database_count": 3, "message_table_count": 1,
            "message_type_counts": {"text": 1}, "decode_status_counts": {"ok": 1},
            "media_enrichment": {"messages_processed": 1, "media_available": 0},
            "notes": ["媒体统计只覆盖普通消息。"],
        }
        self.write_archive(self.archive, self.ordinary + [self.manifest])

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def write_archive(self, path, records):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    def read_archive(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def run_append(self, archive=None, output=None):
        return chatbot.append_chatbot(self.root, self.keys, archive or self.archive, output or self.output)

    def test_white_list_direction_millisecond_order_and_audit_contract(self):
        messages, conversations, details = chatbot.normalize_rows(self.rows, SELF)
        self.assertEqual(details["message_type_counts"], {"link": 1, "text": 5, "file": 1})
        self.assertEqual(details["decode_status_counts"], {"metadata_only": 2, "ok": 5})
        self.assertEqual(details["unknown_direction_count"], 0)
        self.assertEqual(sum(m["is_self"] is True for m in messages), 3)
        self.assertEqual(sum(m["is_self"] is False for m in messages), 4)
        self.assertEqual(details["citation_entry_count"], 2)
        self.assertEqual(details["unmapped_attachment_count"], 1)
        self.assertTrue(details["record_coverage_complete"])
        self.assertFalse(details["content_complete"])
        self.assertEqual([m["source_create_time_ms"] for m in messages], sorted(r["create_time"] for r in self.rows))
        for message in messages:
            self.assertEqual(message["timestamp"], message["source_create_time_ms"] // 1000)
            encoded = message["id"].removeprefix(chatbot.MESSAGE_PREFIX).split(":", 1)[0]
            self.assertEqual(len(encoded), 20)
            self.assertEqual(int(encoded), message["source_create_time_ms"] + (1 << 63))
        expected_digest = hashlib.sha256(json.dumps(sorted([[m["id"], m["conversation_id"]] for m in messages]), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(details["message_identity_sha256"], expected_digest)
        self.assertEqual(sum(c["expected_count"] for c in details["conversation_counts"]), len(self.rows))
        self.assertTrue(all(c["expected_count"] == c["exported_count"] for c in details["conversation_counts"]))
        rendered = json.dumps([messages, conversations, details], ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, rendered)
        self.assertNotIn("https://example.invalid", rendered)
        file_message = next(m for m in messages if m["type"] == "file")
        self.assertEqual(file_message["file_size_display"], "1.25 MB")
        self.assertNotIn("file_size", file_message, "rounded card size is not an exact byte count")

    def test_unknown_direction_and_malformed_card_are_retained(self):
        messages, _, details = chatbot.normalize_rows(self.rows, "different_synthetic_self")
        self.assertEqual(details["unknown_direction_count"], len(self.rows))
        self.assertTrue(all(m["is_self"] is None for m in messages))
        for raw in (b"\x0a\xff", b"\0", wire(1, wire(1, 30) + wire(16, wire(2, b"\xff"))), card(987)):
            decoded = chatbot.decode_card(raw, 30)
            self.assertEqual(decoded["decode_status"], "chatbot_unmapped_content")
            self.assertEqual(decoded["type"], "card")
        conflicting = [dict(row) for row in self.rows]
        conflicting[0]["direction"] = 0
        self.assertEqual(chatbot.direction_evidence(conflicting, SELF)["status"], "unknown")

    def test_live_wal_readonly_append_preserves_scope_and_repeated_append_is_idempotent(self):
        originals = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        report = self.run_append()
        self.assertEqual(report["message_count"], 8)
        self.assertEqual(report["conversation_count"], 4)
        self.assertEqual(report["additional_source"]["snapshot"]["wal_files"], 1)
        records = self.read_archive(self.output)
        manifest = records[-1]
        self.assertEqual(records[:2], self.ordinary)
        for key in ("source_tables", "database_count", "message_table_count", "media_enrichment"):
            self.assertEqual(manifest[key], self.manifest[key])
        self.assertIn(self.manifest["notes"][0], manifest["notes"])
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        for path, fingerprint in originals.items():
            self.assertEqual((hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns), fingerprint)
        output2 = self.state / "with-ai-again.jsonl"
        second = self.run_append(self.output, output2)
        repeated = self.read_archive(output2)
        self.assertEqual(second["previous_adapter_messages_replaced"], 7)
        self.assertEqual(second["previous_adapter_conversations_replaced"], 3)
        self.assertEqual(second["message_count"], 8)
        self.assertEqual(records[:-1], repeated[:-1])
        self.assertEqual(len(repeated[-1]["additional_sources"]), 1)
        self.assertEqual(repeated[-1]["notes"], manifest["notes"])
        self.assertEqual(self.archive.read_bytes(), "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.ordinary + [self.manifest]).encode())

    def test_refuse_overlap_existing_output_and_account_output_without_partial_artifacts(self):
        overlap = [dict(r) for r in self.ordinary]
        overlap[0]["server_id"] = str(self.rows[0]["svrid"])
        self.write_archive(self.archive, overlap + [self.manifest])
        with self.assertRaises(chatbot.ChatbotError):
            self.run_append()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.state.glob("*.pending")))
        self.output.write_text("preserve existing result")
        with self.assertRaises(chatbot.ChatbotError):
            self.run_append()
        self.assertEqual(self.output.read_text(), "preserve existing result")
        with self.assertRaises(chatbot.ChatbotError):
            chatbot.validate_paths(self.root, self.archive, self.root.parent / "unsafe.jsonl", None)
        with self.assertRaises(chatbot.ChatbotError):
            chatbot.validate_paths(self.root, self.archive, self.archive, None)

    def test_duplicate_id_and_incomplete_archive_fail_atomically(self):
        with self.assertRaises(chatbot.ChatbotError):
            chatbot.normalize_rows(self.rows + [self.rows[0]], SELF)
        bad = dict(self.manifest, message_count=2)
        self.write_archive(self.archive, self.ordinary + [bad])
        with self.assertRaises(chatbot.ChatbotError):
            self.run_append()
        self.assertFalse(self.output.exists())
        collision = [dict(r) for r in self.ordinary]
        collision[0]["id"] = chatbot.MESSAGE_PREFIX + "unowned"
        self.write_archive(self.archive, collision + [self.manifest])
        with self.assertRaises(chatbot.ChatbotError):
            self.run_append()
        self.assertFalse(self.output.exists())

    def test_cli_outputs_only_aggregate_metadata(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exitcode = chatbot.main(["--db-root", str(self.root), "--key-file", str(self.keys), "--archive", str(self.archive), "--output", str(self.output)])
        self.assertEqual(exitcode, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["additional_source"]["exported_count"], 7)
        self.assertNotIn("conversation_counts", report["additional_source"])
        for value in [SELF, "合成正文", "合成文件", "合成标题", chatbot.CONVERSATION_PREFIX, *SECRETS]:
            self.assertNotIn(value, output.getvalue())


if __name__ == "__main__":
    unittest.main()
