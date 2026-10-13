"""Release-boundary tests use manufactured values, never real private input."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("personal_world_release_check", APP / "scripts" / "release_check.py")
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aiman-release-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def scan(self):
        return CHECKER.scan(self.root, require_project=False)

    def codes(self):
        return {item["code"] for item in self.scan()["issues"]}

    def test_manual_demo_has_valid_identity_and_count_contract(self):
        document = json.loads((APP / "examples" / "demo.json").read_text(encoding="utf-8"))
        self.write("examples/demo.json", json.dumps(document, ensure_ascii=False))
        result = self.scan()
        self.assertTrue(result["ok"], result)
        self.assertTrue(document["manifest"]["synthetic"])
        self.assertGreaterEqual(len(document["conversations"]), 5)
        self.assertEqual(len(document["messages"]), len({m["id"] for m in document["messages"]}))

    def test_synthetic_label_cannot_hide_config_or_unapproved_fields(self):
        forged = {"synthetic": True, "keys": {"default": hashlib.sha256(b"manufactured release fixture").hexdigest()}}
        self.write("examples/demo.json", json.dumps(forged))
        self.assertIn("synthetic_example_envelope_invalid", self.codes())
        document = json.loads((APP / "examples" / "demo.json").read_text(encoding="utf-8"))
        document = copy.deepcopy(document)
        document["messages"][0]["raw_payload"] = "manufactured payload"
        self.write("examples/demo.json", json.dumps(document))
        self.assertIn("synthetic_example_message_invalid", self.codes())

    def test_runtime_artifacts_are_blocked_even_with_safe_contents(self):
        for name in ("keys.json", "source-config.json", "archive.jsonl", "index.sqlite", "state/README.md"):
            self.write(name, "synthetic fixture only")
        result = self.scan()
        self.assertFalse(result["ok"])
        self.assertEqual(5, sum(i["code"] == "forbidden_runtime_artifact" for i in result["issues"]))

    def test_binary_cannot_be_disguised_with_a_document_extension(self):
        self.write("harmless.md", b"SQLite format 3\0" + bytes(40))
        self.assertIn("binary_disguised_as_source", self.codes())

    def test_credentials_are_contextual_and_reports_do_not_reveal_values(self):
        self.write("safe.py", "token = 'SYNTHETIC_TOKEN_FOR_RELEASE_TEST'\n")
        self.assertTrue(self.scan()["ok"])
        manufactured = hashlib.sha256(b"independently manufactured secret-like fixture").hexdigest()
        self.write("unsafe.py", "configuration = " + repr({"token": manufactured}) + "\n")
        result = self.scan()
        self.assertFalse(result["ok"])
        self.assertIn("secret_value_in_mapping", {i["code"] for i in result["issues"]})
        self.assertNotIn(manufactured, json.dumps(result))
        self.assertNotIn("SYNTHETIC_TOKEN_FOR_RELEASE_TEST", json.dumps(result))

    def test_split_and_escaped_private_paths_are_checked_as_literals(self):
        manufactured_path = "/".join(("", "Users", "invented-private-account", "records"))
        source = "destination = " + repr(manufactured_path[:5]) + " + " + repr(manufactured_path[5:]) + "\n"
        self.write("paths.py", source)
        self.assertIn("private_hardcoded_user_path", self.codes())
        document = json.loads((APP / "examples" / "demo.json").read_text(encoding="utf-8"))
        document["messages"][0]["text"] = manufactured_path
        escaped = json.dumps(document).replace("/", "\\u002f")
        self.write("examples/demo.json", escaped)
        flagged = [i for i in self.scan()["issues"] if i["path"] == "examples/demo.json"]
        self.assertIn("private_hardcoded_user_path", {i["code"] for i in flagged})

    def test_symlink_does_not_import_an_external_source(self):
        target = self.write("external-source.txt", "manufactured text")
        try:
            (self.root / "linked.md").symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this platform")
        self.assertIn("symlink_or_unsafe_release_path", self.codes())

    @unittest.skipUnless(shutil.which("git"), "Git unavailable for tracked-set test")
    def test_ignored_but_force_tracked_key_still_blocks_release(self):
        def git(*args):
            subprocess.run(["git", "-C", str(self.root), *args], stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, check=True, timeout=10)
        git("init", "-q")
        self.write(".gitignore", "keys.json\n")
        self.write("keys.json", json.dumps({"synthetic": True}))
        git("add", ".gitignore")
        self.assertTrue(self.scan()["ok"])
        git("add", "-f", "keys.json")
        result = self.scan()
        self.assertEqual("git_publishable_set", result["scan_mode"])
        self.assertIn("forbidden_runtime_artifact", {i["code"] for i in result["issues"]})


if __name__ == "__main__":
    unittest.main()
