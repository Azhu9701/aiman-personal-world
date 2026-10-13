"""Synthetic-only tests: no WeChat installation or private records required."""
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import urlencode

import server


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        cls.source = cls.state / "fixture.jsonl"
        conversations = [
            {"record_type": "conversation", "id": "alpha", "name": "测试会话甲", "is_group": False},
            {"record_type": "conversation", "id": "beta@chatroom", "name": "人工测试群", "is_group": True},
        ]
        cls.messages = [{"record_type": "message", "id": f"msg-{i:04}", "conversation_id": "alpha",
                         "text": f"人工测试 机器人采购 {i}" if i % 2 == 0 else f"消息 <script>alert(1)</script> {i}",
                         "timestamp": 1791734400 + i // 3, "sender_name": "测试者", "is_self": bool(i % 2), "type": "text"}
                        for i in range(253)]
        cls.messages += [{"record_type": "message", "id": "group-1", "conversation_id": "beta@chatroom",
                          "text": "人工测试群消息", "timestamp": 1791734400, "is_self": None, "type": "text"}]
        records = conversations + cls.messages + [{"record_type": "manifest", "complete": True, "message_count": len(cls.messages), "source": "人工测试数据"}]
        cls.source.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in records), encoding="utf-8")
        cls.result = server.import_archive(cls.source, cls.state)
        cls.token = "synthetic-test-token-only"
        cls.httpd = server.ViewerServer(0, cls.state, cls.token)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def request(self, path, headers=None, auth=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Cookie": f"{self.httpd.cookie_name}={self.token}"} if auth else {}
        h.update(headers or {})
        connection.request("GET", path, headers=h)
        response = connection.getresponse()
        data, status, response_headers = response.read(), response.status, dict(response.getheaders())
        connection.close()
        return status, data, response_headers

    def api(self, path, **params):
        status, body, _ = self.request(path + "?" + urlencode(params))
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_import_and_status_counts(self):
        info = self.api("/api/status")
        self.assertEqual(info["message_count"], 254)
        self.assertEqual(info["conversation_count"], 2)
        self.assertTrue(info["ready"])

    def test_cursor_pages_no_loss_or_duplicates_at_identical_timestamps(self):
        ids, before = [], ""
        while True:
            page = self.api("/api/messages", conversation_id="alpha", limit=17, before=before)
            ids += [x["id"] for x in page["items"]]
            if not page["has_more"]:
                break
            before = page["next_before"]
            self.assertTrue(before)
        self.assertEqual(len(ids), 253)
        self.assertEqual(len(set(ids)), 253)
        self.assertEqual(set(ids), {x["id"] for x in self.messages if x["conversation_id"] == "alpha"})

    def test_search_anchor_in_context(self):
        page = self.api("/api/messages", conversation_id="alpha", limit=50, around="msg-0100")
        self.assertIn("msg-0100", [x["id"] for x in page["items"]])
        self.assertEqual(len(page["items"]), 50)
        self.assertFalse(page["latest"])
        self.assertTrue(page["has_more"])

    def test_chinese_search_and_scoping(self):
        result = self.api("/api/search", q="机器人", limit=200)
        self.assertEqual(result["total"], 127)
        self.assertEqual(len(result["items"]), 127)
        self.assertEqual(self.api("/api/search", q="采购")["total"], 127)
        self.assertEqual(self.api("/api/search", q="机器人", conversation_id="beta@chatroom")["total"], 0)
        self.assertEqual(self.api("/api/search", q="' OR 1=1 --")["total"], 0)

    def test_session_filter_and_literal_content(self):
        self.assertEqual(self.api("/api/conversations", kind="group")["total"], 1)
        self.assertEqual(self.api("/api/conversations", q="甲")["items"][0]["id"], "alpha")
        item = self.api("/api/messages", conversation_id="alpha", around="msg-0001", limit=4)["items"]
        self.assertTrue(any("<script>" in x["text"] for x in item))

    def test_unknown_self_is_preserved(self):
        items = self.api("/api/messages", conversation_id="beta@chatroom")["items"]
        self.assertIsNone(items[0]["is_self"])

    def test_cookies_and_cross_origin_are_required(self):
        self.assertEqual(self.request("/api/status", auth=False)[0], 401)
        self.assertEqual(self.request("/api/status", {"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.request("/api/status", {"Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.request("/api/status", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        status, _, headers = self.request("/?access=" + self.token, auth=False)
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])

    def test_media_paths_cannot_escape_cache(self):
        self.assertEqual(self.request("/api/media/%2e%2e%2findex.sqlite")[0], 400)
        media = self.state / "media"
        media.mkdir(exist_ok=True)
        (media / "outside.txt").symlink_to(self.source)
        self.assertEqual(self.request("/api/media/outside.txt")[0], 404)
        (media / "fixture.mp4").write_bytes(b"0123456789")
        status, data, headers = self.request("/api/media/fixture.mp4", {"Range": "bytes=2-5"})
        self.assertEqual((status, data), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")

    def test_invalid_import_preserves_previous_index(self):
        path = self.state / "index.sqlite"
        digest = hashlib.sha256(path.read_bytes()).digest()
        bad = self.state / "bad.jsonl"
        bad.write_text(json.dumps({"record_type": "manifest", "complete": False}) + "\n")
        with self.assertRaises(ValueError):
            server.import_archive(bad, self.state)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)

    def test_exports_are_complete_and_reimportable(self):
        status, body, _ = self.request("/api/export?conversation_id=alpha&format=json")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data["messages"]), 253)
        self.assertEqual(data["conversation"]["id"], "alpha")
        status, text, _ = self.request("/api/export?conversation_id=alpha&format=txt")
        self.assertEqual(status, 200)
        self.assertIn("北京时间", text.decode())

    def test_source_index_connections_are_read_only(self):
        with server.read_index(self.state) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM messages")

    def test_import_does_not_invert_string_false_or_shift_naive_times(self):
        self.assertIs(server.normalized_boolean("false"), False)
        self.assertIs(server.normalized_boolean("0"), False)
        self.assertIsNone(server.normalized_boolean(None, allow_unknown=True))
        with self.assertRaises(ValueError):
            server.normalized_boolean("maybe")
        self.assertEqual(server.numeric_time("2026-10-13T00:00:00"), server.numeric_time("2026-10-13T00:00:00+08:00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
