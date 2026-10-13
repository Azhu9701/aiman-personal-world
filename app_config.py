"""Local application paths, private configuration, and optional runtime probes.

Importing this module neither creates files nor inspects an account.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

APP_ID = "aiman-personal-world"
VERSION = "0.4.0"
DEFAULT_PORT = 18792
ACCESS_FILE = "personal-world-access.json"
CONFIG_FILE = "source-config.json"
DEMO_FILE = "demo-state.json"
APP_DIR = Path(__file__).resolve().parent


class AppError(Exception):
    """Only fixed, user-safe messages; never include input values or subprocess text."""
    def __init__(self, code: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code


def default_state() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/AIMAN/PersonalWorld"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "AIMAN/PersonalWorld"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / APP_ID


def default_demo_state() -> Path:
    state = default_state()
    return state.with_name(state.name + "-demo")


def resolve_state(value: Path | str | None = None) -> Path:
    return Path(value if value is not None else default_state()).expanduser().resolve()


def state_id(state: Path | str) -> str:
    canonical = os.path.normcase(str(Path(state).expanduser().resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise AppError("state_symlink", "状态目录不能是符号链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise AppError("state_invalid", "状态位置不是目录。")
    if os.name == "posix":
        if path.stat().st_uid != os.geteuid():
            raise AppError("state_owner", "状态目录不属于当前用户。")
        path.chmod(0o700)


def private_file(path: Path, *, limit: int | None = None) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise AppError("file_unavailable", "所需的本机文件不存在或不可读取。") from None
    if not stat.S_ISREG(info.st_mode):
        raise AppError("file_not_regular", "配置、凭据和密钥必须是普通文件，不能是符号链接。")
    if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise AppError("file_permissions", "私有配置或密钥文件必须属于当前用户，且仅本人可读写（0600）。")
    if limit is not None and info.st_size > limit:
        raise AppError("file_too_large", "配置文件超出允许的大小。")


def read_private_json(path: Path, *, limit: int = 65536) -> dict:
    private_file(path, limit=limit)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AppError("config_invalid", "本机配置不是有效的 JSON 对象。") from None
    if not isinstance(value, dict):
        raise AppError("config_invalid", "本机配置不是有效的 JSON 对象。")
    return value


def atomic_json(path: Path, value: dict) -> None:
    private_dir(path.parent)
    if path.is_symlink():
        raise AppError("config_symlink", "不能替换符号链接形式的配置文件。")
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            os.chmod(pending, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        if os.name == "posix":
            with contextlib.suppress(OSError):
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    finally:
        pending.unlink(missing_ok=True)


class StateLock:
    """Nonblocking per-state lock; imports/configuration/refresh share one lock."""
    def __init__(self, state: Path, name: str = "refresh.lock"):
        self.state, self.name, self.stream, self.backend = state, name, None, None

    def __enter__(self):
        private_dir(self.state)
        path = self.state / self.name
        if path.is_symlink():
            raise AppError("lock_symlink", "状态锁文件无效。")
        try:
            import fcntl
            self.backend = fcntl
        except ImportError:
            try:
                import msvcrt
                self.backend = msvcrt
            except ImportError:
                raise AppError("locking_unsupported", "此平台没有可用的文件锁；归档更新功能暂不支持。") from None
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.stream = os.fdopen(descriptor, "r+b")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        try:
            if hasattr(self.backend, "flock"):
                self.backend.flock(descriptor, self.backend.LOCK_EX | self.backend.LOCK_NB)
            else:
                if self.stream.seek(0, os.SEEK_END) == 0:
                    self.stream.write(b"\0")
                    self.stream.flush()
                self.stream.seek(0)
                self.backend.locking(descriptor, self.backend.LK_NBLCK, 1)
        except OSError:
            self.stream.close()
            self.stream = None
            raise AppError("state_busy", "这个数据目录已有任务正在运行，请等待它完成。") from None
        if self.name == "refresh.lock":
            try:
                from archive_publish import recover
                self.recovery = recover(self.state)
            except BaseException:
                self.__exit__(None,None,None)
                raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.stream is not None:
            try:
                if hasattr(self.backend, "flock"):
                    self.backend.flock(self.stream.fileno(), self.backend.LOCK_UN)
                else:
                    self.stream.seek(0)
                    self.backend.locking(self.stream.fileno(), self.backend.LK_UNLCK, 1)
            finally:
                self.stream.close()


def read_config(state: Path, *, required: bool = False) -> dict:
    path = state / CONFIG_FILE
    if not path.exists() and not path.is_symlink():
        if required:
            raise AppError("not_configured", "尚未配置数据来源；先用 configure 指定已有数据库和密钥文件。")
        return {}
    return read_private_json(path)


def executable(value: str | Path | None, *, fallback: str | None = None) -> str:
    candidate = str(value) if value else fallback
    if not candidate or "\0" in candidate:
        raise AppError("executable_missing", "没有找到指定的解释器或可执行文件。")
    expanded = os.path.expanduser(candidate)
    found = shutil.which(expanded)
    if not found:
        raise AppError("executable_missing", "没有找到指定的解释器或可执行文件。")
    # Keep the venv entry-point path: resolving its symlink would run the base
    # interpreter without that environment's installed optional dependencies.
    return str(Path(found).absolute())


PROBE_CODE = r'''
import contextlib, ctypes, ctypes.util, importlib, io, json, os, sqlite3, sys
result = {"python_version": list(sys.version_info[:3]), "python_supported": sys.version_info >= (3, 10), "modules": {}, "sqlcipher_available": False, "sqlcipher_library_available": False}
for label, module in (("sqlcipher3", "sqlcipher3.dbapi2"), ("pysqlcipher3", "pysqlcipher3.dbapi2"), ("Pillow", "PIL.Image"), ("cryptography", "cryptography.hazmat.primitives.ciphers"), ("zstandard", "zstandard"), ("pilk", "pilk")):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            imported = importlib.import_module(module)
            if label in ("sqlcipher3", "pysqlcipher3"):
                connection = imported.connect(":memory:")
                try:
                    value = connection.execute("PRAGMA cipher_version").fetchone()
                    result["sqlcipher_available"] = result["sqlcipher_available"] or bool(value and value[0])
                finally:
                    connection.close()
        result["modules"][label] = True
    except Exception:
        result["modules"][label] = False
for value in (os.environ.get("SQLCIPHER_LIBRARY"), ctypes.util.find_library("sqlcipher")):
    if value:
        try:
            library = ctypes.CDLL(value)
            result["sqlcipher_library_available"] = hasattr(library, "sqlite3_key") and hasattr(library, "sqlite3_open_v2")
            if result["sqlcipher_library_available"]:
                break
        except Exception:
            pass
connection = sqlite3.connect(":memory:")
try:
    connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text, tokenize='trigram')")
    result["trigram_search_available"] = True
except sqlite3.DatabaseError:
    result["trigram_search_available"] = False
finally:
    connection.close()
print("AIMAN_RUNTIME_PROBE:" + json.dumps(result, separators=(",", ":")))
'''


def runtime_probe(python: str, *, sqlcipher_library: str | None = None) -> dict:
    environment = os.environ.copy()
    if sqlcipher_library:
        environment["SQLCIPHER_LIBRARY"] = sqlcipher_library
    try:
        result = subprocess.run([python, "-c", PROBE_CODE], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=environment, timeout=20, check=False)
        marker = b"AIMAN_RUNTIME_PROBE:"
        payloads = [line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)]
        value = json.loads(payloads[-1]) if result.returncode == 0 and payloads else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        value = None
    if not isinstance(value, dict) or not isinstance(value.get("python_version"), list):
        raise AppError("python_probe_failed", "无法运行指定的 Python 解释器或完成依赖检测。")
    # Only fixed keys, booleans and a numeric version leave the subprocess.
    version = value["python_version"]
    if len(version) != 3 or any(type(n) is not int or not 0 <= n < 10000 for n in version):
        raise AppError("python_probe_failed", "Python 解释器返回了无效版本信息。")
    modules = value.get("modules", {})
    return {"python_version": version, "python_supported": value.get("python_supported") is True,
            "modules": {name: modules.get(name) is True for name in ("sqlcipher3", "pysqlcipher3", "Pillow", "cryptography", "zstandard", "pilk")},
            "sqlcipher_available": value.get("sqlcipher_available") is True,
            "sqlcipher_library_available": value.get("sqlcipher_library_available") is True,
            "trigram_search_available": value.get("trigram_search_available") is True}


def configured_python(config: dict) -> str:
    return executable(config.get("python") or config.get("sqlcipher_python"), fallback=sys.executable)


def configured_image_python(config: dict) -> str:
    fallback = "/usr/bin/python3" if sys.platform == "darwin" and Path("/usr/bin/python3").is_file() else configured_python(config)
    return executable(config.get("image_python"), fallback=fallback)


def validate_source(config: dict) -> tuple[Path, Path]:
    if not isinstance(config.get("db_root"), str) or not isinstance(config.get("key_file"), str):
        raise AppError("not_configured", "配置缺少数据库目录或已有密钥文件。")
    root = Path(config["db_root"]).expanduser().resolve()
    key_file = Path(config["key_file"]).expanduser().absolute()
    if not root.is_dir():
        raise AppError("db_root_missing", "配置的数据源目录不存在。")
    read_private_json(key_file, limit=16 * 1024 * 1024)
    return root, key_file


def configure(state: Path, changes: dict) -> dict:
    with StateLock(state):
        config = read_config(state)
        for name, value in changes.items():
            if value is not None:
                config[name] = str(value)
        root, key_file = validate_source(config)
        python = configured_python(config)
        library = config.get("sqlcipher_library")
        if library:
            library_path = Path(library).expanduser().resolve()
            if not library_path.is_file():
                raise AppError("sqlcipher_library_missing", "指定的 SQLCipher 库文件不存在。")
            library = str(library_path)
        probe = runtime_probe(python, sqlcipher_library=library)
        if not probe["python_supported"]:
            raise AppError("python_version", "读取数据使用的 Python 必须是 3.10 或更新版本。")
        config.update(schema_version=1, db_root=str(root), key_file=str(key_file), python=python)
        if library:
            if not probe["sqlcipher_library_available"]:
                raise AppError("sqlcipher_library_invalid", "指定库没有可用的 SQLCipher 接口。")
            config["sqlcipher_library"] = library
        if config.get("self_id") is not None:
            identity = config["self_id"]
            if not isinstance(identity, str) or not 1 <= len(identity) <= 512 or any(ord(c) < 32 for c in identity):
                raise AppError("self_id_invalid", "本人标识格式无效。")
        if config.get("image_python"):
            image_python = configured_image_python(config)
            image_probe = runtime_probe(image_python)
            if tuple(image_probe["python_version"]) < (3, 9, 0):
                raise AppError("image_python_version", "图片辅助解释器至少需要 Python 3.9。")
            config["image_python"] = image_python
        if config.get("ffmpeg"):
            config["ffmpeg"] = executable(config["ffmpeg"])
            try:
                result = subprocess.run([config["ffmpeg"], "-version"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                raise AppError("ffmpeg_unavailable", "指定的 ffmpeg 无法运行。") from None
            if result.returncode:
                raise AppError("ffmpeg_unavailable", "指定的 ffmpeg 无法运行。")
        if config.get("app_data"):
            app_data = Path(config["app_data"]).expanduser().resolve()
            if not app_data.is_dir():
                raise AppError("app_data_missing", "指定的应用资源目录不存在。")
            config["app_data"] = str(app_data)
        atomic_json(state / CONFIG_FILE, config)
    return {"ok": True, "configured": True, "self_identity_configured": bool(config.get("self_id")),
            "reader_sqlcipher_available": probe["sqlcipher_available"] or probe["sqlcipher_library_available"],
            "optional_dependencies": probe["modules"]}


def publish_archive(staging: Path, state: Path) -> None:
    """Publish an already-verified pair with a crash-recoverable journal."""
    from archive_publish import publish
    publish(staging,state)
