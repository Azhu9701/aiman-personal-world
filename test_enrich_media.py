"""Synthetic media only; never discovers a real account or accesses a network."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest import mock
import wave

import enrich_media as media
import server
import sync_wechat as sync
from test_sync_wechat import messages_db, message_table


HAS_IMAGES = bool(importlib.util.find_spec("PIL") and importlib.util.find_spec("cryptography"))


def xml_file(name, data, digest=None):
    return '<msg><appmsg><type>6</type><title>' + name + '</title><appattach><totallen>' + str(len(data)) + '</totallen><md5>' + (digest or hashlib.md5(data).hexdigest()) + '</md5><aeskey>SYNTHETIC_AES_SECRET</aeskey></appattach></appmsg></msg>'


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechat-media-synthetic-")
        self.base = Path(self.temp.name).resolve()
        self.account = self.base / "account"
        self.root = self.account / "db_storage"
        self.root.mkdir(parents=True)
        self.keys = self.base / "keys.json"
        self.keys.write_text("{}")
        self.keys.chmod(0o600)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.archive = self.state / "archive.jsonl"
        self.cache = self.state / "media"
        self.connections = []
        self.main = messages_db(self.root / "message/message_0.db")
        self.connections.append(self.main)

    def tearDown(self):
        for db in self.connections:
            db.close()
        self.temp.cleanup()

    def rows(self):
        return [json.loads(line) for line in self.archive.read_text().splitlines()]

    def run_enrich(self, **kwargs):
        return media.enrich(self.archive, self.root, self.keys, self.cache,
                            self_id="self_user", image_python=sys.executable, **kwargs)

    def export(self):
        sync.sync(self.root, self.keys, self.archive, "self_user")

    def file_path(self, name, data):
        path = self.account / "msg/file/2023-11" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def hardlinks(self, entries):
        path = self.root / "hardlink/hardlink.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE dir2id(username TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO dir2id(rowid,username) VALUES (?,?)", [(1, hashlib.md5(b"peer").hexdigest()), (2, "2023-11")])
            for family in ("image", "video", "file"):
                db.execute('CREATE TABLE "' + family + '_hardlink_info_v4" (md5 TEXT,type INTEGER,file_name TEXT,file_size INTEGER,dir1 INTEGER,dir2 INTEGER)')
            for family, digest, name, size, d1, d2 in entries:
                db.execute('INSERT INTO "' + family + '_hardlink_info_v4" VALUES (?,1,?,?,?,?)', (digest, name, size, d1, d2))

    @unittest.skipUnless(HAS_IMAGES, "Existing Pillow and cryptography are required for synthetic DAT decoding")
    def test_images_files_voice_avatar_identity_and_idempotence(self):
        from PIL import Image
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        app_data = self.base / "app_data"
        (app_data / "net/kvcomm").mkdir(parents=True)
        (app_data / "net/kvcomm/key_123_0.statistic").write_bytes(b"")
        image_key = hashlib.md5(b"123self_user").hexdigest()[:16].encode()
        links, images = [], []
        for color in ("red", "blue"):
            stream = io.BytesIO()
            Image.new("RGB", (3, 2), color).save(stream, format="PNG")
            raw = stream.getvalue()
            digest = hashlib.md5(raw).hexdigest()
            a, x = 32, 5
            pad = PKCS7(128).padder()
            padded = pad.update(raw[:a]) + pad.finalize()
            cipher = Cipher(algorithms.AES(image_key), modes.ECB()).encryptor()
            encrypted = cipher.update(padded) + cipher.finalize()
            dat = b"\x07\x08V2\x08\x07" + struct.pack("<II", a, x) + b"\0" + encrypted + raw[a:-x] + bytes(c ^ 123 for c in raw[-x:])
            name = digest + ".dat"
            path = self.account / "msg/attach" / hashlib.md5(b"peer").hexdigest() / "2023-11/Img" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(dat)
            links.append(("image", digest, name, len(dat), 1, 2))
            images.append((raw, digest))
        data = b"synthetic attachment with a changed local filename"
        self.file_path("renamed-report.txt", data)
        links.append(("file", hashlib.md5(data).hexdigest(), "renamed-report.txt", len(data), 2, 0))
        self.hardlinks(links)
        message_table(self.main, "peer", [
            (1, 100, 1, 7, 1700000000, "synthetic text", 0),
            (2, 101, 3, 19, 1700000001, '<msg><img md5="' + images[0][1] + '" aeskey="SYNTHETIC_IMAGE_SECRET"/></msg>', 0),
            (3, 102, (6 << 32) | 49, 19, 1700000002, xml_file("original-report.txt", data), 0),
            (4, 103, 34, 19, 1700000003, '<msg><voicemsg voicelength="200"/></msg>', 0),
            (5, 104, (6 << 32) | 49, 19, 1700000004, xml_file("missing.txt", b"never locally present", "0" * 32), 0),
        ])
        biz = messages_db(self.root / "message/biz_message_0.db")
        self.connections.append(biz)
        message_table(biz, "peer", [(2, 201, 3, 19, 1700000005, '<msg><img md5="' + images[1][1] + '"/></msg>', 0)])
        voice = io.BytesIO()
        with wave.open(voice, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
            out.writeframes(b"\0\0" * 4800)
        with contextlib.closing(sqlite3.connect(self.root / "message/media_0.db")) as db, db:
            db.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)")
            db.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (1,'peer')")
            db.execute("CREATE TABLE VoiceInfo(chat_name_id INTEGER,create_time INTEGER,local_id INTEGER,svr_id INTEGER,voice_data BLOB)")
            db.execute("INSERT INTO VoiceInfo VALUES (1,1700000003,4,103,?)", (voice.getvalue(),))
        path = self.root / "head_image/head_image.db"
        path.parent.mkdir()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE head_image(username TEXT PRIMARY KEY,md5 TEXT,image_buffer BLOB,update_time INTEGER)")
            db.execute("INSERT INTO head_image VALUES ('peer','',?,0)", (images[0][0],))
        self.export()
        original = self.rows()
        original_files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        report = self.run_enrich(app_data=app_data)
        self.assertEqual(report["media_available"], 4)
        self.assertEqual(report["avatars_available"], 1)
        enhanced = self.rows()
        self.assertEqual([r.get("id") for r in original], [r.get("id") for r in enhanced])
        for before, after in zip(original, enhanced):
            if before["record_type"] == "message":
                for key, value in before.items():
                    self.assertEqual(after[key], value)
        image_urls = [r["media_url"] for r in enhanced if r.get("type") == "image"]
        self.assertEqual(len(set(image_urls)), 2, "same local_id in different shards must not share the wrong image")
        for path, digest in original_files.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertFalse(enhanced[-1]["content_complete"])
        self.assertTrue(enhanced[-1]["complete"])
        for secret in ("SYNTHETIC_AES_SECRET", "SYNTHETIC_IMAGE_SECRET", image_key.decode()):
            self.assertNotIn(secret, self.archive.read_text())
        for path in self.cache.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        imported = server.import_archive(self.archive, self.state)
        self.assertEqual(imported["message_count"], 6)
        with contextlib.closing(sqlite3.connect(self.state / "index.sqlite")) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE media_url IS NOT NULL").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM conversations WHERE avatar_url IS NOT NULL").fetchone()[0], 1)
        again = self.run_enrich(app_data=app_data)
        self.assertEqual(again["new_cache_files"], 0)
        self.assertEqual(again["media_available"], 4)

    def test_mismatching_digest_and_source_symlink_never_publish(self):
        body = b"synthetic payload"
        bad = self.file_path("bad.txt", b"z" * len(body))
        outside = self.base / "outside.txt"
        outside.write_bytes(body)
        link = bad.parent / "link.txt"
        link.symlink_to(outside)
        digest = hashlib.md5(body).hexdigest()
        self.hardlinks([("file", digest, "bad.txt", len(body), 2, 0), ("file", digest, "link.txt", len(body), 2, 0)])
        message_table(self.main, "peer", [(1, 1, (6 << 32) | 49, 19, 1700000000, xml_file("report.txt", body), 0)])
        self.export()
        report = self.run_enrich()
        self.assertEqual(report["media_available"], 0)
        row = next(r for r in self.rows() if r["record_type"] == "message")
        self.assertNotIn("media_url", row)
        self.assertTrue(self.rows()[-1]["complete"])
        self.assertEqual(outside.read_bytes(), body)

    def test_unsafe_media_directory_rejected_before_publication(self):
        message_table(self.main, "peer", [(1, 1, 1, 19, 1700000000, "synthetic", 0)])
        self.export()
        original = self.archive.read_bytes()
        outside = self.base / "outside"
        outside.mkdir()
        self.cache.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(media.EnrichError):
            self.run_enrich()
        self.assertEqual(self.archive.read_bytes(), original)
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_manifest_and_write_failure_preserve_archive(self):
        message_table(self.main, "peer", [(1, 1, 1, 19, 1700000000, "synthetic", 0)])
        self.export()
        original = self.archive.read_bytes()
        with mock.patch.object(media.W, "emit_record", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.run_enrich()
        self.assertEqual(self.archive.read_bytes(), original)
        values = self.rows()
        values[-1]["message_count"] += 1
        self.archive.write_text("\n".join(json.dumps(r) for r in values) + "\n")
        invalid = self.archive.read_bytes()
        with self.assertRaises(media.EnrichError):
            self.run_enrich()
        self.assertEqual(self.archive.read_bytes(), invalid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
