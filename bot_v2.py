"""
bot_v2.py - Telegram Bot Controller for ytdl-telegram with Rotating File Logging.

Features:
- FastTelethon parallel uploading via uploader_v3.py (with automatic fallback to send_file)
- Cryptg hardware-accelerated AES-IGE encryption logging
- Rotating file logging (logs/bot.log with 5MB max size and 3 backups)
- Live status message throttled to 3.0 seconds with Speed (MB/s) & ETA
- Single Source of Truth state tracking (SQLite jobs.db)
- Two-stage retry pipeline: skips download if video file is already on disk
- Thread-safe asyncio dispatching from yt-dlp worker threads
- Owner-Only Security: Only responds to ALLOWED_USER_ID from .env
- Commands: /start, /help, /playlist, /single, /status, /pause, /resume, /retry_failed, /cancel, /folder
- Batch completion summary with succeeded/failed counts and titles
"""

import asyncio
from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Setup directories
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

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

# -------------------------------------------------------------
# Rotating File Logger Configuration
# -------------------------------------------------------------
logger = logging.getLogger("ytdl_telegram")
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s [%(levelname)s]: %(message)s", datefmt="%H:%M:%S")
console_handler.setFormatter(console_formatter)

# Rotating file handler (5 MB per file, max 3 backups)
log_file_path = LOGS_DIR / "bot.log"
file_handler = RotatingFileHandler(
    log_file_path,
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
file_handler.setFormatter(file_formatter)

if not logger.handlers:
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

import yt_dlp
from telethon import TelegramClient, events, errors
from telethon.tl.custom import Message

from downloader_v4 import YTDownloader, TOOLS_DIR, check_required_tools
from state_v2 import StateManager, Job
from uploader_v4 import TelegramUploader, CRYPTG_STATUS, UPLOAD_MODE

# -------------------------------------------------------------
# Configuration & Security Verification
# -------------------------------------------------------------
raw_api_id = os.getenv("TELEGRAM_API_ID")
raw_api_hash = os.getenv("TELEGRAM_API_HASH")
raw_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
raw_allowed_uid = os.getenv("ALLOWED_USER_ID")

if not raw_api_id or not raw_api_hash or not raw_bot_token:
    logger.error("Telegram credentials (API_ID, API_HASH, BOT_TOKEN) are missing in .env!")
    sys.exit(1)

if not raw_allowed_uid or not str(raw_allowed_uid).strip().lstrip("-").isdigit():
    logger.error("ALLOWED_USER_ID is not configured in .env! (e.g. ALLOWED_USER_ID=123456789)")
    sys.exit(1)

API_ID = int(raw_api_id.strip())
API_HASH = raw_api_hash.strip()
BOT_TOKEN = raw_bot_token.strip()
ALLOWED_USER_ID = int(raw_allowed_uid.strip())

bot = TelegramClient("ytdl_telegram_bot_session", API_ID, API_HASH)
state = StateManager()


# -------------------------------------------------------------
# Background Pipeline Worker
# -------------------------------------------------------------
class BackgroundWorker:
    """Manages the background download/upload loop so the bot stays responsive."""

    def __init__(self, bot_client: TelegramClient, state_mgr: StateManager):
        self.bot = bot_client
        self.state = state_mgr
        self.is_running = False
        self.is_paused = False
        self.cancel_requested = False
        self.current_job: Optional[Job] = None
        self.status_msg: Optional[Message] = None
        self.last_edit_time = 0.0
        self.worker_task: Optional[asyncio.Task] = None
        self.target_chat_id = ALLOWED_USER_ID
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def trigger(self) -> None:
        """Starts the worker task if not already running."""
        if not self.is_running:
            self.cancel_requested = False
            self.worker_task = asyncio.create_task(self.run_loop())

    def pause(self) -> None:
        self.is_paused = True
        logger.info("Worker pause requested by user.")

    def resume(self) -> None:
        self.is_paused = False
        logger.info("Worker resume requested by user.")
        self.trigger()

    def cancel(self) -> None:
        self.cancel_requested = True
        cleared = self.state.cancel_queued_jobs()
        logger.warning("Worker cancellation requested. Cleared %d pending queued jobs.", cleared)

    def dispatch_status(self, coro) -> None:
        """
        Safely dispatches a status update coroutine to the main event loop
        from either async code or worker threads (e.g., yt-dlp hook).
        """
        try:
            curr_loop = asyncio.get_running_loop()
            if curr_loop == self.loop:
                curr_loop.create_task(coro)
                return
        except RuntimeError:
            pass

        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def update_live_status(
        self,
        current_num: int,
        total_num: int,
        title: str,
        stage: str,
        percent: float,
        transferred_str: str = "",
        speed_mbps: float = 0.0,
        eta_seconds: float = 0.0,
        force: bool = False,
    ) -> None:
        """Edits the single live status message (throttled to once every 3.0s)."""
        now = time.time()
        if not force and (now - self.last_edit_time < 3.0) and percent < 100.0:
            return

        self.last_edit_time = now
        bar_len = 10
        filled = int((percent / 100.0) * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        short_title = title if len(title) <= 50 else title[:47] + "..."
        extra = f" ({transferred_str})" if transferred_str else ""

        speed_line = ""
        if speed_mbps > 0.0 or eta_seconds > 0.0:
            eta_str = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60):02d}s"
            speed_line = f"\n🚀 **Speed:** `{speed_mbps:4.2f} MB/s` | ⏳ **ETA:** `{eta_str}`"

        text = (
            f"🎬 **[{current_num}/{total_num}] {short_title}**\n\n"
            f"⚙️ **Stage:** {stage}\n"
            f"📊 **Progress:** `[{bar}]` {percent:5.1f}%{extra}"
            f"{speed_line}\n"
            f"⏱️ _Updating live every 3s..._"
        )

        try:
            if not self.status_msg:
                self.status_msg = await self.bot.send_message(self.target_chat_id, text)
            else:
                await self.status_msg.edit(text)
        except errors.MessageNotModifiedError:
            pass
        except errors.FloodWaitError as fwe:
            logger.warning("FloodWait encountered in status edit: sleeping %ds", fwe.seconds)
            await asyncio.sleep(fwe.seconds + 1)
        except Exception as e:
            logger.debug("Minor status message edit error: %s", e)

    async def run_loop(self) -> None:
        """Sequential processing loop running in background."""
        self.is_running = True
        self.loop = asyncio.get_running_loop()
        logger.info("Pipeline background worker started.")

        recovered = self.state.reset_interrupted_jobs()
        if recovered > 0:
            logger.info("Recovered %d interrupted job(s) back to queue.", recovered)

        uploader = TelegramUploader()
        downloader = YTDownloader()
        await uploader.connect()

        processed_in_this_run = 0

        try:
            while not self.cancel_requested:
                if self.is_paused:
                    await asyncio.sleep(1.0)
                    continue

                job = self.state.get_next_pending_job()
                if not job:
                    logger.info("No more pending jobs in queue.")
                    break

                self.current_job = job
                processed_in_this_run += 1
                stats = self.state.get_stats()
                cur_num = stats["uploaded"] + 1
                tot_num = stats["total"]

                logger.info(
                    "Processing [%d/%d]: '%s' (ID: %s, Quality: %s)",
                    cur_num, tot_num, job.title, job.video_id, job.quality
                )

                # ---------------------------------------------------------
                # Check if file is ALREADY downloaded on disk
                # ---------------------------------------------------------
                downloaded_file: Optional[str] = None
                if job.file_path and os.path.isfile(job.file_path) and os.path.getsize(job.file_path) > 0:
                    downloaded_file = job.file_path
                    logger.info(
                        "Found existing downloaded file for '%s': %s (Skipping download phase)",
                        job.video_id,
                        downloaded_file,
                    )
                else:
                    # ---------------------------------------------------------
                    # PHASE 1: DOWNLOAD
                    # ---------------------------------------------------------
                    self.state.mark_downloading(job.video_id)
                    await self.update_live_status(
                        current_num=cur_num,
                        total_num=tot_num,
                        title=job.title,
                        stage="📥 Downloading from YouTube",
                        percent=0.0,
                        force=True,
                    )

                    dl_start_time = time.time()
                    last_dl_tick = [0.0]

                    def dl_progress(pct: float, cur_bytes: int, tot_bytes: int) -> None:
                        now = time.time()
                        if (now - last_dl_tick[0] < 2.5) and pct < 100.0:
                            return
                        last_dl_tick[0] = now
                        elapsed = max(0.01, now - dl_start_time)
                        speed_bps = cur_bytes / elapsed
                        speed_mb = speed_bps / (1024 * 1024)
                        rem_bytes = max(0, tot_bytes - cur_bytes)
                        eta_s = (rem_bytes / speed_bps) if speed_bps > 0 else 0

                        cur_mb = cur_bytes / (1024 * 1024)
                        tot_mb = tot_bytes / (1024 * 1024)
                        t_str = f"{cur_mb:.1f}/{tot_mb:.1f} MB"

                        self.dispatch_status(
                            self.update_live_status(
                                current_num=cur_num,
                                total_num=tot_num,
                                title=job.title,
                                stage="📥 Downloading from YouTube",
                                percent=pct,
                                transferred_str=t_str,
                                speed_mbps=speed_mb,
                                eta_seconds=eta_s,
                            )
                        )

                    is_audio = (job.quality.lower() == "audio")
                    watch_url = f"https://www.youtube.com/watch?v={job.video_id}"

                    loop = asyncio.get_running_loop()
                    dl_results = await loop.run_in_executor(
                        None,
                        lambda: downloader.download(
                            urls=[watch_url],
                            quality=job.quality,
                            is_audio=is_audio,
                            on_progress=dl_progress,
                        ),
                    )

                    if self.cancel_requested:
                        logger.warning("Pipeline cancelled during download phase.")
                        break

                    if not dl_results or not dl_results[0].success or not dl_results[0].path:
                        err = dl_results[0].error if dl_results else "Download returned no path"
                        retries = self.state.record_failure(job.video_id, f"Download error: {err}")
                        logger.error("Download failed for '%s' (Attempt %d/3): %s", job.video_id, retries, err)
                        continue

                    downloaded_file = dl_results[0].path
                    self.state.mark_downloaded(job.video_id, downloaded_file)
                    logger.info("Download completed: %s", downloaded_file)

                # ---------------------------------------------------------
                # PHASE 2: UPLOAD (Parallel Fast Upload with Fallback)
                # ---------------------------------------------------------
                upload_stage_name = f"📤 Fast Parallel Upload [{uploader.upload_mode.upper()} Mode]"
                self.state.mark_uploading(job.video_id)
                await self.update_live_status(
                    current_num=cur_num,
                    total_num=tot_num,
                    title=job.title,
                    stage=upload_stage_name,
                    percent=0.0,
                    force=True,
                )

                def up_progress(pct: float, cur_bytes: int, tot_bytes: int, spd_mbps: float, eta_s: float) -> None:
                    cur_mb = cur_bytes / (1024 * 1024)
                    tot_mb = tot_bytes / (1024 * 1024)
                    t_str = f"{cur_mb:.1f}/{tot_mb:.1f} MB"
                    self.dispatch_status(
                        self.update_live_status(
                            current_num=cur_num,
                            total_num=tot_num,
                            title=job.title,
                            stage=upload_stage_name,
                            percent=pct,
                            transferred_str=t_str,
                            speed_mbps=spd_mbps,
                            eta_seconds=eta_s,
                        )
                    )

                upload_res = await uploader.upload_file(
                    filepath=downloaded_file,
                    caption=job.title,
                    on_progress=up_progress,
                )

                if self.cancel_requested:
                    logger.warning("Pipeline cancelled during upload phase.")
                    break

                if not upload_res.success:
                    retries = self.state.record_failure(
                        job.video_id,
                        f"Upload error: {upload_res.error}",
                        keep_file_path=True,
                    )
                    logger.error("Upload failed for '%s' (Attempt %d/3): %s", job.video_id, retries, upload_res.error)
                    # Keep local file on disk so retry can proceed directly to upload!
                    continue

                # ---------------------------------------------------------
                # PHASE 3: MARK UPLOADED
                # ---------------------------------------------------------
                msg_ids = ",".join(str(m) for m in upload_res.message_ids)
                self.state.mark_uploaded(job.video_id, telegram_message_id=msg_ids)
                logger.info(
                    "Successfully uploaded '%s' in %.1fs at %.2f MB/s [Mode: %s] (Msg IDs: %s)",
                    job.video_id,
                    upload_res.elapsed_seconds,
                    upload_res.average_speed_mbps,
                    upload_res.upload_mode.upper(),
                    msg_ids,
                )

            # Send final completion summary if videos were processed
            if processed_in_this_run > 0:
                final_stats = self.state.get_stats()
                failed_jobs = self.state.get_failed_jobs()

                summary_text = (
                    "🎉 **Batch Complete!**\n\n"
                    f"✅ **Succeeded:** {final_stats['uploaded']}\n"
                    f"❌ **Failed:** {len(failed_jobs)}\n"
                    f"📁 **Total Tracked:** {final_stats['total']}\n"
                )

                if failed_jobs:
                    summary_text += "\n⚠️ **Failed Titles:**\n"
                    for f in failed_jobs[:10]:
                        summary_text += f"• `{f.video_id}` — {f.title[:35]}...\n"
                    if len(failed_jobs) > 10:
                        summary_text += f"_...and {len(failed_jobs) - 10} more._\n"

                await self.bot.send_message(self.target_chat_id, summary_text)
                logger.info("Batch completed. Summary sent to user.")

        except Exception as e:
            logger.exception("Unexpected error in worker loop: %s", e)
        finally:
            await uploader.disconnect()
            self.is_running = False
            self.current_job = None
            self.status_msg = None
            logger.info("Pipeline worker loop ended.")


worker = BackgroundWorker(bot_client=bot, state_mgr=state)


# -------------------------------------------------------------
# Bot Command Handlers (Restricted to ALLOWED_USER_ID)
# -------------------------------------------------------------
@bot.on(events.NewMessage)
async def handle_user_command(event: events.NewMessage.Event) -> None:
    """Security filter: ignores any user other than ALLOWED_USER_ID."""
    if event.sender_id != ALLOWED_USER_ID:
        logger.warning(
            "Ignored unauthorized message from sender_id: %s (Username: @%s)",
            event.sender_id,
            getattr(event.sender, "username", "none"),
        )
        return

    text = event.raw_text.strip()
    if not text.startswith("/"):
        return

    parts = text.split()
    raw_cmd = parts[0].lower()
    # Support commands with bot username suffix e.g. /status@ytuploader5_bot
    cmd = raw_cmd.split("@")[0]
    args = parts[1:]

    logger.info("Received command from owner: %s (Args: %s)", cmd, args)

    # ---------------------------------------------------------
    # COMMAND: /start or /help
    # ---------------------------------------------------------
    if cmd in ("/start", "/help"):
        help_msg = (
            "🤖 **Welcome to ytdl-telegram Controller!**\n\n"
            "**Commands:**\n"
            "• `/playlist <url> [quality] [items]`\n"
            "  _Enqueue playlist & start processing_\n"
            "  _e.g. `/playlist https://... 1080 1-10`_\n"
            "• `/single <url> [quality]`\n"
            "  _Enqueue single video & start_\n"
            "  _e.g. `/single https://... 720`_\n"
            "• `/status` - Current queue counts & active job\n"
            "• `/pause` - Pause queue after current video\n"
            "• `/resume` - Resume paused queue\n"
            "• `/retry_failed` - Reset and retry any failed jobs\n"
            "• `/cancel` - Stop worker & clear queued jobs\n"
            "• `/folder` - Disk space & portable tools health"
        )
        await event.reply(help_msg)

    # ---------------------------------------------------------
    # COMMAND: /status
    # ---------------------------------------------------------
    elif cmd == "/status":
        stats = state.get_stats()
        worker_state_str = "🟢 Running" if worker.is_running else "⚪ Idle"
        if worker.is_paused:
            worker_state_str = "⏸️ Paused"

        status_text = (
            f"📊 **ytdl-telegram Status**\n\n"
            f"**Worker State:** {worker_state_str}\n"
        )

        if worker.current_job:
            status_text += f"🎬 **Active:** `{worker.current_job.video_id}` — {worker.current_job.title[:40]}...\n\n"

        status_text += (
            f"• **Uploaded:** {stats['uploaded']}\n"
            f"• **Queued:** {stats['queued']}\n"
            f"• **Downloading:** {stats['downloading']}\n"
            f"• **Downloaded (Ready to upload):** {stats['downloaded']}\n"
            f"• **Uploading:** {stats['uploading']}\n"
            f"• **Failed (3 retries):** {stats['failed']}\n"
            f"• **Total Tracked:** {stats['total']}"
        )
        await event.reply(status_text)

    # ---------------------------------------------------------
    # COMMAND: /pause
    # ---------------------------------------------------------
    elif cmd == "/pause":
        if not worker.is_running:
            await event.reply("⚪ Worker is currently idle.")
        else:
            worker.pause()
            await event.reply("⏸️ **Pipeline paused.** Current video will complete, then processing will halt.")

    # ---------------------------------------------------------
    # COMMAND: /resume
    # ---------------------------------------------------------
    elif cmd == "/resume":
        if worker.is_running and not worker.is_paused:
            await event.reply("🟢 Pipeline is already actively running.")
        else:
            worker.resume()
            await event.reply("▶️ **Pipeline resumed.** Continuing queue processing...")

    # ---------------------------------------------------------
    # COMMAND: /retry_failed
    # ---------------------------------------------------------
    elif cmd == "/retry_failed":
        reset_cnt = state.retry_all_failed()
        if reset_cnt == 0:
            await event.reply("ℹ️ No failed jobs found in database to retry.")
        else:
            worker.resume()
            await event.reply(
                f"🔄 **Retrying Failed Jobs!**\n\n"
                f"• Reset **{reset_cnt}** failed job(s) back to queue.\n"
                f"• Files already downloaded on disk will skip downloading and upload directly.\n"
                f"🚀 Worker triggered!"
            )

    # ---------------------------------------------------------
    # COMMAND: /cancel
    # ---------------------------------------------------------
    elif cmd == "/cancel":
        worker.cancel()
        await event.reply("🛑 **Cancelled.** Pending queued jobs have been cleared from database.")

    # ---------------------------------------------------------
    # COMMAND: /folder
    # ---------------------------------------------------------
    elif cmd == "/folder":
        dest_dir = os.getenv("DOWNLOAD_DIR", "downloads")
        os.makedirs(dest_dir, exist_ok=True)
        total, used, free = shutil.disk_usage(dest_dir)
        free_gb = free / (1024 ** 3)
        total_gb = total / (1024 ** 3)

        tools_dir = TOOLS_DIR
        tools_ok = all((tools_dir / t).is_file() for t in ["ffmpeg.exe", "ffprobe.exe", "deno.exe"])
        tools_status = "✅ Present & Healthy" if tools_ok else "❌ Missing some files"

        conns = os.getenv("UPLOAD_CONNECTIONS", "4")

        folder_info = (
            "📁 **System & Storage Info**\n\n"
            f"• **Download Folder:** `{os.path.abspath(dest_dir)}`\n"
            f"• **Free Disk Space:** `{free_gb:.2f} GB` / `{total_gb:.2f} GB`\n"
            f"• **Upload Mode:** `{UPLOAD_MODE.upper()}`\n"
            f"• **Upload Connections:** `{conns} parallel workers`\n"
            f"• **cryptg Status:** `{CRYPTG_STATUS}`\n"
            f"• **Keep Local Files:** `{os.getenv('KEEP_LOCAL', 'false')}`\n"
            f"• **Log File:** `{os.path.abspath(log_file_path)}`\n"
            f"• **Portable Tools:** `{tools_status}`\n"
            f"  `({tools_dir})`"
        )
        await event.reply(folder_info)

    # ---------------------------------------------------------
    # COMMAND: /playlist <url> [quality] [items]
    # ---------------------------------------------------------
    elif cmd == "/playlist":
        if not args:
            await event.reply("⚠️ **Usage:** `/playlist <url> [quality] [items]`\n_e.g. `/playlist https://... 1080 1-10`_")
            return

        url = args[0]
        quality = args[1] if len(args) > 1 else "1080"
        items = args[2] if len(args) > 2 else None

        init_msg = await event.reply("🔍 Scanning playlist metadata in seconds...")

        try:
            from pipeline import discover_and_enqueue_url
            loop = asyncio.get_running_loop()
            title, new_cnt, tot_cnt = await loop.run_in_executor(
                None,
                lambda: discover_and_enqueue_url(
                    url=url,
                    state=state,
                    quality=quality,
                    playlist_items=items,
                ),
            )

            await init_msg.edit(
                f"📋 **Playlist Enqueued!**\n\n"
                f"• **Title:** {title}\n"
                f"• **New Videos:** {new_cnt}\n"
                f"• **Total Detected:** {tot_cnt}\n"
                f"• **Quality:** {quality}\n\n"
                f"🚀 Starting background processing..."
            )
            worker.trigger()

        except Exception as e:
            logger.error("Failed to enqueue playlist: %s", e)
            await init_msg.edit(f"❌ **Failed to enqueue playlist:** {e}")

    # ---------------------------------------------------------
    # COMMAND: /single <url> [quality]
    # ---------------------------------------------------------
    elif cmd == "/single":
        if not args:
            await event.reply("⚠️ **Usage:** `/single <url> [quality]`\n_e.g. `/single https://... 720`_")
            return

        url = args[0]
        quality = args[1] if len(args) > 1 else "1080"

        init_msg = await event.reply("🔍 Inspecting video metadata...")

        try:
            from pipeline import discover_and_enqueue_url
            loop = asyncio.get_running_loop()
            title, new_cnt, tot_cnt = await loop.run_in_executor(
                None,
                lambda: discover_and_enqueue_url(
                    url=url,
                    state=state,
                    quality=quality,
                ),
            )

            await init_msg.edit(
                f"🎬 **Video Enqueued!**\n\n"
                f"• **Title:** {title}\n"
                f"• **Quality:** {quality}\n\n"
                f"🚀 Starting background processing..."
            )
            worker.trigger()

        except Exception as e:
            logger.error("Failed to enqueue video: %s", e)
            await init_msg.edit(f"❌ **Failed to enqueue video:** {e}")


# -------------------------------------------------------------
# Main Startup
# -------------------------------------------------------------
def main() -> None:
    """Launches the Telegram Bot listener."""
    check_required_tools(TOOLS_DIR)
    logger.info("=" * 55)
    logger.info("ytdl-telegram: Telegram Bot Controller (Online)")
    logger.info("Authorized User ID : %s", ALLOWED_USER_ID)
    logger.info("Upload Mode        : %s", UPLOAD_MODE.upper())
    logger.info("cryptg status      : %s", CRYPTG_STATUS)
    logger.info("Parallel Workers   : %s connections", os.getenv("UPLOAD_CONNECTIONS", "4"))
    logger.info("Portable Tools     : %s", TOOLS_DIR)
    logger.info("Rotating Log File  : %s", log_file_path)
    logger.info("Bot is listening for incoming Telegram commands...")
    logger.info("=" * 55)

    bot.start(bot_token=BOT_TOKEN)
    bot.run_until_disconnected()


if __name__ == "__main__":
    main()
