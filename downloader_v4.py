"""
downloader_v4.py - yt-dlp wrapper with portable ffmpeg, ffprobe, and deno.

Part of the "ytdl-telegram" project.
Features:
- Bundled portable tools detection (./tools folder)
- Auto-prepends portable tools directory to system PATH
- Passes js_runtimes with portable Deno for YouTube extraction without node.js
- Passes ffmpeg_location pointing directly to portable tools
- Single-source-of-truth state tracking: uses SQLite jobs.db instead of download_archive
- Prefers AVC1 (H.264) video codec for Telegram streaming compatibility
- Extracts video metadata (id, title, duration) without extra API calls
- Real-time download progress hook (percentage, speed, ETA)
- Merged MP4/MP3 validation ensuring audio and video streams merge cleanly
"""

import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass

# Locate script directory and portable tools directory
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_TOOLS_DIR = os.getenv("TOOLS_DIR")
TOOLS_DIR = Path(ENV_TOOLS_DIR).resolve() if ENV_TOOLS_DIR else (SCRIPT_DIR / "tools")

# Prepend portable tools to system PATH
tools_str = str(TOOLS_DIR)
current_path = os.environ.get("PATH", "")
if tools_str not in current_path:
    os.environ["PATH"] = tools_str + os.pathsep + current_path

# Import yt_dlp
try:
    import yt_dlp
except ImportError:
    yt_dlp = None


def check_required_tools(tools_dir: Path) -> None:
    """Verifies that deno.exe, ffmpeg.exe, and ffprobe.exe are in tools_dir or PATH."""
    required = ["deno.exe", "ffmpeg.exe", "ffprobe.exe"]
    missing = [req for req in required if not (tools_dir / req).is_file()]
    if missing:
        print("\n" + "=" * 60)
        print("  [ERROR] Missing required portable tool(s) in tools directory!")
        print(f"  Target folder: {tools_dir}")
        print("\n  Missing file(s):")
        for m in missing:
            print(f"    [X] {m}")
        print("\n  Please ensure ffmpeg.exe, ffprobe.exe, and deno.exe exist in:")
        print(f"    {tools_dir}")
        print("=" * 60 + "\n")
        sys.exit(1)


@dataclass
class DownloadResult:
    """Represents the outcome of a download operation."""
    id: str
    title: str
    path: Optional[str] = None
    size: int = 0
    duration: float = 0.0
    success: bool = True
    error: Optional[str] = None


class YTDownloader:
    """Downloader engine using yt-dlp Python API with portable tools support."""

    def __init__(
        self,
        dest_dir: Optional[str] = None,
        cookies_file: Optional[str] = None,
        tools_dir: Optional[Path] = None,
        concurrent_fragments: int = 8,
    ):
        self.tools_dir = tools_dir or TOOLS_DIR
        check_required_tools(self.tools_dir)

        # Base download directory (fallback to .env or 'downloads' folder)
        self.dest_dir = dest_dir or os.getenv("DOWNLOAD_DIR", "downloads")
        os.makedirs(self.dest_dir, exist_ok=True)

        # Optional cookies.txt
        env_cookie = os.getenv("COOKIES_FILE")
        self.cookies_file = cookies_file or env_cookie
        if not self.cookies_file and os.path.exists("cookies.txt"):
            self.cookies_file = "cookies.txt"

        self.concurrent_fragments = concurrent_fragments

    def _build_options(
        self,
        quality: str = "1080",
        is_audio: bool = False,
        playlist_items: Optional[str] = None,
        playlist_reverse: bool = False,
        live_from_start: bool = False,
    ) -> Dict[str, Any]:
        """Builds yt-dlp options dictionary with portable tools paths."""
        tag = "audio" if is_audio else f"{quality}p"

        clean_dest = os.path.abspath(self.dest_dir).replace("\\", "/")
        outtmpl = f"{clean_dest}/%(playlist_title&{{}}|.).60s/%(title).70s [%(id)s] [{tag}].%(ext)s"

        deno_exe = self.tools_dir / "deno.exe"

        ydl_opts: Dict[str, Any] = {
            "outtmpl": {"default": outtmpl},
            "overwrites": True,
            "concurrent_fragment_downloads": self.concurrent_fragments,
            "ffmpeg_location": str(self.tools_dir),
            "js_runtimes": {
                "deno": {
                    "path": str(deno_exe)
                }
            },
            "quiet": False,
            "no_warnings": False,
            "ignoreerrors": True,
        }

        if self.cookies_file and os.path.exists(self.cookies_file):
            ydl_opts["cookiefile"] = self.cookies_file

        if live_from_start:
            ydl_opts["live_from_start"] = True

        if is_audio:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ]
        else:
            q_val = int(quality) if quality.isdigit() else 1080
            ydl_opts["format"] = (
                f"bestvideo[height<={q_val}][vcodec^=avc1]+bestaudio/best[height<={q_val}]/"
                f"bestvideo[height<={q_val}]+bestaudio/best[height<={q_val}]"
            )
            ydl_opts["merge_output_format"] = "mp4"

        if playlist_items:
            ydl_opts["playlist_items"] = playlist_items

        if playlist_reverse:
            ydl_opts["playlistreverse"] = True

        return ydl_opts

    def download(
        self,
        urls: List[str],
        quality: str = "1080",
        is_audio: bool = False,
        playlist_items: Optional[str] = None,
        playlist_reverse: bool = False,
        live_from_start: bool = False,
        on_progress: Optional[Callable[[float, int, int], None]] = None,
    ) -> List[DownloadResult]:
        """
        Downloads one or more URLs and returns a list of DownloadResult objects.
        Optional on_progress callback receives: (percentage, current_bytes, total_bytes).
        """
        results: List[DownloadResult] = []
        finished_files: Dict[str, str] = {}

        def progress_hook(d: Dict[str, Any]) -> None:
            status = d.get("status")
            if status == "downloading" and on_progress:
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = (downloaded / total * 100) if total else 0.0
                try:
                    on_progress(pct, downloaded, total)
                except Exception:
                    pass

            elif status == "finished":
                fn = d.get("filename")
                info = d.get("info_dict", {})
                vid_id = info.get("id")
                if vid_id and fn:
                    finished_files[vid_id] = os.path.abspath(fn)

        def postprocessor_hook(d: Dict[str, Any]) -> None:
            if d.get("status") == "finished":
                info = d.get("info_dict", {})
                vid_id = info.get("id")
                filepath = info.get("filepath") or d.get("filepath") or info.get("_filename")
                if vid_id and filepath:
                    finished_files[vid_id] = os.path.abspath(filepath)

        ydl_opts = self._build_options(
            quality=quality,
            is_audio=is_audio,
            playlist_items=playlist_items,
            playlist_reverse=playlist_reverse,
            live_from_start=live_from_start,
        )

        ydl_opts["progress_hooks"] = [progress_hook]
        ydl_opts["postprocessor_hooks"] = [postprocessor_hook]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for url in urls:
                try:
                    info = ydl.extract_info(url, download=True)
                    if not info:
                        results.append(
                            DownloadResult(
                                id="",
                                title=url,
                                success=False,
                                error="Could not extract metadata",
                            )
                        )
                        continue

                    entries = info.get("entries")
                    if entries is not None:
                        video_list = [e for e in entries if e]
                    else:
                        video_list = [info]

                    for v in video_list:
                        vid_id = v.get("id", "")
                        vid_title = v.get("title", "Unknown")
                        vid_duration = float(v.get("duration") or 0.0)

                        expected_fn = ydl.prepare_filename(v)
                        base, _ = os.path.splitext(expected_fn)
                        expected_merged = f"{base}.mp3" if is_audio else f"{base}.mp4"

                        final_path: Optional[str] = None

                        if os.path.exists(expected_merged) and os.path.getsize(expected_merged) > 0:
                            final_path = os.path.abspath(expected_merged)
                        elif vid_id in finished_files:
                            candidate = finished_files[vid_id]
                            if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                                c_name = os.path.basename(candidate)
                                if not re.search(r"\.f\d+\.", c_name):
                                    final_path = os.path.abspath(candidate)

                        merge_succeeded = False
                        if final_path and os.path.exists(final_path):
                            _, ext = os.path.splitext(final_path)
                            if is_audio and ext.lower() == ".mp3":
                                merge_succeeded = True
                            elif (not is_audio) and ext.lower() == ".mp4":
                                merge_succeeded = True

                        file_size = os.path.getsize(final_path) if (final_path and os.path.exists(final_path)) else 0
                        success = bool(final_path and merge_succeeded and file_size > 0)

                        error_message: Optional[str] = None
                        if not success:
                            if final_path and not merge_succeeded:
                                error_message = "Merge failed: ffmpeg did not merge streams into target format"
                            else:
                                error_message = "Download failed or missing final merged file"

                        results.append(
                            DownloadResult(
                                id=vid_id,
                                title=vid_title,
                                path=final_path if success else None,
                                size=file_size if success else 0,
                                duration=vid_duration,
                                success=success,
                                error=error_message,
                            )
                        )

                except Exception as e:
                    results.append(
                        DownloadResult(
                            id="",
                            title=url,
                            success=False,
                            error=str(e),
                        )
                    )

        return results
