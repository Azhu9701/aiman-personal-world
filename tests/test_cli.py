"""Synthetic CLI, isolation and publication tests. Never reads an account."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_config as config
import launch
import personal_world as cli
import refresh
import server


def fixture(native=False):
    message = {"id": "sample:message:1", "conversation_id": "sample:chat:1", "sender_name": "人工测试者",
               "sender_id": "sample:peer", "is_self": False, "type": "text", "text": "人工测试机器人需求",
               "timestamp": 1700000000, "raw_type": 1, "decode_status": "ok"}
    manifest = {"complete": True, "message_count": 1, "conversation_count": 1, "synthetic": True,
                "label": "全人工测试", "content_complete": True}
    if native:
        message.update(id="message/message_0.db:Msg_sample:1", source_local_id=1, server_id="100")
        manifest.update(source_tables=[{"database": "message/message_0.db", "table": "Msg_sample",
                                        "conversation_id": "sample:chat:1", "identity_status": "mapped",
                                        "expected_count": 1, "exported_count": 1}],
                        database_count=1, message_table_count=1, unmapped_table_count=0, unknown_sender_count=0,
                        decode_status_counts={"ok": 1}, message_type_counts={"text": 1}, failed_databases=[], failed_tables=[])
    return {"manifest": manifest, "conversations": [{"id": "sample:chat:1", "name": "人工测试会话", "is_group": False}],
            "messages": [message]}


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="personal-world-synthetic-")
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "state"
        self.source = self.base / "input.json"
        self.write_source(fixture())

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, document):
        self.source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def invoke(self, args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(args)
        return code, json.loads(stdout.getvalue().splitlines()[-1]), stdout.getvalue()

    def imported(self):
        return cli.import_state(self.source, self.state, no_open=True)

    def pair(self):
        return {name: (self.state / name).read_bytes() for name in ("archive.jsonl", "index.sqlite")}

    def test_demo_is_separate_repeatable_and_keeps_synthetic_label(self):
        default = self.base / "real-default"
        demo = self.base / "isolated-demo"
        with mock.patch.object(cli, "default_state", return_value=default), mock.patch.object(cli, "default_demo_state", return_value=demo):
            code, result, _ = self.invoke(["demo", "--no-open"])
            self.assertEqual(code, 0)
            self.assertEqual((result["message_count"], result["conversation_count"]), (40, 8))
            self.assertFalse(default.exists())
            self.assertEqual(self.invoke(["demo", "--no-open"])[0], 0)
        manifest = cli.archive_manifest(demo / "archive.jsonl")
        self.assertTrue(manifest["synthetic"])
        self.assertIn("人工", manifest["label"])
        self.assertTrue(cli.verify_state(demo)["ok"])
        self.assertFalse((demo / config.ACCESS_FILE).exists())
        if os.name == "posix":
            self.assertEqual((demo / config.DEMO_FILE).stat().st_mode & 0o777, 0o600)

    def test_demo_rejects_existing_real_or_configured_state_without_replacement(self):
        document = fixture()
        document["manifest"]["synthetic"] = False
        self.write_source(document)
        self.imported()
        before = self.pair()
        code, result, _ = self.invoke(["demo", "--state", str(self.state), "--no-open"])
        self.assertEqual((code, result["code"]), (2, "demo_state_protected"))
        self.assertEqual(before, self.pair())
        config.atomic_json(self.state / config.DEMO_FILE, {"app": config.APP_ID, "synthetic": True, "state_id": config.state_id(self.state)})
        self.assertEqual(self.invoke(["demo", "--state", str(self.state), "--no-open"])[0], 2)
        config.atomic_json(self.state / config.CONFIG_FILE, {})
        self.assertEqual(self.invoke(["demo", "--state", str(self.state), "--no-open"])[0], 2)
        self.assertEqual(before, self.pair())

    def test_global_state_and_normalization_accept_iso_and_false(self):
        document = fixture()
        document["messages"][0].update(timestamp="2026-10-13T00:00:00+08:00", is_self="false", media_status="metadata_only")
        self.write_source(document)
        code, result, _ = self.invoke(["--state", str(self.state), "import", str(self.source), "--no-open"])
        self.assertEqual(code, 0)
        self.assertEqual(result["validation"], "normalized_archive")
        with contextlib.closing(sqlite3.connect(self.state / "index.sqlite")) as db, db:
            self.assertEqual(db.execute("SELECT is_self,timestamp,media_status FROM messages").fetchone(),
                             (0, server.numeric_time("2026-10-13T00:00:00+08:00"), "metadata_only"))
        self.assertTrue(cli.verify_state(self.state)["ok"])

    def test_duplicate_wrong_count_and_failed_gate_keep_old_pair(self):
        self.imported()
        before = self.pair()
        document = fixture()
        document["messages"] *= 2
        document["manifest"]["message_count"] = 2
        self.write_source(document)
        self.assertEqual(self.invoke(["import", str(self.source), "--state", str(self.state), "--no-open"])[1]["code"], "duplicate_message")
        self.assertEqual(before, self.pair())
        document = fixture()
        document["manifest"]["message_count"] = 5
        self.write_source(document)
        self.assertEqual(self.invoke(["import", str(self.source), "--state", str(self.state), "--no-open"])[1]["code"], "message_count_mismatch")
        self.assertEqual(before, self.pair())
        self.write_source(fixture())
        with mock.patch.object(cli, "verify_state", return_value={"ok": False}):
            self.assertEqual(self.invoke(["import", str(self.source), "--state", str(self.state), "--no-open"])[1]["code"], "import_verification_failed")
        self.assertEqual(before, self.pair())

    def test_native_source_uses_strict_table_manifest_gate(self):
        self.write_source(fixture(native=True))
        result = self.imported()
        self.assertEqual(result["validation"], "wechat_source_manifest")
        self.assertTrue(cli.verify_state(self.state)["ok"])
        before = self.pair()
        document = fixture(native=True)
        document["manifest"]["source_tables"][0]["expected_count"] = 2
        self.write_source(document)
        self.assertEqual(self.invoke(["import", str(self.source), "--state", str(self.state), "--no-open"])[1]["code"], "import_verification_failed")
        self.assertEqual(before, self.pair())
        del document["manifest"]["source_tables"]
        self.write_source(document)
        self.assertEqual(self.invoke(["import", str(self.source), "--state", str(self.state), "--no-open"])[1]["code"], "source_manifest_missing")
        self.assertEqual(before, self.pair())

    def test_viewer_conversation_export_is_reimportable_with_provided_archive_scope(self):
        self.write_source(fixture(native=True))
        self.imported()
        with contextlib.closing(sqlite3.connect(self.state / "index.sqlite")) as db, db:
            db.row_factory = sqlite3.Row
            document = {"conversation": server.public_row(db.execute("SELECT * FROM conversations").fetchone()),
                        "messages": [server.public_row(row) for row in db.execute("SELECT * FROM messages ORDER BY timestamp,id")]}
        self.write_source(document)
        exported = self.base / "reimported-subset"
        result = cli.import_state(self.source, exported, no_open=True)
        self.assertEqual(result["validation"], "normalized_archive")
        manifest = cli.archive_manifest(exported / "archive.jsonl")
        self.assertEqual(manifest["coverage_scope"], "provided_archive")
        self.assertFalse(manifest["source_tables_verified"])
        self.assertTrue(cli.verify_state(exported)["ok"])

    def test_exact_audit_catches_content_media_status_avatar_and_missing_file(self):
        document = fixture()
        media = self.state / "media"
        config.private_dir(media)
        image = media / "sample.png"
        image.write_bytes(b"synthetic-media")
        image.chmod(0o600)
        document["conversations"][0]["avatar_url"] = "/api/media/sample.png"
        document["messages"][0].update(media_url="/api/media/sample.png", file_size=image.stat().st_size,
                                        media_status="available_thumbnail")
        self.write_source(document)
        self.imported()
        self.assertTrue(cli.verify_state(self.state)["ok"])
        with contextlib.closing(sqlite3.connect(self.state / "index.sqlite")) as db, db:
            db.execute("UPDATE messages SET text='changed',media_status='available'")
            db.execute("UPDATE conversations SET avatar_url=NULL")
        result = cli.verify_state(self.state)
        self.assertFalse(result["ok"])
        self.assertEqual(result["index_field_mismatches"]["text"], 1)
        self.assertEqual(result["index_field_mismatches"]["media_status"], 1)
        self.assertEqual(result["issues"]["conversation_avatar_index"], 1)
        image.unlink()
        self.assertGreater(cli.verify_state(self.state)["issues"]["media_url_missing_file"], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions and symlinks")
    def test_media_symlink_never_passes_import_gate(self):
        document = fixture()
        media = self.state / "media"
        config.private_dir(media)
        (media / "sample.png").symlink_to(self.source)
        document["messages"][0]["media_url"] = "/api/media/sample.png"
        self.write_source(document)
        result = self.invoke(["import", str(self.source), "--state", str(self.state), "--no-open"])
        self.assertEqual(result[1]["code"], "import_verification_failed")
        self.assertFalse((self.state / "index.sqlite").exists())

    def test_configure_and_doctor_do_not_print_private_values(self):
        root = self.base / "db-root"
        root.mkdir()
        key = self.base / "keys.json"
        secret = "SYNTHETIC_KEY_VALUE_NEVER_PRINT"
        identity = "SYNTHETIC_SELF_ID_NEVER_PRINT"
        key.write_text(json.dumps({"synthetic": secret}))
        key.chmod(0o600)
        code, result, output = self.invoke(["configure", "--state", str(self.state), "--db-root", str(root),
                                            "--key-file", str(key), "--self-id", identity, "--python", sys.executable])
        self.assertEqual(code, 0)
        self.assertTrue(result["configured"])
        code, report, text = self.invoke(["doctor", "--state", str(self.state)])
        self.assertEqual(code, 0)
        self.assertTrue(report["source"]["key_file_private_and_available"])
        self.assertEqual(set(report["runtime"]["modules"]), {"sqlcipher3", "pysqlcipher3", "Pillow", "cryptography", "zstandard", "pilk"})
        for value in (secret, identity, str(root), str(key)):
            self.assertNotIn(value, output + text)
        if os.name == "posix":
            self.assertEqual((self.state / config.CONFIG_FILE).stat().st_mode & 0o777, 0o600)
            before = (self.state / config.CONFIG_FILE).read_bytes()
            key.chmod(0o644)
            self.assertEqual(self.invoke(["configure", "--state", str(self.state)])[1]["code"], "file_permissions")
            self.assertEqual(before, (self.state / config.CONFIG_FILE).read_bytes())

    @unittest.skipUnless(os.name == "posix", "POSIX venv symlink")
    def test_python_venv_entrypoint_is_not_resolved_to_base_interpreter(self):
        python = self.base / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.assertEqual(config.executable(str(python)), str(python))

    def test_shared_update_lock_and_import_side_effects(self):
        with config.StateLock(self.state):
            with self.assertRaises(config.AppError) as caught:
                self.imported()
        self.assertEqual(caught.exception.code, "state_busy")
        clean = self.base / "unused-home"
        clean.mkdir()
        environment = os.environ.copy()
        environment.update(HOME=str(clean), XDG_DATA_HOME=str(clean), LOCALAPPDATA=str(clean))
        result = subprocess.run([sys.executable, "-c", "import personal_world, app_config, launch, refresh"],
                                cwd=config.APP_DIR, env=environment, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(clean.iterdir()), [])

    def test_publication_restores_old_pair_if_second_replace_fails(self):
        self.imported()
        before = self.pair()
        staging = self.base / "candidate"
        staging.mkdir()
        for name in before:
            path = staging / name
            path.write_bytes(b"synthetic replacement")
            path.chmod(0o600)
        original = os.replace
        def replace(source, target):
            if Path(source) == staging / "index.sqlite":
                raise OSError("synthetic publication failure")
            return original(source, target)
        with mock.patch.object(config.os, "replace", side_effect=replace):
            with self.assertRaises(OSError):
                config.publish_archive(staging, self.state)
        self.assertEqual(before, self.pair())
        self.assertEqual(list(self.state.glob(".publish-backup-*")), [])

    def test_ai_inventory_does_not_treat_unreadable_or_ambiguous_sources_as_absent(self):
        root = self.base / "inventory"
        root.mkdir()
        self.assertFalse(refresh.chatbot_available(root))
        def inaccessible(path, **kwargs):
            kwargs["onerror"](PermissionError())
            return iter(())
        with mock.patch.object(refresh.os, "walk", side_effect=inaccessible):
            with self.assertRaises(config.AppError) as caught:
                refresh.chatbot_available(root)
        self.assertEqual(caught.exception.code, "source_inventory_unreadable")
        for name in ("one", "two"):
            (root / name).mkdir()
            (root / name / "chatbot_message.db").write_bytes(b"synthetic")
        with self.assertRaises(config.AppError) as caught:
            refresh.chatbot_available(root)
        self.assertEqual(caught.exception.code, "chatbot_source_ambiguous")

    @unittest.skipUnless(os.name == "posix", "POSIX credential permissions")
    def test_invalid_access_permissions_do_not_start_a_second_service(self):
        config.private_dir(self.state)
        access = self.state / config.ACCESS_FILE
        access.write_text('{"synthetic":true}')
        access.chmod(0o644)
        with mock.patch.object(launch.subprocess, "Popen") as start:
            with self.assertRaises(config.AppError) as caught:
                launch.open_viewer(self.state, no_open=True)
        self.assertEqual(caught.exception.code, "file_permissions")
        start.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Refresh adapter platform")
    def test_refresh_pipeline_preserves_helpers_ai_and_verification_gate(self):
        self.imported()
        before = self.pair()
        root = self.base / "db-root"
        (root / "chatbot").mkdir(parents=True)
        (root / "chatbot/chatbot_message.db").write_bytes(b"not opened by this test")
        key = self.base / "keys.json"
        key.write_text("{}")
        key.chmod(0o600)
        config.atomic_json(self.state / config.CONFIG_FILE, {"db_root": str(root), "key_file": str(key),
                           "python": sys.executable, "image_python": "/usr/bin/python3", "ffmpeg": sys.executable})
        self.write_source(fixture(native=True))
        calls = []
        fail = [True]
        def stage(name, command, state, environment, timeout):
            calls.append((name, command))
            if name == "messages":
                cli.canonical_archive(self.source, Path(command[command.index("--output") + 1]))
            elif name == "chatbot":
                shutil.copy2(command[command.index("--archive") + 1], command[command.index("--output") + 1])
            elif name == "index":
                server.import_archive(Path(command[command.index("--import-file") + 1]), Path(command[command.index("--data-dir") + 1]))
            elif name == "verify":
                self.assertTrue(cli.verify_state(Path(command[command.index("--state") + 1]), Path(command[command.index("--media-dir") + 1]))["ok"])
                if fail[0]:
                    raise config.AppError("synthetic_gate_failure", "人工测试失败")
        with mock.patch.object(refresh, "runtime_probe", return_value={"python_supported": True}), mock.patch.object(refresh, "run_stage", side_effect=stage):
            with self.assertRaises(config.AppError):
                refresh.refresh_state(self.state, no_open=True)
            self.assertEqual(before, self.pair())
            fail[0] = False
            result = refresh.refresh_state(self.state, no_open=True)
        self.assertTrue(result["ok"])
        self.assertEqual([name for name, _ in calls], ["messages", "media", "chatbot", "index", "verify"] * 2)
        media = calls[1][1]
        self.assertEqual(media[media.index("--image-python") + 1], "/usr/bin/python3")
        self.assertEqual(media[media.index("--ffmpeg") + 1], sys.executable)
        self.assertFalse((self.state / config.ACCESS_FILE).exists())

    def test_real_loopback_services_isolate_states_reuse_and_preserve_legacy_access(self):
        self.imported()
        second = self.base / "second-state"
        cli.import_state(self.source, second, no_open=True)
        legacy = self.state / "local-access.json"
        legacy.write_text('{"synthetic_legacy":true}')
        legacy.chmod(0o600)
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            preferred = port_probe.getsockname()[1]
        processes = []
        original_popen = subprocess.Popen
        def start(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        try:
            with mock.patch.object(launch, "DEFAULT_PORT", preferred), mock.patch.object(launch.subprocess, "Popen", side_effect=start):
                first = launch.open_viewer(self.state, no_open=True)
                other = launch.open_viewer(second, no_open=True)
                again = launch.open_viewer(self.state, no_open=True)
            self.assertEqual(first["port"], preferred)
            self.assertNotEqual(first["port"], other["port"])
            self.assertTrue(again["reused"])
            self.assertEqual(len(processes), 2)
            self.assertEqual(launch.health(first["port"])["state_id"], config.state_id(self.state))
            self.assertEqual(launch.health(other["port"])["state_id"], config.state_id(second))
            access = config.read_private_json(self.state / config.ACCESS_FILE)
            self.assertNotIn(access["token"], json.dumps([first, other, again]))
            self.assertNotIn(str(self.state), json.dumps(launch.health(first["port"])))
            self.assertEqual(legacy.read_text(), '{"synthetic_legacy":true}')
            with self.assertRaises(config.AppError) as caught:
                launch.open_viewer(self.base / "third", port=preferred, no_open=True)
            self.assertEqual(caught.exception.code, "port_in_use")
        finally:
            for process in processes:
                launch.stop_owned(process)


if __name__ == "__main__":
    unittest.main(verbosity=2)
