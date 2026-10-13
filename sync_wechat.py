#!/usr/bin/env python3
"""Export an authorized local WeChat 4.x account to a private JSONL archive.

This program does not acquire keys, attach to a process, alter an application,
read Keychain, or contact a network service. Input databases are copied together
with their WAL files into a private temporary directory. Only a stable copy that
passes SQLite quick_check is read. Publication is atomic and happens after every
message-table row count has been reconciled. A failed run keeps the old output.

Run with a Python interpreter that has the optional SQLCipher dependencies:
  python3 sync_wechat.py \
      --db-root /path/to/account/db_storage --key-file /private/keys.json \
      --self-id YOUR_CONFIRMED_USERNAME --output /private/archive.jsonl

Keys: JSON {"keys": {"message/message_0.db": "64 hexadecimal characters"}}.
Absolute source paths, basenames, default/*, __default_key and salt_keys maps
are also accepted. Values may be {"key": "...", "cipher_compatibility": 4}.
Only existing authorized keys are used. SQLCipher DB-API drivers are preferred;
the Homebrew SQLCipher shared library is an optional fallback. Plain SQLite
snapshots are supported as inputs but still require an explicit key manifest
(an empty object is sufficient when every selected database is plaintext).

The JSONL discriminator is record_type. Message timestamp is Unix seconds;
is_self is null when the own-account identity is not known reliably. A complete
manifest establishes row coverage, not full multimedia decoding. No raw XML,
attachment encryption keys, or signed download URLs are included in the output.
"""

from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import ctypes
import ctypes.util
import datetime as dt
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Any, Iterator
import xml.etree.ElementTree as ET


EXPORT_SCHEMA_VERSION = 1
MAX_CONTENT_BYTES = 32 * 1024 * 1024
MESSAGE_DATABASE = re.compile(r"^(?:biz_)?message_\d+\.db$", re.IGNORECASE)
HEX_KEY = re.compile(r"^(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{96})$")
SQLITE_HEADER = b"SQLite format 3\x00"
ZSTD_HEADER = b"\x28\xb5\x2f\xfd"


class SyncError(Exception):
    """A diagnostic safe to print without source content or key material."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(code)


def fail(code: str, message: str) -> None:
    raise SyncError(code, message)


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def ordered_identity(value: Any) -> str:
    """Fixed-width signed-integer encoding keeps same-second IDs ordered."""
    number = integer(value)
    if number is not None and -(1 << 63) <= number < (1 << 63):
        return f"{number + (1 << 63):020d}"
    return string(value)


def text_factory(value: bytes) -> str | bytes:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        # Some WCDB builds store compressed bytes with SQLite's TEXT affinity.
        return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_keys(path: Path) -> dict[str, Any]:
    if not path.is_file():
        fail("key_file_missing", "没有可用的本机密钥文件；没有导出聊天，也没有覆盖已有结果。")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        fail("key_file_permissions", "密钥文件应仅允许本人读写（权限 0600）；本次未更改文件权限。")
    if path.stat().st_size > 16 * 1024 * 1024:
        fail("invalid_key_file", "密钥配置文件过大，无法安全识别。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("invalid_key_file", "无法识别本机密钥文件格式。")
    if not isinstance(value, dict):
        fail("invalid_key_file", "本机密钥文件必须是 JSON 对象。")
    return value


def key_spec(keys: dict[str, Any], source: Path, root: Path, header: bytes) -> dict[str, Any]:
    if header == SQLITE_HEADER:
        return {}
    relative = source.relative_to(root).as_posix()
    values = keys.get("keys", keys)
    if not isinstance(values, dict):
        values = {}
    value: Any = None
    for candidate in (relative, "db_storage/" + relative, str(source), str(source.resolve()), source.name, "*", "default"):
        if values.get(candidate):
            value = values[candidate]
            break
    if value is None:
        value = keys.get("__default_key") or keys.get("default_key")
    if value is None:
        salt_values = keys.get("salt_keys")
        if not isinstance(salt_values, dict) and integer(keys.get("schema_version")) in (2, 3):
            salt_values = values
        if isinstance(salt_values, dict):
            value = salt_values.get(header.hex())
    if isinstance(value, str):
        result = {"key": value.strip()}
    elif isinstance(value, dict):
        result = {**value, "key": string(value.get("key") or value.get("enc_key") or value.get("raw_key")).strip()}
    else:
        fail("database_key_missing", "所选账号至少有一个数据库缺少已验证的密钥；未发布新导出。")
    if not HEX_KEY.fullmatch(result["key"]):
        fail("invalid_database_key", "数据库密钥必须是 64 或 96 位十六进制 raw key。")
    if len(result["key"]) == 96 and result["key"][64:].lower() != header.hex():
        fail("database_key_salt_mismatch", "密钥中的数据库盐值与所选文件不一致；未发布新导出。")
    clean: dict[str, Any] = {"key": result["key"], "cipher_compatibility": 4}
    ranges = {"cipher_compatibility": (1, 4), "cipher_page_size": (512, 65536), "kdf_iter": (1, 100000000), "cipher_plaintext_header_size": (0, 4096)}
    for name, bounds in ranges.items():
        if name in result:
            number = integer(result[name])
            if number is None or not bounds[0] <= number <= bounds[1]:
                fail("invalid_cipher_parameters", "密钥文件包含无效的数据库加密参数。")
            if name == "cipher_page_size" and number & (number - 1):
                fail("invalid_cipher_parameters", "数据库页大小不是有效的 2 的幂。")
            if name == "cipher_plaintext_header_size" and number % 16:
                fail("invalid_cipher_parameters", "数据库明文头大小必须是 16 的倍数。")
            clean[name] = number
    if "cipher_use_hmac" in result:
        if result["cipher_use_hmac"] not in (True, 1, "1"):
            fail("hmac_required", "此导入器要求保留数据库 HMAC 完整性验证。")
        clean["cipher_use_hmac"] = 1
    for name, allowed in {
        "cipher_hmac_algorithm": {"HMAC_SHA1", "HMAC_SHA256", "HMAC_SHA512"},
        "cipher_kdf_algorithm": {"PBKDF2_HMAC_SHA1", "PBKDF2_HMAC_SHA256", "PBKDF2_HMAC_SHA512"},
    }.items():
        if name in result:
            value = string(result[name]).upper()
            if value not in allowed:
                fail("invalid_cipher_parameters", "数据库密钥文件包含不支持的加密算法。")
            clean[name] = value
    return clean


class CDatabase:
    """Small read-only SQLCipher C-API fallback; no executable subprocesses."""

    def __init__(self, path: Path, spec: dict[str, Any]):
        candidates = [os.environ.get("SQLCIPHER_LIBRARY"), "/opt/homebrew/opt/sqlcipher/lib/libsqlcipher.dylib", "/usr/local/opt/sqlcipher/lib/libsqlcipher.dylib", ctypes.util.find_library("sqlcipher")]
        self.lib: Any = None
        self.handle = ctypes.c_void_p()
        for name in candidates:
            if not name:
                continue
            try:
                self.lib = ctypes.CDLL(name)
                if not hasattr(self.lib, "sqlite3_key"):
                    self.lib = None
                    continue
                break
            except OSError:
                pass
        if self.lib is None:
            fail("sqlcipher_missing", "缺少 SQLCipher。请安装可选导入依赖，或通过 SQLCIPHER_LIBRARY 指定本机 SQLCipher 库。")
        declarations = {
            "sqlite3_open_v2": ([ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_char_p], ctypes.c_int),
            "sqlite3_close": ([ctypes.c_void_p], ctypes.c_int),
            "sqlite3_prepare_v2": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_char_p)], ctypes.c_int),
            "sqlite3_step": ([ctypes.c_void_p], ctypes.c_int),
            "sqlite3_finalize": ([ctypes.c_void_p], ctypes.c_int),
            "sqlite3_column_count": ([ctypes.c_void_p], ctypes.c_int),
            "sqlite3_column_name": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_char_p),
            "sqlite3_column_type": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            "sqlite3_column_int64": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int64),
            "sqlite3_column_double": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_double),
            "sqlite3_column_blob": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_void_p),
            "sqlite3_column_text": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_void_p),
            "sqlite3_column_bytes": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result
        rc = self.lib.sqlite3_open_v2((path.resolve().as_uri() + "?mode=ro").encode(), ctypes.byref(self.handle), 0x00000001 | 0x00000040, None)
        if rc:
            self.close()
            fail("database_open_failed", "数据库副本无法只读打开。")
        try:
            self.apply_spec(spec)
        except BaseException:
            self.close()
            raise

    def apply_spec(self, spec: dict[str, Any]) -> None:
        for statement in cipher_pragmas(spec):
            list(self.rows(statement))

    def rows(self, sql: str) -> Iterator[dict[str, Any]]:
        statement = ctypes.c_void_p()
        if self.lib.sqlite3_prepare_v2(self.handle, sql.encode(), -1, ctypes.byref(statement), None):
            if statement.value:
                self.lib.sqlite3_finalize(statement)
            fail("database_query_failed", "数据库副本查询失败，可能是密钥或版本不兼容。")
        try:
            count = self.lib.sqlite3_column_count(statement)
            names = [self.lib.sqlite3_column_name(statement, i).decode() for i in range(count)]
            while True:
                code = self.lib.sqlite3_step(statement)
                if code == 101:
                    return
                if code != 100:
                    fail("database_query_failed", "数据库副本读取失败，未发布不完整导出。")
                row: dict[str, Any] = {}
                for i, name in enumerate(names):
                    kind = self.lib.sqlite3_column_type(statement, i)
                    if kind == 1:
                        value: Any = self.lib.sqlite3_column_int64(statement, i)
                    elif kind == 2:
                        value = self.lib.sqlite3_column_double(statement, i)
                    elif kind == 5:
                        value = None
                    else:
                        size = self.lib.sqlite3_column_bytes(statement, i)
                        pointer = (self.lib.sqlite3_column_text if kind == 3 else self.lib.sqlite3_column_blob)(statement, i)
                        raw = ctypes.string_at(pointer, size) if size else b""
                        value = text_factory(raw) if kind == 3 else raw
                    row[name] = value
                yield row
        finally:
            self.lib.sqlite3_finalize(statement)

    def close(self) -> None:
        if self.handle.value and self.lib is not None:
            self.lib.sqlite3_close(self.handle)
            self.handle = ctypes.c_void_p()


def cipher_pragmas(spec: dict[str, Any]) -> list[str]:
    statements = []
    if spec:
        statements.append("PRAGMA key=\"x'" + spec["key"] + "'\"")
        for name, value in spec.items():
            if name != "key":
                statements.append(f"PRAGMA {name}={value}")
    statements.extend(["PRAGMA trusted_schema=OFF", "PRAGMA temp_store=MEMORY", "PRAGMA query_only=ON"])
    return statements


class Database:
    def __init__(self, path: Path, spec: dict[str, Any]):
        self.native: Any = None
        self.c_database: CDatabase | None = None
        module: Any = sqlite3
        if spec:
            module = None
            for name in ("sqlcipher3.dbapi2", "pysqlcipher3.dbapi2"):
                try:
                    module = importlib.import_module(name)
                    break
                except ImportError:
                    pass
        try:
            if module is None:
                self.c_database = CDatabase(path, spec)
            else:
                self.native = module.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                self.native.text_factory = text_factory
                for statement in cipher_pragmas(spec):
                    self.native.execute(statement)
            self.one("SELECT count(*) AS n FROM sqlite_master")
        except SyncError:
            self.close()
            raise
        except Exception:
            self.close()
            fail("database_open_failed", "数据库副本无法解密或只读打开；已有导出保持不变。")

    def rows(self, sql: str) -> Iterator[dict[str, Any]]:
        if self.c_database:
            yield from self.c_database.rows(sql)
            return
        cursor = None
        try:
            cursor = self.native.execute(sql)
            names = [description[0] for description in (cursor.description or [])]
            for values in cursor:
                yield dict(zip(names, values))
        except Exception:
            fail("database_query_failed", "数据库副本读取失败，未发布不完整导出。")
        finally:
            if cursor is not None:
                cursor.close()

    def one(self, sql: str) -> dict[str, Any]:
        result = self.rows(sql)
        try:
            return next(result, {})
        finally:
            result.close()

    def verify(self) -> None:
        result = list(self.rows("PRAGMA quick_check"))
        if len(result) != 1 or list(result[0].values()) != ["ok"]:
            fail("snapshot_integrity_failed", "数据库副本未通过完整性检查，未发布新导出。")
        query_only = self.one("PRAGMA query_only")
        if not query_only or integer(next(iter(query_only.values()))) != 1:
            fail("readonly_verification_failed", "数据库未能保持只读状态，导入已停止。")

    def tables(self) -> list[dict[str, Any]]:
        return list(self.rows("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"))

    def columns(self, table: str) -> list[dict[str, Any]]:
        return list(self.rows("PRAGMA table_info(" + quote_identifier(table) + ")"))

    def close(self) -> None:
        if self.native is not None:
            self.native.close()
            self.native = None
        if self.c_database is not None:
            self.c_database.close()
            self.c_database = None


def discover(root: Path) -> list[Path]:
    if not root.is_dir():
        fail("database_root_missing", "所选数据库目录不存在。")
    result = []
    for path in root.rglob("*.db"):
        if path.is_symlink():
            fail("source_symlink_rejected", "所选数据库目录含数据库软链接，请明确选择真实文件目录。")
        if path.is_file() and (MESSAGE_DATABASE.fullmatch(path.name) or path.relative_to(root).as_posix() in {"contact/contact.db", "session/session.db"}):
            result.append(path)
    result.sort(key=lambda path: path.relative_to(root).as_posix())
    if not any(MESSAGE_DATABASE.fullmatch(path.name) for path in result):
        fail("message_databases_missing", "没有找到 WeChat 4.x 的 message_N.db 或 biz_message_N.db；未创建空导出。")
    return result


def fingerprints(root: Path, sources: list[Path]) -> dict[str, tuple[int, ...]]:
    result = {}
    for source in sources:
        for path in (source, Path(str(source) + "-wal")):
            try:
                info = path.stat()
            except FileNotFoundError:
                if path == source:
                    fail("snapshot_source_changed", "复制时数据库文件发生变化，请稍后再同步。")
                continue
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                fail("source_file_invalid", "数据库或 WAL 不是可读取的普通文件。")
            result[path.relative_to(root).as_posix()] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return result


@contextlib.contextmanager
def snapshot(root: Path, attempts: int) -> Iterator[tuple[Path, list[Path], dict[str, Any]]]:
    """Require a stable DB+WAL file set throughout a complete copy window.

    SQLite rebuilds SHM only in the private copy. Copying a live SHM would not
    create a transaction snapshot: it contains volatile reader/lock state.
    Source files are never opened through SQLite and never checkpointed.
    """
    for attempt in range(attempts):
        sources = discover(root)
        before = fingerprints(root, sources)
        with tempfile.TemporaryDirectory(prefix="wechat-readonly-snapshot-") as temporary:
            destination = Path(temporary)
            byte_count = 0
            try:
                for relative, expected in before.items():
                    source = root / relative
                    target = destination / relative
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with source.open("rb") as inp, target.open("xb") as out:
                        os.chmod(target, 0o600)
                        initial = os.fstat(inp.fileno())
                        initial_fingerprint = (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
                        if initial_fingerprint != expected:
                            fail("snapshot_source_changed", "复制时数据库文件发生变化，请稍后再同步。")
                        while True:
                            data = inp.read(1024 * 1024)
                            if not data:
                                break
                            out.write(data)
                            byte_count += len(data)
                        final = os.fstat(inp.fileno())
                        final_fingerprint = (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
                        if final_fingerprint != expected or out.tell() != expected[2]:
                            fail("snapshot_source_changed", "复制时数据库文件发生变化，请稍后再同步。")
                after_sources = discover(root)
                if after_sources != sources or fingerprints(root, sources) != before:
                    fail("snapshot_source_changed", "复制时数据库或 WAL 发生变化，请稍后再同步。")
            except (FileNotFoundError, SyncError) as error:
                retryable = isinstance(error, FileNotFoundError) or error.code == "snapshot_source_changed"
                if retryable and attempt + 1 < attempts:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                if isinstance(error, SyncError):
                    raise
                fail("snapshot_source_changed", "复制时数据库或 WAL 发生变化，未发布新导出。")
            yield destination, sources, {"method": "stable_db_and_wal_copy_then_quick_check", "attempt": attempt + 1, "files": len(before), "bytes": byte_count, "wal_files": sum(name.endswith("-wal") for name in before), "source_mutated_by_importer": False}
            return
    fail("snapshot_source_changed", "数据库持续变化，无法生成可靠副本；请稍后再同步。")


def decompress(value: Any, compression_type: Any) -> tuple[str, str]:
    if value is None:
        return "", "content_missing"
    if isinstance(value, str):
        if len(value.encode("utf-8", errors="replace")) > MAX_CONTENT_BYTES:
            return "[消息内容超过解析大小上限]", "content_too_large"
        return value, "ok"
    if not isinstance(value, bytes):
        return string(value), "ok"
    if len(value) > MAX_CONTENT_BYTES:
        return "[消息内容超过解析大小上限]", "content_too_large"
    if integer(compression_type) == 4 or value.startswith(ZSTD_HEADER):
        try:
            zstandard = importlib.import_module("zstandard")
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(value)) as reader:
                value = reader.read(MAX_CONTENT_BYTES + 1)
        except ImportError:
            try:
                zstd = importlib.import_module("compression.zstd")
                decoder = zstd.ZstdDecompressor()
                value = decoder.decompress(value, max_length=MAX_CONTENT_BYTES + 1)
                if not decoder.eof and len(value) <= MAX_CONTENT_BYTES:
                    return "[压缩消息尚未解码]", "decompression_failed"
            except ImportError:
                return "[压缩消息需要 Zstandard 解析依赖]", "decompressor_missing"
            except Exception:
                return "[压缩消息尚未解码]", "decompression_failed"
        except Exception:
            return "[压缩消息尚未解码]", "decompression_failed"
        if len(value) > MAX_CONTENT_BYTES:
            return "[消息内容超过解析大小上限]", "content_too_large"
    try:
        return value.decode("utf-8"), "ok"
    except UnicodeDecodeError:
        return "[二进制消息内容尚未解析]", "binary_content_unsupported"


XML_INVALID_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
XML_NUMERIC_REFERENCE = re.compile(r"&#(?:x[0-9A-Fa-f]+|[0-9]+);")
XML_MARKUP = re.compile(r"<!\[CDATA\[[\s\S]*?\]\]>|<!--[\s\S]*?-->|<\?[\s\S]*?\?>|<[^<>]*>")
XML_ATTRIBUTE_STRING = re.compile(r"""(["'])(.*?)\1""", re.DOTALL)
XML_MISPLACED_DECLARATION = re.compile(
    r"""\A(<msg>[ \t\r\n]*)<\?xml[ \t\r\n]+version[ \t\r\n]*=[ \t\r\n]*(?:"1\.0"|'1\.0')"""
    r"""(?:[ \t\r\n]+encoding[ \t\r\n]*=[ \t\r\n]*(?:"[A-Za-z][A-Za-z0-9._-]*"|'[A-Za-z][A-Za-z0-9._-]*'))?"""
    r"""(?:[ \t\r\n]+standalone[ \t\r\n]*=[ \t\r\n]*(?:"(?:yes|no)"|'(?:yes|no)'))?[ \t\r\n]*\?>"""
)
MAX_NESTED_MESSAGES = 4
MAX_FORWARDED_ITEMS = 1000


def looks_like_xml(content: str) -> bool:
    return content.lstrip("\ufeff \t\r\n").startswith("<")


def repair_xml(content: str) -> tuple[str, Counter[str]]:
    """Repair only three observed serialization defects, preserving visible text.

    Literal illegal characters become U+FFFD. Numeric references are repaired
    only in text and attribute values, never in CDATA, comments or processing
    instructions. Other malformed XML must still fail strict parsing.
    """
    counts: Counter[str] = Counter()
    content, number = XML_INVALID_CHAR.subn("\ufffd", content)
    if number:
        counts["invalid_literal_character"] += number
    content, number = XML_MISPLACED_DECLARATION.subn(r"\1", content, count=1)
    if number:
        counts["misplaced_xml_declaration"] += number

    def replace_reference(match: re.Match[str]) -> str:
        token = match.group()
        hexadecimal = token.startswith("&#x")
        digits = token[3:-1] if hexadecimal else token[2:-1]
        digits = digits.lstrip("0") or "0"
        # Reject oversized values without converting an unbounded integer.
        number = int(digits, 16 if hexadecimal else 10) if len(digits) <= (6 if hexadecimal else 7) else 0x110000
        if number in (9, 10, 13) or 0x20 <= number <= 0xD7FF or 0xE000 <= number <= 0xFFFD or 0x10000 <= number <= 0x10FFFF:
            return token
        counts["invalid_numeric_reference"] += 1
        return "\ufffd"

    def repair_references(text: str) -> str:
        return XML_NUMERIC_REFERENCE.sub(replace_reference, text)

    pieces = []
    previous = 0
    for match in XML_MARKUP.finditer(content):
        pieces.append(repair_references(content[previous:match.start()]))
        token = match.group()
        if not token.startswith(("<!", "<?")):
            token = XML_ATTRIBUTE_STRING.sub(
                lambda attribute: attribute.group(1) + repair_references(attribute.group(2)) + attribute.group(1),
                token,
            )
        pieces.append(token)
        previous = match.end()
    pieces.append(repair_references(content[previous:]))
    return "".join(pieces), counts


def xml_root(content: str, repairs: Counter[str] | None = None) -> ET.Element | None:
    if not looks_like_xml(content):
        return None
    # No DTD or entity expansion is needed for a chat viewer.
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        return None
    try:
        return ET.fromstring(content)
    except ET.ParseError as error:
        if error.code not in (4, 14, 17):
            return None
    except ValueError:
        pass
    repaired, counts = repair_xml(content)
    if not counts:
        return None
    try:
        root = ET.fromstring(repaired)
    except (ET.ParseError, ValueError):
        return None
    if repairs is not None:
        repairs.update(counts)
    return root


def app_message(root: ET.Element | None) -> ET.Element | None:
    if root is None:
        return None
    if root.tag == "appmsg":
        return root
    return root.find("appmsg") if root.tag == "msg" else None


def xml_text(element: ET.Element | None, path: str, repairs: Counter[str] | None = None, depth: int = 0) -> str:
    if element is None:
        return ""
    found = element.find(path)
    if found is None:
        return ""
    # Only explicitly selected human-facing elements, never arbitrary XML.
    if len(found):
        return "[嵌套文本内容暂未解析]"
    value = (found.text or "").strip()
    if looks_like_xml(value):
        if depth >= MAX_NESTED_MESSAGES:
            return "[引用内容层级超过解析上限]"
        nested = xml_root(value, repairs)
        if nested is None:
            return "[引用内容暂未解析]"
        app = app_message(nested)
        if app is None:
            return "[引用的媒体或未支持消息]"
        safe = [xml_text(app, p, repairs, depth + 1) for p in ("title", "des")]
        return "\n".join(item for item in safe if item) or "[引用的媒体消息]"
    return value


def forwarded_text(node: ET.Element | None, repairs: Counter[str], depth: int = 0) -> tuple[str, str]:
    """Expand only known recordinfo fields; leave every other item explicit."""
    if node is None:
        return "[转发记录正文未保留]", "metadata_only"
    if depth >= MAX_NESTED_MESSAGES:
        return "[嵌套转发层级超过解析上限]", "metadata_only"
    root = node.find("recordinfo") if len(node) else xml_root(node.text or "", repairs)
    if root is None or root.tag != "recordinfo":
        return "[转发记录正文暂未解析]", "metadata_only"
    items = root.findall("datalist/dataitem")
    if not items:
        return "[转发记录正文未保留]", "metadata_only"
    blocks = []
    complete = True
    labels = {2: "图片", 3: "语音", 4: "视频", 6: "位置", 8: "文件", 37: "表情"}
    for item in items[:MAX_FORWARDED_ITEMS]:
        kind = integer(item.attrib.get("datatype"))
        heading = " · ".join(value for value in (xml_text(item, "sourcename", repairs), xml_text(item, "sourcetime", repairs)) if value)
        if kind == 1:
            description = item.find("datadesc")
            if description is None or len(description):
                body, complete = "[转发文本暂未解析]", False
            else:
                # This known datatype's explicit text field may contain literal
                # angle brackets typed by the sender; it is rendered as text.
                body = description.text or ""
            if item.find("refermsgitem") is not None:
                body += "\n[此条引用内容暂未展开]"
                complete = False
        elif kind == 17:
            body, nested_status = forwarded_text(item.find("recordxml"), repairs, depth + 1)
            body = "[嵌套转发]\n" + body
            complete = complete and nested_status == "ok"
        else:
            body = "[" + labels.get(kind, "未支持的转发条目类型 " + str(kind)) + "]"
            if kind == 8:
                title = xml_text(item, "datatitle", repairs)
                if title:
                    body += " " + title
            complete = False
        blocks.append((heading + "\n" if heading else "") + body)
    if len(items) > MAX_FORWARDED_ITEMS:
        blocks.append(f"[另有 {len(items) - MAX_FORWARDED_ITEMS} 条转发内容超过展开上限]")
        complete = False
    return "\n\n".join(blocks), "ok" if complete else "metadata_only"


def message_content(raw_type: int | None, content: str, status: str) -> dict[str, Any]:
    repairs: Counter[str] = Counter()
    result = _message_content(raw_type, content, status, repairs)
    if repairs:
        result["xml_repaired"] = True
        result["xml_repair_counts"] = dict(repairs)
        # Media/unsupported status remains independent of serialization repair.
        if result["decode_status"] == "ok":
            result["decode_status"] = "xml_repaired"
    return result


def _message_content(raw_type: int | None, content: str, status: str, repairs: Counter[str], depth: int = 0) -> dict[str, Any]:
    base = (raw_type or 0) & 0xFFFFFFFF
    result: dict[str, Any] = {"type": "unsupported", "text": content, "decode_status": status}
    if status not in {"ok", "content_missing"}:
        return result
    if base == 1:
        result.update(type="text")
        return result
    names = {3: ("image", "图片"), 34: ("voice", "语音"), 43: ("video", "视频"), 62: ("video", "视频"), 47: ("sticker", "表情"), 48: ("location", "位置"), 50: ("call", "通话"), 42: ("contact", "联系人名片"), 37: ("friend_request", "好友申请")}
    root = xml_root(content, repairs)
    if base in names:
        kind, label = names[base]
        result.update(type=kind, text="[" + label + "]", decode_status="metadata_only")
        if base == 34 and root is not None:
            node = root.find(".//voicemsg")
            if node is not None:
                duration = integer(node.attrib.get("voicelength"))
                if duration is not None and 0 <= duration <= 24 * 3600 * 1000:
                    result["text"] += f" {duration / 1000:g} 秒"
        elif base in (43, 62) and root is not None:
            node = root.find(".//videomsg")
            if node is not None:
                duration = integer(node.attrib.get("playlength"))
                if duration is not None and 0 <= duration <= 24 * 3600:
                    result["text"] += f" {duration} 秒"
        elif base == 48 and root is not None:
            node = root.find(".//location")
            if node is not None:
                label = node.attrib.get("poiname") or node.attrib.get("label")
                if label:
                    result["text"] += " " + label
        return result
    if base in (10000, 10002):
        if root is None and not looks_like_xml(content):
            result.update(type="system", text=content, decode_status="ok")
        else:
            safe = xml_text(root, ".//replacemsg", repairs) or xml_text(root, ".//plain", repairs) or xml_text(root, ".//content", repairs)
            result.update(type="system", text=safe or "[系统消息]", decode_status="ok" if safe else "metadata_only")
        return result
    if base == 49:
        if root is None:
            result.update(type="card", text="[分享消息内容暂未解析]", decode_status="xml_parse_failed")
            return result
        app = app_message(root)
        if app is None:
            result.update(type="card", text="[分享消息内容暂未解析]", decode_status="xml_parse_failed")
            return result
        subtype = integer(xml_text(app, "type", repairs))
        if subtype is None and raw_type is not None:
            subtype = raw_type >> 32
        title = xml_text(app, "title", repairs)
        description = xml_text(app, "des", repairs)
        if subtype == 6:
            result.update(type="file", text="[文件]" + (" " + title if title else ""), file_name=title or None, decode_status="metadata_only")
        elif subtype == 57:
            author = xml_text(app, "refermsg/displayname", repairs)
            reference = app.find("refermsg/content")
            reference_type = integer(xml_text(app, "refermsg/type", repairs))
            quoted_status = "metadata_only"
            if reference is not None and not len(reference) and reference_type is not None and depth < MAX_NESTED_MESSAGES:
                nested = _message_content(reference_type, reference.text or "", "ok", repairs, depth + 1)
                quoted, quoted_status = nested["text"], nested["decode_status"]
            elif reference is not None and not len(reference) and not looks_like_xml(reference.text or ""):
                quoted, quoted_status = reference.text or "", "ok"
            else:
                quoted = xml_text(app, "refermsg/content", repairs)
            quote = "引用" + (" " + author if author else "") + ("：" + quoted if quoted else " [消息内容未保留]")
            result.update(type="quote", text=(title + "\n\n" if title else "") + quote, decode_status="ok" if quoted and quoted_status == "ok" else "metadata_only")
        elif subtype in (2000, 2001):
            result.update(type="transfer" if subtype == 2000 else "red_packet", text=("[转账]" if subtype == 2000 else "[红包]") + (" " + title if title else ""), decode_status="metadata_only")
        elif subtype == 19:
            body, body_status = forwarded_text(app.find("recorditem"), repairs, depth)
            result.update(type="forwarded", text="[合并转发的聊天记录]" + (" " + title if title else "") + "\n\n" + body, decode_status=body_status)
        elif subtype in (33, 36):
            result.update(type="mini_program", text="[小程序]" + (" " + title if title else ""), decode_status="metadata_only")
        else:
            result.update(type="link" if subtype == 5 else "card", text="\n".join(text for text in (title, description) if text) or "[分享卡片]", decode_status="metadata_only")
        return result
    if looks_like_xml(content):
        result.update(text="[暂未支持的消息类型]", decode_status="unsupported_type")
    else:
        result.update(text=content or "[暂未支持的消息类型]", decode_status="unsupported_type")
    return result


def load_contacts(database: Database | None) -> dict[str, dict[str, Any]]:
    if database is None:
        return {}
    tables = {string(row["name"]) for row in database.tables()}
    if "contact" not in tables:
        fail("contact_schema_incompatible", "联系人数据库表结构尚不兼容；未发布新导出。")
    columns = {string(row["name"]) for row in database.columns("contact")}
    if "username" not in columns:
        fail("contact_schema_incompatible", "联系人数据库缺少身份字段；未发布新导出。")
    selected = [name for name in ("username", "remark", "nick_name", "alias") if name in columns]
    result = {}
    for row in database.rows("SELECT " + ",".join(map(quote_identifier, selected)) + " FROM contact"):
        username = string(row.get("username"))
        if username:
            result[username] = {"id": username, "name": string(row.get("remark") or row.get("nick_name") or row.get("alias") or username)}
    return result


def load_sessions(database: Database | None) -> dict[str, dict[str, Any]]:
    if database is None:
        return {}
    tables = {string(row["name"]) for row in database.tables()}
    if "SessionTable" not in tables:
        fail("session_schema_incompatible", "会话数据库表结构尚不兼容；未发布新导出。")
    columns = {string(row["name"]) for row in database.columns("SessionTable")}
    if "username" not in columns:
        fail("session_schema_incompatible", "会话数据库缺少身份字段；未发布新导出。")
    selected = [name for name in ("username", "last_timestamp", "sort_timestamp") if name in columns]
    result = {}
    for row in database.rows("SELECT " + ",".join(map(quote_identifier, selected)) + " FROM SessionTable"):
        username = string(row.get("username"))
        if username:
            result[username] = {"last_message_at": integer(row.get("last_timestamp"))}
    return result


def emit_record(handle: Any, record: dict[str, Any]) -> None:
    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def sync(db_root: Path, key_file: Path, output: Path, self_id: str | None = None, attempts: int = 3, source_version: str | None = None) -> dict[str, Any]:
    """Return only a safe summary; all chat records go exclusively to output."""
    root = db_root.expanduser().resolve()
    keys = load_keys(key_file.expanduser())
    output = output.expanduser().absolute()
    if output.is_symlink() or any(path.is_symlink() for path in output.parents):
        fail("output_symlink_rejected", "导出路径含软链接，请选择独立的本机普通目录。")
    if output == key_file.expanduser().absolute() or root == output or root in output.parents:
        fail("output_conflicts_with_source", "导出文件必须位于微信原始数据和密钥文件以外的位置。")
    started = utc_now()
    with snapshot(root, attempts) as (copy_root, sources, snapshot_info), contextlib.ExitStack() as stack:
        databases: dict[str, Database] = {}
        encrypted_count = 0
        # Verify every selected database before opening an output stream.
        for source in sources:
            relative = source.relative_to(root).as_posix()
            copied = copy_root / relative
            with copied.open("rb") as handle:
                header = handle.read(16)
            spec = key_spec(keys, source, root, header)
            encrypted_count += bool(spec)
            database = Database(copied, spec)
            stack.callback(database.close)
            database.verify()
            databases[relative] = database

        contacts = load_contacts(databases.get("contact/contact.db"))
        sessions = load_sessions(databases.get("session/session.db"))
        names = set(contacts) | set(sessions)
        source_tables: list[dict[str, Any]] = []
        senders_by_db: dict[str, dict[int, str]] = {}
        for relative, database in databases.items():
            if not MESSAGE_DATABASE.fullmatch(Path(relative).name):
                continue
            tables = database.tables()
            senders = {}
            name_table = next((table for table in tables if string(table["name"]) == "Name2Id"), None)
            if name_table is not None:
                cols = {string(row["name"]) for row in database.columns("Name2Id")}
                name_id = "rowid" if "WITHOUT ROWID" not in string(name_table.get("sql")).upper() else next((name for name in ("id", "user_id") if name in cols), None)
                if "user_name" in cols and name_id:
                    for row in database.rows("SELECT " + quote_identifier(name_id) + ' AS internal_id, user_name FROM "Name2Id"'):
                        name = string(row.get("user_name"))
                        index = integer(row.get("internal_id"))
                        if name and index is not None:
                            senders[index] = name
                            names.add(name)
            senders_by_db[relative] = senders
            for table in tables:
                name = string(table["name"])
                if not name.startswith("Msg_"):
                    continue
                count = int(database.one("SELECT count(*) AS n FROM " + quote_identifier(name))["n"])
                source_tables.append({"database": relative, "table": name, "expected_count": count, "exported_count": 0, "without_rowid": "WITHOUT ROWID" in string(table.get("sql")).upper()})
        if not source_tables:
            fail("message_tables_missing", "消息数据库内没有可识别的 Msg_ 会话表；未创建空导出。")

        hashes: dict[str, set[str]] = {}
        for name in names:
            hashes.setdefault(hashlib.md5(name.encode("utf-8")).hexdigest(), set()).add(name)
        identity_status = "explicit" if self_id else "unknown"
        if not self_id:
            configured_self = keys.get("self_id") or keys.get("self_username")
            if isinstance(configured_self, str) and configured_self in names:
                self_id, identity_status = configured_self, "key_manifest_name_match"
            elif root.parent.name in names:
                self_id, identity_status = root.parent.name, "exact_account_directory_name_match"

        conversations: dict[str, dict[str, Any]] = {}
        for username, session in sessions.items():
            conversations[username] = {"record_type": "conversation", "id": username, "name": contacts.get(username, {}).get("name", username), "is_group": username.endswith("@chatroom"), "last_message": "", "last_message_at": session.get("last_message_at"), "message_count": 0}
        unknown_tables = 0
        for table in source_tables:
            candidates = hashes.get(table["table"][4:].lower(), set())
            if len(candidates) == 1:
                conversation_id = next(iter(candidates))
                table["identity_status"] = "mapped"
            else:
                conversation_id = "opaque:" + hashlib.sha256((table["database"] + "/" + table["table"]).encode()).hexdigest()[:24]
                table["identity_status"] = "unmapped" if not candidates else "ambiguous"
                unknown_tables += 1
            table["conversation_id"] = conversation_id
            conversations.setdefault(conversation_id, {"record_type": "conversation", "id": conversation_id, "name": contacts.get(conversation_id, {}).get("name") or ("未识别的会话 " + conversation_id[-8:] if conversation_id.startswith("opaque:") else conversation_id), "is_group": conversation_id.endswith("@chatroom"), "last_message": "", "last_message_at": None, "message_count": 0})

        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".pending", dir=output.parent)
        pending = Path(temporary_name)
        os.fchmod(fd, 0o600)
        total = 0
        statuses: Counter[str] = Counter()
        types: Counter[str] = Counter()
        xml_repair_counts: Counter[str] = Counter()
        xml_repaired_messages = 0
        unknown_sender_count = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                for table in source_tables:
                    database = databases[table["database"]]
                    name = table["table"]
                    column_info = database.columns(name)
                    columns = {string(row["name"]) for row in column_info}
                    fields = [field for field in ("local_id", "server_id", "sort_seq", "local_type", "real_sender_id", "create_time", "message_content", "compress_content", "WCDB_CT_message_content") if field in columns]
                    pk = [string(row["name"]) for row in sorted(column_info, key=lambda row: integer(row.get("pk")) or 0) if integer(row.get("pk"))]
                    if table["without_rowid"]:
                        if not pk:
                            fail("message_identity_missing", "某个消息表无法建立稳定的记录标识，未发布新导出。")
                        fields.extend(field for field in pk if field not in fields)
                        query = "SELECT " + ",".join(map(quote_identifier, fields)) + " FROM " + quote_identifier(name)
                    else:
                        query = 'SELECT rowid AS "__source_rowid"' + ("," + ",".join(map(quote_identifier, fields)) if fields else "") + " FROM " + quote_identifier(name)
                    conversation_id = table["conversation_id"]
                    conversation = conversations[conversation_id]
                    senders = senders_by_db[table["database"]]
                    for row in database.rows(query):
                        row_identity = (json.dumps([string(row.get(field)) for field in pk], ensure_ascii=False, separators=(",", ":")) if table["without_rowid"] else ordered_identity(row.get("__source_rowid")))
                        local_id = string(row.get("local_id"))
                        message_id = table["database"] + ":" + name + ":" + ordered_identity(row.get("local_id")) + ":" + row_identity
                        sender_id = senders.get(integer(row.get("real_sender_id")) or 0)
                        raw = row.get("message_content")
                        if raw in (None, "", b""):
                            raw = row.get("compress_content")
                        content, status = decompress(raw, row.get("WCDB_CT_message_content"))
                        # Strip only an exact sender prefix, or a known sender in an identified group.
                        if sender_id and content.startswith(sender_id + ":\n"):
                            content = content[len(sender_id) + 2:]
                        elif conversation_id.endswith("@chatroom") and ":\n" in content[:256]:
                            candidate, tail = content.split(":\n", 1)
                            if candidate in names:
                                sender_id, content = candidate, tail
                        raw_type = integer(row.get("local_type"))
                        decoded = message_content(raw_type, content, status)
                        timestamp = integer(row.get("create_time"))
                        message = {"record_type": "message", "id": message_id, "conversation_id": conversation_id, "sender_name": contacts.get(sender_id or "", {}).get("name") or sender_id or "未知发送者", "sender_id": sender_id, "is_self": sender_id == self_id if sender_id and self_id else None, "timestamp": timestamp, "raw_type": raw_type, "source_local_id": local_id, "source_sort_seq": string(row.get("sort_seq")) or None, "server_id": string(row.get("server_id")) or None, **decoded}
                        emit_record(handle, message)
                        statuses[decoded["decode_status"]] += 1
                        types[decoded["type"]] += 1
                        if decoded.get("xml_repaired"):
                            xml_repaired_messages += 1
                            xml_repair_counts.update(decoded["xml_repair_counts"])
                        unknown_sender_count += sender_id is None
                        table["exported_count"] += 1
                        conversation["message_count"] += 1
                        if timestamp is not None and (conversation.get("_last_record_at") is None or timestamp >= conversation["_last_record_at"]):
                            conversation["last_message_at"] = timestamp
                            conversation["_last_record_at"] = timestamp
                            conversation["last_message"] = decoded["text"][:400]
                        total += 1
                    if table["exported_count"] != table["expected_count"]:
                        fail("table_count_mismatch", "消息表行数核对不一致，未发布新导出。")
                if total != sum(table["expected_count"] for table in source_tables):
                    fail("total_count_mismatch", "全库消息数量核对不一致，未发布新导出。")
                for conversation in sorted(conversations.values(), key=lambda item: (-(item.get("last_message_at") or 0), item["id"])):
                    conversation.pop("_last_record_at", None)
                    emit_record(handle, conversation)
                notes = ["仅包含本机已保存的聊天记录；未下载、未同步或已删除的内容无法补回。", "图片、视频、语音、文件、表情和部分分享消息保留了说明，附件目前尚未接通。", "已核对所有已读取会话的记录数量，附件是否能打开会另行提示。"]
                if not self_id:
                    notes.append("部分消息的发送方向尚未确认。")
                if unknown_tables:
                    notes.append("部分会话名称尚未识别，其中记录仍已保留。")
                content_complete = all(status == "ok" for status in statuses)
                manifest = {"record_type": "manifest", "schema_version": EXPORT_SCHEMA_VERSION, "source": "本机微信数据库的只读快照", "complete": True, "content_complete": content_complete, "message_count": total, "conversation_count": len(conversations), "database_count": len(databases), "encrypted_database_count": encrypted_count, "message_table_count": len(source_tables), "failed_databases": [], "failed_tables": [], "unmapped_table_count": unknown_tables, "unknown_sender_count": unknown_sender_count, "self_identity_status": identity_status, "self_id": self_id, "decode_status_counts": dict(statuses), "message_type_counts": dict(types), "media_content_available": False, "source_version": source_version, "started_at": started, "completed_at": utc_now(), "snapshot": snapshot_info, "source_tables": [{key: value for key, value in table.items() if key != "without_rowid"} for table in source_tables], "notes": notes}
                manifest["xml_repaired_message_count"] = xml_repaired_messages
                manifest["xml_repair_counts"] = dict(xml_repair_counts)
                emit_record(handle, manifest)
                handle.flush()
                os.fsync(handle.fileno())
            # Finish every database operation before the atomic publication.
            stack.close()
            os.replace(pending, output)
            # Some filesystems reject fsync on a directory. The fully flushed
            # archive has already been published; do not report it as a failed
            # import merely because this extra durability request is unavailable.
            with contextlib.suppress(OSError):
                directory_fd = os.open(output.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                pending.unlink()
    return {"status": "complete" if manifest["content_complete"] else "complete_with_content_limits", "complete": True, "content_complete": manifest["content_complete"], "message_count": manifest["message_count"], "conversation_count": manifest["conversation_count"], "database_count": manifest["database_count"], "message_table_count": manifest["message_table_count"], "unmapped_table_count": manifest["unmapped_table_count"], "unknown_sender_count": manifest["unknown_sender_count"], "decode_status_counts": manifest["decode_status_counts"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读导入本人现有 WeChat 4.x 数据。不会自动获取密钥或修改微信。")
    parser.add_argument("--db-root", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--self-id", help="已确认的本人微信内部 username；缺少时保留未知方向。")
    parser.add_argument("--snapshot-attempts", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--source-version", help="可选：本机微信版本号，仅作为导出来源说明。")
    args = parser.parse_args(argv)
    old_umask = os.umask(0o077)
    try:
        result = sync(args.db_root, args.key_file, args.output, args.self_id, args.snapshot_attempts, args.source_version)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SyncError as error:
        print(json.dumps({"status": "blocked", "complete": False, "code": error.code, "message": error.message}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "complete": False, "message": "同步已取消；未完成的导出不会替换原文件。"}, ensure_ascii=False))
        return 130
    except Exception as error:
        # Native errors can contain SQL, paths or message contents. Keep them local.
        print(json.dumps({"status": "failed", "complete": False, "code": "unexpected_" + type(error).__name__, "message": "同步未完成，请检查本机依赖和文件访问权限。"}, ensure_ascii=False))
        return 2
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())
