"""
uploader_v2.py - Telegram video uploader using Telethon as a Bot.

Part of the "ytdl-telegram" project.
Handles video uploads to Telegram channels/groups with:
- Portable tools detection (uses ./tools for ffmpeg.exe & ffprobe.exe)
- Video metadata (duration, width, height) & generated thumbnails via ffmpeg
- Fast streaming support (supports_streaming=True)
- Zero-re-encode splitting for files > 1.95 GB into Parts
- Live upload progress percentage in console
- FloodWait and network error retry with exponential backoff
- Configurable local cleanup (KEEP_LOCAL=true/false)
"""

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

# Attempt to load .env using python-dotenv, or fall back to simple line parser
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

# Portable tools configuration
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_TOOLS_DIR = os.getenv("TOOLS_DIR")
TOOLS_DIR = Path(ENV_TOOLS_DIR).resolve() if ENV_TOOLS_DIR else (SCRIPT_DIR / "tools")

# Prepend portable tools directory to process PATH so ffmpeg & ffprobe are found directly
tools_str = str(TOOLS_DIR)
current_path = os.environ.get("PATH", "")
if tools_str not in current_path:
    os.environ["PATH"] = tools_str + os.pathsep + current_path

try:
    from telethon import TelegramClient, errors
    from telethon.tl.types import DocumentAttributeVideo
except ImportError:
    TelegramClient = None
    errors = None
    DocumentAttributeVideo = None

# Threshold for Telegram Bot upload: 1.95 GB
MAX_TELEGRAM_BOT_FILE_SIZE = int(1.95 * 1024 * 1024 * 1024)


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


def get_video_metadata(filepath: str) -> Tuple[int, int, float]:
    """
    Extracts (width, height, duration_seconds) using ffprobe.
    Returns (1280, 720, 0.0) fallback if extraction fails.
    """
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
            width = int(streams[0].get("width") or default_w)
            height = int(streams[0].get("height") or default_h)
            if streams[0].get("duration"):
                try:
                    duration = float(streams[0]["duration"])
                except (ValueError, TypeError):
                    pass

        # If stream duration was missing, check container format duration
        if duration == 0.0 and data.get("format", {}).get("duration"):
            try:
                duration = float(data["format"]["duration"])
            except (ValueError, TypeError):
                pass

        return width, height, duration
    except Exception:
        return default_w, default_h, default_dur


def generate_thumbnail(filepath: str, duration: float) -> Optional[str]:
    """
    Generates a JPEG thumbnail scaled to max width 320 at ~15% of the video duration.
    """
    thumb_path = f"{filepath}_thumb.jpg"
    seek_time = max(1.0, min(duration * 0.15, 30.0))

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
    except Exception:
        pass
    return None


def split_video_into_parts(filepath: str, duration: float, file_size: int) -> List[Tuple[str, str, bool]]:
    """
    Splits video exceeding 1.95 GB into smaller parts using ffmpeg stream copy (-c copy).
    Zero re-encoding, extremely fast.
    Returns a list of tuples: (part_filepath, part_title_suffix, is_temporary_flag).
    """
    if file_size <= MAX_TELEGRAM_BOT_FILE_SIZE:
        return [(filepath, "", False)]

    # Calculate part count (each part ~ 1.80 GB to be safely under 1.95 GB)
    target_part_size = int(1.80 * 1024 * 1024 * 1024)
    num_parts = math.ceil(file_size / target_part_size)
    part_duration = duration / num_parts

    base, ext = os.path.splitext(filepath)
    part_files: List[Tuple[str, str, bool]] = []

    print(f"\n  [!] File size ({file_size / (1024**3):.2f} GB) exceeds 1.95 GB limit.")
    print(f"      Splitting into {num_parts} parts using ffmpeg stream-copy (no quality loss)...")

    for i in range(num_parts):
        start_time = i * part_duration
        part_path = f"{base}.part{i+1}of{num_parts}{ext}"
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
            # Clean up previously generated parts on failure
            for p, _, is_temp in part_files:
                if is_temp and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            raise RuntimeError(f"Failed splitting video at part {i+1}: {e}")

    return part_files


def create_progress_callback(filename: str) -> Callable[[int, int], None]:
    """Returns a progress callback displaying percentage and MBs transferred."""
    last_update_time = [0.0]

    def callback(current: int, total: int) -> None:
        now = time.time()
        # Update once per second or when upload completes
        if now - last_update_time[0] >= 1.0 or current == total:
            last_update_time[0] = now
            percent = (current / total) * 100 if total else 0.0
            mb_cur = current / (1024 * 1024)
            mb_tot = total / (1024 * 1024)
            short_name = Path(filename).name
            if len(short_name) > 30:
                short_name = short_name[:27] + "..."
            print(
                f"\r  [Upload] {percent:5.1f}% ({mb_cur:6.1f} MB / {mb_tot:6.1f} MB) - {short_name}",
                end="",
                flush=True,
            )
            if current == total:
                print()  # Newline after complete

    return callback


class TelegramUploader:
    """Telethon-based video uploader operating as a Telegram Bot."""

    def __init__(
        self,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
        keep_local: Optional[bool] = None,
        tools_dir: Optional[Path] = None,
    ):
        self.tools_dir = tools_dir or TOOLS_DIR
        check_required_uploader_tools(self.tools_dir)

        raw_api_id = api_id or os.getenv("TELEGRAM_API_ID")
        self.api_id = int(raw_api_id) if raw_api_id and str(raw_api_id).strip().isdigit() else None
        self.api_hash = api_hash or os.getenv("TELEGRAM_API_HASH")
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")

        # Channel ID can be integer (-100xxxxxxx) or public username string (@channel)
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

        self.client: Optional[TelegramClient] = None

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
        if not self.bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.channel_id:
            missing.append("TELEGRAM_CHANNEL_ID")

        if missing:
            raise ValueError(
                f"Missing required Telegram credentials in .env: {', '.join(missing)}\n"
                "Please configure them in your .env file."
            )

    async def connect(self) -> None:
        """Initializes and authenticates the Telethon Bot client."""
        self._validate_credentials()
        if self.client and self.client.is_connected():
            return

        session_name = "ytdl_bot_session"
        self.client = TelegramClient(session_name, self.api_id, self.api_hash)
        await self.client.start(bot_token=self.bot_token)
        print("  [+] Telegram Bot connected successfully.")

    async def disconnect(self) -> None:
        """Gracefully disconnects the Telegram client."""
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def upload_file(
        self,
        filepath: str,
        caption: Optional[str] = None,
    ) -> UploadResult:
        """
        Uploads a video to the configured Telegram channel/group.
        Handles splitting, thumbnail creation, streaming attributes, progress, and cleanup.
        """
        self._validate_credentials()
        if not os.path.exists(filepath):
            return UploadResult(
                filepath=filepath,
                caption=caption or "",
                message_ids=[],
                total_parts=0,
                success=False,
                error=f"File not found: {filepath}",
            )

        if not self.client or not self.client.is_connected():
            await self.connect()

        file_size = os.path.getsize(filepath)
        video_caption = caption or Path(filepath).stem

        # Extract video metadata
        width, height, duration = get_video_metadata(filepath)

        # Split into parts if file exceeds 1.95 GB limit
        parts = split_video_into_parts(filepath, duration, file_size)
        total_parts = len(parts)

        uploaded_message_ids: List[int] = []

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

                progress_cb = create_progress_callback(part_path)

                # Upload with FloodWait protection and exponential backoff retry
                max_retries = 5
                sent_message = None

                for attempt in range(1, max_retries + 1):
                    try:
                        sent_message = await self.client.send_file(
                            entity=self.channel_id,
                            file=part_path,
                            caption=part_caption,
                            thumb=part_thumb,
                            attributes=attributes,
                            supports_streaming=True,
                            progress_callback=progress_cb,
                        )
                        break
                    except errors.FloodWaitError as fwe:
                        wait_seconds = fwe.seconds + 2
                        print(f"\n  [!] Telegram FloodWait: Pausing for {wait_seconds}s before retry...")
                        await asyncio.sleep(wait_seconds)
                    except (errors.RPCError, ConnectionError, TimeoutError, OSError) as net_err:
                        if attempt == max_retries:
                            raise
                        backoff = 2 ** attempt
                        print(f"\n  [!] Upload error ({net_err}). Retrying in {backoff}s (Attempt {attempt}/{max_retries})...")
                        await asyncio.sleep(backoff)

                # Clean up part thumbnail
                if part_thumb and os.path.exists(part_thumb):
                    try:
                        os.remove(part_thumb)
                    except OSError:
                        pass

                # Clean up temporary split file
                if is_temp and os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass

                if sent_message and sent_message.id:
                    uploaded_message_ids.append(sent_message.id)
                else:
                    raise RuntimeError(f"Telegram upload failed to return message ID for: {part_path}")

            # All parts uploaded successfully! Clean up original file if KEEP_LOCAL=false
            if not self.keep_local:
                try:
                    os.remove(filepath)
                    print(f"  [Cleanup] Deleted local file: {filepath}")
                except OSError as e:
                    print(f"  [Warning] Could not delete local file ({e})")

            return UploadResult(
                filepath=filepath,
                caption=video_caption,
                message_ids=uploaded_message_ids,
                total_parts=total_parts,
                success=True,
                error=None,
            )

        except Exception as e:
            return UploadResult(
                filepath=filepath,
                caption=video_caption,
                message_ids=uploaded_message_ids,
                total_parts=total_parts,
                success=False,
                error=str(e),
            )


# ============================================================
# CLI Test Runner
# ============================================================
async def _async_cli_test(test_filepath: str) -> None:
    uploader = TelegramUploader()
    try:
        await uploader.connect()
        print(f"\n  Uploading test file: {test_filepath}")
        print(f"  Destination channel: {uploader.channel_id}")
        print("  Starting transfer...\n")

        result = await uploader.upload_file(test_filepath)
        print("\n" + "=" * 50)
        if result.success:
            print(f"  [SUCCESS] Uploaded {result.total_parts} part(s) to Telegram!")
            print(f"  Message ID(s) : {result.message_ids}")
        else:
            print(f"  [FAILED] Reason: {result.error}")
        print("=" * 50)
    finally:
        await uploader.disconnect()


def run_cli_uploader() -> None:
    """CLI utility to test-upload any video to your Telegram channel."""
    print("\n" + "#" * 50)
    print("#        ytdl-telegram: Telegram Uploader (v2)     #")
    print("#" * 50)

    target_file = ""
    if len(sys.argv) > 1:
        target_file = sys.argv[1]
    else:
        target_file = input("\n  Enter path to a video file to test upload: ").strip()

    # Strip quotes if copied from Windows Explorer
    target_file = target_file.strip('\"').strip("\'")

    if not target_file:
        print("  [!] No file path provided.")
        sys.exit(1)

    if not os.path.exists(target_file):
        print(f"  [!] File not found: {target_file}")
        sys.exit(1)

    try:
        asyncio.run(_async_cli_test(target_file))
    except KeyboardInterrupt:
        print("\n  [!] Upload cancelled by user.")
    except Exception as e:
        print(f"\n  [ERROR] {e}")


if __name__ == "__main__":
    run_cli_uploader()
