"""
pipeline.py - Sequential Download-Upload-Delete Pipeline for ytdl-telegram.

Orchestrates the entire workflow:
1. Enqueues YouTube playlists / videos into SQLite (jobs.db).
2. Processes queue sequentially: Download ONE video -> Upload to Telegram -> Delete local file.
3. Disk footprint never exceeds a single video.
4. Resumes automatically on rerun; never re-downloads or re-uploads finished videos.
5. Retries failed videos up to 3 times, then marks them failed and continues the playlist.
"""

import asyncio
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple

import yt_dlp

from downloader import YTDownloader, TOOLS_DIR, check_required_tools
from state import StateManager, Job
from uploader import TelegramUploader



def discover_and_enqueue_url(
    url: str,
    state: StateManager,
    quality: str = "1080",
    playlist_items: Optional[str] = None,
    playlist_reverse: bool = False,
) -> Tuple[str, int, int]:
    """
    Extracts video metadata from a playlist or single URL in seconds using flat extraction.
    Enqueues items into SQLite jobs.db.
    Returns: (playlist_title, newly_enqueued_count, total_discovered_count)
    """
    check_required_tools(TOOLS_DIR)
    deno_exe = TOOLS_DIR / "deno.exe"

    ydl_opts: Dict[str, Any] = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": str(TOOLS_DIR),
        "js_runtimes": {
            "deno": {
                "path": str(deno_exe)
            }
        },
    }

    if playlist_items:
        ydl_opts["playlist_items"] = playlist_items

    if playlist_reverse:
        ydl_opts["playlistreverse"] = True

    print("\n  Scanning URL metadata (flat discovery)...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            raise RuntimeError(f"Could not extract metadata from URL: {e}")

    if not info:
        raise RuntimeError("No metadata returned for the provided URL.")

    # Determine if it's a playlist or a single video
    entries = info.get("entries")
    if entries is not None:
        video_list = [e for e in entries if e]
        playlist_title = info.get("title") or "Untitled Playlist"
    else:
        video_list = [info]
        playlist_title = "Single Video"

    new_count = 0
    total_count = len(video_list)

    for item in video_list:
        vid_id = item.get("id") or ""
        vid_title = item.get("title") or "Unknown Title"
        if not vid_id:
            continue

        inserted = state.add_job(
            playlist_url=url,
            video_id=vid_id,
            title=vid_title,
            quality=quality,
            playlist_title=playlist_title,
        )
        if inserted:
            new_count += 1

    return playlist_title, new_count, total_count


async def run_pipeline(playlist_url: Optional[str] = None) -> None:
    """
    Processes jobs in SQLite one by one:
    Download -> Upload -> Delete local file -> Next video.
    """
    check_required_tools(TOOLS_DIR)
    state = StateManager()

    # Reset any jobs left in-progress due to prior crash/interruption
    interrupted_count = state.reset_interrupted_jobs()
    if interrupted_count > 0:
        print(f"  [Resume] Recovered {interrupted_count} interrupted job(s) back to queue.")

    uploader = TelegramUploader()
    downloader = YTDownloader()

    # Connect to Telegram bot
    await uploader.connect()

    try:
        while True:
            job = state.get_next_pending_job(playlist_url)
            if not job:
                print("\n  [+] All pending videos in the queue have been processed!")
                break

            stats = state.get_stats(playlist_url)
            current_num = stats["uploaded"] + 1
            total_items = stats["total"]

            print("\n" + "=" * 65)
            print(f"  Processing Video [{current_num}/{total_items}]")
            print(f"  Title   : {job.title}")
            print(f"  Video ID: {job.video_id}")
            print(f"  Quality : {job.quality}")
            if job.retry_count > 0:
                print(f"  Retry   : Attempt {job.retry_count + 1} of 3")
            print("=" * 65)

            # -------------------------------------------------------------
            # STEP 1: DOWNLOAD SINGLE VIDEO
            # -------------------------------------------------------------
            state.mark_downloading(job.video_id)
            is_audio = (job.quality.lower() == "audio")
            video_watch_url = f"https://www.youtube.com/watch?v={job.video_id}"

            print("\n  [Phase 1/2] Downloading video...")
            results = downloader.download(
                urls=[video_watch_url],
                quality=job.quality,
                is_audio=is_audio,
            )

            if not results or not results[0].success or not results[0].path:
                err_msg = results[0].error if results else "Unknown download error"
                new_retries = state.record_failure(job.video_id, f"Download failed: {err_msg}")
                print(f"\n  [X] Download Failed (Attempt {new_retries}/3): {err_msg}")
                continue

            downloaded_path = results[0].path
            state.mark_downloaded(job.video_id, downloaded_path)
            print(f"  [OK] Downloaded: {downloaded_path}")

            # -------------------------------------------------------------
            # STEP 2: UPLOAD SINGLE VIDEO TO TELEGRAM
            # -------------------------------------------------------------
            state.mark_uploading(job.video_id)
            print("\n  [Phase 2/2] Uploading to Telegram...")

            upload_result = await uploader.upload_file(
                filepath=downloaded_path,
                caption=job.title,
            )

            if not upload_result.success:
                new_retries = state.record_failure(
                    job.video_id,
                    f"Upload failed: {upload_result.error}"
                )
                print(f"\n  [X] Upload Failed (Attempt {new_retries}/3): {upload_result.error}")
                # Clean up failed video to keep disk completely empty
                if not uploader.keep_local and os.path.exists(downloaded_path):
                    try:
                        os.remove(downloaded_path)
                    except OSError:
                        pass
                continue

            # -------------------------------------------------------------
            # STEP 3: MARK COMPLETED (File already removed by uploader)
            # -------------------------------------------------------------
            msg_ids_str = ",".join(str(m) for m in upload_result.message_ids)
            state.mark_uploaded(job.video_id, telegram_message_id=msg_ids_str)

            print(f"\n  [SUCCESS] Uploaded to Telegram! (Message ID: {msg_ids_str})")
            print(f"  [Disk Check] Local file removed. Disk remains free.")

            # Print intermediate progress
            live_stats = state.get_stats(playlist_url)
            print(
                f"\n  >>> Progress: {live_stats['uploaded']}/{live_stats['total']} Uploaded "
                f"| {live_stats['queued']} Remaining "
                f"| {live_stats['failed']} Failed"
            )

    finally:
        await uploader.disconnect()

    # Final summary display
    print("\n" + "#" * 65)
    print("#                    PIPELINE SUMMARY                           #")
    print("#" * 65)
    final_stats = state.get_stats(playlist_url)
    print(f"  Total Tracked Videos : {final_stats['total']}")
    print(f"  Successfully Uploaded: {final_stats['uploaded']}")
    print(f"  Permanently Failed   : {final_stats['failed']}")
    print(f"  Still In Queue       : {final_stats['queued']}")
    print("#" * 65 + "\n")


def print_queue_dashboard(state: StateManager) -> None:
    """Displays current database queue status."""
    stats = state.get_stats()
    print("\n" + "=" * 50)
    print("             JOBS.DB QUEUE DASHBOARD             ")
    print("=" * 50)
    print(f"  Total Videos Tracked : {stats['total']}")
    print(f"  Uploaded to Telegram : {stats['uploaded']}")
    print(f"  Pending in Queue     : {stats['queued']}")
    print(f"  Downloading          : {stats['downloading']}")
    print(f"  Uploading            : {stats['uploading']}")
    print(f"  Failed (3 retries)   : {stats['failed']}")
    print("=" * 50)

    if stats["failed"] > 0:
        print("\n  Failed Videos:")
        for j in state.get_all_jobs():
            if j.status == "failed":
                print(f"    - [{j.video_id}] {j.title[:40]} | Error: {j.error}")
    print()


def run_cli_pipeline() -> None:
    """Interactive command-line interface for the pipeline."""
    state = StateManager()

    # If URL is passed as command-line argument
    if len(sys.argv) > 1:
        target_url = sys.argv[1].strip().strip('\"').strip("\'")
        title, new_c, tot_c = discover_and_enqueue_url(target_url, state, quality="1080")
        print(f"\n  Enqueued '{title}': {new_c} new videos added ({tot_c} total discovered).")
        asyncio.run(run_pipeline(target_url))
        return

    while True:
        print("\n" + "#" * 55)
        print("#       ytdl-telegram: Download & Upload Pipeline      #")
        print("#" * 55)
        print("  [1] Add and process a Playlist or Video URL")
        print("  [2] Resume existing queue (all pending jobs)")
        print("  [3] View Queue Dashboard (jobs.db statistics)")
        print("  [4] Reset failed jobs (clear failure count to re-try)")
        print("  [5] Exit")

        choice = input("\n  Choose option [1-5]: ").strip()

        if choice == "5":
            print("\nGoodbye!")
            sys.exit(0)

        elif choice == "3":
            print_queue_dashboard(state)
            input("  Press Enter to continue...")

        elif choice == "4":
            count = state.reset_all_failed()
            print(f"\n  [+] Reset {count} failed job(s) back to 'queued'.")
            input("  Press Enter to continue...")

        elif choice == "2":
            stats = state.get_stats()
            if stats["queued"] == 0 and stats["failed"] == 0:
                print("\n  [!] No pending jobs in queue.")
                input("  Press Enter to continue...")
                continue

            print_queue_dashboard(state)
            confirm = input("  Start processing pending queue now? [Y/n]: ").strip().lower()
            if confirm != "n":
                try:
                    asyncio.run(run_pipeline())
                except KeyboardInterrupt:
                    print("\n  [!] Pipeline paused by user. Re-run anytime to resume.")
            input("  Press Enter to continue...")

        elif choice == "1":
            target_url = input("\n  Paste YouTube Playlist or Video URL: ").strip().strip('\"').strip("\'")
            if not target_url:
                print("  [!] No URL entered.")
                continue

            print("\n  Select Quality:")
            print("    1) 1080p (MP4)")
            print("    2) 720p (MP4)")
            print("    3) 480p (MP4)")
            print("    4) Audio only (MP3 320k)")
            print("    5) Custom height (e.g. 1440, 2160)")
            q_choice = input("  Enter quality choice [1-5]: ").strip()

            quality = "1080"
            if q_choice == "2":
                quality = "720"
            elif q_choice == "3":
                quality = "480"
            elif q_choice == "4":
                quality = "audio"
            elif q_choice == "5":
                custom_h = input("  Enter max height: ").strip()
                quality = custom_h if custom_h.isdigit() else "1080"

            playlist_items: Optional[str] = None
            playlist_reverse = False

            range_ans = input("\n  Specify custom item range or reverse order? [y/N]: ").strip().lower()
            if range_ans == "y":
                print("    1) Range (e.g. 1-10, 50-40)")
                print("    2) Oldest first (reverse order)")
                print("    3) Range + reverse order")
                opt = input("    Choose option [1-3]: ").strip()
                if opt == "1":
                    playlist_items = input("    Enter range (e.g. 1-10 or 50-40): ").strip()
                elif opt == "2":
                    playlist_reverse = True
                elif opt == "3":
                    playlist_items = input("    Enter range (e.g. 1-10 or 50-40): ").strip()
                    playlist_reverse = True

            try:
                pl_title, new_count, total_count = discover_and_enqueue_url(
                    url=target_url,
                    state=state,
                    quality=quality,
                    playlist_items=playlist_items,
                    playlist_reverse=playlist_reverse,
                )
                print(f"\n  [+] Target: '{pl_title}'")
                print(f"      Enqueued {new_count} new video(s) ({total_count} total detected).")

                start_ans = input("\n  Start downloading and uploading now? [Y/n]: ").strip().lower()
                if start_ans != "n":
                    asyncio.run(run_pipeline(target_url))

            except KeyboardInterrupt:
                print("\n  [!] Pipeline paused by user. Re-run anytime to resume.")
            except Exception as e:
                print(f"\n  [ERROR] {e}")

            input("  Press Enter to continue...")


if __name__ == "__main__":
    run_cli_pipeline()
