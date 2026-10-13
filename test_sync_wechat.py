"""Synthetic-only tests. They never discover or open real WeChat data."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from xml.sax.saxutils import escape

import sync_wechat as exporter


def table_name(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode()).hexdigest()


def messages_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
    connection.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)", [(7, "self_user"), (19, "peer"), (30, "gh_demo")])
    connection.commit()
    return connection


def message_table(connection: sqlite3.Connection, username: str, records: list[tuple], *, exact_name: bool = False) -> None:
    name = username if exact_name else table_name(username)
    connection.execute(f'CREATE TABLE "{name}" (local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content, WCDB_CT_message_content INTEGER)')
    connection.executemany(f'INSERT INTO "{name}" VALUES (?,?,?,?,?,?,?)', records)
    connection.commit()


class XmlContentTests(unittest.TestCase):
    @staticmethod
    def card(subtype: int, body: str, title: str = "") -> str:
        return f"<msg><appmsg><type>{subtype}</type><title>{title}</title>{body}</appmsg></msg>"

    @staticmethod
    def recordinfo(items: str) -> str:
        return "<recordinfo><datalist>" + items + "</datalist></recordinfo>"

    def test_invalid_references_preserve_legal_characters_and_report_repairs(self) -> None:
        content = self.card(57, "<refermsg><type>1</type><content>quoted&#0;ok&#9;&#10;&#13;&#x1F600;</content></refermsg>", "reply&#x1;")
        result = exporter.message_content(49 | (57 << 32), content, "ok")
        self.assertEqual(result["decode_status"], "xml_repaired")
        self.assertEqual(result["xml_repair_counts"], {"invalid_numeric_reference": 2})
        self.assertIn("reply\ufffd", result["text"])
        self.assertIn("quoted\ufffdok\t\n\r😀", result["text"])

    def test_repair_does_not_reinterpret_cdata_comments_or_processing_instructions(self) -> None:
        xml = '<msg label="A&#1;B"><![CDATA[literal &#1; and &#x2; \x03]]><!-- &#4; --><?keep value="&#5;"?></msg>'
        fixed, counts = exporter.repair_xml(xml)
        self.assertEqual(counts, {"invalid_literal_character": 1, "invalid_numeric_reference": 1})
        self.assertIn('label="A\ufffdB"', fixed)
        self.assertIn("<![CDATA[literal &#1; and &#x2; \ufffd]]>", fixed)
        self.assertIn("<!-- &#4; -->", fixed)
        self.assertIn('<?keep value="&#5;"?>', fixed)
        self.assertIsNotNone(exporter.xml_root(fixed))
        oversized = self.card(5, "<des>&#" + "9" * 8000 + ";</des>")
        result = exporter.message_content(49, oversized, "ok")
        self.assertEqual(result["xml_repair_counts"], {"invalid_numeric_reference": 1})
        self.assertEqual(result["text"], "\ufffd")

    def test_only_observed_declaration_position_is_repaired(self) -> None:
        body = self.card(5, "<des>description</des>", "title")
        damaged = body.replace("<msg>", '<msg><?xml version="1.0" encoding="UTF-8"?>', 1)
        result = exporter.message_content(49, damaged, "ok")
        self.assertEqual(result["text"], "title\ndescription")
        self.assertEqual(result["decode_status"], "metadata_only")
        self.assertTrue(result["xml_repaired"])
        self.assertEqual(result["xml_repair_counts"], {"misplaced_xml_declaration": 1})
        other_position = body.replace("<appmsg>", '<appmsg><?xml version="1.0"?>', 1)
        result = exporter.message_content(49, other_position, "ok")
        self.assertEqual(result["decode_status"], "xml_parse_failed")
        self.assertNotIn("xml_repaired", result)
        literal = '<msg><?xml version="1.0"?> typed by sender'
        text_quote = self.card(57, "<refermsg><type>1</type><content>" + escape(literal) + "</content></refermsg>")
        result = exporter.message_content(49, text_quote, "ok")
        self.assertIn(literal, result["text"])
        self.assertNotIn("xml_repaired", result)

    def test_malformed_or_external_entity_xml_remains_unparsed(self) -> None:
        cases = [
            "<msg><appmsg><type>19</type>",
            self.card(5, "<des>&#xGG;</des>"),
            '<!DOCTYPE msg [<!ENTITY x SYSTEM "file:///SYNTHETIC_PRIVATE">]><msg><appmsg><type>5</type><title>&x;</title></appmsg></msg>',
            self.card(5, "<des>unclosed&#1;"),
        ]
        for content in cases:
            with self.subTest(content_length=len(content)):
                result = exporter.message_content(49, content, "ok")
                self.assertEqual(result["decode_status"], "xml_parse_failed")
                self.assertNotIn("SYNTHETIC_PRIVATE", result["text"])
                self.assertNotIn("xml_repaired", result)

    def test_forwarded_text_order_nested_records_and_unavailable_items(self) -> None:
        inner = self.recordinfo('<dataitem datatype="1"><sourcename>Nested sender</sourcename><datadesc>nested body</datadesc></dataitem>')
        items = (
            '<dataitem datatype="1"><sourcename>Sender A</sourcename><sourcetime>10:00</sourcetime><datadesc>first body</datadesc><sourceheadurl>PRIVATE_URL</sourceheadurl></dataitem>'
            '<dataitem datatype="17"><recordxml>' + escape(inner) + '</recordxml></dataitem>'
            '<dataitem datatype="1"><datadesc>last body</datadesc></dataitem>'
            '<dataitem datatype="99"><datadesc>PRIVATE_UNKNOWN_BODY</datadesc><appattach><content>PRIVATE_METADATA</content><aeskey>PRIVATE_KEY</aeskey></appattach></dataitem>'
        )
        content = self.card(19, "<recorditem>" + escape(self.recordinfo(items)) + "</recorditem>", "forwarded title")
        result = exporter.message_content(49 | (19 << 32), content, "ok")
        self.assertEqual(result["type"], "forwarded")
        self.assertEqual(result["decode_status"], "metadata_only")
        self.assertIn("Sender A · 10:00\nfirst body", result["text"])
        self.assertLess(result["text"].index("first body"), result["text"].index("nested body"))
        self.assertLess(result["text"].index("nested body"), result["text"].index("last body"))
        self.assertIn("[未支持的转发条目类型 99]", result["text"])
        self.assertNotIn("PRIVATE_", result["text"])
        with mock.patch.object(exporter, "MAX_FORWARDED_ITEMS", 2):
            result = exporter.message_content(49, content, "ok")
        self.assertIn("另有 2 条", result["text"])
        self.assertEqual(result["decode_status"], "metadata_only")

    def test_text_forward_can_be_complete_without_claiming_media_or_unknown_quotes(self) -> None:
        items = '<dataitem datatype="1"><datadesc>hello</datadesc></dataitem>'
        content = self.card(19, "<recorditem>" + escape(self.recordinfo(items)) + "</recorditem>")
        self.assertEqual(exporter.message_content(49, content, "ok")["decode_status"], "ok")
        for raw_type, quoted in [
            (3, '<msg><img aeskey="PRIVATE_KEY"/></msg>'),
            (49, '<msg><appattach><content>PRIVATE_METADATA</content></appattach></msg>'),
        ]:
            content = self.card(57, f"<refermsg><type>{raw_type}</type><content>{escape(quoted)}</content></refermsg>", "reply")
            result = exporter.message_content(49, content, "ok")
            self.assertEqual(result["decode_status"], "metadata_only")
            self.assertNotIn("PRIVATE_", result["text"])
        result = exporter.message_content(66, '\ufeff<msg ticket="PRIVATE_TICKET"/>', "ok")
        self.assertEqual(result["decode_status"], "unsupported_type")
        self.assertNotIn("PRIVATE_TICKET", result["text"])


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="wechat-synthetic-test-")
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "account" / "db_storage"
        self.root.mkdir(parents=True)
        self.output = self.base / "export.jsonl"
        self.keys = self.base / "keys.json"
        self.keys.write_text("{}")
        self.keys.chmod(0o600)
        self.connections: list[sqlite3.Connection] = []

    def tearDown(self) -> None:
        for connection in self.connections:
            connection.close()
        self.temporary.cleanup()

    def shard(self, name: str = "message_0.db") -> sqlite3.Connection:
        connection = messages_db(self.root / "message" / name)
        self.connections.append(connection)
        return connection

    def metadata(self) -> None:
        contact = self.root / "contact" / "contact.db"
        contact.parent.mkdir()
        with sqlite3.connect(contact) as connection:
            connection.execute("CREATE TABLE contact(username TEXT PRIMARY KEY, nick_name TEXT, remark TEXT)")
            connection.executemany("INSERT INTO contact VALUES (?,?,?)", [("self_user", "自己", ""), ("peer", "示例联系人", "备注名"), ("group@chatroom", "示例群", ""), ("gh_demo", "示例公众号", ""), ("empty_peer", "空会话", "")])
        session = self.root / "session" / "session.db"
        session.parent.mkdir()
        with sqlite3.connect(session) as connection:
            connection.execute("CREATE TABLE SessionTable(username TEXT PRIMARY KEY, last_timestamp INTEGER)")
            connection.executemany("INSERT INTO SessionTable VALUES (?,?)", [("peer", 1700000000), ("empty_peer", 1700000000)])

    def read(self) -> list[dict]:
        return [json.loads(line) for line in self.output.read_text().splitlines()]

    def test_all_shards_wal_identity_compression_and_xml_sanitization(self) -> None:
        self.metadata()
        main = self.shard()
        attachment = '<msg><appmsg><type>6</type><title>design.txt</title><appattach><aeskey>AES_ONLY_SECRET</aeskey><cdnattachurl>https://invalid.test/?token=TOKEN_ONLY_SECRET</cdnattachurl></appattach></appmsg></msg>'
        quotation = '<msg><appmsg><type>57</type><title>这是回复</title><refermsg><displayname>示例联系人</displayname><content>&lt;msg&gt;&lt;img aeskey="QUOTED_ONLY_SECRET"/&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>'
        message_table(main, "peer", [(1, 9007199254740993, 1, 7, 1700000001, "hello", 0), (2, 999, 49 | (6 << 32), 19, 1700000002, attachment, 0)])
        group_text: object = b"peer:\n\xe7\xbe\xa4\xe8\x81\x8a\xe5\x8e\x9f\xe6\x96\x87"
        flag = 0
        if importlib.util.find_spec("zstandard"):
            import zstandard
            group_text, flag = zstandard.ZstdCompressor().compress(group_text), 4
        message_table(main, "group@chatroom", [(1, 998, 1, 19, 1700000003, group_text, flag), (2, 997, 49 | (57 << 32), 7, 1700000004, quotation, 0)])
        unknown = "Msg_" + "0" * 32
        message_table(main, unknown, [(42, 996, 1, 19, 1700000005, "opaque conversation text", 0)], exact_name=True)
        business = self.shard("biz_message_0.db")
        message_table(business, "peer", [(1, 995, 1, 19, 1700000006, "another shard with the same local_id", 0)])
        message_table(business, "gh_demo", [(1, 994, 49 | (5 << 32), 30, 1700000007, "<msg><appmsg><type>5</type><title>新闻标题</title><des>新闻摘要</des><url>https://invalid.test/?token=URL_ONLY_SECRET</url></appmsg></msg>", 0)])
        source_files = sorted(self.root.rglob("*.db")) + sorted(self.root.rglob("*.db-wal"))
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
        summary = exporter.sync(self.root, self.keys, self.output, "self_user")
        after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
        self.assertEqual(before, after)
        self.assertEqual(summary["message_count"], 7)
        self.assertEqual(summary["conversation_count"], 5)
        records = self.read()
        messages = [record for record in records if record["record_type"] == "message"]
        conversations = {record["id"]: record for record in records if record["record_type"] == "conversation"}
        manifest = records[-1]
        self.assertEqual(manifest["record_type"], "manifest")
        self.assertTrue(manifest["complete"])
        self.assertFalse(manifest["content_complete"])
        self.assertGreater(manifest["snapshot"]["wal_files"], 0)
        self.assertEqual(len({message["id"] for message in messages}), 7)
        self.assertEqual(conversations["peer"]["name"], "备注名")
        self.assertEqual(conversations["peer"]["message_count"], 3)
        self.assertEqual(conversations["peer"]["last_message"], "another shard with the same local_id")
        self.assertEqual(conversations["empty_peer"]["message_count"], 0)
        self.assertEqual(manifest["unmapped_table_count"], 1)
        self.assertEqual(sum(row["expected_count"] for row in manifest["source_tables"]), 7)
        self.assertTrue(all(row["expected_count"] == row["exported_count"] for row in manifest["source_tables"]))
        group = next(message for message in messages if message["text"] == "群聊原文")
        self.assertFalse(group["is_self"])
        self.assertEqual(group["sender_name"], "备注名")
        hello = next(message for message in messages if message["text"] == "hello")
        self.assertTrue(hello["is_self"])
        payload = self.output.read_text()
        for secret in ("AES_ONLY_SECRET", "TOKEN_ONLY_SECRET", "QUOTED_ONLY_SECRET", "URL_ONLY_SECRET", "cdnattachurl", "aeskey"):
            self.assertNotIn(secret, payload)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_missing_key_file_preserves_previous_archive(self) -> None:
        self.output.write_text("previous-successful-export")
        with self.assertRaises(exporter.SyncError) as error:
            exporter.sync(self.root, self.base / "missing-key-file.json", self.output)
        self.assertEqual(error.exception.code, "key_file_missing")
        self.assertEqual(self.output.read_text(), "previous-successful-export")
        self.assertFalse(list(self.base.glob("*.pending")))

    def test_missing_per_database_key_preserves_previous_archive(self) -> None:
        path = self.root / "message" / "message_0.db"
        path.parent.mkdir()
        path.write_bytes(b"synthetic-encrypted-database-placeholder" * 200)
        self.output.write_text("previous-successful-export")
        with self.assertRaises(exporter.SyncError) as error:
            exporter.sync(self.root, self.keys, self.output)
        self.assertEqual(error.exception.code, "database_key_missing")
        self.assertEqual(self.output.read_text(), "previous-successful-export")

    def test_unreadable_compressed_row_is_counted_but_never_claimed_decoded(self) -> None:
        main = self.shard()
        message_table(main, "peer", [(1, 1, 1, 19, 1700000000, b"not-a-zstd-stream-PRIVATE_PAYLOAD", 4)])
        summary = exporter.sync(self.root, self.keys, self.output)
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["content_complete"])
        records = self.read()
        message = next(record for record in records if record["record_type"] == "message")
        self.assertEqual(message["decode_status"], "decompression_failed")
        self.assertIsNone(message["is_self"])
        self.assertNotIn("PRIVATE_PAYLOAD", self.output.read_text())
        self.assertEqual(records[-1]["message_count"], 1)

    def test_changing_wal_is_rejected_without_replacing_archive(self) -> None:
        main = self.shard()
        name = table_name("peer")
        message_table(main, "peer", [(1, 1, 1, 19, 1700000000, "initial", 0)])
        self.output.write_text("previous-successful-export")
        original = exporter.fingerprints
        calls = 0

        def race(root: Path, sources: list[Path]) -> dict:
            nonlocal calls
            result = original(root, sources)
            calls += 1
            if calls == 1:
                main.execute(f'INSERT INTO "{name}" VALUES (2,2,1,19,1700000001,"new during snapshot",0)')
                main.commit()
            return result

        with mock.patch.object(exporter, "fingerprints", side_effect=race):
            with self.assertRaises(exporter.SyncError) as error:
                exporter.sync(self.root, self.keys, self.output, attempts=1)
        self.assertEqual(error.exception.code, "snapshot_source_changed")
        self.assertEqual(self.output.read_text(), "previous-successful-export")

    def test_same_second_numeric_order_is_preserved_in_ids(self) -> None:
        main = self.shard()
        message_table(main, "peer", [(number, number, 1, 19, 1700000000, f"row {number}", 0) for number in (2, 10, 100)])
        exporter.sync(self.root, self.keys, self.output)
        messages = sorted((record for record in self.read() if record["record_type"] == "message"), key=lambda record: record["id"])
        self.assertEqual([record["source_local_id"] for record in messages], ["2", "10", "100"])

    def test_xml_repair_metadata_survives_export_and_manifest_counts(self) -> None:
        main = self.shard()
        content = XmlContentTests.card(5, "<des>description&#1;</des>", "title")
        message_table(main, "peer", [(1, 1, 49 | (5 << 32), 19, 1700000000, content, 0)])
        exporter.sync(self.root, self.keys, self.output, "self_user")
        records = self.read()
        message = next(record for record in records if record["record_type"] == "message")
        manifest = records[-1]
        self.assertEqual(message["text"], "title\ndescription\ufffd")
        self.assertTrue(message["xml_repaired"])
        self.assertEqual(message["decode_status"], "metadata_only")
        self.assertEqual(manifest["xml_repaired_message_count"], 1)
        self.assertEqual(manifest["xml_repair_counts"], {"invalid_numeric_reference": 1})
        self.assertFalse(manifest["content_complete"])
        self.assertEqual(manifest["message_count"], 1)

    @unittest.skipUnless(importlib.util.find_spec("sqlcipher3") or importlib.util.find_spec("pysqlcipher3"), "SQLCipher native runtime not installed in this test environment")
    def test_encrypted_synthetic_database_and_wrong_key(self) -> None:
        try:
            from sqlcipher3 import dbapi2 as cipher
        except ImportError:
            from pysqlcipher3 import dbapi2 as cipher
        path = self.root / "message" / "message_0.db"
        path.parent.mkdir()
        connection = cipher.connect(path)
        self.connections.append(connection)
        # Deliberately public synthetic key; never used with a real account.
        test_key = "ab" * 32
        connection.execute("PRAGMA key=\"x'" + test_key + "'\"")
        connection.execute("PRAGMA cipher_compatibility=4")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
        connection.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (19,'peer')")
        message_table(connection, "peer", [(1, 1, 1, 19, 1700000000, "encrypted synthetic row", 0)])
        self.keys.write_text(json.dumps({"keys": {"message/message_0.db": test_key}}))
        summary = exporter.sync(self.root, self.keys, self.output)
        self.assertEqual(summary["message_count"], 1)
        self.assertEqual(self.read()[-1]["encrypted_database_count"], 1)
        previous = self.output.read_bytes()
        self.keys.write_text(json.dumps({"keys": {"message/message_0.db": "cd" * 32}}))
        with self.assertRaises(exporter.SyncError):
            exporter.sync(self.root, self.keys, self.output)
        self.assertEqual(self.output.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main(verbosity=2)
