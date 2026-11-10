"""
downloader_v3.py - Core YouTube downloader using yt-dlp as a Python library.

Part of the "ytdl-telegram" project.
Handles single URLs, batch lists, and playlists with custom ranges and qualities.
Features:
- Portable tools integration: uses ./tools for ffmpeg.exe, ffprobe.exe, and deno.exe
- Automatic PATH registration for subprocesses and child tools
- Startup verification of required binaries
- Format merge verification: does not count video as succeeded if merge failed
- Returns structured DownloadResult objects per video
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional

# Core yt-dlp library import (top-level)
import yt_dlp

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

# Portable tools configuration (relative to script directory)
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_TOOLS_DIR = os.getenv("TOOLS_DIR")
TOOLS_DIR = Path(ENV_TOOLS_DIR).resolve() if ENV_TOOLS_DIR else (SCRIPT_DIR / "tools")

# Prepend portable tools directory to process PATH so ffmpeg, ffprobe, and deno are found system-wide
tools_str = str(TOOLS_DIR)
current_path = os.environ.get("PATH", "")
if tools_str not in current_path:
    os.environ["PATH"] = tools_str + os.pathsep + current_path


def check_required_tools(tools_dir: Path) -> None:
    """
    Verifies that ffmpeg.exe, ffprobe.exe, and deno.exe exist in tools_dir.
    Prints a clear message naming any missing files and terminates execution.
    """
    required = ["ffmpeg.exe", "ffprobe.exe", "deno.exe"]
    missing = [req for req in required if not (tools_dir / req).is_file()]

    if missing:
        print("\n" + "=" * 65)
        print("  [ERROR] Missing required portable tool(s) in tools folder!")
        print(f"  Target directory: {tools_dir}")
        print("\n  Missing file(s):")
        for m in missing:
            print(f"    [X] {m}")
        print("\n  Please place the required executable(s) into:")
        print(f"    {tools_dir}")
        print("  See README.md for instructions and download details.")
        print("=" * 65 + "\n")
        sys.exit(1)


@dataclass
class DownloadResult:
    """Represents the outcome of a single video download."""
    id: str
    title: str
    path: Optional[str] = None
    size: int = 0  # Size in bytes
    duration: float = 0.0  # Duration in seconds
    success: bool = False
    error: Optional[str] = None

    def __repr__(self) -> str:
        size_mb = self.size / (1024 * 1024) if self.size else 0.0
        status_str = "SUCCESS" if self.success else f"FAILED ({self.error})"
        return (
            f"<DownloadResult id='{self.id}' title='{self.title[:30]}...' "
            f"size={size_mb:.2f}MB duration={self.duration:.0f}s status={status_str} "
            f"path='{self.path}'>"
        )


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
        # Verify tools exist on initialization
        check_required_tools(self.tools_dir)

        # Base download directory (fallback to .env or 'downloads' folder)
        self.dest_dir = dest_dir or os.getenv("DOWNLOAD_DIR", "downloads")
        os.makedirs(self.dest_dir, exist_ok=True)

        # Archive file to remember downloaded videos and prevent re-downloading
        self.archive_file = os.path.join(self.dest_dir, "download-archive.txt")

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

        # Output template:
        # If playlist_title exists -> <dest>/<playlist_title[:60]>/<title[:70]> [<id>] [<quality>].<ext>
        # If single video -> <dest>/<title[:70]> [<id>] [<quality>].<ext>
        clean_dest = os.path.abspath(self.dest_dir).replace("\\", "/")
        outtmpl = f"{clean_dest}/%(playlist_title&{{}}|.).60s/%(title).70s [%(id)s] [{tag}].%(ext)s"

        deno_exe = self.tools_dir / "deno.exe"

        ydl_opts: Dict[str, Any] = {
            "outtmpl": {"default": outtmpl},
            "download_archive": self.archive_file,
            "nooverwrites": True,
            "concurrent_fragment_downloads": self.concurrent_fragments,
            # Portable ffmpeg & deno locations
            "ffmpeg_location": str(self.tools_dir),
            "js_runtimes": {
                "deno": {
                    "path": str(deno_exe)
                }
            },
            "quiet": False,
            "no_warnings": False,
            "ignoreerrors": True,  # Continues playlist if a single video fails
        }

        # Optional cookies
        if self.cookies_file and os.path.exists(self.cookies_file):
            ydl_opts["cookiefile"] = self.cookies_file

        # Live from start option
        if live_from_start:
            ydl_opts["live_from_start"] = True

        # Format & Post-processors
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
            # Video: Prefer avc1 (H.264) + bestaudio, fallback to any codec <= quality height
            q_val = int(quality) if quality.isdigit() else 1080
            ydl_opts["format"] = (
                f"bestvideo[height<={q_val}][vcodec^=avc1]+bestaudio/best[height<={q_val}]/"
                f"bestvideo[height<={q_val}]+bestaudio/best[height<={q_val}]"
            )
            ydl_opts["merge_output_format"] = "mp4"

        # Playlist ranges & ordering
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
    ) -> List[DownloadResult]:
        """
        Downloads one or more URLs and returns a list of DownloadResult objects.
        Ensures formats are merged and valid before reporting success.
        """
        results: List[DownloadResult] = []
        finished_files: Dict[str, str] = {}

        def progress_hook(d: Dict[str, Any]) -> None:
            if d.get("status") == "finished":
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

                    # If this URL was a playlist, entries contains each video's info
                    entries = info.get("entries")
                    if entries is not None:
                        video_list = [e for e in entries if e]
                    else:
                        video_list = [info]

                    for v in video_list:
                        vid_id = v.get("id", "")
                        vid_title = v.get("title", "Unknown")
                        vid_duration = float(v.get("duration") or 0.0)

                        # Expected merged filename
                        expected_fn = ydl.prepare_filename(v)
                        base, _ = os.path.splitext(expected_fn)
                        expected_merged = f"{base}.mp3" if is_audio else f"{base}.mp4"

                        final_path: Optional[str] = None

                        # Check 1: Does the exact expected merged file exist?
                        if os.path.exists(expected_merged) and os.path.getsize(expected_merged) > 0:
                            final_path = os.path.abspath(expected_merged)
                        elif vid_id in finished_files:
                            candidate = finished_files[vid_id]
                            if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                                # Ensure candidate is not an unmerged intermediate stream (e.g. .f137.)
                                c_name = os.path.basename(candidate)
                                if not re.search(r"\.f\d+\.", c_name):
                                    final_path = os.path.abspath(candidate)

                        # Verify merge status:
                        # Video MUST be .mp4, Audio MUST be .mp3
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


def run_cli_menu() -> None:
    """Interactive command-line interface mimicking the original .bat experience."""
    # Check tools at startup
    check_required_tools(TOOLS_DIR)
    downloader = YTDownloader()

    while True:
        print("\n" + "#" * 45)
        print("#       yt-dlp Python Downloader (v3)       #")
        print("#" * 45)
        print(f"  Saving to   : {os.path.abspath(downloader.dest_dir)}")
        print(f"  Tools folder: {os.path.abspath(downloader.tools_dir)}")
        print("\n  [1] Download a single URL")
        print("  [2] Download multiple URLs (batch)")
        print("  [3] Download playlist (with range / order options)")
        print("  [4] Exit")

        mode = input("\n  Choose mode [1-4]: ").strip()
        if mode == "4":
            print("\nGoodbye!")
            sys.exit(0)

        urls: List[str] = []
        playlist_items: Optional[str] = None
        playlist_reverse: bool = False

        if mode == "1":
            url = input("\n  Paste video/live URL: ").strip()
            if not url:
                print("  [!] No URL entered.")
                continue
            urls.append(url)

        elif mode == "2":
            print("\n  Paste one URL per line. Press Enter on an empty line when finished:")
            idx = 1
            while True:
                line = input(f"  URL {idx}: ").strip()
                if not line:
                    break
                urls.append(line)
                idx += 1
            if not urls:
                print("  [!] No URLs entered.")
                continue

        elif mode == "3":
            pl_url = input("\n  Paste playlist URL: ").strip()
            if not pl_url:
                print("  [!] No URL entered.")
                continue
            urls.append(pl_url)

            print("\n  Playlist Options:")
            print("    1) Download all items")
            print("    2) Specify item range (e.g. 1-10, 5,8,12, or 50-40 descending)")
            print("    3) Oldest first (reverse playlist order)")
            print("    4) Custom range + reverse order")
            pl_choice = input("  Choose playlist option [1-4]: ").strip()

            if pl_choice == "2":
                playlist_items = input("  Enter item range (e.g. 1-10 or 50-40): ").strip()
            elif pl_choice == "3":
                playlist_reverse = True
            elif pl_choice == "4":
                playlist_items = input("  Enter item range (e.g. 1-10 or 50-40): ").strip()
                playlist_reverse = True

        else:
            print("  [!] Invalid choice.")
            continue

        # Quality selection
        print("\n  What quality do you want?")
        print("    1) Best video (up to 1080p, H.264/MP4)")
        print("    2) 720p")
        print("    3) 480p")
        print("    4) Audio only (MP3 320k)")
        print("    5) Custom max height (e.g. 1440, 2160)")

        q_choice = input("  Enter choice [1-5]: ").strip()
        quality = "1080"
        is_audio = False

        if q_choice == "1":
            quality = "1080"
        elif q_choice == "2":
            quality = "720"
        elif q_choice == "3":
            quality = "480"
        elif q_choice == "4":
            is_audio = True
        elif q_choice == "5":
            custom_h = input("  Enter max height (e.g. 1440, 2160): ").strip()
            quality = custom_h if custom_h.isdigit() else "1080"

        # Live stream option
        live_ans = input("\n  Is this a currently live stream? (record from start) [y/N]: ").strip().lower()
        live_from_start = live_ans == "y"

        print("\n" + "=" * 50)
        print(f"  Starting download ({len(urls)} target(s))...")
        print("=" * 50 + "\n")

        results = downloader.download(
            urls=urls,
            quality=quality,
            is_audio=is_audio,
            playlist_items=playlist_items,
            playlist_reverse=playlist_reverse,
            live_from_start=live_from_start,
        )

        print("\n" + "#" * 45)
        print("#               SUMMARY                     #")
        print("#" * 45)
        successes = sum(1 for r in results if r.success)
        failures = len(results) - successes
        print(f"  Total items processed : {len(results)}")
        print(f"  Succeeded             : {successes}")
        print(f"  Failed                : {failures}")
        print("-" * 45)

        for res in results:
            if res.success:
                size_mb = res.size / (1024 * 1024) if res.size else 0.0
                print(f"  [OK] {res.title} ({size_mb:.2f} MB)")
                print(f"       -> {res.path}")
            else:
                print(f"  [FAIL] {res.title}")
                print(f"         Reason: {res.error}")

        input("\n  Press Enter to continue...")


if __name__ == "__main__":
    run_cli_menu()
