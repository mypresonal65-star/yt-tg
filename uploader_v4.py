"""
uploader_v4.py - High-Performance Telegram Video Uploader supporting Bot and User sessions.

Part of the "ytdl-telegram" project.
Features:
- Dual Upload Modes (UPLOAD_MODE=bot|user):
    * 'bot' (default): Uploads to Telegram channel as the Telegram Bot.
    * 'user': Uploads to Telegram channel as a regular Telegram user account,
              while the bot client continues handling commands and live status updates.
- FastTelethon parallel chunked uploading (multi-connection pipelined streaming)
- Cryptg hardware-accelerated AES-IGE encryption detection and logging
- Configurable UPLOAD_CONNECTIONS from .env (default: 4)
- Upload speeds reach full line capacity (5+ MB/s)
- Automatic fallback to standard Telethon client.send_file if parallel upload encounters issues
- Throttled progress callback (3.0 seconds) with live Speed (MB/s) and ETA
- Logs average upload speed for each file and which mode was used (BOT vs USER)
- Zero-re-encode splitting for files > 1.95 GB into Parts via ffmpeg
- Native playable video metadata (duration, width, height) & generated thumbnails
- Fast streaming support (supports_streaming=True)
- FloodWait and network error retry with exponential backoff
- Configurable local cleanup (KEEP_LOCAL=true/false)
- Dedicated "sessions/" directory for session storage (ignored by git for security)
- Interactive first-run login with phone code & 2FA password support (never logs credentials)
- Standalone CLI mode (python uploader_v4.py --login) for convenient one-time setup
"""

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple, Union

# Portable tools and sessions configuration (relative to script directory)
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
SESSIONS_DIR = SCRIPT_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# Attempt to load .env relative to script directory
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except ImportError:
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

ENV_TOOLS_DIR = os.getenv("TOOLS_DIR")
TOOLS_DIR = Path(ENV_TOOLS_DIR).resolve() if ENV_TOOLS_DIR else (SCRIPT_DIR / "tools")

# Prepend portable tools to PATH
tools_str = str(TOOLS_DIR)
current_path = os.environ.get("PATH", "")
if tools_str not in current_path:
    os.environ["PATH"] = tools_str + os.pathsep + current_path

# Logger setup
logger = logging.getLogger("ytdl_telegram.uploader")

# Cryptg detection & logging
try:
    import cryptg
    CRYPTG_AVAILABLE = True
    CRYPTG_STATUS = "ACTIVE (Hardware-accelerated AES-IGE enabled)"
except ImportError:
    CRYPTG_AVAILABLE = False
    CRYPTG_STATUS = "INACTIVE (cryptg not found, running pure-python encryption)"

# Upload mode configuration (bot vs user)
UPLOAD_MODE = os.getenv("UPLOAD_MODE", "bot").strip().lower()
if UPLOAD_MODE not in ("bot", "user"):
    UPLOAD_MODE = "bot"

try:
    from telethon import TelegramClient, errors, helpers, utils
    from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
    from telethon.tl.types import DocumentAttributeVideo, InputFile, InputFileBig
except ImportError:
    TelegramClient = None
    errors = None
    helpers = None
    utils = None
    DocumentAttributeVideo = None
    SaveBigFilePartRequest = None
    SaveFilePartRequest = None
    InputFile = None
    InputFileBig = None

MAX_TELEGRAM_BOT_FILE_SIZE = int(1.95 * 1024 * 1024 * 1024)


def generate_file_id() -> int:
    """
    Generates a random 64-bit signed integer suitable for Telegram InputFile ID.
    Compatible across all Telethon versions (1.x - 2.x).
    """
    try:
        if helpers is not None and hasattr(helpers, "generate_random_long"):
            return helpers.generate_random_long()
    except Exception:
        pass
    try:
        if utils is not None and hasattr(utils, "get_random_int"):
            return utils.get_random_int()
    except Exception:
        pass
    return struct.unpack("q", os.urandom(8))[0]


def check_required_uploader_tools(tools_dir: Path) -> None:
    """Verifies that ffmpeg.exe and ffprobe.exe are available in tools_dir or PATH."""
    required = ["ffmpeg.exe", "ffprobe.exe"]
    missing = [req for req in required if not (tools_dir / req).is_file()]
    if missing:
        print("\n" + "=" * 60)
        print("  [ERROR] Missing required tool(s) for uploader in portable tools folder!")
        print(f"  Target folder: {tools_dir}")
        print("\n  Missing file(s):")
        for m in missing:
            print(f"    [X] {m}")
        print("\n  Please place ffmpeg.exe and ffprobe.exe into:")
        print(f"    {tools_dir}")
        print("=" * 60 + "\n")
        sys.exit(1)


@dataclass
class UploadResult:
    """Represents the outcome of a video upload operation."""
    filepath: str
    caption: str
    message_ids: List[int]
    total_parts: int
    success: bool
    error: Optional[str] = None
    average_speed_mbps: float = 0.0
    elapsed_seconds: float = 0.0
    upload_mode: str = "bot"


def get_video_metadata(filepath: str) -> Tuple[int, int, float]:
    """Extracts (width, height, duration_seconds) using ffprobe."""
    default_w, default_h, default_dur = 1280, 720, 0.0
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-show_entries", "format=duration",
        "-of", "json",
        filepath,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)

        width = default_w
        height = default_h
        duration = default_dur

        streams = data.get("streams", [])
        if streams:
            v_stream = streams[0]
            width = int(v_stream.get("width", default_w))
            height = int(v_stream.get("height", default_h))
            if "duration" in v_stream and v_stream["duration"]:
                try:
                    duration = float(v_stream["duration"])
                except ValueError:
                    pass

        if duration == 0.0 and "format" in data:
            fmt_dur = data["format"].get("duration")
            if fmt_dur:
                try:
                    duration = float(fmt_dur)
                except ValueError:
                    pass

        return width, height, duration
    except Exception as e:
        logger.warning("Could not probe video metadata for '%s' (%s). Using defaults.", filepath, e)
        return default_w, default_h, default_dur


def generate_thumbnail(filepath: str, duration: float) -> Optional[str]:
    """Extracts a video frame thumbnail at ~10% into the video as a JPEG file."""
    thumb_path = f"{filepath}_thumb.jpg"
    seek_time = max(1.0, duration * 0.1) if duration > 0 else 1.0

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(seek_time),
        "-i", filepath,
        "-vframes", "1",
        "-vf", "scale='min(320,iw)':-1",
        "-q:v", "2",
        thumb_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as e:
        logger.warning("Thumbnail generation failed for '%s': %s", filepath, e)

    if os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
        except OSError:
            pass
    return None


def split_video_into_parts(
    filepath: str,
    duration: float,
    file_size: int,
) -> List[Tuple[str, str, bool]]:
    """
    Checks if file exceeds 1.95 GB. If so, splits without re-encoding into parts.
    Returns: List of (part_path, caption_suffix, is_temporary)
    """
    if file_size <= MAX_TELEGRAM_BOT_FILE_SIZE:
        return [(filepath, "", False)]

    num_parts = math.ceil(file_size / (1.85 * 1024 * 1024 * 1024))
    logger.info(
        "File '%s' (%s MB) exceeds 1.95 GB limit. Splitting into %d parts...",
        Path(filepath).name,
        f"{file_size / (1024*1024):.1f}",
        num_parts,
    )
    print(f"  [!] Video size > 1.95 GB. Splitting losslessly into {num_parts} parts with ffmpeg...")

    part_duration = duration / num_parts if duration > 0 else 0
    part_files: List[Tuple[str, str, bool]] = []
    base_name = Path(filepath).stem
    ext = Path(filepath).suffix

    for i in range(num_parts):
        start_time = i * part_duration
        part_path = str(Path(filepath).parent / f"{base_name}.part{i+1:02d}{ext}")
        suffix = f" (Part {i+1}/{num_parts})"

        cmd = [
            "ffmpeg",
            "-y",
            "-ss", str(start_time),
            "-i", filepath,
            "-t", str(part_duration),
            "-c", "copy",
            "-map", "0",
            part_path,
        ]
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            if os.path.exists(part_path) and os.path.getsize(part_path) > 0:
                part_files.append((part_path, suffix, True))
            else:
                raise RuntimeError(f"Split file missing or empty: {part_path}")
        except Exception as e:
            for p, _, is_temp in part_files:
                if is_temp and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            raise RuntimeError(f"Failed splitting video at part {i+1}: {e}")

    return part_files


# -------------------------------------------------------------
# FastTelethon Parallel Chunked Upload Engine
# -------------------------------------------------------------
async def fast_parallel_upload(
    client: TelegramClient,
    filepath: str,
    connections: int = 4,
    on_progress: Optional[Callable[[float, int, int, float, float], None]] = None,
) -> Union[InputFile, InputFileBig]:
    """
    Streams file chunks to Telegram using parallel pipelined connections.
    Saturates high-speed connections (5+ MB/s) instead of sequential 200 KB/s.
    """
    file_size = os.path.getsize(filepath)
    part_size = 512 * 1024  # 512 KB optimal chunk size for Telegram
    total_parts = math.ceil(file_size / part_size)
    is_big = file_size > 10 * 1024 * 1024
    file_id = generate_file_id()
    file_name = Path(filepath).name

    start_time = time.time()
    last_callback_time = [0.0]
    uploaded_bytes = [0]
    lock = asyncio.Lock()

    # Pre-populate queue with part indices
    queue: asyncio.Queue[int] = asyncio.Queue()
    for idx in range(total_parts):
        queue.put_nowait(idx)

    # Compute MD5 for smaller files (required by Telegram InputFile)
    md5_hasher = hashlib.md5() if not is_big else None
    if not is_big:
        with open(filepath, "rb") as f:
            md5_hasher.update(f.read())

    async def worker() -> None:
        while True:
            try:
                part_idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            offset = part_idx * part_size
            with open(filepath, "rb") as f:
                f.seek(offset)
                chunk = f.read(part_size)

            # Send part with exponential backoff & FloodWait handling
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    if is_big:
                        req = SaveBigFilePartRequest(
                            file_id=file_id,
                            file_part=part_idx,
                            file_total_parts=total_parts,
                            bytes=chunk,
                        )
                    else:
                        req = SaveFilePartRequest(
                            file_id=file_id,
                            file_part=part_idx,
                            bytes=chunk,
                        )
                    await client(req)
                    break
                except errors.FloodWaitError as fwe:
                    wait_s = fwe.seconds + 2
                    logger.warning("FloodWait (%ds) encountered in parallel upload worker.", wait_s)
                    await asyncio.sleep(wait_s)
                except Exception as e:
                    if attempt == max_retries:
                        raise RuntimeError(f"Failed to upload part {part_idx + 1}/{total_parts}: {e}")
                    backoff = 1.5 ** attempt
                    await asyncio.sleep(backoff)

            # Thread-safe progress and speed calculation
            async with lock:
                uploaded_bytes[0] += len(chunk)
                now = time.time()
                elapsed = now - start_time
                if elapsed > 0:
                    speed_bps = uploaded_bytes[0] / elapsed
                    speed_mbps = speed_bps / (1024 * 1024)
                    remaining = max(0, file_size - uploaded_bytes[0])
                    eta_sec = (remaining / speed_bps) if speed_bps > 0 else 0.0
                else:
                    speed_mbps = 0.0
                    eta_sec = 0.0

                pct = (uploaded_bytes[0] / file_size * 100) if file_size else 0.0

                # Throttle progress callbacks to once every 3.0 seconds (or 100% completion)
                if (now - last_callback_time[0] >= 3.0) or (uploaded_bytes[0] == file_size):
                    last_callback_time[0] = now
                    mb_cur = uploaded_bytes[0] / (1024 * 1024)
                    mb_tot = file_size / (1024 * 1024)
                    eta_str = f"{int(eta_sec // 60)}m {int(eta_sec % 60):02d}s"

                    print(
                        f"\r  [FastUpload] {pct:5.1f}% ({mb_cur:6.1f}/{mb_tot:6.1f} MB) | "
                        f"Speed: {speed_mbps:4.2f} MB/s | ETA: {eta_str}",
                        end="",
                        flush=True,
                    )
                    if uploaded_bytes[0] == file_size:
                        print()

                    if on_progress:
                        try:
                            on_progress(pct, uploaded_bytes[0], file_size, speed_mbps, eta_sec)
                        except Exception:
                            pass

            queue.task_done()

    # Launch parallel worker tasks
    num_workers = min(connections, total_parts)
    workers = [asyncio.create_task(worker()) for _ in range(num_workers)]
    await asyncio.gather(*workers)

    if is_big:
        return InputFileBig(id=file_id, parts=total_parts, name=file_name)
    else:
        return InputFile(
            id=file_id,
            parts=total_parts,
            name=file_name,
            md5_checksum=md5_hasher.hexdigest(),
        )


class TelegramUploader:
    """
    High-Performance Telethon-based uploader supporting both Bot and User session modes.
    Mode 'bot' : Uses TELEGRAM_BOT_TOKEN to upload.
    Mode 'user': Uses an authenticated Telegram user account (sessions/ytdl_user_session).
    """

    def __init__(
        self,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
        connections: Optional[int] = None,
        keep_local: Optional[bool] = None,
        tools_dir: Optional[Path] = None,
        upload_mode: Optional[str] = None,
        phone_number: Optional[str] = None,
    ):
        self.tools_dir = tools_dir or TOOLS_DIR
        check_required_uploader_tools(self.tools_dir)

        # Upload Mode: 'bot' (default) or 'user'
        raw_mode = upload_mode or os.getenv("UPLOAD_MODE", "bot")
        self.upload_mode = raw_mode.strip().lower() if raw_mode else "bot"
        if self.upload_mode not in ("bot", "user"):
            self.upload_mode = "bot"

        raw_api_id = api_id or os.getenv("TELEGRAM_API_ID")
        self.api_id = int(raw_api_id) if raw_api_id and str(raw_api_id).strip().isdigit() else None
        self.api_hash = api_hash or os.getenv("TELEGRAM_API_HASH")
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")

        # Phone number for user session login
        raw_phone = phone_number or os.getenv("TELEGRAM_USER_PHONE") or os.getenv("TELEGRAM_PHONE") or os.getenv("PHONE_NUMBER")
        self.phone_number = str(raw_phone).strip() if raw_phone else None

        # Number of parallel upload connections (default 4)
        raw_conns = connections or os.getenv("UPLOAD_CONNECTIONS", "4")
        self.connections = int(raw_conns) if str(raw_conns).isdigit() else 4

        raw_channel = channel_id or os.getenv("TELEGRAM_CHANNEL_ID")
        if raw_channel:
            raw_channel_str = str(raw_channel).strip()
            if raw_channel_str.startswith("-100") or (
                raw_channel_str.startswith("-") and raw_channel_str[1:].isdigit()
            ):
                self.channel_id = int(raw_channel_str)
            elif raw_channel_str.isdigit():
                self.channel_id = int(raw_channel_str)
            else:
                self.channel_id = raw_channel_str
        else:
            self.channel_id = None

        if keep_local is not None:
            self.keep_local = keep_local
        else:
            self.keep_local = os.getenv("KEEP_LOCAL", "false").lower() in ("true", "1", "yes")

        self.sessions_dir = SESSIONS_DIR
        self.session_file_base: Optional[str] = None
        self.client: Optional[TelegramClient] = None
        self._channel_entity: Any = None

    def _validate_credentials(self) -> None:
        """Verifies that Telethon is installed and credentials are configured."""
        if TelegramClient is None:
            raise ImportError(
                "Telethon is not installed.\n"
                "Please run: pip install -r requirements.txt\n"
                "Or run:     pip install telethon cryptg"
            )

        missing = []
        if not self.api_id:
            missing.append("TELEGRAM_API_ID")
        if not self.api_hash:
            missing.append("TELEGRAM_API_HASH")
        if not self.channel_id:
            missing.append("TELEGRAM_CHANNEL_ID")

        if self.upload_mode == "bot" and not self.bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")

        if missing:
            raise ValueError(
                f"Missing required Telegram credentials in .env: {', '.join(missing)}\n"
                f"Please update your .env file."
            )

    def _get_session_path(self) -> str:
        """Returns the full path prefix for the session file in the sessions directory."""
        if self.upload_mode == "user":
            return str(self.sessions_dir / "ytdl_user_session")
        else:
            # Backward-compatible bot session location
            sessions_bot = self.sessions_dir / "ytdl_bot_session.session"
            root_bot = SCRIPT_DIR / "ytdl_bot_session.session"
            if sessions_bot.exists():
                return str(self.sessions_dir / "ytdl_bot_session")
            elif root_bot.exists():
                return str(SCRIPT_DIR / "ytdl_bot_session")
            return str(self.sessions_dir / "ytdl_bot_session")

    async def connect(self) -> None:
        """
        Initializes and connects the Telethon client.
        In 'user' mode: Performs interactive one-time login if session is not yet authorized.
        In 'bot' mode : Connects using the bot token.
        """
        self._validate_credentials()
        self.session_file_base = self._get_session_path()

        if not self.client:
            self.client = TelegramClient(
                self.session_file_base,
                self.api_id,
                self.api_hash,
                connection_retries=10,
                retry_delay=2,
            )

        if not self.client.is_connected():
            await self.client.connect()

        if self.upload_mode == "user":
            if not await self.client.is_user_authorized():
                print("\n" + "=" * 62)
                print("  Telegram User Account: One-Time Authentication")
                print(f"  Session storage : {self.sessions_dir}")
                print("  (Never share session files, verification codes, or 2FA passwords)")
                print("=" * 62)

                phone = self.phone_number
                if not phone:
                    phone = input("Please enter your Telegram phone number (with country code, e.g. +1234567890): ").strip()

                def interactive_code():
                    print("\n[!] A login code has been sent to your Telegram app (or SMS).")
                    return input("Enter the login code: ").strip()

                def interactive_password():
                    import getpass
                    print("\n[!] Two-Step Verification (2FA) is enabled on this account.")
                    return getpass.getpass("Enter your 2FA Cloud Password: ")

                await self.client.start(
                    phone=phone,
                    code_callback=interactive_code,
                    password=interactive_password,
                )

            me = await self.client.get_me()
            name = getattr(me, "first_name", "Telegram User")
            handle = f"@{me.username}" if getattr(me, "username", None) else f"ID:{me.id}"
            logger.info("[+] Telegram User Account connected: %s (%s). Mode: USER", name, handle)
            logger.info("[+] cryptg status: %s", CRYPTG_STATUS)
            logger.info("[+] Parallel Upload Connections: %d", self.connections)

        else:
            if not await self.client.is_user_authorized():
                await self.client.start(bot_token=self.bot_token)

            me = await self.client.get_me()
            bot_handle = f"@{me.username}" if getattr(me, "username", None) else f"ID:{me.id}"
            logger.info("[+] Telegram Bot connected: %s. Mode: BOT", bot_handle)
            logger.info("[+] cryptg status: %s", CRYPTG_STATUS)
            logger.info("[+] Parallel Upload Connections: %d", self.connections)

        # Pre-resolve target channel entity
        await self.get_target_entity()

    async def get_target_entity(self) -> Any:
        """Resolves and caches the channel entity from the configured channel ID."""
        if self._channel_entity is None and self.client and self.client.is_connected():
            try:
                self._channel_entity = await self.client.get_entity(self.channel_id)
                ch_title = getattr(self._channel_entity, "title", str(self.channel_id))
                logger.info("[+] Target Channel confirmed: '%s' (ID: %s)", ch_title, self.channel_id)
            except Exception as e:
                logger.warning(
                    "Could not resolve entity for channel ID %s (%s). Using raw ID. (Ensure account is channel member/admin)",
                    self.channel_id,
                    e,
                )
                self._channel_entity = self.channel_id
        return self._channel_entity or self.channel_id

    async def disconnect(self) -> None:
        """Gracefully disconnects the client."""
        if self.client and self.client.is_connected():
            await self.client.disconnect()
            logger.info("[-] Telegram Client disconnected.")

    async def upload_file(
        self,
        filepath: str,
        caption: Optional[str] = None,
        on_progress: Optional[Callable[[float, int, int, float, float], None]] = None,
    ) -> UploadResult:
        """
        Uploads a video to the configured Telegram Channel using FastTelethon parallel streaming.
        Supports both Bot and User session modes.
        If parallel upload fails, automatically falls back to standard client.send_file.
        Handles splitting files > 1.95 GB into sequenced parts.
        Deletes original file only on 100% successful completion (if KEEP_LOCAL is false).
        """
        if not os.path.isfile(filepath):
            return UploadResult(
                filepath=filepath,
                caption=caption or "",
                message_ids=[],
                total_parts=0,
                success=False,
                error=f"File not found on disk: {filepath}",
                upload_mode=self.upload_mode,
            )

        if not self.client or not self.client.is_connected():
            await self.connect()

        target_entity = await self.get_target_entity()

        total_file_size = os.path.getsize(filepath)
        video_caption = caption or Path(filepath).stem

        width, height, duration = get_video_metadata(filepath)
        parts = split_video_into_parts(filepath, duration, total_file_size)
        total_parts = len(parts)

        uploaded_message_ids: List[int] = []
        overall_start = time.time()

        try:
            for part_path, part_suffix, is_temp in parts:
                part_caption = f"{video_caption}{part_suffix}"
                part_w, part_h, part_dur = (width, height, duration) if total_parts == 1 else get_video_metadata(part_path)
                part_thumb = generate_thumbnail(part_path, part_dur)

                attributes = [
                    DocumentAttributeVideo(
                        duration=int(part_dur),
                        w=part_w,
                        h=part_h,
                        supports_streaming=True,
                    )
                ]

                part_size = os.path.getsize(part_path)
                part_start_time = time.time()

                sent_message = None

                # Step 1: Attempt Parallel Chunked Upload to Telegram
                try:
                    input_file = await fast_parallel_upload(
                        client=self.client,
                        filepath=part_path,
                        connections=self.connections,
                        on_progress=on_progress,
                    )

                    part_upload_time = max(0.01, time.time() - part_start_time)
                    part_avg_speed = (part_size / (1024 * 1024)) / part_upload_time
                    logger.info(
                        "[%s MODE] Parallel uploaded '%s' (%s MB) in %.1fs (Average speed: %.2f MB/s)",
                        self.upload_mode.upper(),
                        Path(part_path).name,
                        f"{part_size / (1024*1024):.1f}",
                        part_upload_time,
                        part_avg_speed,
                    )
                    print(
                        f"  [+] [{self.upload_mode.upper()}] Parallel upload finished in {part_upload_time:.1f}s "
                        f"(Speed: {part_avg_speed:.2f} MB/s)"
                    )

                    # Step 2: Send Media Message with Video Attributes
                    sent_message = await self.client.send_file(
                        entity=target_entity,
                        file=input_file,
                        caption=part_caption,
                        thumb=part_thumb,
                        attributes=attributes,
                        supports_streaming=True,
                    )
                except Exception as fast_err:
                    logger.warning(
                        "[%s MODE] Parallel upload failed for '%s' (%s). Falling back to standard send_file...",
                        self.upload_mode.upper(),
                        Path(part_path).name,
                        fast_err,
                    )
                    print(f"\n  [!] Parallel upload failed ({fast_err}). Falling back to standard send_file...")

                    last_std_cb = [0.0]

                    def std_progress_callback(cur_bytes: int, tot_bytes: int) -> None:
                        now = time.time()
                        pct = (cur_bytes / tot_bytes * 100) if tot_bytes else 0.0
                        if (now - last_std_cb[0] >= 3.0) or (cur_bytes == tot_bytes):
                            last_std_cb[0] = now
                            elapsed = max(0.01, now - part_start_time)
                            speed_bps = cur_bytes / elapsed
                            speed_mbps = speed_bps / (1024 * 1024)
                            rem_bytes = max(0, tot_bytes - cur_bytes)
                            eta_sec = (rem_bytes / speed_bps) if speed_bps > 0 else 0.0
                            if on_progress:
                                try:
                                    on_progress(pct, cur_bytes, tot_bytes, speed_mbps, eta_sec)
                                except Exception:
                                    pass

                    sent_message = await self.client.send_file(
                        entity=target_entity,
                        file=part_path,
                        caption=part_caption,
                        thumb=part_thumb,
                        attributes=attributes,
                        supports_streaming=True,
                        progress_callback=std_progress_callback,
                    )

                    part_upload_time = max(0.01, time.time() - part_start_time)
                    part_avg_speed = (part_size / (1024 * 1024)) / part_upload_time
                    logger.info(
                        "[%s MODE] Standard uploaded '%s' (%s MB) in %.1fs (Average speed: %.2f MB/s)",
                        self.upload_mode.upper(),
                        Path(part_path).name,
                        f"{part_size / (1024*1024):.1f}",
                        part_upload_time,
                        part_avg_speed,
                    )

                if part_thumb and os.path.exists(part_thumb):
                    try:
                        os.remove(part_thumb)
                    except OSError:
                        pass

                if is_temp and os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass

                if sent_message and sent_message.id:
                    uploaded_message_ids.append(sent_message.id)
                else:
                    raise RuntimeError(f"Telegram upload failed to return message ID for: {part_path}")

            total_elapsed = max(0.01, time.time() - overall_start)
            overall_avg_speed = (total_file_size / (1024 * 1024)) / total_elapsed

            logger.info(
                "[%s MODE] Upload complete for '%s' (%s MB) in %.1fs (Average speed: %.2f MB/s)",
                self.upload_mode.upper(),
                Path(filepath).name,
                f"{total_file_size / (1024*1024):.1f}",
                total_elapsed,
                overall_avg_speed,
            )

            if not self.keep_local:
                try:
                    os.remove(filepath)
                except OSError:
                    pass

            return UploadResult(
                filepath=filepath,
                caption=video_caption,
                message_ids=uploaded_message_ids,
                total_parts=total_parts,
                success=True,
                error=None,
                average_speed_mbps=overall_avg_speed,
                elapsed_seconds=total_elapsed,
                upload_mode=self.upload_mode,
            )

        except Exception as e:
            return UploadResult(
                filepath=filepath,
                caption=video_caption,
                message_ids=uploaded_message_ids,
                total_parts=total_parts,
                success=False,
                error=str(e),
                upload_mode=self.upload_mode,
            )


# -------------------------------------------------------------
# Standalone CLI Helper (for one-time interactive login)
# -------------------------------------------------------------
async def cli_interactive_login(mode: str = "user") -> None:
    """Performs one-time interactive login and verifies Telegram connectivity."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
    uploader = TelegramUploader(upload_mode=mode)
    print("\n" + "=" * 65)
    print(f"  ytdl-telegram: Telegram Authentication Helper (Mode: {mode.upper()})")
    print("=" * 65)
    await uploader.connect()

    me = await uploader.client.get_me()
    print("\n" + "-" * 65)
    print("  [SUCCESS] Authenticated successfully!")
    if mode == "user":
        name = getattr(me, "first_name", "User")
        user_handle = f"@{me.username}" if getattr(me, "username", None) else "none"
        print(f"  Account Name   : {name}")
        print(f"  Username       : {user_handle}")
        print(f"  User ID        : {me.id}")
        print(f"  Session File   : {uploader.session_file_base}.session")
    else:
        print(f"  Bot Username   : @{getattr(me, 'username', 'none')}")
        print(f"  Bot ID         : {me.id}")
        print(f"  Session File   : {uploader.session_file_base}.session")
    print("-" * 65)
    print("  You are ready to upload videos!")
    print("=" * 65 + "\n")
    await uploader.disconnect()


def main():
    parser = argparse.ArgumentParser(description="ytdl-telegram Uploader & Session Manager")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Perform one-time interactive Telegram login (for User or Bot session)",
    )
    parser.add_argument(
        "--mode",
        choices=["bot", "user"],
        default=None,
        help="Override upload mode (bot or user)",
    )
    args = parser.parse_args()

    selected_mode = args.mode or os.getenv("UPLOAD_MODE", "user" if args.login else "bot").strip().lower()
    if selected_mode not in ("bot", "user"):
        selected_mode = "bot"

    if args.login:
        asyncio.run(cli_interactive_login(mode=selected_mode))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
