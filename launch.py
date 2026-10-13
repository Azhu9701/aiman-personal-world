#!/usr/bin/env python3
"""Launch or reopen one private Personal World service per data directory."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

from app_config import (ACCESS_FILE, APP_DIR, APP_ID, DEFAULT_PORT, VERSION,
                        AppError, StateLock, private_dir, read_private_json,
                        resolve_state, state_id)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def health(port: int) -> dict | None:
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=0.6) as response:
            body = response.read(8193)
        value = json.loads(body) if len(body) <= 8192 else None
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def running_access(state: Path) -> dict | None:
    try:
        access = read_private_json(state / ACCESS_FILE, limit=8192)
    except AppError as error:
        if error.code == "file_unavailable" and not (state / ACCESS_FILE).exists():
            return None
        raise
    token = access.get("token")
    if (not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None
            or access.get("state_id") != state_id(state)):
        return None
    status = health(access.get("port"))
    if not status or status.get("app") != APP_ID or status.get("state_id") != state_id(state):
        return None
    if status.get("version") != VERSION:
        raise AppError("running_version_mismatch", "此数据目录已有其他版本的 Personal World 服务，请先正常停止它。")
    return access


def free_port(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def choose_port(port: int | None) -> int:
    if port is not None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise AppError("port_invalid", "端口必须是 0 到 65535 之间的整数。")
        if port and not free_port(port):
            raise AppError("port_in_use", "指定端口已被占用；请选择其他端口，或省略 --port 自动选择。")
        return port
    return DEFAULT_PORT if free_port(DEFAULT_PORT) else 0


def matching_running(state: Path, port: int | None) -> dict | None:
    access = running_access(state)
    if access and port not in (None, 0, access["port"]):
        raise AppError("state_running_on_other_port", "此数据目录已在另一个端口运行；省略 --port 可重新打开同一服务。")
    return access


def stop_owned(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def open_viewer(state: Path, *, port: int | None = None, no_open: bool = False) -> dict:
    from refresh import interruption_handlers
    state = resolve_state(state)
    private_dir(state)
    access = matching_running(state, port)
    reused = access is not None
    if access is None:
        with StateLock(state, "personal-world-launch.lock"), interruption_handlers():
            access = matching_running(state, port)
            reused = access is not None
            if access is None:
                selected = choose_port(port)
                server_file = APP_DIR / "server.py"
                if not server_file.is_file():
                    raise AppError("server_missing", "源码目录缺少 server.py。")
                log_path = state / "personal-world.log"
                if log_path.is_symlink():
                    raise AppError("log_symlink", "服务日志位置不能是符号链接。")
                descriptor = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                options = {"start_new_session": True} if os.name == "posix" else {
                    "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)}
                with os.fdopen(descriptor, "ab") as log:
                    if os.name == "posix":
                        os.fchmod(log.fileno(), 0o600)
                    process = subprocess.Popen([sys.executable, str(server_file), "--data-dir", str(state), "--port", str(selected)],
                                               stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
                try:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        access = running_access(state)
                        if access and access.get("pid") == process.pid:
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(0.12)
                    else:
                        access = None
                    if not access or access.get("pid") != process.pid:
                        stop_owned(process)
                        raise AppError("server_start_failed", "本地服务没有完成启动；请检查私有服务日志或端口占用。")
                except BaseException:
                    stop_owned(process)
                    raise
    browser_opened = False
    if not no_open:
        # The bootstrap secret goes only to the local browser, never to stdout.
        url = f"http://127.0.0.1:{int(access['port'])}/?access={urllib.parse.quote(access['token'], safe='')}"
        with contextlib.suppress(Exception):
            browser_opened = bool(webbrowser.open(url, new=2))
    return {"ok": True, "status": "running", "app": APP_ID, "version": VERSION,
            "port": int(access["port"]), "reused": reused, "browser_opened": browser_opened}


def serve_foreground(state: Path, *, port: int | None = None) -> int:
    from refresh import interruption_handlers
    state = resolve_state(state)
    private_dir(state)
    if matching_running(state, port):
        raise AppError("already_running", "这个数据目录的服务已经运行；使用 open 重新打开。")
    with StateLock(state, "personal-world-launch.lock"), interruption_handlers():
        if matching_running(state, port):
            raise AppError("already_running", "这个数据目录的服务已经运行；使用 open 重新打开。")
        selected = choose_port(port)
        process = subprocess.Popen([sys.executable, str(APP_DIR / "server.py"), "--data-dir", str(state), "--port", str(selected)])
        try:
            return process.wait()
        except BaseException:
            stop_owned(process)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 10):
            raise AppError("python_version", "Personal World 需要 Python 3.10 或更新版本。")
        result = open_viewer(resolve_state(args.state), port=args.port, no_open=args.no_open)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except AppError as error:
        print(json.dumps({"ok": False, "code": error.code, "message": error.message}, ensure_ascii=False))
        return error.exit_code
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "code": "cancelled"}))
        return 130
    except Exception:
        print(json.dumps({"ok": False, "code": "launch_failed", "message": "本地服务启动失败。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
