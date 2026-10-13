#!/usr/bin/env python3
"""Attach verified, already-local WeChat media to an existing private JSONL.

No network client, download, ASR, app access, key acquisition or sending exists
in this module. Database handles only open newly created private DB+WAL copies.
Hardlink *index* paths follow the installed Rion reader's macOS layout; source
files are copied, never hardlinked. V1/V2 DAT decoding follows the independently
published macOS format (acmerfight's MIT local toolkit); personal image keys are
derived in memory from this account's local metadata, never logged or stored.
Existing row identities, content, direction and original decode status survive.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import contextlib
import datetime as dt
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import select
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import warnings
import wave

import sync_wechat as W


MAX_IMAGE = 128 * 1024 * 1024
MAX_VOICE = 16 * 1024 * 1024
HEX32 = re.compile(r"[0-9a-fA-F]{32}")
MEDIA_DB = re.compile(r"media_\d+\.db$", re.I)
EXTRA_DBS = {"message/message_resource.db", "hardlink/hardlink.db", "head_image/head_image.db"}
CACHE_NAME = re.compile(r"[0-9a-f]{64}\.(?:png|jpg|gif|webp|mp4|wav|bin)$")


class EnrichError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class MediaError(EnrichError):
    pass


def integer(value, default=None):
    answer = W.integer(value)
    return default if answer is None else answer


def sqltext(value):
    return "CAST(x'" + str(value).encode("utf-8").hex() + "' AS TEXT)"


def component(value):
    value = W.string(value)
    return bool(value) and value not in (".", "..") and len(value.encode()) <= 255 and not any(c in value for c in ("/", "\\", "\0"))


def fingerprint(path):
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def private_directory(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise EnrichError("output_symlink_rejected")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise EnrichError("output_directory_invalid")
    path.chmod(0o700)
    return path.resolve()


@contextlib.contextmanager
def secure_read(path, root):
    """Walk every component with openat/O_NOFOLLOW; never follow a source link."""
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise MediaError("unsafe_path")
    if not parts or not all(component(p) for p in parts):
        raise MediaError("unsafe_path")
    directory_fd = None
    data_fd = None
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        data_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(data_fd).st_mode):
            raise MediaError("unsafe_path")
        with os.fdopen(data_fd, "rb") as stream:
            data_fd = None
            yield stream
    except FileNotFoundError:
        raise MediaError("not_local")
    except OSError:
        raise MediaError("source_unreadable_or_symlink")
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def stream_fingerprint(stream):
    s = os.fstat(stream.fileno())
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def copy_file(source, destination, root, *, limit, size=None, digests=None):
    sha, md5, total = hashlib.sha256(), hashlib.md5(), 0
    with secure_read(source, root) as inp:
        before = stream_fingerprint(inp)
        if before[2] > limit:
            raise MediaError("size_limit")
        if size is not None and before[2] != size:
            raise MediaError("size_mismatch")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            while True:
                block = inp.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise MediaError("size_limit")
                out.write(block)
                sha.update(block)
                md5.update(block)
            if stream_fingerprint(inp) != before or total != before[2]:
                raise MediaError("source_changed")
            out.flush()
        if digests and md5.hexdigest() not in digests:
            raise MediaError("digest_mismatch")
    return {"size": total, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}


def selected_databases(root):
    answer = []
    for p in root.rglob("*.db"):
        rel = p.relative_to(root).as_posix()
        if W.MESSAGE_DATABASE.fullmatch(p.name) or MEDIA_DB.fullmatch(p.name) or rel in EXTRA_DBS:
            if p.is_symlink() or not p.is_file():
                raise EnrichError("database_source_invalid")
            answer.append(p)
    return sorted(answer)


@contextlib.contextmanager
def selected_snapshot(source_root, progress):
    for attempt in range(3):
        sources = selected_databases(source_root)
        before = W.fingerprints(source_root, sources)
        with tempfile.TemporaryDirectory(prefix="wechat-media-snapshot-") as temporary:
            copied_root = Path(temporary).resolve()
            copied_root.chmod(0o700)
            try:
                for rel, signature in before.items():
                    out = copied_root / rel
                    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    copy_file(source_root / rel, out, source_root, limit=max(signature[2], 1), size=signature[2])
                    progress()
                if selected_databases(source_root) != sources or W.fingerprints(source_root, sources) != before:
                    raise MediaError("source_changed")
            except (MediaError, W.SyncError):
                if attempt < 2:
                    continue
                raise EnrichError("media_snapshot_not_stable")
            yield copied_root, [p.relative_to(source_root).as_posix() for p in sources]
            return
    raise EnrichError("media_snapshot_not_stable")


def image_keys(self_id, db_root, app_data):
    if not self_id:
        return []
    # Exact provided identity and exact selected directory only. Never strip,
    # invent or substitute an unverified wxid from a directory suffix.
    identities = list(dict.fromkeys([self_id, db_root.parent.name]))
    result = []
    if app_data:
        for sub in ("net/kvcomm", "ilink/kvcomm"):
            for path in (app_data / sub).glob("key_*_*.statistic"):
                match = re.fullmatch(r"key_(\d+)_.*\.statistic", path.name)
                if match and path.is_file() and not path.is_symlink():
                    number = int(match[1])
                    for identity in identities:
                        key = hashlib.md5((str(number) + identity).encode()).hexdigest()[:16]
                        result.append((key, number & 255))
    return list(dict.fromkeys(result))


def dat_decode(data, key, xor):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    if len(data) < 15:
        raise ValueError()
    a, x = struct.unpack("<II", data[6:14])
    n = a + (16 - a % 16) if a else 0
    if 15 + n + x > len(data):
        raise ValueError()
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    front = decryptor.update(data[15:15 + n]) + decryptor.finalize()
    if n:
        unpad = PKCS7(128).unpadder()
        front = unpad.update(front) + unpad.finalize()
    tail = data[15 + n:]
    return front + (tail[:-x] if x else tail) + (bytes(b ^ xor for b in tail[-x:]) if x else b"")


def decode_image_file(source, destination, keys, ffmpeg):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 60_000_000
    data = Path(source).read_bytes()
    if not data or len(data) > MAX_IMAGE:
        return {"status": "image_size_limit"}
    variants = []
    if data.startswith((b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07")):
        candidates = [(k.encode(), x) for k, x in keys]
        if data.startswith(b"\x07\x08V1"):
            candidates = [(b"cfcd208495d565ef", x) for x in range(256)]
        if not candidates:
            return {"status": "image_key_metadata_missing"}
        def decoded_variants():
            for key, xor in candidates:
                try:
                    yield dat_decode(data, key, xor)
                except Exception:
                    continue
        variants = decoded_variants()
    elif data.startswith((b"\xff\xd8", b"\x89PNG", b"GIF8", b"RIFF", b"wxgf")):
        variants = [data]
    else:
        variants = [bytes(b ^ xor for b in data) for xor in {data[0] ^ 255, data[0] ^ 137, data[0] ^ 71}]
    for raw in variants:
        try:
            first_frame = False
            if raw.startswith(b"wxgf"):
                if not ffmpeg:
                    continue
                positions = [m.start() for m in re.finditer(b"\x00\x00\x00\x01\x40|\x00\x00\x01\x40", raw)]
                if not positions:
                    continue
                bitstream = Path(destination).with_suffix(".hevc")
                bitstream.write_bytes(raw[min(positions):])
                proc = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-threads", "1", "-protocol_whitelist", "file,pipe", "-f", "hevc", "-i", str(bitstream), "-frames:v", "1", "-f", "image2", "-vcodec", "png", "-y", str(destination)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
                if proc.returncode:
                    continue
                raw = Path(destination).read_bytes()
                first_frame = True
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                im = Image.open(io.BytesIO(raw))
                im.load()
                fmt, width, height = im.format, im.width, im.height
                if width <= 0 or height <= 0 or width * height > 60_000_000:
                    continue
                ext = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}.get(fmt)
                if not ext:
                    continue
                animated = int(getattr(im, "n_frames", 1)) > 1
                im.close()
            Path(destination).write_bytes(raw)
            return {"status": "available_first_frame" if first_frame else "available", "ext": ext, "width": width, "height": height, "animated": animated}
        except Exception:
            continue
    return {"status": "image_decode_unsupported_or_invalid"}


def image_worker_main():
    os.umask(0o077)
    try:
        config = json.loads(sys.stdin.readline())
        from PIL import Image  # noqa: F401
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
        print(json.dumps({"ready": 1}), flush=True)
        for line in sys.stdin:
            try:
                job = json.loads(line)
                result = decode_image_file(job["source"], job["destination"], config.get("keys", []), config.get("ffmpeg"))
            except Exception:
                result = {"status": "image_decode_failed"}
            print(json.dumps(result, separators=(",", ":")), flush=True)
    except Exception:
        return 2
    return 0


class ImageWorker:
    def __init__(self, interpreter, keys, ffmpeg):
        self.process = None
        self.interpreter = interpreter
        self.keys, self.ffmpeg = keys, ffmpeg
        self.buffer = b""

    def read(self, timeout):
        end = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.process.stdout], [], [], left)[0]:
                raise MediaError("image_worker_timeout")
            block = os.read(self.process.stdout.fileno(), 4096)
            if not block:
                raise MediaError("image_worker_unavailable")
            self.buffer += block
            if len(self.buffer) > 16384:
                raise MediaError("image_worker_invalid_output")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def start(self):
        self.process = subprocess.Popen([self.interpreter, "-B", str(Path(__file__).resolve()), "--image-worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.buffer = b""
        self.process.stdin.write((json.dumps({"keys": self.keys, "ffmpeg": self.ffmpeg}) + "\n").encode())
        self.process.stdin.flush()
        if self.read(20).get("ready") != 1:
            raise MediaError("image_worker_unavailable")

    def decode(self, source, destination):
        try:
            if self.process is None or self.process.poll() is not None:
                self.close()
                self.start()
            self.process.stdin.write((json.dumps({"source": str(source), "destination": str(destination)}) + "\n").encode())
            self.process.stdin.flush()
            return self.read(40)
        except Exception:
            self.close()
            return {"status": "image_worker_unavailable"}

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
            self.process = None


class Publisher:
    def __init__(self, directory):
        self.directory = private_directory(directory)
        self.verified = set()
        self.created = 0

    def existing(self, url):
        if not isinstance(url, str) or not url.startswith("/api/media/"):
            return None
        name = urllib.parse.unquote(url[len("/api/media/"):])
        if not CACHE_NAME.fullmatch(name):
            return None
        path = self.directory / name
        try:
            if name not in self.verified:
                digest = hashlib.sha256()
                with secure_read(path, self.directory) as stream:
                    before = stream_fingerprint(stream)
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                    if stream_fingerprint(stream) != before or digest.hexdigest() != name.split(".")[0]:
                        return None
                self.verified.add(name)
            return {"media_url": "/api/media/" + name, "file_size": path.stat().st_size}
        except (MediaError, OSError):
            return None

    def publish(self, source, ext):
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        name = digest.hexdigest() + "." + ext
        if not CACHE_NAME.fullmatch(name):
            raise EnrichError("cache_format_invalid")
        target = self.directory / name
        if target.exists() or target.is_symlink():
            result = self.existing("/api/media/" + name)
            if result is None:
                raise EnrichError("cache_content_mismatch")
            return result
        if shutil.disk_usage(self.directory).free < source.stat().st_size + 32 * 1024 * 1024:
            raise EnrichError("media_cache_disk_full")
        fd, temporary = tempfile.mkstemp(prefix=".media-", dir=self.directory)
        pending = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(pending, 0o600)
            os.replace(pending, target)
            self.created += 1
            self.verified.add(name)
            return {"media_url": "/api/media/" + name, "file_size": target.stat().st_size}
        finally:
            pending.unlink(missing_ok=True)


def metadata(content):
    root = W.xml_root(content[content.find("<"):]) if "<" in content else None
    result = {"hashes": set(), "name": None, "size": None}
    if root is None:
        return result
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        for key in ("md5", "filemd5", "rawfilemd5"):
            value = node.attrib.get(key)
            if value and HEX32.fullmatch(value):
                result["hashes"].add(value.lower())
        if tag in ("md5", "filemd5", "rawfilemd5") and HEX32.fullmatch((node.text or "").strip()):
            result["hashes"].add(node.text.strip().lower())
    app = root if root.tag == "appmsg" else root.find(".//appmsg")
    if app is not None and W.xml_text(app, "type") == "6":
        result["name"] = W.xml_text(app, "title") or None
        result["size"] = integer(W.xml_text(app, "appattach/totallen"))
    return result


class Resolver:
    def __init__(self, databases, original_root, publisher, worker, work, max_file, ffmpeg):
        self.db = databases
        self.root = original_root
        self.account = original_root.parent
        self.publisher, self.worker, self.work = publisher, worker, work
        self.max_file, self.ffmpeg = max_file, ffmpeg
        self.stage, self.decoded = work / "source.bin", work / "decoded.bin"
        self.columns_cache, self.result_cache = {}, {}
        self.hardlinks = defaultdict(list)
        self.resources = defaultdict(list)
        self.voices = defaultdict(list)
        self.asset_hashes = defaultdict(list)
        self.file_sizes = defaultdict(list)
        self.schema_counts = Counter()
        self.roots = [original_root]
        for base in (original_root, self.account):
            for sub in ("msg", "resource", "FileStorage", "files"):
                path = base / sub
                if path.is_dir() and not path.is_symlink():
                    self.roots.append(path)

    def has_columns(self, db, table, needed):
        try:
            return set(needed) <= {r["name"] for r in db.columns(table)}
        except W.SyncError:
            return False

    def build(self, progress):
        hard = self.db.get("hardlink/hardlink.db")
        if hard and self.has_columns(hard, "dir2id", {"username"}):
            dirs = {integer(r["n"]): W.string(r["username"]) for r in hard.rows("SELECT rowid AS n,username FROM dir2id")}
            for family in ("image", "video", "file"):
                table = family + "_hardlink_info_v4"
                if not self.has_columns(hard, table, {"md5", "file_name", "file_size", "dir1", "dir2"}):
                    self.schema_counts["hardlink_table_unavailable"] += 1
                    continue
                for row in hard.rows("SELECT md5,file_name,file_size,dir1,dir2 FROM " + W.quote_identifier(table)):
                    digest = W.string(row["md5"]).lower()
                    name, d1, d2 = W.string(row["file_name"]), dirs.get(integer(row["dir1"]), ""), dirs.get(integer(row["dir2"]), "")
                    if not HEX32.fullmatch(digest) or not component(name) or any(v and not component(v) for v in (d1, d2)):
                        continue
                    for root in self.roots:
                        candidates = [root / d1 / d2 / name, root / family / d1 / d2 / name, root / "HardLink" / d1 / d2 / name, root / "FileStorage/HardLink" / d1 / d2 / name]
                        if family == "image" and d1 and d2:
                            candidates.append(root / "attach" / d1 / d2 / "Img" / name)
                        elif family in ("video", "file") and d1:
                            candidates.append(root / family / d1 / name)
                        for path in candidates:
                            if path.is_file():
                                self.hardlinks[(family, digest)].append((path, integer(row["file_size"])))
                    progress()
        resource = self.db.get("message/message_resource.db")
        if resource and all(self.has_columns(resource, table, cols) for table, cols in (
                ("ChatName2Id", {"user_name"}),
                ("MessageResourceInfo", {"message_id", "chat_id", "message_svr_id", "message_local_id", "message_create_time", "message_local_type", "packed_info"}),
                ("MessageResourceDetail", {"message_id", "size", "status", "data_index", "packed_info"}))):
            query = """SELECT c.user_name AS chat,i.message_id,i.message_svr_id AS svr,
              i.message_local_id AS local_id,i.message_create_time AS timestamp,i.message_local_type AS kind,
              i.packed_info AS info,d.size,d.status,d.data_index,d.packed_info AS detail
              FROM MessageResourceInfo i JOIN ChatName2Id c ON c.rowid=i.chat_id
              JOIN MessageResourceDetail d ON d.message_id=i.message_id"""
            for row in resource.rows(query):
                if integer(row["status"]) != 1 or integer(row["size"], 0) <= 0:
                    continue
                hashes = set()
                for field in ("info", "detail", "data_index"):
                    value = row.get(field)
                    raw = value if isinstance(value, bytes) else W.string(value).encode()
                    hashes.update(v.decode().lower() for v in re.findall(rb"[0-9a-fA-F]{32}", raw))
                item = {"message_id": row["message_id"], "size": integer(row["size"]), "hashes": hashes,
                        "local_id": integer(row["local_id"]), "timestamp": integer(row["timestamp"]), "kind": integer(row["kind"])}
                self.resources[(W.string(row["chat"]), integer(row["svr"], 0))].append(item)
                progress()
        elif resource:
            self.schema_counts["resource_schema_unavailable"] += 1
        for relative, db in self.db.items():
            if not MEDIA_DB.fullmatch(Path(relative).name):
                continue
            if not self.has_columns(db, "VoiceInfo", {"chat_name_id", "create_time", "local_id", "svr_id", "voice_data"}) or not self.has_columns(db, "Name2Id", {"user_name"}):
                self.schema_counts["voice_schema_unavailable"] += 1
                continue
            for row in db.rows("SELECT v.rowid AS n,n.user_name AS chat,v.create_time,v.local_id,v.svr_id FROM VoiceInfo v JOIN Name2Id n ON n.rowid=v.chat_name_id"):
                self.voices[(W.string(row["chat"]), integer(row["svr_id"], 0))].append((relative, integer(row["n"]), integer(row["create_time"]), integer(row["local_id"])))
                progress()
        for family in ("attach", "video", "file"):
            folder = self.account / "msg" / family
            if not folder.is_dir() or folder.is_symlink():
                continue
            for current, directories, files in os.walk(folder, followlinks=False):
                directories[:] = [d for d in directories if not (Path(current) / d).is_symlink()]
                for name in files:
                    path = Path(current) / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    size = path.stat().st_size
                    prefix = re.match(r"([0-9a-fA-F]{32})", name)
                    if prefix:
                        self.asset_hashes[(family, prefix[1].lower())].append((path, size))
                    if family in ("file", "attach"):
                        self.file_sizes[size].append(path)
                progress()

    def source_row(self, record):
        parts = record["id"].split(":", 3)
        if len(parts) != 4:
            raise MediaError("source_identity_unavailable")
        relative, table = parts[:2]
        db = self.db.get(relative)
        if db is None:
            raise MediaError("source_database_unavailable")
        key = (relative, table)
        if key not in self.columns_cache:
            self.columns_cache[key] = {r["name"] for r in db.columns(table)}
        columns = self.columns_cache[key]
        local_id = integer(record.get("source_local_id"))
        if "local_id" not in columns or local_id is None:
            raise MediaError("source_identity_unavailable")
        fields = [c for c in ("local_id", "server_id", "create_time", "local_type", "message_content", "compress_content", "WCDB_CT_message_content") if c in columns]
        query = 'SELECT rowid AS "__row",' + ",".join(W.quote_identifier(c) for c in fields) + " FROM " + W.quote_identifier(table) + " WHERE local_id=" + str(local_id) + " LIMIT 3"
        rows = list(db.rows(query))
        rows = [r for r in rows if relative + ":" + table + ":" + W.ordered_identity(r.get("local_id")) + ":" + W.ordered_identity(r.get("__row")) == record["id"]]
        if len(rows) != 1:
            raise MediaError("source_message_missing_or_ambiguous")
        row = rows[0]
        if integer(row.get("server_id")) != integer(record.get("server_id")) or integer(row.get("create_time")) != integer(record.get("timestamp")) or integer(row.get("local_type")) != integer(record.get("raw_type")):
            raise MediaError("source_message_changed")
        return row

    def candidates(self, record, family, meta):
        found = {}
        for digest in meta["hashes"]:
            for path, size in self.hardlinks.get((family, digest), []):
                found[path] = size
        rows = self.resources.get((record["conversation_id"], integer(record.get("server_id"), 0)), [])
        if integer(record.get("server_id"), 0) <= 0:
            rows = [r for r in rows if r["local_id"] == integer(record.get("source_local_id")) and r["timestamp"] == integer(record.get("timestamp"))]
        rows = [r for r in rows if (r["kind"] or 0) & 0xffffffff == (integer(record.get("raw_type"), 0) & 0xffffffff)]
        family_dir = "attach" if family == "image" else family
        for row in rows:
            for digest in row["hashes"]:
                for path, size in self.asset_hashes.get((family_dir, digest), []):
                    if size == row["size"]:
                        if family == "image" and hashlib.md5(record["conversation_id"].encode()).hexdigest() not in path.parts:
                            continue
                        found[path] = size
        if family == "file" and meta["hashes"] and meta["size"] is not None and 0 <= meta["size"] <= self.max_file:
            # Full content digest, not a similar filename, decides this fallback.
            for path in self.file_sizes.get(meta["size"], []):
                found.setdefault(path, meta["size"])
        return sorted(found.items(), key=lambda item: (item[0].name.lower().endswith("_t.dat"), -(item[1] or 0), str(item[0])))

    def avatar(self, record):
        existing = self.publisher.existing(record.get("avatar_url"))
        if existing:
            return {"avatar_url": existing["media_url"], "avatar_status": "available"}
        db = self.db.get("head_image/head_image.db")
        if not db or not self.has_columns(db, "head_image", {"username", "image_buffer"}):
            return {"avatar_status": "avatar_database_unavailable"}
        rows = list(db.rows("SELECT image_buffer FROM head_image WHERE username=" + sqltext(record["id"]) + " LIMIT 2"))
        if len(rows) != 1 or not isinstance(rows[0]["image_buffer"], bytes) or not rows[0]["image_buffer"]:
            return {"avatar_status": "not_local"}
        blob = rows[0]["image_buffer"]
        if len(blob) > MAX_IMAGE:
            return {"avatar_status": "size_limit"}
        self.stage.write_bytes(blob)
        result = self.worker.decode(self.stage, self.decoded)
        if result.get("status", "").startswith("available"):
            value = self.publisher.publish(self.decoded, result["ext"])
            return {"avatar_url": value["media_url"], "avatar_status": result["status"]}
        return {"avatar_status": result["status"]}

    def voice(self, record):
        rows = self.voices.get((record["conversation_id"], integer(record.get("server_id"), 0)), [])
        stamp, local = integer(record.get("timestamp")), integer(record.get("source_local_id"))
        same_time = [r for r in rows if r[2] == stamp]
        rows = same_time or rows
        if integer(record.get("server_id"), 0) <= 0:
            rows = [r for r in rows if r[2] == stamp and r[3] == local]
        if not rows:
            raise MediaError("voice_not_local")
        bodies = {}
        for rel, rowid, _, _ in rows:
            row = self.db[rel].one("SELECT voice_data FROM VoiceInfo WHERE rowid=" + str(rowid))
            blob = row.get("voice_data")
            if isinstance(blob, bytes) and 0 < len(blob) <= MAX_VOICE:
                bodies[hashlib.sha256(blob).hexdigest()] = blob
        if len(bodies) != 1:
            raise MediaError("voice_missing_or_ambiguous")
        digest, body = next(iter(bodies.items()))
        if ("voice", digest) in self.result_cache:
            return dict(self.result_cache[("voice", digest)])
        self.stage.write_bytes(body)
        if body.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
            vendor = Path(__file__).resolve().parent / "vendor"
            if vendor.is_dir() and str(vendor) not in sys.path:
                sys.path.insert(0, str(vendor))
            try:
                pilk = importlib.import_module("pilk")
            except ImportError:
                raise MediaError("silk_decoder_unavailable")
            pcm = self.work / "voice.pcm"
            try:
                pilk.decode(str(self.stage), str(pcm), pcm_rate=24000)
                if not pcm.is_file() or not 0 < pcm.stat().st_size <= 24000 * 2 * 1800 or pcm.stat().st_size % 2:
                    raise ValueError()
                with wave.open(str(self.decoded), "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(24000)
                    with pcm.open("rb") as inp:
                        for block in iter(lambda: inp.read(1024 * 1024), b""):
                            out.writeframesraw(block)
            except Exception:
                raise MediaError("silk_decode_failed")
            finally:
                pcm.unlink(missing_ok=True)
        elif body.startswith(b"RIFF") and body[8:12] == b"WAVE":
            self.decoded.write_bytes(body)
        elif body.startswith(b"#!AMR") and self.ffmpeg:
            proc = subprocess.run([self.ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-f", "amr", "-i", str(self.stage), "-f", "wav", "-ac", "1", "-ar", "24000", "-y", str(self.decoded)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if proc.returncode:
                raise MediaError("voice_decode_failed")
        else:
            raise MediaError("voice_format_unsupported")
        try:
            with wave.open(str(self.decoded), "rb") as inp:
                if inp.getnchannels() not in (1, 2) or inp.getnframes() <= 0 or inp.getframerate() <= 0 or inp.getnframes() / inp.getframerate() > 1800:
                    raise ValueError()
        except Exception:
            raise MediaError("voice_output_invalid")
        result = {**self.publisher.publish(self.decoded, "wav"), "media_status": "available"}
        self.result_cache[("voice", digest)] = result
        return result

    def media(self, record, family):
        existing = self.publisher.existing(record.get("media_url"))
        if existing:
            return {**existing, "media_status": record.get("media_status") or "available"}
        if family == "voice":
            return self.voice(record)
        if family == "sticker":
            raise MediaError("sticker_mapping_unavailable")
        row = self.source_row(record)
        raw = row.get("message_content")
        if raw in (None, "", b""):
            raw = row.get("compress_content")
        content, status = W.decompress(raw, row.get("WCDB_CT_message_content"))
        if status != "ok":
            raise MediaError("source_payload_unavailable")
        meta = metadata(content)
        if family == "file" and not meta["hashes"]:
            raise MediaError("file_digest_missing")
        candidates = self.candidates(record, family, meta)
        if not candidates:
            raise MediaError("not_local_or_unmatched")
        failures = []
        for path, size in candidates:
            if family == "video" and path.suffix.lower() not in (".mp4", ".mov", ".m4v"):
                continue
            try:
                info = copy_file(path, self.stage, self.account, limit=MAX_IMAGE if family == "image" else self.max_file, size=size if size and size > 0 else None,
                                 digests=meta["hashes"] if family == "file" else None)
                if family == "file" and meta["size"] is not None and info["size"] != meta["size"]:
                    raise MediaError("size_mismatch")
                cached = self.result_cache.get((family, info["sha256"]))
                if cached:
                    return dict(cached)
                if family == "image":
                    decoded = self.worker.decode(self.stage, self.decoded)
                    if not decoded.get("status", "").startswith("available"):
                        raise MediaError(decoded["status"])
                    status = "available_thumbnail" if path.name.lower().endswith("_t.dat") else decoded["status"]
                    result = {**self.publisher.publish(self.decoded, decoded["ext"]), "media_status": status}
                elif family == "video":
                    with self.stage.open("rb") as f:
                        header = f.read(64)
                    if len(header) < 12 or header[4:8] != b"ftyp":
                        raise MediaError("video_format_unsupported")
                    ffprobe = shutil.which("ffprobe") or (str(Path(self.ffmpeg).with_name("ffprobe")) if self.ffmpeg else None)
                    if not ffprobe or not Path(ffprobe).is_file():
                        raise MediaError("video_probe_unavailable")
                    probe = subprocess.run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mov", "-show_entries", "stream=codec_type,codec_name:format=duration", "-of", "json", str(self.stage)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=25)
                    parsed = json.loads(probe.stdout) if probe.returncode == 0 else {}
                    streams = parsed.get("streams", [])
                    if not any(s.get("codec_type") == "video" and s.get("codec_name") in ("h264", "hevc", "av1", "vp9") for s in streams):
                        raise MediaError("video_codec_unsupported")
                    result = {**self.publisher.publish(self.stage, "mp4"), "media_status": "available"}
                else:
                    result = {**self.publisher.publish(self.stage, "bin"), "media_status": "available"}
                self.result_cache[(family, info["sha256"])] = result
                return result
            except MediaError as error:
                failures.append(error.code)
            except (ValueError, OSError, subprocess.SubprocessError):
                failures.append("media_validation_failed")
        raise MediaError(failures[-1] if failures else "not_local_or_unmatched")


def family_of(rec):
    known = {"image": "image", "video": "video", "voice": "voice", "audio": "voice", "file": "file", "attachment": "file", "sticker": "sticker", "emoji": "sticker"}
    if rec.get("type") in known:
        return known[rec["type"]]
    raw = integer(rec.get("raw_type"), 0)
    return {3: "image", 34: "voice", 43: "video", 62: "video", 47: "sticker"}.get(raw & 0xffffffff) or ("file" if raw == (6 << 32) | 49 else None)


def read_records(path):
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise EnrichError("archive_record_invalid")
                yield value


def enrich(archive, db_root, key_file, media_dir, *, output=None, snapshot_root=None, self_id=None, app_data=None, image_python=None, ffmpeg=None, progress=None, max_file_bytes=1024 * 1024 * 1024):
    archive = Path(archive).expanduser().absolute()
    output = Path(output).expanduser().absolute() if output else archive
    db_root = Path(db_root).expanduser().resolve()
    source_root = Path(snapshot_root).expanduser().resolve() if snapshot_root else db_root
    key_file = Path(key_file).expanduser().absolute()
    if any(p.is_symlink() for p in (archive, output, *output.parents)):
        raise EnrichError("output_symlink_rejected")
    if output == key_file or db_root.parent == output or db_root.parent in output.parents or source_root == output or source_root in output.parents:
        raise EnrichError("output_conflicts_with_source")
    media_path = Path(media_dir).expanduser().absolute()
    if media_path == db_root.parent or db_root.parent in media_path.parents or media_path == source_root or source_root in media_path.parents:
        raise EnrichError("media_directory_conflicts_with_source")
    if stat.S_IMODE(archive.stat().st_mode) & 0o077:
        raise EnrichError("archive_permissions_invalid")
    original_fingerprint = fingerprint(archive)
    original_hash = hashlib.sha256()
    identities, conversation_ids = set(), set()
    manifest = None
    message_count = 0
    manifest_count = 0
    for rec in read_records(archive):
        kind = rec.get("record_type")
        original_hash.update(json.dumps([kind, rec.get("id")], separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
        if kind == "message":
            mid = rec.get("id")
            if not isinstance(mid, str) or mid in identities:
                raise EnrichError("archive_message_identity_invalid")
            identities.add(mid)
            message_count += 1
        elif kind == "conversation":
            if rec.get("id") in conversation_ids:
                raise EnrichError("archive_conversation_identity_invalid")
            conversation_ids.add(rec.get("id"))
        elif kind == "manifest":
            manifest, manifest_count = rec, manifest_count + 1
        else:
            raise EnrichError("archive_record_invalid")
    if manifest_count != 1 or not manifest or manifest.get("complete") is not True or manifest.get("failed_databases") or manifest.get("failed_tables") or message_count <= 0 or manifest.get("message_count") != message_count or manifest.get("conversation_count") != len(conversation_ids):
        raise EnrichError("archive_manifest_invalid")
    if fingerprint(archive) != original_fingerprint:
        raise EnrichError("archive_changed")
    keys = W.load_keys(key_file)
    publisher = Publisher(media_dir)
    if app_data is None:
        app_data = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data"
    else:
        app_data = Path(app_data).expanduser().resolve()
    ffmpeg = ffmpeg or shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").is_file() else None)
    image_python = image_python or ("/usr/bin/python3" if sys.platform == "darwin" else sys.executable)
    counts, status_counts, by_type, db_counts = Counter(), Counter(), defaultdict(Counter), Counter()
    last_progress = [0.0]
    def tick(force=False):
        if progress and (force or time.monotonic() - last_progress[0] >= 10):
            last_progress[0] = time.monotonic()
            progress({"status": "enriching", "messages_processed": counts["messages_processed"], "media_available": counts["media_available"], "avatars_available": counts["avatars_available"]})
    tick(True)
    with selected_snapshot(source_root, tick) as (copied_root, relatives), contextlib.ExitStack() as stack:
        databases = {}
        for relative in relatives:
            copied = copied_root / relative
            try:
                with copied.open("rb") as f:
                    header = f.read(16)
                db = W.Database(copied, W.key_spec(keys, db_root / relative, db_root, header))
                stack.callback(db.close)
                db.verify()
                databases[relative] = db
                db_counts["verified"] += 1
            except W.SyncError:
                db_counts["unavailable"] += 1
            tick()
        with tempfile.TemporaryDirectory(prefix="wechat-media-work-") as temporary:
            work = Path(temporary).resolve()
            work.chmod(0o700)
            worker = ImageWorker(str(image_python), image_keys(self_id, db_root, app_data), ffmpeg)
            stack.callback(worker.close)
            resolver = Resolver(databases, db_root, publisher, worker, work, max_file_bytes, ffmpeg)
            resolver.build(tick)
            private_directory(output.parent)
            fd, name = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".pending", dir=output.parent)
            pending = Path(name)
            new_hash = hashlib.sha256()
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                    for rec in read_records(archive):
                        kind = rec["record_type"]
                        new_hash.update(json.dumps([kind, rec.get("id")], separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
                        if kind == "message":
                            counts["messages_processed"] += 1
                            family = family_of(rec)
                            if family:
                                counts["media_candidates"] += 1
                                try:
                                    update = resolver.media(rec, family)
                                except MediaError as error:
                                    update = {"media_status": error.code}
                                except W.SyncError:
                                    update = {"media_status": "source_query_unavailable"}
                                rec.update(update)
                                available = bool(update.get("media_url"))
                                if not available:
                                    rec.pop("media_url", None)
                                state = update["media_status"]
                                status_counts[state] += 1
                                by_type[family]["candidates"] += 1
                                by_type[family]["available" if available else "unavailable"] += 1
                                counts["media_available"] += available
                                counts["thumbnail_messages"] += state == "available_thumbnail"
                                counts["first_frame_messages"] += state == "available_first_frame"
                        elif kind == "conversation":
                            counts["avatar_candidates"] += 1
                            try:
                                update = resolver.avatar(rec)
                            except (MediaError, W.SyncError):
                                update = {"avatar_status": "avatar_unavailable"}
                            rec.update(update)
                            if not update.get("avatar_url"):
                                rec.pop("avatar_url", None)
                            counts["avatars_available"] += bool(update.get("avatar_url"))
                        elif kind == "manifest":
                            # The standard exporter writes the manifest last. Reject
                            # other orderings rather than emit incomplete statistics.
                            if counts["messages_processed"] != message_count or counts["avatar_candidates"] != len(conversation_ids):
                                raise EnrichError("archive_manifest_not_last")
                            rec["media_content_available"] = bool(counts["media_available"])
                            report = {**dict(counts), "unique_cache_files_used": len(publisher.verified), "new_cache_files": publisher.created,
                                      "by_type": {k: dict(v) for k, v in by_type.items()}, "status_counts": dict(status_counts),
                                      "database_counts": dict(db_counts), "schema_counts": dict(resolver.schema_counts),
                                      "completed_at": W.utc_now(), "network_used": False, "asr_used": False}
                            rec["media_enrichment"] = report
                            old_notes = rec.get("notes", [])
                            notes = [n for n in old_notes if not (isinstance(n, str) and ("附件目前尚未接通" in n or n.startswith("本地附件补全：") or n.startswith("本地图片说明：")))]
                            notes.append("本地附件补全：" + str(counts["media_available"]) + " 条消息的附件已可打开，" + str(counts["media_candidates"] - counts["media_available"]) + " 条尚未取得可打开文件；已补全 " + str(counts["avatars_available"]) + " 个会话头像。未下载、已清理或暂不支持的附件仍保留原消息说明。")
                            if counts["thumbnail_messages"] or counts["first_frame_messages"]:
                                notes.append("本地图片说明：" + str(counts["thumbnail_messages"]) + " 条使用本机缩略图，" + str(counts["first_frame_messages"]) + " 条仅恢复首帧。")
                            rec["notes"] = notes
                        W.emit_record(out, rec)
                        tick()
                    if new_hash.digest() != original_hash.digest() or fingerprint(archive) != original_fingerprint:
                        raise EnrichError("archive_changed_or_identity_mismatch")
                    out.flush()
                    os.fsync(out.fileno())
                worker.close()
                stack.close()
                os.chmod(pending, 0o600)
                os.replace(pending, output)
            finally:
                pending.unlink(missing_ok=True)
    return {"status": "media_enriched", "message_count": message_count, "conversation_count": len(conversation_ids),
            "media_candidates": counts["media_candidates"], "media_available": counts["media_available"],
            "avatars_available": counts["avatars_available"], "by_type": {k: dict(v) for k, v in by_type.items()},
            "status_counts": dict(status_counts), "database_counts": dict(db_counts), "new_cache_files": publisher.created}


def main(argv=None):
    if "--image-worker" in (argv if argv is not None else sys.argv[1:]):
        return image_worker_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-root", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--self-id")
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--app-data", type=Path)
    parser.add_argument("--image-python")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--max-file-mb", type=int, default=1024)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if not 1 <= args.max_file_mb <= 16384:
            raise EnrichError("size_limit_invalid")
        report = enrich(args.archive, args.db_root, args.key_file, args.media_dir, output=args.output,
                        snapshot_root=args.snapshot_root, self_id=args.self_id, app_data=args.app_data,
                        image_python=args.image_python, ffmpeg=args.ffmpeg, max_file_bytes=args.max_file_mb * 1024 * 1024,
                        progress=lambda item: print(json.dumps(item, ensure_ascii=False), flush=True))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0
    except (EnrichError, W.SyncError) as error:
        print(json.dumps({"status": "media_enrichment_blocked", "code": error.code, "archive_replaced": False}), flush=True)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "media_enrichment_cancelled", "archive_replaced": False}), flush=True)
        return 130
    except Exception:
        # Never print native exception text, SQL, paths, XML or decoder output.
        print(json.dumps({"status": "media_enrichment_failed", "code": "unexpected_error", "archive_replaced": False}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
