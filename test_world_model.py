"""Meaningful synthetic invariants for evidence, uncertainty and local writes."""
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
from world_model import WorldModel, DAY, SourceChangedError


class WorldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.base = 1672531200
        chats = [dict(id=x, name="人工 " + x, is_group=False) for x in ("a", "b", "c", "d")]
        def msg(mid, cid, text, days, sender="同名测试者"):
            return dict(id=mid, conversation_id=cid, sender_name=sender, is_self=False,
                        type="text", text=text, timestamp=self.base + days * DAY)
        self.messages = [
            msg("need-a", "a", "我们需要采购机械臂 20 套，可以报价吗？", 0),
            msg("a-follow", "a", "已经签约并交付完成，谢谢。", 45),  # No product keyword.
            msg("need-b", "b", "我们需要机器人关节，先打样 100 套。", 50),
            msg("b-context", "b", "电话里已经讨论，实际进度这里没记录。", 80),
            msg("need-c", "c", "我们需要机器人关节 10 台，请确认报价。", 60),
            msg("supply-c", "c", "机器人关节可以提供，具体产能还要确认。", 61),
            msg("literal", "c", "<script>fetch('https://invalid')</script> & 原话", 61),
            msg("unrelated", "d", "周末打球", 400),
        ]
        self.source = self.state / "synthetic.json"
        self.write_source(chats, self.messages)
        server.import_archive(self.source, self.state)
        self.world = WorldModel(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def write_source(self, chats, messages):
        self.source.write_text(json.dumps({"manifest": {"synthetic": True, "complete": True}, "conversations": chats, "messages": messages}), encoding="utf-8")

    def candidates(self):
        return {r["conversation_id"]: r for r in self.world.opportunities()["items"]}

    def test_evidence_is_exact_and_context_stays_in_conversation(self):
        result = self.world.evidence("literal", radius=25)
        self.assertEqual(result["message"]["text"], self.messages[6]["text"])
        self.assertTrue(all(r["conversation_id"] == "c" for r in result["context"]))
        self.assertEqual(len(result["fingerprint"]), 64)
        with self.assertRaises(KeyError):
            self.world.evidence("missing")

    def test_closure_without_product_keyword_prevents_abandonment_claim(self):
        item = self.candidates()["a"]
        self.assertIn("a-follow", item["evidence_ids"])
        self.assertTrue(item["possible_progress"])
        self.assertEqual(item["follow_up"]["state"], "later_related_evidence")
        self.assertEqual(item["status"], "needs_review")

    def test_offline_follow_up_and_missing_follow_up_keep_uncertainty(self):
        items = self.candidates()
        self.assertIn("b-context", items["b"]["evidence_ids"])
        self.assertEqual(items["b"]["follow_up"]["state"], "later_related_evidence")
        self.assertEqual(items["c"]["follow_up"]["state"], "no_later_match")
        self.assertIn("待核实", items["c"]["follow_up"]["label"])
        self.assertNotIn("d", items)

    def test_timeline_cursor_has_no_gaps_when_timestamps_tie(self):
        found, cursor = [], ""
        while True:
            page = self.world.timeline(limit=2, cursor=cursor)
            found += [r["id"] for r in page["items"]]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(len(found), len(set(found)))
        self.assertEqual(set(found), {m["id"] for m in self.messages})
        self.assertEqual(self.world.timeline(query="nonexistent'OR(1=1)--")["items"], [])

    def test_graph_does_not_merge_same_named_people_across_chats(self):
        result = self.world.graph()
        people = [n for n in result["nodes"] if n["kind"] == "person"]
        self.assertGreaterEqual(len(people), 3)
        self.assertEqual(len({n["id"] for n in people}), len(people))
        node_ids = {n["id"] for n in result["nodes"]}
        for edge in result["edges"]:
            self.assertIn(edge["source"], node_ids)
            self.assertIn(edge["target"], node_ids)
            self.assertTrue(edge["evidence_ids"])
            for mid in edge["evidence_ids"]:
                self.world.evidence(mid)

    def test_every_candidate_signal_has_resolvable_evidence(self):
        for item in self.world.opportunities()["items"]:
            self.assertIn(item["anchor_id"], item["evidence_ids"])
            self.assertTrue(all(s["message_id"] in item["evidence_ids"] for s in item["signals"]))
            for mid in item["evidence_ids"]:
                self.world.evidence(mid)

    def test_cross_conversation_leads_retain_their_own_evidence_and_uncertainty(self):
        leads = [(item, lead) for item in self.world.opportunities()["items"] for lead in item["related_leads"]]
        self.assertTrue(leads)
        for item, lead in leads:
            message = self.world.evidence(lead["anchor_id"])["message"]
            self.assertNotEqual(message["conversation_id"], item["conversation_id"])
            self.assertIn("未核实", lead["relation"])
            self.assertIn(lead["anchor_id"], lead["evidence_ids"])

    def test_get_requests_never_create_workspace(self):
        self.world.overview()
        self.world.coverage()
        self.world.opportunities()
        self.world.graph()
        self.world.drafts()
        self.assertFalse((self.state / "workspace.sqlite").exists())

    def test_explicit_review_and_idempotent_draft_preserve_source(self):
        source = self.state / "index.sqlite"
        before = hashlib.sha256(source.read_bytes()).digest()
        item = self.candidates()["c"]
        review = self.world.save_review(item["id"], "follow_up", "本人核对后再联系")
        self.assertEqual(review["review"]["note"], "本人核对后再联系")
        one = self.world.create_draft(item["id"])
        two = self.world.create_draft(item["id"])
        self.assertEqual(one, two)
        self.assertEqual(len(self.world.drafts()["items"]), 1)
        self.assertEqual(self.candidates()["c"]["status"], "follow_up")
        self.assertIn("未发送", one["body"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)
        self.assertEqual((self.state / "workspace.sqlite").stat().st_mode & 0o777, 0o600)

    def test_invalid_review_does_not_create_workspace(self):
        item = self.candidates()["c"]
        with self.assertRaises(ValueError):
            self.world.save_review(item["id"], "send_now")
        with self.assertRaises(ValueError):
            self.world.create_draft("../../something")
        self.assertFalse((self.state / "workspace.sqlite").exists())

    def test_changed_archive_invalidates_cache_and_marks_review_stale(self):
        item = self.candidates()["c"]
        self.world.save_review(item["id"], "follow_up", "old source")
        updated = [dict(m) for m in self.messages]
        updated[4]["text"] = "我们需要机器人关节 50 台，请确认报价与预算。"
        self.write_source([dict(id=x, name="人工 " + x, is_group=False) for x in ("a", "b", "c", "d")], updated)
        server.import_archive(self.source, self.state)
        new = self.candidates()["c"]
        self.assertEqual(new["id"], item["id"])
        self.assertTrue(new["review_stale"])
        self.assertIn("50", new["excerpt"])
        with self.assertRaises(SourceChangedError):
            self.world.save_review(item["id"], "completed", "outdated view", item["source_fingerprint"])
        with self.assertRaises(SourceChangedError):
            self.world.create_draft(item["id"], item["source_fingerprint"])
        self.assertEqual(self.candidates()["c"]["status"], "follow_up")

    def test_new_counterevidence_invalidates_review_and_previous_draft(self):
        item = self.candidates()["c"]
        self.world.save_review(item["id"], "follow_up", "waiting")
        draft = self.world.create_draft(item["id"])
        updated = list(self.messages) + [dict(id="new-closure", conversation_id="c", sender_name="同名测试者", is_self=False,
                                            type="text", text="已经交付并完成验收。", timestamp=self.base + 180 * DAY)]
        self.write_source([dict(id=x, name="人工 " + x, is_group=False) for x in ("a", "b", "c", "d")], updated)
        server.import_archive(self.source, self.state)
        new = self.candidates()["c"]
        self.assertTrue(new["review_stale"])
        self.assertIn("new-closure", new["evidence_ids"])
        self.assertTrue(self.world.drafts()["items"][0]["source_stale"])
        replacement = self.world.create_draft(item["id"])
        self.assertNotEqual(replacement["id"], draft["id"])
        self.assertIn("new-closure", replacement["evidence_ids"])


class WorldHTTPTests(unittest.TestCase):
    write_source = WorldTests.write_source
    candidates = WorldTests.candidates

    def setUp(self):
        WorldTests.setUp(self)
        self.httpd = server.ViewerServer(0, self.state, "synthetic-session")
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        WorldTests.tearDown(self)

    def request(self, method, path, body=None, auth=True, origin=True, headers=None):
        h = {"Content-Type": "application/json"}
        if auth:
            h["Cookie"] = self.httpd.cookie_name + "=synthetic-session"
        if origin:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        h.update(headers or {})
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        client.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        response = client.getresponse()
        status, raw, returned_headers = response.status, response.read(), dict(response.getheaders())
        client.close()
        return status, json.loads(raw) if raw else None, returned_headers

    def test_http_review_requires_cookie_exact_origin_and_json(self):
        item = self.candidates()["c"]
        body = {"id": item["id"], "status": "follow_up", "note": "test", "source_fingerprint": item["source_fingerprint"]}
        self.assertEqual(self.request("POST", "/api/world/review", body, auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/world/review", body, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/world/review", body, headers={"Origin": "https://invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/world/review", body, headers={"Sec-Fetch-Site": "same-site"})[0], 403)
        self.assertEqual(self.request("POST", "/api/world/review", body, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertFalse((self.state / "workspace.sqlite").exists())
        self.assertEqual(self.request("POST", "/api/world/review", {**body, "source_fingerprint": "0" * 64})[0], 409)
        self.assertEqual(self.request("POST", "/api/world/draft", {"id": item["id"]})[0], 400)
        self.assertEqual(self.request("POST", "/api/world/review", body)[0], 200)
        self.assertEqual(self.request("POST", "/api/world/draft", {"id": item["id"], "source_fingerprint": item["source_fingerprint"]})[0], 200)
        self.assertEqual(self.request("POST", "/api/send", body)[0], 405)

    def test_http_world_reads_require_session_and_health_omits_secrets(self):
        for name in ("overview", "timeline", "graph", "opportunities", "coverage", "drafts"):
            self.assertEqual(self.request("GET", "/api/world/" + name, auth=False)[0], 401)
            self.assertEqual(self.request("GET", "/api/world/" + name)[0], 200)
        code, health, _ = self.request("GET", "/healthz", auth=False)
        self.assertEqual(code, 200)
        self.assertEqual(health["app"], "aiman-personal-world")
        self.assertEqual(len(health["state_id"]), 24)
        self.assertNotIn("token", health)
        self.assertNotIn(str(self.state), json.dumps(health))
        self.assertEqual(self.request("GET", "/api/world/evidence?id=unknown")[0], 404)


if __name__ == "__main__":
    unittest.main()
