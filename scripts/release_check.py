#!/usr/bin/env python3
"""Check publishable source files without printing matched private values.

Inside a Git repository, inspect tracked plus unignored untracked files. A
tracked file remains checked even if .gitignore would hide a new copy. Before
git init, inspect the source tree, excluding only disposable Python caches.
This is a guardrail, not proof that arbitrary prose is safe to publish.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys


REQUIRED = {"README.md", "README.en.md", "WORLD.md", "LICENSE", "SECURITY.md",
            "THIRD-PARTY-NOTICES.txt", "examples/demo.json", ".github/workflows/ci.yml"}
IGNORED_SCAN_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
RUNTIME_DIRS = {"state", "private", "media", "archives", "backups", "db_storage", "xwechat_files",
                "vendor", ".venv", "venv", ".wechat-local", "encrypted-db-backup", "captures",
                "models", "weights", "node_modules", "dist", "build"}
RUNTIME_NAMES = {"keys.json", "key.json", "source-config.json", "media-config.json", "local-access.json",
                 "personal-world-access.json", "profile-state.json", "profile.lock", "archive.json", "archive.jsonl", "workspace.sqlite",
                 "index.sqlite", "credentials.json", "auth.json", ".env", ".ds_store"}
BINARY_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".db-wal", ".db-shm", ".jsonl", ".ndjson",
                   ".log", ".pyc", ".pyo", ".dat", ".bin", ".pem", ".key", ".p12", ".pfx", ".dmg", ".app",
                   ".exe", ".dll", ".dylib", ".so", ".whl", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
                   ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".wav", ".silk", ".pdf"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".html", ".css", ".js", ".mjs", ".toml", ".yml", ".yaml", ".command", ".sh", ".json"}
TEXT_NAMES = {"LICENSE", "NOTICE", "CODEOWNERS", ".gitignore", ".gitattributes", ".editorconfig"}
MAX_SOURCE_BYTES = 4 * 1024 * 1024
PRIVATE_PATH = re.compile(r"(?:/Users/|/home/)([A-Za-z0-9._-]+)(?=/|\b)|[A-Za-z]:[\\/]Users[\\/]([A-Za-z0-9._-]+)(?=[\\/]|\b)")
PLACEHOLDER_USERS = {"user", "username", "your-user", "your_username", "example", "example-user", "synthetic-user", "synthetic_user"}
FORBIDDEN_LITERAL_MARKERS = (
    "/".join(("", "workspace", "scratch", "")),
    "/".join(("", "workspace", "library", "")),
    "/".join((".codex", "share", "rion-wechat-cli")),
    "-".join(("key", "bootstrap", "ipc", "")),
)
STRONG_SECRET = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{24,}|sk-(?:proj-)?[A-Za-z0-9_-]{24,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[A-Z0-9]{16})\b")
HEX_SECRET = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{64}(?:[0-9a-fA-F]{32})?(?![0-9A-Za-z])")
PRIVATE_WXID = re.compile(r"\bwxid_[A-Za-z0-9]{6,}\b")
SECRET_FIELD = re.compile(r"(?:^|_)(?:key|secret|password|token|credential|aeskey)(?:$|_)", re.I)
SAFE_DUMMY = re.compile(r"^(?:SYNTHETIC(?:_|-)|TEST(?:_|-)|TEST_ONLY(?:_|-)|DEMO(?:_|:|-)|EXAMPLE(?:_|-)|REPLACE(?:_|-)|YOUR(?:_|-)|<).+", re.I)
MANIFEST_FIELDS = {"format_version", "synthetic", "label", "source", "generated_at", "complete", "content_complete",
                   "message_count", "conversation_count", "failed_databases", "failed_tables", "notes"}
MESSAGE_FIELDS = {"id", "conversation_id", "sender_name", "sender_id", "is_self", "type", "text", "timestamp", "raw_type", "decode_status"}
CONVERSATION_FIELDS = {"id", "name", "is_group"}


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    line: int | None = None


def add(issues, relative, code, line=None):
    item = Issue(str(relative), code, line)
    if item not in issues:
        issues.append(item)


def publishable_files(root):
    """Return the prospective release set, including forced tracked files."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=10)
        if Path(os.fsdecode(top.stdout.strip())).resolve() == root:
            result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=20)
            return sorted({os.fsdecode(value) for value in result.stdout.split(b"\0") if value}), "git_publishable_set"
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for current, directories, names in os.walk(root, followlinks=False):
        kept = []
        for name in directories:
            path = Path(current) / name
            if path.is_symlink():
                files.append(path.relative_to(root).as_posix())
            elif name not in IGNORED_SCAN_DIRS:
                kept.append(name)
        directories[:] = kept
        files.extend((Path(current) / name).relative_to(root).as_posix() for name in names)
    return sorted(set(files)), "source_tree_before_git"


def literal(value):
    """Fold simple strings without evaluating code or executing imports."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        left, right = literal(value.left), literal(value.right)
        if left is not None and right is not None and len(left) + len(right) <= MAX_SOURCE_BYTES:
            return left + right
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mult):
        text = literal(value.left)
        if text is not None and isinstance(value.right, ast.Constant) and type(value.right.value) is int and 0 <= value.right.value <= 1024:
            return text * value.right.value
    return None


def suspicious_secret(value, test_file=False):
    if not isinstance(value, str) or not value or SAFE_DUMMY.match(value):
        return False
    if value.lower() in {"key", "token", "password", "secret", "required", "missing", "invalid", "none"}:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,128}", value):
        # Explicit low-entropy test constructions are distinguishable from a
        # copied high-entropy key; arbitrary real-looking test literals fail.
        return not (test_file and len(set(value.casefold())) <= 2)
    return len(value) >= 24 and bool(re.fullmatch(r"[A-Za-z0-9_+/=-]+", value)) and len(set(value)) >= 10


def check_literal(text, relative, issues, line=None, test_file=False):
    for match in PRIVATE_PATH.finditer(text.replace("\\\\", "\\")):
        username = next(group for group in match.groups() if group is not None).casefold()
        if username not in PLACEHOLDER_USERS:
            add(issues, relative, "private_hardcoded_user_path", line)
    if any(marker in text for marker in FORBIDDEN_LITERAL_MARKERS):
        add(issues, relative, "machine_specific_private_path", line)
    if STRONG_SECRET.search(text):
        add(issues, relative, "credential_literal", line)
    if PRIVATE_WXID.search(text) and not SAFE_DUMMY.match(text):
        add(issues, relative, "private_account_identifier", line)
    for match in HEX_SECRET.finditer(text):
        if not (test_file and len(set(match.group().casefold())) <= 2):
            add(issues, relative, "unexplained_key_or_private_fingerprint", line)


def check_python(text, relative, issues):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        add(issues, relative, "python_syntax_invalid")
        return
    test_file = Path(relative).name.startswith("test_")
    for node in ast.walk(tree):
        value = literal(node)
        if value is not None:
            check_literal(value, relative, issues, getattr(node, "lineno", None), test_file)
        if isinstance(node, ast.Dict):
            for key, item in zip(node.keys, node.values):
                field, value = literal(key), literal(item)
                if field and SECRET_FIELD.search(field) and not field.endswith(("_file", "_path", "_name", "_status")) and suspicious_secret(value, test_file):
                    add(issues, relative, "secret_value_in_mapping", getattr(item, "lineno", None))
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = literal(node.value)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = target.id if isinstance(target, ast.Name) else ""
                if SECRET_FIELD.search(name) and suspicious_secret(value, test_file):
                    add(issues, relative, "secret_value_in_assignment", node.lineno)


def check_demo(document, relative, issues):
    """Synthetic is an explicit schema/identity contract, not a magic bypass."""
    valid = isinstance(document, dict) and set(document) == {"manifest", "conversations", "messages"}
    if not valid:
        add(issues, relative, "synthetic_example_envelope_invalid")
        return
    manifest, conversations, messages = document["manifest"], document["conversations"], document["messages"]
    if not isinstance(manifest, dict) or set(manifest) - MANIFEST_FIELDS or manifest.get("synthetic") is not True or not isinstance(manifest.get("label"), str) or not manifest["label"].strip():
        add(issues, relative, "synthetic_example_manifest_invalid")
        return
    if manifest.get("complete") is not True or manifest.get("failed_databases") or manifest.get("failed_tables"):
        add(issues, relative, "synthetic_example_manifest_invalid")
    if not isinstance(conversations, list) or not conversations or not isinstance(messages, list) or not messages:
        add(issues, relative, "synthetic_example_records_invalid")
        return
    conversation_ids, message_ids = set(), set()
    for item in conversations:
        if not isinstance(item, dict) or set(item) - CONVERSATION_FIELDS:
            add(issues, relative, "synthetic_example_conversation_invalid")
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.startswith("demo:") or cid in conversation_ids or not isinstance(item.get("name"), str) or type(item.get("is_group")) is not bool:
            add(issues, relative, "synthetic_example_conversation_invalid")
        else:
            conversation_ids.add(cid)
    for item in messages:
        if not isinstance(item, dict) or set(item) - MESSAGE_FIELDS:
            add(issues, relative, "synthetic_example_message_invalid")
            continue
        mid, sender = item.get("id"), item.get("sender_id")
        if not isinstance(mid, str) or not mid.startswith("demo:") or mid in message_ids or item.get("conversation_id") not in conversation_ids:
            add(issues, relative, "synthetic_example_message_identity_invalid")
        else:
            message_ids.add(mid)
        if sender is not None and (not isinstance(sender, str) or not sender.startswith("demo:")):
            add(issues, relative, "synthetic_example_sender_invalid")
        if not isinstance(item.get("sender_name"), str) or not isinstance(item.get("text"), str) or item.get("type") != "text" or type(item.get("timestamp")) is not int or type(item.get("is_self")) is not bool:
            add(issues, relative, "synthetic_example_message_invalid")
    if manifest.get("message_count") != len(messages) or manifest.get("conversation_count") != len(conversations):
        add(issues, relative, "synthetic_example_count_mismatch")


def scan(root, require_project=True):
    root = Path(root).resolve()
    relatives, mode = publishable_files(root)
    issues = []
    if require_project:
        for missing in sorted(REQUIRED - set(relatives)):
            add(issues, missing, "required_public_file_missing")
    for relative in relatives:
        parts = Path(relative).parts
        path = root / relative
        if Path(relative).is_absolute() or ".." in parts or path.is_symlink() or any((root.joinpath(*parts[:n])).is_symlink() for n in range(1, len(parts))):
            add(issues, relative, "symlink_or_unsafe_release_path")
            continue
        lower_parts = {part.casefold() for part in parts}
        lower_name = path.name.casefold()
        if lower_parts & RUNTIME_DIRS or any(part.startswith("key-bootstrap") or part.endswith(".app") for part in lower_parts) or lower_name in RUNTIME_NAMES or lower_name.endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm")):
            add(issues, relative, "forbidden_runtime_artifact")
            continue
        if not path.is_file():
            add(issues, relative, "release_file_missing")
            continue
        if path.suffix.casefold() in BINARY_SUFFIXES:
            add(issues, relative, "binary_private_or_generated_artifact")
            continue
        if path.suffix.casefold() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            add(issues, relative, "unreviewed_file_type")
            continue
        if path.stat().st_size > MAX_SOURCE_BYTES:
            add(issues, relative, "unexpected_large_source_artifact")
            continue
        try:
            raw = path.read_bytes()
            if raw.startswith((b"SQLite format 3\0", b"PK\x03\x04", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")) or b"\0" in raw:
                add(issues, relative, "binary_disguised_as_source")
                continue
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            add(issues, relative, "unreadable_or_nontext_source")
            continue
        if path.suffix.casefold() == ".json":
            if relative not in {"examples/demo.json", "examples/profile-demo.json"}:
                add(issues, relative, "json_not_approved_synthetic_example")
            else:
                try:
                    document = json.loads(text)
                    check_demo(document, relative, issues)
                    pending = [document]
                    while pending:
                        value = pending.pop()
                        if isinstance(value, dict):
                            pending.extend(value.values())
                        elif isinstance(value, list):
                            pending.extend(value)
                        elif isinstance(value, str):
                            check_literal(value, relative, issues)
                except (ValueError, TypeError, KeyError):
                    add(issues, relative, "synthetic_example_invalid")
        if path.suffix.casefold() == ".py":
            check_python(text, relative, issues)
        else:
            for line, value in enumerate(text.splitlines(), 1):
                check_literal(value, relative, issues, line)
        # These checks include comments, not only executable Python literals.
        if path.suffix.casefold() == ".py":
            for line, value in enumerate(text.splitlines(), 1):
                check_literal(value, relative, issues, line, path.name.startswith("test_"))
    if require_project:
        notice = root / "THIRD-PARTY-NOTICES.txt"
        if notice.is_file() and not notice.is_symlink():
            content = notice.read_text(encoding="utf-8")
            if "0a01249ef72970d07a2603dbb629e80f" not in content or "Copyright (c) 2026 Contributors" not in content or "MIT License" not in content:
                add(issues, "THIRD-PARTY-NOTICES.txt", "upstream_notice_missing")
    return {"ok": not issues, "scan_mode": mode, "files_checked": len(relatives), "issues": [asdict(item) for item in issues]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        result = scan(args.root)
    except Exception:
        print(json.dumps({"ok": False, "code": "release_scan_failed"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
