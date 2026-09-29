from __future__ import annotations

import json
import hashlib
import html
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import winreg
import zipfile
import ctypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
import yt_dlp

HOST = "127.0.0.1"
PORT = int(os.environ.get("SARCHAO_HELPER_PORT", "17891"))
HELPER_VERSION = "1.7"
MUTEX_NAME = os.environ.get("SARCHAO_HELPER_MUTEX_NAME", "Local\\SarchaoLocalHelperSingletonV1")
MAX_FILE_BYTES = 1024 * 1024 * 1024
ALLOWED_PRODUCTION_ORIGINS = frozenset({
    "https://sarchao-ve5xjm4u.manus.space",
    "https://sarchao-myanmar-ai-editor.onrender.com",
})
PO_PROVIDER_VERSION = "2.0.0"
PO_PROVIDER_PORT = 4416
PO_PROVIDER_ARCHIVE_URL = os.environ.get(
    "SARCHAO_PO_PROVIDER_ARCHIVE_URL",
    "https://github.com/mmtoprecap-jpg/sarchao-local-helper-releases/releases/latest/download/Sarchao-PO-Provider-v2.0.0.zip",
)
PO_PROVIDER_ARCHIVE_SHA256 = os.environ.get(
    "SARCHAO_PO_PROVIDER_ARCHIVE_SHA256",
    "8d12a53e80a7c32805a6d06ac1f40e986286520d07e9301825224f4b69c7524b",
).lower()
FORMATS = {
    "best_mp4": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
    "1080_mp4": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720_mp4": "bv*[height<=720]+ba/b[height<=720]/b",
    "480_mp4": "bv*[height<=480]+ba/b[height<=480]/b",
    "audio_mp3": "bestaudio/best",
}
BROWSER_COOKIE_SOURCES = ("edge", "chrome", "brave", "firefox")
YOUTUBE_RETRYABLE_MARKERS = (
    "http error 403", "403: forbidden", "sign in to confirm", "not a bot",
    "cookies-from-browser", "authentication", "login required",
)


_MUTEX_HANDLE = None
_PO_PROCESS: subprocess.Popen | None = None
_PO_LOCK = threading.Lock()


def app_root_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    target = base / "Sarchao"
    target.mkdir(parents=True, exist_ok=True)
    return target


def app_directory() -> Path:
    """Stable installation directory for the helper executable."""
    target = app_root_directory() / "Helper"
    target.mkdir(parents=True, exist_ok=True)
    return target


def logs_directory() -> Path:
    target = app_root_directory() / "Logs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def temp_directory() -> Path:
    target = app_root_directory() / "Temp"
    target.mkdir(parents=True, exist_ok=True)
    return target


def cache_directory() -> Path:
    target = app_root_directory() / "Cache"
    target.mkdir(parents=True, exist_ok=True)
    return target


def runtime_directory() -> Path:
    target = app_root_directory() / "Runtime"
    target.mkdir(parents=True, exist_ok=True)
    return target


def log_startup(message: str) -> None:
    try:
        with (logs_directory() / "helper.log").open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def register_url_protocol(executable: Path | None = None) -> None:
    """Register a per-user URL protocol so the web app can launch this helper."""
    executable = (executable or Path(sys.executable if getattr(sys, "frozen", False) else __file__)).resolve()
    command = f'"{executable}" "%1"'
    base = r"Software\Classes\sarchao-helper"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Sarchao Local Helper")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{executable}",0')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        winreg.SetValueEx(key, "SarchaoLocalHelper", 0, winreg.REG_SZ, f'"{executable}" --background')


def install_and_relaunch_if_needed() -> bool:
    """Keep the registered helper in a stable per-user location.

    Returns True in the installed process and False in the downloaded bootstrap
    process after starting the installed copy.
    """
    if not getattr(sys, "frozen", False):
        return True
    current = Path(sys.executable).resolve()
    target = app_directory() / f"Sarchao-Local-Helper-v{HELPER_VERSION}.exe"
    if current == target:
        return True
    try:
        shutil.copy2(current, target)
        if os.environ.get("SARCHAO_HELPER_SKIP_REGISTRATION") != "1":
            register_url_protocol(target)
        # Upgrades must release the port and singleton mutex held by an older
        # installed helper before the new installed copy starts.
        terminate_legacy_port_owners()
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.Popen([str(target), "--background"], creationflags=flags, close_fds=True)
        log_startup(f"installed {current} -> {target}")
        return False
    except Exception as error:
        log_startup(f"install failed: {error!r}; running from {current}")
        return True


def acquire_single_instance() -> bool:
    global _MUTEX_HANDLE
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        log_startup(f"CreateMutex failed: {ctypes.get_last_error()}")
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


def terminate_legacy_port_owners() -> None:
    """Stop v1.0 helper children, which accidentally allowed shared binds."""
    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), timeout=10,
        ).stdout
        current_pid = os.getpid()
        owners: set[int] = set()
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{PORT}"):
                try:
                    pid = int(parts[-1])
                except ValueError:
                    continue
                if pid and pid != current_pid:
                    owners.add(pid)
        stopped: set[int] = set()
        for pid in owners:
            details = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), timeout=10,
            ).stdout.lower()
            if "sarchao-local-helper" not in details:
                continue
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), timeout=10,
            )
            stopped.add(pid)
        if stopped:
            log_startup("stopped legacy listeners: " + ",".join(map(str, sorted(stopped))))
            time.sleep(1)
    except Exception as error:
        log_startup(f"legacy cleanup failed: {error!r}")


def resource_directory() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def documents_directory() -> Path:
    """Return the user's Windows Documents directory, including OneDrive redirection."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Personal")
        resolved = Path(os.path.expandvars(str(value))).expanduser()
        if resolved:
            return resolved
    except OSError:
        pass
    return Path.home() / "Documents"


def default_download_directory() -> Path:
    configured = os.environ.get("SARCHAO_HELPER_DOWNLOAD_DIR", "").strip()
    return Path(configured).expanduser() if configured else documents_directory() / "Sarchao Downloads"


def settings_path() -> Path:
    return app_root_directory() / "settings.json"


def load_settings() -> dict:
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(value: dict) -> None:
    target = settings_path()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def configured_download_directory() -> Path:
    configured = str(load_settings().get("download_root", "")).strip()
    target = Path(configured).expanduser() if configured else default_download_directory()
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    for name in ("Audio", "Video", "Transcript", "Other"):
        (target / name).mkdir(parents=True, exist_ok=True)
    return target


def choose_download_directory(current: Path) -> Path | None:
    """Open the native Windows folder picker from the background helper."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root,
            title="Sarchao Download folder ရွေးမည်",
            initialdir=str(current if current.exists() else documents_directory()),
            mustexist=False,
        )
    finally:
        root.destroy()
    return Path(selected).resolve() if selected else None


def po_provider_directory() -> Path:
    return runtime_directory() / f"po-provider-v{PO_PROVIDER_VERSION}"


def po_provider_ready() -> bool:
    try:
        request = Request(f"http://127.0.0.1:{PO_PROVIDER_PORT}/ping", headers={"User-Agent": "Sarchao-Local-Helper"})
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read(4096).decode("utf-8"))
        return payload.get("version") == PO_PROVIDER_VERSION
    except Exception:
        return False


def _download_po_provider(target_zip: Path) -> None:
    digest = hashlib.sha256()
    total = 0
    request = Request(PO_PROVIDER_ARCHIVE_URL, headers={"User-Agent": f"Sarchao-Local-Helper/{HELPER_VERSION}"})
    with urlopen(request, timeout=60) as response, target_zip.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 100 * 1024 * 1024:
                raise RuntimeError("Download service ပြင်ဆင်ဖိုင် အရွယ်အစား မမှန်ပါ။")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest().lower() != PO_PROVIDER_ARCHIVE_SHA256:
        target_zip.unlink(missing_ok=True)
        raise RuntimeError("Download service ပြင်ဆင်ဖိုင် စစ်ဆေးမှု မအောင်မြင်ပါ။")


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            member_path = (destination / member.filename).resolve()
            try:
                member_path.relative_to(destination_root)
            except ValueError as error:
                raise RuntimeError("Download service archive လမ်းကြောင်း မမှန်ပါ။") from error
        bundle.extractall(destination)


def ensure_po_provider(stage_callback=None) -> Path:
    """Install and start the cookie-free local YouTube PO-token provider."""
    global _PO_PROCESS
    with _PO_LOCK:
        provider_dir = po_provider_directory()
        node = provider_dir / "node.exe"
        entrypoint = provider_dir / "build" / "main.js"
        if not node.is_file() or not entrypoint.is_file():
            if stage_callback:
                stage_callback("YouTube download service ကို တစ်ကြိမ်သာ ပြင်ဆင်နေသည်…")
            archive = runtime_directory() / f".po-provider-v{PO_PROVIDER_VERSION}.zip.part"
            staging = runtime_directory() / f".po-provider-v{PO_PROVIDER_VERSION}-{uuid.uuid4().hex}"
            try:
                archive.unlink(missing_ok=True)
                _download_po_provider(archive)
                staging.mkdir(parents=True, exist_ok=False)
                _safe_extract_zip(archive, staging)
                if not (staging / "node.exe").is_file() or not (staging / "build" / "main.js").is_file():
                    raise RuntimeError("Download service archive မပြည့်စုံပါ။")
                if provider_dir.exists():
                    shutil.rmtree(provider_dir)
                staging.replace(provider_dir)
            finally:
                archive.unlink(missing_ok=True)
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
        if po_provider_ready():
            return node
        if stage_callback:
            stage_callback("YouTube download service စတင်နေသည်…")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        _PO_PROCESS = subprocess.Popen(
            [str(node), str(entrypoint), "--host", "127.0.0.1", "--port", str(PO_PROVIDER_PORT)],
            cwd=str(provider_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if po_provider_ready():
                return node
            if _PO_PROCESS.poll() is not None:
                break
            time.sleep(0.5)
        raise RuntimeError("YouTube download service ကို စတင်၍မရပါ။")


def origin_allowed(origin: str) -> bool:
    if not origin:
        return True
    if origin in ALLOWED_PRODUCTION_ORIGINS:
        return True
    return bool(re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?", origin))


def valid_youtube_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in allowed or not parsed.path:
        raise ValueError("YouTube video link မမှန်ပါ။")
    return url


def _clean_caption_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<\d\d:\d\d:\d\d[.,]\d+>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[ \t]+", " ", text.replace("\u200b", "").replace("\xa0", " ")).strip()


def _parse_caption_timestamp(value: object) -> int:
    parts = str(value or "0").strip().replace(",", ".").split(":")
    if len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    else:
        hours, minutes, seconds = parts[-3:]
    return int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)


def parse_caption_data(data: bytes, extension: str) -> list[dict]:
    cues: list[dict] = []
    if extension == "json3":
        document = json.loads(data.decode("utf-8-sig"))
        for event in document.get("events") or []:
            text = _clean_caption_text("".join(str(item.get("utf8") or "") for item in event.get("segs") or []))
            if not text:
                continue
            start = int(event.get("tStartMs") or 0)
            duration = max(1, int(event.get("dDurationMs") or 1))
            cues.append({"start_ms": start, "end_ms": start + duration, "text": text})
    else:
        source = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
        for block in re.split(r"\n\s*\n", source):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
            if timing_index is None:
                continue
            timing = lines[timing_index].split("-->", 1)
            text = _clean_caption_text("\n".join(lines[timing_index + 1:]))
            if text:
                cues.append({
                    "start_ms": _parse_caption_timestamp(timing[0]),
                    "end_ms": _parse_caption_timestamp(timing[1].strip().split()[0]),
                    "text": text,
                })
    merged: list[dict] = []
    for cue in cues:
        if merged and merged[-1]["text"] == cue["text"]:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], cue["end_ms"])
        else:
            merged.append(cue)
    return merged


def choose_caption_track(info: dict, requested: str = "") -> tuple[str, dict, bool]:
    requested = requested.lower().replace("_", "-").strip()
    source_language = str(info.get("language") or "").lower().replace("_", "-")
    for automatic, collection_name in ((False, "subtitles"), (True, "automatic_captions")):
        collection = info.get(collection_name) or {}
        candidates = list(collection.items())
        if automatic:
            originals = [(code, formats) for code, formats in candidates if code.lower().endswith("-orig")]
            if originals:
                candidates = originals
        def rank(item: tuple[str, list]) -> tuple[int, int, str]:
            code = item[0].lower().replace("_", "-")
            if requested and (code == requested or code.startswith(requested + "-") or code.startswith(requested.split("-", 1)[0] + "-")):
                return (0, len(code), code)
            if source_language and (code == source_language or code.startswith(source_language + "-")):
                return (1, len(code), code)
            return (2 if not requested else 3, len(code), code)
        for code, formats in sorted(candidates, key=rank):
            normalized = code.lower().replace("_", "-")
            if requested and rank((code, formats))[0] >= 3:
                continue
            preferred = next((item for item in formats or [] if item.get("ext") == "json3"), None)
            preferred = preferred or next((item for item in formats or [] if item.get("ext") == "vtt"), None)
            if preferred and preferred.get("url"):
                return code, preferred, automatic
    raise ValueError("ဒီ YouTube video တွင် အသုံးပြုနိုင်သော CC/subtitle မရှိပါ။")


def youtube_error_allows_local_retry(error: Exception) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in YOUTUBE_RETRYABLE_MARKERS)


def with_browser_cookies(options: dict, browser: str) -> dict:
    """Use the signed-in browser session in memory; never export a cookie file."""
    result = dict(options)
    result["cookiesfrombrowser"] = (browser, None, None, None)
    return result


def clear_partial_downloads(directory: Path) -> None:
    for partial in directory.iterdir():
        if partial.is_file():
            partial.unlink(missing_ok=True)


def caption_data_once(url: str, language: str, options: dict) -> tuple[dict, str, dict, bool, bytes]:
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(valid_youtube_url(url), download=False)
        code, track, automatic = choose_caption_track(info, language)
        with downloader.urlopen(track["url"]) as response:
            data = response.read()
    return info, code, track, automatic, data


def download_caption_cues(url: str, language: str = "") -> dict:
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "remote_components": {"ejs:github"}}
    try:
        info, code, track, automatic, data = caption_data_once(url, language, options)
    except Exception as first_error:
        if not youtube_error_allows_local_retry(first_error):
            raise
        last_error = first_error
        for browser in BROWSER_COOKIE_SOURCES:
            try:
                info, code, track, automatic, data = caption_data_once(
                    url, language, with_browser_cookies(options, browser)
                )
                break
            except Exception as browser_error:
                last_error = browser_error
        else:
            node = ensure_po_provider()
            fallback_options = dict(options)
            fallback_options["extractor_args"] = {"youtube": {"player_client": ["mweb"]}}
            fallback_options["js_runtimes"] = {"node": {"path": str(node)}}
            try:
                info, code, track, automatic, data = caption_data_once(url, language, fallback_options)
            except Exception:
                raise last_error
    cues = parse_caption_data(data, str(track.get("ext") or "").lower())
    if not cues:
        raise ValueError("YouTube CC ရှိသော်လည်း timestamp စာသားမရပါ။")
    return {
        "ok": True, "cues": cues, "language_code": code, "automatic": automatic,
        "video_title": str(info.get("title") or "YouTube video"),
    }


class HelperState:
    def __init__(self) -> None:
        self.token = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.resource_dir = resource_directory()
        self.download_dir = configured_download_directory()
        self.ffmpeg = self.resource_dir / "ffmpeg.exe"

    def settings(self) -> dict:
        with self.lock:
            root = self.download_dir
        return {
            "ok": True,
            "download_root": str(root),
            "default_download_root": str(default_download_directory().resolve()),
            "folders": {
                "audio": str(root / "Audio"),
                "video": str(root / "Video"),
                "transcript": str(root / "Transcript"),
                "other": str(root / "Other"),
            },
        }

    def set_download_directory(self, value: Path) -> dict:
        target = value.expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        for name in ("Audio", "Video", "Transcript", "Other"):
            (target / name).mkdir(parents=True, exist_ok=True)
        save_settings({"download_root": str(target)})
        with self.lock:
            self.download_dir = target
        log_startup(f"download root changed to {target}")
        return self.settings()

    def select_download_directory(self) -> dict:
        with self.lock:
            current = self.download_dir
        selected = choose_download_directory(current)
        if selected is None:
            return {**self.settings(), "cancelled": True}
        return self.set_download_directory(selected)

    def reset_download_directory(self) -> dict:
        return self.set_download_directory(default_download_directory())

    def public_job(self, job: dict) -> dict:
        return {
            key: job.get(key, default)
            for key, default in (
                ("job_id", ""), ("status", "queued"), ("stage", ""), ("percent", 0),
                ("title", ""), ("filename", ""), ("file_size", 0),
                ("mime_type", "application/octet-stream"), ("format", ""), ("error", ""),
            )
        }

    def start_download(self, url: str, selected_format: str) -> dict:
        if selected_format not in FORMATS:
            raise ValueError("Download format မမှန်ပါ။")
        if not self.ffmpeg.is_file():
            raise ValueError("Download service ထဲတွင် ffmpeg မတွေ့ပါ။")
        with self.lock:
            active = next((j for j in self.jobs.values() if j["status"] in {"queued", "running"}), None)
            if active:
                raise RuntimeError("Download တစ်ခု လုပ်နေပြီးသားပါ။")
            job_id = uuid.uuid4().hex
            job_dir = temp_directory() / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            job = {
                "job_id": job_id, "url": valid_youtube_url(url), "format": selected_format,
                "job_dir": job_dir, "status": "queued", "stage": "စတင်ရန်ပြင်ဆင်နေသည်…",
                "percent": 0, "title": "", "filename": "", "file_path": None,
                "file_size": 0, "mime_type": "application/octet-stream", "error": "", "cancel_requested": False,
            }
            self.jobs[job_id] = job
        threading.Thread(target=self._download_worker, args=(job_id,), daemon=True).start()
        return self.public_job(job)

    def _download_worker(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            selected_format = job["format"]
            job.update(status="running", stage="YouTube မှ download လုပ်နေသည်…")

        def progress(data: dict) -> None:
            with self.lock:
                if job.get("cancel_requested"):
                    raise RuntimeError("Download ကို ရပ်လိုက်ပါပြီ။")
                status = str(data.get("status", ""))
                if status == "downloading":
                    downloaded = int(data.get("downloaded_bytes", 0) or 0)
                    total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
                    job["percent"] = min(96, max(1, round(downloaded / total * 96))) if total else 1
                elif status in {"finished", "processing", "started"}:
                    job.update(stage="Video/Audio format ပြင်ဆင်နေသည်…", percent=max(97, int(job.get("percent", 0))))

        try:
            options = {
                "format": FORMATS[selected_format],
                "outtmpl": str(job["job_dir"] / "%(title).160B [%(id)s].%(ext)s"),
                "noplaylist": True, "windowsfilenames": True, "trim_file_name": 180,
                "max_filesize": MAX_FILE_BYTES, "continuedl": True, "retries": 3,
                "fragment_retries": 3, "concurrent_fragment_downloads": 4,
                "socket_timeout": 30, "quiet": True, "no_warnings": True,
                "progress_hooks": [progress], "postprocessor_hooks": [progress],
                "ffmpeg_location": str(self.resource_dir), "remote_components": {"ejs:github"},
                "cachedir": str(cache_directory() / "yt-dlp"),
                "merge_output_format": "mp4",
            }
            if selected_format == "audio_mp3":
                options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
                options.pop("merge_output_format", None)
            try:
                with yt_dlp.YoutubeDL(options) as downloader:
                    downloader.extract_info(job["url"], download=True)
            except Exception as first_error:
                if not youtube_error_allows_local_retry(first_error):
                    raise

                def provider_stage(message: str) -> None:
                    with self.lock:
                        job.update(stage=message, percent=1)

                with self.lock:
                    job.update(stage="Browser login ဖြင့် အလိုအလျောက် ပြန်ချိတ်နေသည်…", percent=1)
                last_error = first_error
                downloaded = False
                for browser in BROWSER_COOKIE_SOURCES:
                    clear_partial_downloads(job["job_dir"])
                    try:
                        with yt_dlp.YoutubeDL(with_browser_cookies(options, browser)) as downloader:
                            downloader.extract_info(job["url"], download=True)
                        downloaded = True
                        break
                    except Exception as browser_error:
                        last_error = browser_error
                if not downloaded:
                    with self.lock:
                        job.update(stage="YouTube ကို အခြားနည်းဖြင့် ပြန်ချိတ်နေသည်…", percent=1)
                    clear_partial_downloads(job["job_dir"])
                    node = ensure_po_provider(provider_stage)
                    fallback_options = dict(options)
                    fallback_options["extractor_args"] = {
                        "youtube": {"player_client": ["mweb"]}
                    }
                    fallback_options["js_runtimes"] = {"node": {"path": str(node)}}
                    try:
                        with yt_dlp.YoutubeDL(fallback_options) as downloader:
                            downloader.extract_info(job["url"], download=True)
                    except Exception:
                        raise last_error
            candidates = [p for p in job["job_dir"].iterdir() if p.is_file() and p.suffix.lower() not in {".part", ".ytdl"}]
            if not candidates:
                raise RuntimeError("Download ပြီးသောဖိုင်ကို မတွေ့ပါ။")
            output = max(candidates, key=lambda p: (p.stat().st_mtime, p.stat().st_size))
            if output.stat().st_size > MAX_FILE_BYTES:
                output.unlink(missing_ok=True)
                raise RuntimeError("Download ဖိုင်သည် 1 GB ကျော်နေပါသည်။")
            with self.lock:
                download_root = self.download_dir
            destination_dir = download_root / ("Audio" if selected_format == "audio_mp3" else "Video")
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / output.name
            if destination.exists():
                for index in range(1, 10_000):
                    candidate = destination_dir / f"{output.stem}_{index}{output.suffix}"
                    if not candidate.exists():
                        destination = candidate
                        break
            output.replace(destination)
            output = destination
            try:
                Path(job["job_dir"]).rmdir()
            except OSError:
                pass
            with self.lock:
                job.update(
                    status="complete", stage="Download ပြီးပါပြီ။", percent=100,
                    title=re.sub(r"\s*\[[^\]]+\]$", "", output.stem), filename=output.name,
                    file_path=output, file_size=output.stat().st_size,
                    mime_type=mimetypes.guess_type(output.name)[0] or "application/octet-stream",
                )
        except Exception as error:
            try:
                shutil.rmtree(Path(job["job_dir"]), ignore_errors=True)
            except (KeyError, OSError):
                pass
            with self.lock:
                if job.get("status") != "cancelled":
                    job.update(status="error", stage="Download မအောင်မြင်ပါ", percent=0, error=str(error)[:1200])

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            job.update(status="cancelled", stage="ရပ်ထားသည်", percent=0, error="", cancel_requested=True)
        return self.public_job(job)


STATE = HelperState()


class HelperHandler(BaseHTTPRequestHandler):
    server_version = f"SarchaoLocalHelper/{HELPER_VERSION}"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin or "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range, X-Helper-Token")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")
        self.send_header("Cache-Control", "no-store")

    def end_headers(self) -> None:
        self._cors()
        super().end_headers()

    def _json(self, value: dict, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return origin_allowed(self.headers.get("Origin", "")) and self.headers.get("X-Helper-Token", "") == STATE.token

    def _body(self) -> dict:
        length = min(int(self.headers.get("Content-Length", "0") or 0), 32_768)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_OPTIONS(self) -> None:
        if not origin_allowed(self.headers.get("Origin", "")):
            self.send_response(HTTPStatus.FORBIDDEN)
        else:
            self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            if not origin_allowed(self.headers.get("Origin", "")):
                return self._json({"error": "Origin not allowed"}, HTTPStatus.FORBIDDEN)
            return self._json({"ok": True, "version": HELPER_VERSION, "token": STATE.token, "downloads": str(STATE.download_dir)})
        if not self._authorized():
            return self._json({"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
        if parsed.path == "/settings":
            return self._json(STATE.settings())
        job_id = (parse_qs(parsed.query).get("id") or [""])[0]
        with STATE.lock:
            job = STATE.jobs.get(job_id)
            public = STATE.public_job(job) if job else None
            file_path = job.get("file_path") if job else None
        if parsed.path == "/status":
            return self._json(public or {"error": "Job not found"}, 200 if public else HTTPStatus.NOT_FOUND)
        if parsed.path == "/file" and file_path and Path(file_path).is_file():
            return self._send_file(Path(file_path))
        self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _send_file(self, path: Path) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self) -> None:
        if not self._authorized():
            return self._json({"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
        try:
            payload = self._body()
            if self.path == "/download":
                return self._json(STATE.start_download(payload.get("url", ""), str(payload.get("format", "best_mp4"))), HTTPStatus.ACCEPTED)
            if self.path == "/captions":
                return self._json(download_caption_cues(payload.get("url", ""), str(payload.get("language", ""))))
            if self.path == "/cancel":
                return self._json(STATE.cancel(str(payload.get("job_id", ""))))
            if self.path == "/settings/select-download-folder":
                return self._json(STATE.select_download_directory())
            if self.path == "/settings/reset-download-folder":
                return self._json(STATE.reset_download_directory())
            if self.path == "/open-folder":
                job_id = str(payload.get("job_id", ""))
                with STATE.lock:
                    job = STATE.jobs.get(job_id)
                    folder = Path(job["file_path"]).parent if job and job.get("file_path") else STATE.download_dir
                os.startfile(folder)
                return self._json({"ok": True})
            if self.path == "/settings/open-download-folder":
                with STATE.lock:
                    folder = STATE.download_dir
                os.startfile(folder)
                return self._json({"ok": True, "download_root": str(folder)})
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except RuntimeError as error:
            self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    if not install_and_relaunch_if_needed():
        raise SystemExit(0)
    if not acquire_single_instance():
        raise SystemExit(0)
    terminate_legacy_port_owners()
    if os.environ.get("SARCHAO_HELPER_SKIP_REGISTRATION") != "1":
        try:
            register_url_protocol()
        except OSError as error:
            # Managed/sandboxed Windows sessions can deny registry writes. The
            # service still works when started directly.
            log_startup(f"registration failed: {error!r}")
    try:
        log_startup(f"starting v{HELPER_VERSION} pid={os.getpid()} on {HOST}:{PORT}")
        ExclusiveThreadingHTTPServer((HOST, PORT), HelperHandler).serve_forever()
    except OSError as error:
        # A running instance already owns the loopback port. Custom-protocol
        # launches should be silent in that case.
        log_startup(f"server failed: {error!r}")
