#!/usr/bin/env python3
"""AIMAN Personal World: source-first, local archive and data-source commands."""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile

from app_config import (APP_DIR, APP_ID, CONFIG_FILE, DEMO_FILE, VERSION, AppError,
                        StateLock, atomic_json, configure, configured_image_python,
                        configured_python, default_demo_state, default_state,
                        private_dir, private_file, publish_archive, read_config,
                        read_private_json, resolve_state, runtime_probe, state_id)


def archive_manifest(path: Path) -> dict:
    """Read only into local memory; callers never print this private object."""
    import server
    manifests = []
    after_manifest = False
    for record in server.source_records(path):
        kind = record.get("record_type") or record.get("record") or record.get("kind")
        if kind == "manifest":
            manifests.append(record)
        elif manifests:
            after_manifest = True
    if len(manifests) != 1 or after_manifest:
        raise AppError("manifest_invalid", "归档需要且只能有一条位于末尾的清单。")
    return manifests[0]


def source_provenance_present(record: dict) -> bool:
    # Conversation exports retain message IDs but intentionally omit DB source
    # metadata. Such exports can be checked as normalized records only.
    return any(name in record for name in ("source_adapter", "source_subsystem", "source_table",
                                          "source_local_id", "source_blob_sha256", "source_create_time_ms"))


def canonical_archive(source: Path, output: Path) -> dict:
    """Normalize interchange JSON/JSONL without losing unrecognized source fields."""
    import server
    try:
        before = source.stat()
        if not stat.S_ISREG(before.st_mode):
            raise AppError("import_file_invalid", "导入来源必须是普通 JSON 或 JSONL 文件。")
        conversations, ids, counts, manifest = {}, set(), Counter(), None
        native_records = False
        body = output.with_name("messages-normalizing.jsonl")
        with body.open("x", encoding="utf-8", newline="\n") as stream:
            body.chmod(0o600)
            for record in server.source_records(source):
                kind = record.get("record_type") or record.get("record") or record.get("kind")
                record = {key: value for key, value in record.items() if key not in ("record_type", "record", "kind")}
                if kind == "manifest":
                    if manifest is not None:
                        raise AppError("duplicate_manifest", "导入来源包含重复清单。")
                    manifest = record
                    continue
                if kind not in ("conversation", "message"):
                    raise AppError("record_type_invalid", "导入来源包含未支持的记录类型。")
                identifier = record.get("id")
                if not isinstance(identifier, str) or not identifier:
                    raise AppError("record_id_invalid", "每条消息和会话都需要非空的稳定字符串 ID。")
                if kind == "conversation":
                    if identifier in conversations:
                        raise AppError("duplicate_conversation", "导入来源存在重复会话 ID。")
                    record["is_group"] = server.normalized_boolean(record.get("is_group", identifier.endswith("@chatroom")))
                    conversations[identifier] = record
                    continue
                conversation_id = record.get("conversation_id")
                if not isinstance(conversation_id, str) or not conversation_id:
                    raise AppError("conversation_id_invalid", "每条消息都需要所属会话的稳定字符串 ID。")
                if identifier in ids:
                    raise AppError("duplicate_message", "导入来源存在重复消息 ID。")
                ids.add(identifier)
                counts[conversation_id] += 1
                record["timestamp"] = server.numeric_time(record.get("timestamp"))
                record["is_self"] = server.normalized_boolean(record.get("is_self"), allow_unknown=True)
                size = record.get("file_size")
                if size is not None and (type(size) is not int or size < 0):
                    raise AppError("file_size_invalid", "附件大小必须是非负整数或 null。")
                native_records |= source_provenance_present(record)
                stream.write(json.dumps({"record_type": "message", **record}, ensure_ascii=False) + "\n")
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise AppError("import_changed", "导入来源在读取期间发生变化，请在导出完成后重试。")
        if not ids:
            raise AppError("empty_import", "文件里没有消息，现有归档保持不变。")
        manifest = dict(manifest or {})
        if manifest.get("complete") is False or manifest.get("failed_databases") or manifest.get("failed_tables"):
            raise AppError("incomplete_import", "来源清单标记了导出失败，现有归档保持不变。")
        for name in ("message_count", "total_messages"):
            if name in manifest and (type(manifest[name]) is not int or manifest[name] != len(ids)):
                raise AppError("message_count_mismatch", "清单消息数与文件中的记录数不一致。")
        for identifier, count in counts.items():
            conversations.setdefault(identifier, {"id": identifier, "name": identifier, "is_group": identifier.endswith("@chatroom")})
        for identifier, record in conversations.items():
            count = counts[identifier]
            if "message_count" in record and (type(record["message_count"]) is not int or record["message_count"] != count):
                raise AppError("conversation_count_mismatch", "会话声明的消息数与实际记录不一致。")
            record["message_count"] = count
        if "conversation_count" in manifest and (type(manifest["conversation_count"]) is not int or manifest["conversation_count"] != len(conversations)):
            raise AppError("conversation_count_mismatch", "清单会话数与实际记录不一致。")
        native = native_records or "source_tables" in manifest or "additional_sources" in manifest
        if native and "source_tables" not in manifest:
            raise AppError("source_manifest_missing", "微信来源记录缺少逐表来源清单，不能通过严格导入验证。")
        # These two fields describe an index build, not source provenance.
        manifest.pop("imported_at", None)
        manifest.pop("search_engine", None)
        manifest.update(message_count=len(ids), conversation_count=len(conversations))
        if not native:
            manifest.setdefault("complete", True)
            manifest["coverage_scope"] = "provided_archive"
            manifest["source_tables_verified"] = False
            manifest["archive_format"] = "normalized.v1"
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            output.chmod(0o600)
            for record in conversations.values():
                stream.write(json.dumps({"record_type": "conversation", **record}, ensure_ascii=False) + "\n")
            with body.open(encoding="utf-8") as messages:
                shutil.copyfileobj(messages, stream)
            stream.write(json.dumps({"record_type": "manifest", **manifest}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        body.unlink()
        return manifest
    except AppError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, KeyError, RecursionError):
        raise AppError("import_invalid", "无法读取导入文件，或记录格式不符合本地归档要求。") from None


def generic_audit(state: Path, media_dir: Path | None = None) -> dict:
    """Exact normalized-archive audit; makes no claims about upstream DB tables."""
    import verify_archive as audit
    archive, index = state / "archive.jsonl", state / "index.sqlite"
    for path in (archive, index):
        private_file(path)
    before = {path: audit.fp(path) for path in (archive, index)}
    issues, summary, field_errors = Counter(), Counter(), Counter()
    media = audit.MediaAudit(Path(media_dir) if media_dir is not None else state / "media", issues, summary)
    ids, conversations, chat_counts, manifests = set(), {}, Counter(), []
    with contextlib.closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        issues["index_quick_check"] = int(db.execute("PRAGMA quick_check").fetchall() != [("ok",)])
        with archive.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if not line.strip():
                    continue
                rec = json.loads(line)
                kind = rec.get("record_type") or rec.get("record") or rec.get("kind")
                if kind == "manifest":
                    manifests.append(rec)
                    continue
                issues["records_after_manifest"] += bool(manifests)
                if kind == "conversation":
                    cid = rec["id"]
                    issues["duplicate_conversations"] += cid in conversations
                    conversations[cid] = rec
                    media.check(rec.get("avatar_url"), "avatar")
                    continue
                if kind != "message":
                    issues["unknown_record_type"] += 1
                    continue
                expected = audit.expected_row(rec)
                mid, cid = expected[:2]
                issues["empty_message_identity"] += not mid or not cid
                issues["duplicate_messages"] += mid in ids
                ids.add(mid)
                chat_counts[cid] += 1
                summary["archive_messages"] += 1
                issues["invalid_direction"] += rec.get("is_self") is not None and type(rec["is_self"]) is not bool
                issues["native_record_without_source_manifest"] += source_provenance_present(rec)
                media.check(rec.get("media_url"), "media", rec.get("file_size"))
                actual = db.execute("SELECT " + ",".join(audit.FIELDS) + " FROM messages WHERE id=?", (mid,)).fetchone()
                if actual is None:
                    issues["index_missing_message"] += 1
                else:
                    for name, want, got in zip(audit.FIELDS, expected, actual):
                        field_errors[name] += want != got
        issues["manifest_record_count"] = int(len(manifests) != 1)
        manifest = manifests[-1] if manifests else {}
        issues["manifest_message_count"] = manifest.get("message_count") != summary["archive_messages"]
        issues["manifest_conversation_count"] = manifest.get("conversation_count") != len(conversations)
        issues["manifest_not_complete"] = manifest.get("complete") is not True
        issues["manifest_failed_sources"] = bool(manifest.get("failed_databases") or manifest.get("failed_tables"))
        stored_row = db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
        stored = json.loads(stored_row[0]) if stored_row else {}
        issues["stored_manifest_fields"] = sum(stored.get(key) != value for key, value in manifest.items() if key not in ("record_type", "record", "kind"))
        for cid, rec in conversations.items():
            want = (str(rec.get("name") or cid), bool(rec.get("is_group", cid.endswith("@chatroom"))), chat_counts[cid], audit.media_reference(rec.get("avatar_url")))
            row = db.execute("SELECT name,is_group,message_count,avatar_url FROM conversations WHERE id=?", (cid,)).fetchone()
            issues["conversation_declared_count"] += rec.get("message_count") != chat_counts[cid]
            issues["conversation_index_fields"] += row != want
            issues["conversation_avatar_index"] += row is not None and row[3] != want[3]
        summary["archive_conversations"] = len(conversations)
        summary["index_messages"] = db.execute("SELECT count(*) FROM messages").fetchone()[0]
        summary["index_conversations"] = db.execute("SELECT count(*) FROM conversations").fetchone()[0]
        summary["permission_check"] = "posix_mode_and_owner" if os.name == "posix" else "not_checked_platform_acl"
        issues["message_without_declared_conversation"] = len(set(chat_counts) - set(conversations))
        issues["index_total_messages"] = int(summary["index_messages"] != len(ids))
        issues["index_total_conversations"] = int(summary["index_conversations"] != len(conversations))
        issues["index_orphan_messages"] = db.execute("SELECT count(*) FROM messages m LEFT JOIN conversations c ON c.id=m.conversation_id WHERE c.id IS NULL").fetchone()[0]
        issues["index_chat_count_sum"] = int(db.execute("SELECT COALESCE(sum(message_count),0) FROM conversations").fetchone()[0] != summary["index_messages"])
        issues["index_last_message_fields"] = db.execute("""SELECT count(*) FROM conversations c WHERE
          last_message_at IS NOT COALESCE((SELECT timestamp FROM messages WHERE conversation_id=c.id ORDER BY timestamp DESC,id DESC LIMIT 1),0)
          OR last_message IS NOT COALESCE((SELECT text FROM messages WHERE conversation_id=c.id ORDER BY timestamp DESC,id DESC LIMIT 1),'')""").fetchone()[0]
    issues["archive_or_index_changed_during_audit"] = sum(audit.fp(path) != old for path, old in before.items())
    media.finish()
    issues["index_message_field_mismatches"] = sum(field_errors.values())
    return {"archive_index_ok": int(not any(issues.values())), "validation": "normalized_archive",
            "counts": dict(summary), "issues": dict(issues), "index_field_mismatches": dict(field_errors)}


def verify_state(state: Path, media_dir: Path | None = None) -> dict:
    state = resolve_state(state)
    private_file(state / "archive.jsonl")
    private_file(state / "index.sqlite")
    manifest = archive_manifest(state / "archive.jsonl")
    if manifest.get("archive_format") == "personal-world.collection.v1":
        from history_import import verify_collection
        return verify_collection(state,media_dir=media_dir)
    if "source_tables" in manifest or "additional_sources" in manifest:
        import verify_archive
        result, _ = verify_archive.archive_index(state, media_dir=media_dir)
        result["validation"] = "wechat_source_manifest"
    else:
        result = generic_audit(state, media_dir=media_dir)
    return {"ok": result.get("archive_index_ok") == 1, **result}


def guard_demo(state: Path) -> None:
    if state == resolve_state(default_state()) or (state / CONFIG_FILE).exists() or (state / CONFIG_FILE).is_symlink():
        raise AppError("demo_state_protected", "演示必须使用独立目录，不能覆盖默认真实数据目录或已配置的数据源。")
    marker_path = state / DEMO_FILE
    if marker_path.exists() or marker_path.is_symlink():
        marker = read_private_json(marker_path)
        if marker.get("app") != APP_ID or marker.get("synthetic") is not True or marker.get("state_id") != state_id(state):
            raise AppError("demo_state_protected", "现有目录没有有效的独立演示标记。")
        archive, index = state / "archive.jsonl", state / "index.sqlite"
        if index.exists() and not archive.exists():
            raise AppError("demo_state_protected", "现有目录包含未能确认来源的索引，不能用演示覆盖。")
        if archive.exists() and archive_manifest(archive).get("synthetic") is not True:
            raise AppError("demo_state_protected", "现有归档不是人工演示数据，不能用演示覆盖。")
    elif any(path.name != "refresh.lock" for path in state.iterdir()):
        raise AppError("demo_state_protected", "演示目标目录已有文件，请选择新的独立目录。")


def import_state(source: Path, state: Path, *, demo: bool = False, no_open: bool = False, port: int | None = None) -> dict:
    import server
    from refresh import interruption_handlers
    source, state = source.expanduser().resolve(), resolve_state(state)
    with StateLock(state), interruption_handlers():
        if demo:
            guard_demo(state)
        if not demo and (state / "archive.jsonl").is_file() and archive_manifest(state/"archive.jsonl").get("archive_format")=="personal-world.collection.v1":
            raise AppError("collection_requires_merge","已有历史集合请使用 import 的安全合并入口，不能用旧接口整体替换。")
        with tempfile.TemporaryDirectory(prefix=".import-", dir=state) as temporary:
            staging = Path(temporary)
            manifest = canonical_archive(source, staging / "archive.jsonl")
            if demo and manifest.get("synthetic") is not True:
                raise AppError("demo_source_invalid", "演示来源缺少人工数据标记。")
            server.import_archive(staging / "archive.jsonl", staging)
            verification = verify_state(staging, media_dir=state / "media")
            if not verification["ok"]:
                raise AppError("import_verification_failed", "候选归档没有通过逐条校验，现有归档保持不变。")
            if demo:
                atomic_json(state / DEMO_FILE, {"app": APP_ID, "synthetic": True, "state_id": state_id(state)})
            publish_archive(staging, state)
            if not demo and manifest.get("synthetic") is not True:
                (state / DEMO_FILE).unlink(missing_ok=True)
    result = {"ok": True, "status": "demo_ready" if demo else "imported", "synthetic": manifest.get("synthetic") is True,
              "message_count": manifest["message_count"], "conversation_count": manifest["conversation_count"],
              "validation": verification["validation"]}
    if not no_open:
        from launch import open_viewer
        result["viewer"] = open_viewer(state, port=port)
    return result


def doctor(state: Path) -> dict:
    """Inspect availability using fixed labels, counts and actual dependency imports."""
    state = resolve_state(state)
    report = {"ok": True, "app": APP_ID, "version": VERSION, "state_exists": state.is_dir(),
              "permission_check": "posix_mode_and_owner" if os.name == "posix" else "not_checked_platform_acl",
              "runtime": runtime_probe(sys.executable), "source": {"configured": False}, "archive": {"status": "not_imported"}}
    try:
        config = read_config(state)
        if config:
            source = report["source"]
            source.update(configured=True, self_identity_configured=bool(config.get("self_id")),
                          database_directory_available=isinstance(config.get("db_root"), str) and Path(config["db_root"]).expanduser().is_dir(),
                          key_file_configured=isinstance(config.get("key_file"), str))
            try:
                private_file(Path(config["key_file"]).expanduser(), limit=16 * 1024 * 1024)
                source["key_file_private_and_available"] = True
            except (AppError, KeyError, TypeError):
                source["key_file_private_and_available"] = False
            reader = configured_python(config)
            source["reader"] = runtime_probe(reader, sqlcipher_library=config.get("sqlcipher_library"))
            image_reader = configured_image_python(config)
            source["image_reader"] = source["reader"] if image_reader == reader else runtime_probe(image_reader)
            source["ffmpeg_available"] = bool(shutil.which(str(config.get("ffmpeg") or "ffmpeg")))
    except AppError as error:
        report["source"]["status"] = error.code
    index = state / "index.sqlite"
    if index.exists() or index.is_symlink():
        try:
            private_file(index)
            with contextlib.closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                count, start, end = db.execute("SELECT count(*),min(nullif(timestamp,0)),max(timestamp) FROM messages").fetchone()
                conversations = db.execute("SELECT count(*) FROM conversations").fetchone()[0]
                media = db.execute("SELECT count(*) FROM messages WHERE media_url IS NOT NULL").fetchone()[0]
                row = db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
                manifest = json.loads(row[0]) if row else {}
                report["archive"] = {"status": "indexed", "message_count": count, "conversation_count": conversations,
                                     "first_message_at": start, "last_message_at": end, "messages_with_media": media,
                                     "synthetic": manifest.get("synthetic") is True,
                                     "source_table_manifest_available": "source_tables" in manifest,
                                     "scope": "imported_snapshot_only", "archive_file_available": (state / "archive.jsonl").is_file()}
        except AppError as error:
            report["archive"] = {"status": error.code}
        except (OSError, sqlite3.DatabaseError, ValueError, TypeError):
            report["archive"] = {"status": "index_unreadable"}
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="AIMAN Personal World · 本地归档与人工行动工作台")
    result.add_argument("--version", action="version", version=APP_ID + " " + VERSION)
    result.add_argument("--state", type=Path, dest="global_state", help="本机私有状态目录；默认使用平台用户数据目录")
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (("demo", "加载独立的人工演示数据"), ("open", "启动或重新打开本地界面"),
                            ("serve", "在当前终端运行本地服务"), ("import", "导入已有 JSON/JSONL 归档"),
                            ("doctor", "安全检测本机依赖、配置和导入范围"), ("configure", "配置已有数据库和已有密钥"),
                            ("refresh", "从已配置来源建立并合并新快照"), ("verify", "只读逐条核对归档、索引和媒体"),
                            ("apply-import", "确认已核对的历史导入预览"), ("cancel-import", "取消未确认的预览"),
                            ("history", "查看月份覆盖和导入批次"), ("recover", "检查并恢复中断的归档发布")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--state", type=Path)
        if name in ("demo", "open", "serve", "import", "refresh"):
            command.add_argument("--port", type=int, help="本地端口；0 为自动分配，省略时优先 18792")
        if name in ("demo", "open", "import", "refresh"):
            command.add_argument("--no-open", action="store_true", help="数据命令完成后不启动界面；open 命令仅不唤起浏览器")
        if name == "import":
            command.add_argument("file", type=Path)
            command.add_argument("--preview",action="store_true",help="只生成本地预览，不改变当前聊天")
            command.add_argument("--source-id",default="primary",help="当前账号/相同编号体系用primary；其他来源请另取标识")
            command.add_argument("--policy",choices=("keep_existing","prefer_incoming"),default="keep_existing")
            command.add_argument("--label",default="手动归档")
        if name in ("apply-import","cancel-import"):
            command.add_argument("plan_id")
        if name == "refresh":
            command.add_argument("--preview",action="store_true",help="核对本机新快照并给出合并预览，不先改变当前历史")
            command.add_argument("--timeout", type=int, default=7200, help="每个刷新步骤的最大秒数，默认 7200")
        if name == "verify":
            command.add_argument("--media-dir", type=Path)
        if name == "configure":
            for flag in ("db-root", "key-file", "self-id", "python", "image-python", "ffmpeg", "sqlcipher-library", "app-data", "source-version"):
                command.add_argument("--" + flag)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if sys.version_info < (3, 10):
            raise AppError("python_version", "Personal World 需要 Python 3.10 或更新版本。")
        selected = args.state if args.state is not None else args.global_state
        state = resolve_state(selected if selected is not None else (default_demo_state() if args.command == "demo" else default_state()))
        if args.command == "demo":
            result = import_state(APP_DIR / "examples/demo.json", state, demo=True, no_open=args.no_open, port=args.port)
        elif args.command == "import":
            import history_import
            preview=history_import.prepare(args.file,state,source_id=args.source_id,policy=args.policy,label=args.label)
            result=preview if args.preview else history_import.commit(preview["plan_id"],state)
            if not args.preview and not args.no_open:
                from launch import open_viewer
                result["viewer"]=open_viewer(state,port=args.port)
        elif args.command == "apply-import":
            from history_import import commit
            result=commit(args.plan_id,state)
        elif args.command == "cancel-import":
            from history_import import discard
            result=discard(args.plan_id,state)
        elif args.command == "history":
            from history_import import catalog
            result={"ok":True,**catalog(state)}
        elif args.command == "recover":
            with StateLock(state) as lock:
                result={"ok":True,**getattr(lock,"recovery",{"recovered":False})}
        elif args.command == "configure":
            names = ("db_root", "key_file", "self_id", "python", "image_python", "ffmpeg", "sqlcipher_library", "app_data", "source_version")
            result = configure(state, {name: getattr(args, name) for name in names})
        elif args.command == "doctor":
            result = doctor(state)
        elif args.command == "verify":
            result = verify_state(state, args.media_dir)
        elif args.command == "refresh":
            from refresh import refresh_state
            result = refresh_state(state, no_open=args.no_open, port=args.port, timeout=args.timeout,preview=args.preview)
        elif args.command == "open":
            from launch import open_viewer
            result = open_viewer(state, no_open=args.no_open, port=args.port)
        else:
            from launch import serve_foreground
            return serve_foreground(state, port=args.port)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") is True else 1
    except AppError as error:
        print(json.dumps({"ok": False, "code": error.code, "message": error.message}, ensure_ascii=False))
        return error.exit_code
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "code": "cancelled", "message": "本次任务已取消。"}, ensure_ascii=False))
        return 130
    except Exception:
        # Paths, SQLite errors and import text can contain private identifiers.
        print(json.dumps({"ok": False, "code": "operation_failed", "message": "本机操作未完成；请检查配置、权限和文件格式。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
