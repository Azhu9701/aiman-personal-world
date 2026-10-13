#!/usr/bin/env python3
"""Refresh from configured, already-existing keys through a verified staging area."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile

from app_config import (APP_DIR, AppError, StateLock, configured_python,
                        private_dir, publish_archive, read_config, resolve_state,
                        runtime_probe, validate_source)


@contextlib.contextmanager
def interruption_handlers():
    old = {}
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    try:
        for name in ("SIGTERM", "SIGHUP"):
            value = getattr(signal, name, None)
            if value is not None:
                try:
                    old[value] = signal.signal(value, interrupted)
                except ValueError:
                    pass
        yield
    finally:
        for value, previous in old.items():
            signal.signal(value, previous)


def stop_stage(process: subprocess.Popen) -> None:
    """Stop only the process group created for this pipeline stage."""
    if process.poll() is not None:
        return
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)


def run_stage(stage: str, command: list[str], state: Path, environment: dict, timeout: int) -> None:
    print(json.dumps({"stage": stage, "status": "running"}), flush=True)
    log_path = state / "personal-world-refresh.log"
    if log_path.is_symlink():
        raise AppError("log_symlink", "刷新日志位置不能是符号链接。")
    descriptor = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "ab") as log:
        if os.name == "posix":
            os.fchmod(log.fileno(), 0o600)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=environment, start_new_session=os.name == "posix")
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_stage(process)
            raise AppError("refresh_stage_timeout", "刷新步骤超时；已停止本次子进程，保留原有归档。") from None
        except BaseException:
            stop_stage(process)
            raise
    if code:
        raise AppError("refresh_" + stage + "_failed", "刷新步骤没有通过，原有归档未被替换；详情保留在本机私有刷新日志中。",
                       code if 0 < code < 126 else 2)
    print(json.dumps({"stage": stage, "status": "complete"}), flush=True)


def chatbot_available(root: Path) -> bool:
    """Do not let glob's permission-error suppression turn a source into absence."""
    def unreadable(error):
        raise AppError("source_inventory_unreadable", "无法完整读取来源目录清单，未替换现有归档。") from None
    matches = 0
    for directory, subdirectories, files in os.walk(root, followlinks=False, onerror=unreadable):
        if "chatbot_message.db" not in files:
            continue
        candidate = Path(directory) / "chatbot_message.db"
        try:
            info = candidate.lstat()
        except OSError:
            unreadable(None)
        if not stat.S_ISREG(info.st_mode):
            raise AppError("chatbot_source_invalid", "AI 来源必须是普通数据库文件，不能是符号链接。")
        matches += 1
    if matches > 1:
        raise AppError("chatbot_source_ambiguous", "来源目录中有多个 AI 数据库，未替换现有归档。")
    return matches == 1


def refresh_state(state: Path, *, no_open: bool = False, port: int | None = None, timeout: int = 7200, preview: bool = False) -> dict:
    if os.name != "posix":
        raise AppError("refresh_platform_unsupported", "此版本的微信数据库刷新仅支持 macOS/Linux；此平台仍可查看和导入离线归档。")
    if not 1 <= timeout <= 86400:
        raise AppError("timeout_invalid", "每步超时必须在 1 到 86400 秒之间。")
    state = resolve_state(state)
    private_dir(state)
    required = ("sync_wechat.py", "enrich_media.py", "append_chatbot.py", "server.py", "verify_archive.py")
    if any(not (APP_DIR / name).is_file() for name in required):
        raise AppError("source_tree_incomplete", "源码目录缺少刷新所需文件，请使用完整源码目录。")
    with StateLock(state), interruption_handlers():
        config = read_config(state, required=True)
        db_root, key_file = validate_source(config)
        python = configured_python(config)
        probe = runtime_probe(python, sqlcipher_library=config.get("sqlcipher_library"))
        if not probe["python_supported"]:
            raise AppError("python_version", "读取数据使用的 Python 必须是 3.10 或更新版本。")
        environment = os.environ.copy()
        if config.get("sqlcipher_library"):
            environment["SQLCIPHER_LIBRARY"] = str(config["sqlcipher_library"])
        identity_args = ["--self-id", str(config["self_id"])] if config.get("self_id") else []
        source_args = ["--db-root", str(db_root), "--key-file", str(key_file)]
        with tempfile.TemporaryDirectory(prefix=".refresh-", dir=state) as temporary:
            staging = Path(temporary)
            archive = staging / "archive.jsonl"
            command = [python, str(APP_DIR / "sync_wechat.py"), *source_args, "--output", str(archive), *identity_args]
            if config.get("source_version"):
                command += ["--source-version", str(config["source_version"])]
            run_stage("messages", command, state, environment, timeout)

            command = [python, str(APP_DIR / "enrich_media.py"), *source_args,
                       "--archive", str(archive), "--media-dir", str(state / "media"), *identity_args]
            for name, flag in (("image_python", "--image-python"), ("ffmpeg", "--ffmpeg"), ("app_data", "--app-data")):
                if config.get(name):
                    command += [flag, str(config[name])]
            run_stage("media", command, state, environment, timeout)

            # An absent subsystem is distinct from an unreadable existing one.
            # Existing AI databases must pass the full adapter and audit stages.
            chatbot_present = chatbot_available(db_root)
            if chatbot_present:
                augmented = staging / "archive-with-ai.jsonl"
                command = [python, str(APP_DIR / "append_chatbot.py"), *source_args,
                           "--archive", str(archive), "--output", str(augmented), *identity_args]
                run_stage("chatbot", command, state, environment, timeout)
                os.replace(augmented, archive)
            else:
                print(json.dumps({"stage": "chatbot", "status": "source_not_present"}), flush=True)

            run_stage("index", [sys.executable, str(APP_DIR / "server.py"), "--data-dir", str(staging),
                                "--import-file", str(archive), "--import-only", "--skip-search"], state, environment, timeout)
            run_stage("verify", [sys.executable, str(APP_DIR / "verify_archive.py"), "--state", str(staging),
                                 "--media-dir", str(state / "media")], state, environment, timeout)
            from history_import import _prepare_locked, _commit_locked
            prepared=_prepare_locked(archive,state,policy="prefer_incoming",label="微信本机快照",validated_dir=staging)
            merge_result=prepared if preview else _commit_locked(prepared["plan_id"],state)
        with contextlib.closing(sqlite3.connect((state / "index.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            counts = {"message_count": db.execute("SELECT count(*) FROM messages").fetchone()[0],
                      "conversation_count": db.execute("SELECT count(*) FROM conversations").fetchone()[0]}
    result = {"ok": True, "status": "refresh_preview" if preview else "refreshed", "chatbot_source_present": chatbot_present, "merge":merge_result,
              "extraction_mode":"verified_full_snapshot", **counts}
    if not no_open:
        from launch import open_viewer
        result["viewer"] = open_viewer(state, port=port)
    return result


def main(argv: list[str] | None = None) -> int:
    from personal_world import main as cli_main
    return cli_main(["refresh", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
