#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Drive → Google Photos Auto Sync
💾 Works without local downloads
📅 Sorts by date and file type (photo/video)
📁 Creates albums YYYY_MM_photo / YYYY_MM_video
🧠 Automatically handles quota errors (429 Too Many Requests)
📝 Saves JSON report and detailed log
⚡ Optimizations to prevent quota overruns:
   - Album caching (avoids repeated checks)
   - Precise API request counting (accounts for different operation types)
   - Safety reserve of requests (100 requests)
   - Proactive stop at 98% limit
"""

import os
import re
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
from dotenv import load_dotenv
from collections import defaultdict
import pytz

# ==============================
# ⚙️ CONFIG
# ==============================
load_dotenv()

GDRIVE = os.getenv("GDRIVE_REMOTE", "gdrive")
GPHOTOS = os.getenv("GPHOTOS_REMOTE", "gphotos")
SOURCE_PATH = os.getenv("SOURCE_PATH", "Photo")
LOG_DIR = os.path.expanduser(os.getenv("LOG_DIR", "~/gphoto_logs"))
MAX_PARALLEL_UPLOADS = int(os.getenv("MAX_PARALLEL_UPLOADS", 2))
UPLOAD_TIMEOUT = int(os.getenv("UPLOAD_TIMEOUT", 600))

Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
LOG_PATH = Path(LOG_DIR) / f"sync_{datetime.now():%Y%m%d_%H%M%S}.log"
SUMMARY_PATH = Path(LOG_DIR) / f"summary_{datetime.now():%Y%m%d_%H%M%S}.json"

PHOTO_EXT = tuple(x.strip() for x in os.getenv(
    "PHOTO_EXT", ".jpg,.jpeg,.png,.heic,.cr2").split(","))
VIDEO_EXT = tuple(x.strip() for x in os.getenv(
    "VIDEO_EXT", ".mp4,.mov,.avi,.mkv").split(","))
IGNORED_EXT = tuple(x.strip() for x in os.getenv(
    "IGNORED_EXT", ".thm,.lrv,.json").split(","))

# Quota limit constants
API_QUOTA_LIMIT = 10000  # requests per day
UPLOAD_QUOTA_LIMIT = 53_687_091_200  # 50 GB in bytes
WARNING_THRESHOLD = 0.8  # 80%
CRITICAL_THRESHOLD = 0.9  # 90%
STOP_THRESHOLD = 0.98  # 98% - more conservative approach, leaving a reserve
REQUESTS_PER_UPLOAD = 2  # Approximate number of API requests per upload (upload + possible check)
REQUESTS_FIRST_IN_ALBUM = 3  # First file in album may require more requests (check/create album)
SAFETY_RESERVE = 100  # Request reserve for unexpected operations

STATE_FILE = Path(LOG_DIR) / "state.json"
if STATE_FILE.exists():
    try:
        DONE = set(json.load(open(STATE_FILE)))
    except Exception:
        DONE = set()
else:
    DONE = set()

DAILY_QUOTA_FILE = Path(LOG_DIR) / "daily_quota.json"

# Cache for tracking albums that have already been used (to avoid repeated checks)
KNOWN_ALBUMS = set()

METRICS = {
    "started": datetime.now().isoformat(),
    "finished": None,
    "processed_files": 0,
    "uploaded_files": 0,
    "errors": 0,
    "albums_created": set(),
    "by_album": defaultdict(int),
    "duration_sec": 0.0,
    "quota_exceeded": False,
    "quota_reset_time": None,
    "api_requests_used": 0,
    "uploaded_bytes": 0
}


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


class QuotaExceededError(Exception):
    """Exception raised when daily quota limit is reached."""
    def __init__(self, reset_time: datetime, seconds_until_reset: int):
        self.reset_time = reset_time
        self.seconds_until_reset = seconds_until_reset
        super().__init__(f"Daily quota limit reached. Reset at {reset_time.strftime('%Y-%m-%d %H:%M:%S PST')}")


def is_daily_quota_exceeded(stderr: str) -> bool:
    """Determines if daily quota limit has been reached."""
    stderr_lower = stderr.lower()
    daily_quota_indicators = [
        "all requests per day",
        "quota exceeded for quota metric 'all requests'",
        "quota metric 'all requests' and limit 'all requests per day'"
    ]
    return any(indicator in stderr_lower for indicator in daily_quota_indicators)


def get_current_pst_date():
    """Returns current date in PST."""
    pst = pytz.timezone('America/Los_Angeles')
    return datetime.now(pst).date()


def get_quota_reset_time():
    """Calculates the time of the next quota reset (midnight PST of the next day)."""
    # Get current time in PST
    pst = pytz.timezone('America/Los_Angeles')
    now_pst = datetime.now(pst)
    
    # Calculate midnight of the next day in PST
    next_day = now_pst.date() + timedelta(days=1)
    reset_time_naive = datetime.combine(next_day, dt_time.min)
    reset_time = pst.localize(reset_time_naive)
    
    # Calculate number of seconds until reset
    seconds_until_reset = int((reset_time - now_pst).total_seconds())
    
    return reset_time, seconds_until_reset


def load_daily_quota():
    """Loads daily quotas, resets if new day."""
    current_date = get_current_pst_date()
    date_str = current_date.isoformat()
    
    if DAILY_QUOTA_FILE.exists():
        try:
            with open(DAILY_QUOTA_FILE, "r") as f:
                quota_data = json.load(f)
            
            # Check if it's a new day
            if quota_data.get("date") == date_str:
                return quota_data.get("api_requests", 0), quota_data.get("uploaded_bytes", 0)
        except Exception:
            pass
    
    # New day or file doesn't exist - reset
    quota_data = {
        "date": date_str,
        "api_requests": 0,
        "uploaded_bytes": 0
    }
    save_daily_quota(quota_data)
    return 0, 0


def save_daily_quota(quota_data=None):
    """Saves daily quotas."""
    if quota_data is None:
        quota_data = {
            "date": get_current_pst_date().isoformat(),
            "api_requests": 0,
            "uploaded_bytes": 0
        }
    with open(DAILY_QUOTA_FILE, "w") as f:
        json.dump(quota_data, f, indent=2)


def increment_api_request():
    """Increments API request counter and saves."""
    api_requests, uploaded_bytes = load_daily_quota()
    api_requests += 1
    quota_data = {
        "date": get_current_pst_date().isoformat(),
        "api_requests": api_requests,
        "uploaded_bytes": uploaded_bytes
    }
    save_daily_quota(quota_data)
    return api_requests


def increment_upload_bytes(bytes_count):
    """Increments uploaded bytes counter and saves."""
    api_requests, uploaded_bytes = load_daily_quota()
    uploaded_bytes += bytes_count
    quota_data = {
        "date": get_current_pst_date().isoformat(),
        "api_requests": api_requests,
        "uploaded_bytes": uploaded_bytes
    }
    save_daily_quota(quota_data)
    return uploaded_bytes


def check_api_quota(requests_needed=1):
    """Checks API request limit, returns True if can continue.
    
    Args:
        requests_needed: Number of requests planned to make
    """
    api_requests, _ = load_daily_quota()
    projected_requests = api_requests + requests_needed
    effective_limit = API_QUOTA_LIMIT - SAFETY_RESERVE  # Account for reserve
    percentage = projected_requests / API_QUOTA_LIMIT
    
    if projected_requests >= effective_limit:
        log(f"🛑 Safe API request limit exceeded: {api_requests}/{API_QUOTA_LIMIT} (after operation will be {projected_requests})")
        log(f"⏸ Stopping work to prevent quota overrun (reserve: {SAFETY_RESERVE} requests)")
        return False
    
    if percentage >= STOP_THRESHOLD:
        log(f"🛑 Close to API request limit: {api_requests}/{API_QUOTA_LIMIT} ({percentage*100:.1f}%)")
        log(f"⏸ Stopping work to prevent quota overrun")
        return False
    
    if percentage >= CRITICAL_THRESHOLD:
        remaining = API_QUOTA_LIMIT - api_requests
        log(f"🔴 Critical API request level: {api_requests}/{API_QUOTA_LIMIT} ({percentage*100:.1f}%)")
        log(f"⚠️ {remaining} requests remaining until limit")
        return True
    
    if percentage >= WARNING_THRESHOLD:
        remaining = API_QUOTA_LIMIT - api_requests
        log(f"🟡 Warning: API requests at {percentage*100:.1f}% ({api_requests}/{API_QUOTA_LIMIT})")
        log(f"⚠️ {remaining} requests remaining")
        return True
    
    return True


def check_upload_quota(file_size):
    """Checks upload volume limit, returns True if file can be uploaded."""
    _, uploaded_bytes = load_daily_quota()
    new_total = uploaded_bytes + file_size
    percentage = new_total / UPLOAD_QUOTA_LIMIT
    
    if percentage >= STOP_THRESHOLD:
        uploaded_mb = uploaded_bytes / (1024 * 1024)
        limit_mb = UPLOAD_QUOTA_LIMIT / (1024 * 1024)
        log(f"🛑 Upload volume limit reached: {uploaded_mb:.1f}/{limit_mb:.1f} MB ({percentage*100:.1f}%)")
        log(f"⏸ Stopping work to prevent quota overrun")
        return False
    
    if percentage >= CRITICAL_THRESHOLD:
        uploaded_mb = uploaded_bytes / (1024 * 1024)
        limit_mb = UPLOAD_QUOTA_LIMIT / (1024 * 1024)
        remaining_mb = (UPLOAD_QUOTA_LIMIT - uploaded_bytes) / (1024 * 1024)
        log(f"🔴 Critical upload volume level: {uploaded_mb:.1f}/{limit_mb:.1f} MB ({percentage*100:.1f}%)")
        log(f"⚠️ {remaining_mb:.1f} MB remaining until limit")
        return True
    
    if percentage >= WARNING_THRESHOLD:
        uploaded_mb = uploaded_bytes / (1024 * 1024)
        limit_mb = UPLOAD_QUOTA_LIMIT / (1024 * 1024)
        remaining_mb = (UPLOAD_QUOTA_LIMIT - uploaded_bytes) / (1024 * 1024)
        log(f"🟡 Warning: upload volume at {percentage*100:.1f}% ({uploaded_mb:.1f}/{limit_mb:.1f} MB)")
        log(f"⚠️ {remaining_mb:.1f} MB remaining")
        return True
    
    return True


def run_cmd(cmd, retries=3, cooldown=5, is_gphotos_api=False, estimated_requests=1):
    """Safe rclone call with 429/Quota exceeded handling.
    
    Args:
        estimated_requests: Estimated number of API requests for this operation
    """
    # Check quota before making request to Google Photos API
    if is_gphotos_api:
        if not check_api_quota(estimated_requests):
            reset_time, seconds_until_reset = get_quota_reset_time()
            raise QuotaExceededError(reset_time, seconds_until_reset)
    
    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            # Increment request counter by estimated amount for successful operations
            if is_gphotos_api:
                for _ in range(estimated_requests):
                    increment_api_request()
            return result.stdout.strip()

        stderr = result.stderr

        # Check for daily quota limit
        if is_daily_quota_exceeded(stderr):
            reset_time, seconds_until_reset = get_quota_reset_time()
            hours_until_reset = seconds_until_reset / 3600
            log(f"🚫 Daily quota limit reached (10,000 requests/day)")
            log(f"⏰ Quota will reset at: {reset_time.strftime('%Y-%m-%d %H:%M:%S PST')}")
            log(f"⏳ Wait time remaining: {hours_until_reset:.1f} hours ({seconds_until_reset // 60} minutes)")
            log(f"💾 Saving state to continue after quota reset...")
            raise QuotaExceededError(reset_time, seconds_until_reset)

        stderr_lower = stderr.lower()
        # Detect temporary quota errors (rate limit)
        if "quota exceeded" in stderr_lower or "too many requests" in stderr_lower or "rate limit" in stderr_lower:
            wait = cooldown * attempt * 2
            log(
                f"⚠️ Temporary rate limit — pausing {wait} sec before retry (attempt {attempt}/{retries})")
            time.sleep(wait)
            continue

        # If error is different — just retry
        log(f"⚠️ Error: {stderr.strip()} (attempt {attempt}/{retries})")
        time.sleep(cooldown * attempt)

    raise RuntimeError(stderr or "rclone error")


def list_drive_files():
    """List all photos/videos in Google Drive. Returns list of tuples (path, size)."""
    cmd = ["rclone", "lsjson",
           f"{GDRIVE}:{SOURCE_PATH}", "--recursive", "--files-only"]
    try:
        out = run_cmd(cmd, is_gphotos_api=False)  # Request to Google Drive, don't count
        data = json.loads(out)
    except Exception as e:
        log(f"❌ Error getting file list: {e}")
        return []

    files = []
    for f in data:
        name = f["Path"]
        size = f.get("Size", 0)
        if size == 0 or name.lower().endswith(IGNORED_EXT):
            continue
        files.append((name, size))
    return files


def detect_from_name(name):
    """Extracts YYYY_MM from filename."""
    m = re.search(r"(19|20\d{2})[-_.]?(0[1-9]|1[0-2])", name)
    return f"{m.group(1)}_{m.group(2)}" if m else "unsorted"


def infer_album(relpath, is_video):
    base = detect_from_name(relpath)
    suffix = "video" if is_video else "photo"
    album = f"{base}_{suffix}"
    METRICS["albums_created"].add(album)
    return album


def ensure_album(album_name):
    """Tracks album usage for request optimization."""
    if album_name not in KNOWN_ALBUMS:
        KNOWN_ALBUMS.add(album_name)
        log(f"🌀 Album {album_name} → will be created on first upload (if doesn't exist)")
        return True  # First use of album
    return False  # Album already used


def upload_file(relpath, album, file_size):
    """Uploads file from GDrive → GPhotos with request optimization."""
    # Check upload volume quota before upload
    if not check_upload_quota(file_size):
        reset_time, seconds_until_reset = get_quota_reset_time()
        raise QuotaExceededError(reset_time, seconds_until_reset)
    
    src = f"{GDRIVE}:{SOURCE_PATH}/{relpath}"
    dest = f"{GPHOTOS}:album/{album}/"   # ← added 'album/'
    
    # Determine if this is the first file in album (requires more requests)
    is_first_in_album = ensure_album(album)
    
    # Estimate number of requests: first file in album may require more
    estimated_requests = REQUESTS_FIRST_IN_ALBUM if is_first_in_album else REQUESTS_PER_UPLOAD

    cmd = [
        "rclone", "copy", src, dest,
        "--checkers", "8",
        "--transfers", str(MAX_PARALLEL_UPLOADS),
        "--timeout", f"{UPLOAD_TIMEOUT}s",
        "--low-level-retries", "5",
        "--retries", "3",
        "--bwlimit", "2M",
        "--quiet"
    ]

    try:
        run_cmd(cmd, retries=5, cooldown=30, is_gphotos_api=True, estimated_requests=estimated_requests)
        # Increment uploaded bytes counter after successful upload
        increment_upload_bytes(file_size)
        DONE.add(relpath)
        json.dump(list(DONE), open(STATE_FILE, "w"))
        METRICS["uploaded_files"] += 1
        METRICS["by_album"][album] += 1
        log(f"✅ {relpath} → {album}")
    except QuotaExceededError:
        # Re-raise QuotaExceededError to stop the loop
        raise
    except Exception as e:
        METRICS["errors"] += 1
        log(f"❌ Error uploading {relpath}: {e}")


def save_summary():
    METRICS["finished"] = datetime.now().isoformat()
    METRICS["duration_sec"] = round(time.time() - START_TIME, 2)
    METRICS["albums_created"] = sorted(list(METRICS["albums_created"]))
    # Save current quotas to metrics
    api_requests, uploaded_bytes = load_daily_quota()
    METRICS["api_requests_used"] = api_requests
    METRICS["uploaded_bytes"] = uploaded_bytes
    with open(SUMMARY_PATH, "w") as f:
        json.dump(METRICS, f, indent=2, ensure_ascii=False)
    log(f"📊 Report saved: {SUMMARY_PATH}")


def main():
    global START_TIME
    START_TIME = time.time()

    log(f"📄 Log: {LOG_PATH}")
    
    # Load daily quotas at startup
    api_requests, uploaded_bytes = load_daily_quota()
    uploaded_mb = uploaded_bytes / (1024 * 1024)
    log(f"📊 Current quotas: {api_requests}/{API_QUOTA_LIMIT} requests, {uploaded_mb:.1f} MB uploaded")
    
    files = list_drive_files()
    total = len(files)
    METRICS["processed_files"] = total
    log(f"📂 Files found: {total}")

    try:
        for i, (relpath, file_size) in enumerate(files, 1):
            if relpath in DONE:
                continue

            low = relpath.lower()
            is_photo = low.endswith(PHOTO_EXT)
            is_video = low.endswith(VIDEO_EXT)
            if not (is_photo or is_video):
                continue

            album = infer_album(relpath, is_video)
            try:
                upload_file(relpath, album, file_size)
            except QuotaExceededError as e:
                # Save limit reached information to metrics
                METRICS["quota_exceeded"] = True
                METRICS["quota_reset_time"] = e.reset_time.isoformat()
                # Save state before stopping
                json.dump(list(DONE), open(STATE_FILE, "w"))
                save_summary()
                log(f"⏸ Stopping due to daily quota limit reached")
                log(f"📊 Progress saved: {METRICS['uploaded_files']} files uploaded, {METRICS['errors']} errors")
                log(f"🔄 Run script again after {e.reset_time.strftime('%Y-%m-%d %H:%M:%S PST')} to continue")
                raise

            if i % 100 == 0 or i == total:
                elapsed = time.time() - START_TIME
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate / 60 if rate > 0 else 0
                # Show current quotas in progress
                api_requests, uploaded_bytes = load_daily_quota()
                uploaded_mb = uploaded_bytes / (1024 * 1024)
                log(f"📈 Progress: {i}/{total} ({i/total*100:.1f}%) ETA ≈ {eta:.1f} min | Quotas: {api_requests}/{API_QUOTA_LIMIT} requests, {uploaded_mb:.1f} MB")

        save_summary()
        log(f"🎉 Completed: {METRICS['uploaded_files']} files, {METRICS['errors']} errors.")
    except QuotaExceededError:
        # Exception already handled above, just re-raise
        raise


if __name__ == "__main__":
    try:
        main()
    except QuotaExceededError as e:
        log(f"⏹ Exiting due to daily quota limit")
        log(f"⏰ Quota will reset at: {e.reset_time.strftime('%Y-%m-%d %H:%M:%S PST')}")
        # State already saved in main()
    except KeyboardInterrupt:
        log("⏹ Stopped by user.")
        save_summary()
