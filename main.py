#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Drive → Google Photos Auto Sync
Works without local downloads
Sorts by date and file type (photo/video)
Creates albums YYYY_MM_photo / YYYY_MM_video
Automatically handles quota errors (429 Too Many Requests)
Saves JSON report and detailed log
Optimizations to prevent quota overruns:
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
# CONFIG
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

PHOTO_EXT = tuple(
    x.strip() for x in os.getenv("PHOTO_EXT", ".jpg,.jpeg,.png,.heic,.cr2").split(",")
)
VIDEO_EXT = tuple(
    x.strip() for x in os.getenv("VIDEO_EXT", ".mp4,.mov,.avi,.mkv").split(",")
)
IGNORED_EXT = tuple(
    x.strip() for x in os.getenv("IGNORED_EXT", ".thm,.lrv,.json").split(",")
)

# Quota limit constants
API_QUOTA_LIMIT = 10000  # requests per day
UPLOAD_QUOTA_LIMIT = 53_687_091_200  # 50 GB in bytes
WARNING_THRESHOLD = 0.8  # 80%
CRITICAL_THRESHOLD = 0.9  # 90%
STOP_THRESHOLD = 0.95  # 95% - more conservative approach, leaving a reserve
SAFETY_RESERVE = 300  # Request reserve for unexpected operations and retries

STATE_FILE = Path(LOG_DIR) / "state.json"
FAILED_FILE = Path(LOG_DIR) / "failed.json"
if STATE_FILE.exists():
    try:
        with open(STATE_FILE, "r") as state_file:
            DONE = set(json.load(state_file))
    except Exception:
        DONE = set()
else:
    DONE = set()

try:
    with open(FAILED_FILE, "r") as failed_file:
        failed_raw = json.load(failed_file)
        if isinstance(failed_raw, dict):
            FAILED = failed_raw
        elif isinstance(failed_raw, list):
            FAILED = {
                path: {
                    "reason": "previous failure (legacy list entry)",
                    "timestamp": None,
                }
                for path in failed_raw
            }
        else:
            FAILED = {}
except Exception:
    FAILED = {}

DAILY_QUOTA_FILE = Path(LOG_DIR) / "daily_quota.json"

# Cache for tracking albums that have already been used (to avoid repeated checks)
KNOWN_ALBUMS = set()

# Quota synchronization tracking
LAST_SYNC_TIME = None
LAST_SYNC_UPLOADS = 0
SYNC_INTERVAL_UPLOADS = 15  # Sync every 15 uploads
SYNC_INTERVAL_SECONDS = 300  # Sync every 5 minutes

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
    "uploaded_bytes": 0,
    "failed_files": {},
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
        super().__init__(
            f"Daily quota limit reached. Reset at {reset_time.strftime('%Y-%m-%d %H:%M:%S PST')}"
        )


def is_daily_quota_exceeded(stderr: str) -> bool:
    """Determines if daily quota limit has been reached."""
    stderr_lower = stderr.lower()
    daily_quota_indicators = [
        "all requests per day",
        "quota exceeded for quota metric 'all requests'",
        "quota metric 'all requests' and limit 'all requests per day'",
    ]
    return any(indicator in stderr_lower for indicator in daily_quota_indicators)


def get_current_pst_date():
    """Returns current date in PST."""
    pst = pytz.timezone("America/Los_Angeles")
    return datetime.now(pst).date()


def get_quota_reset_time():
    """Calculates the time of the next quota reset (midnight PST of the next day)."""
    # Get current time in PST
    pst = pytz.timezone("America/Los_Angeles")
    now_pst = datetime.now(pst)

    # Calculate midnight of the next day in PST
    next_day = now_pst.date() + timedelta(days=1)
    reset_time_naive = datetime.combine(next_day, dt_time.min)
    reset_time = pst.localize(reset_time_naive)

    # Calculate number of seconds until reset
    seconds_until_reset = int((reset_time - now_pst).total_seconds())

    return reset_time, seconds_until_reset


def get_real_quota_usage():
    """Gets real quota usage via Google Cloud Monitoring API.
    
    Uses metric serviceruntime.googleapis.com/api/request_count filtered by
    photoslibrary.googleapis.com service to get real request count for today.
    Metric updates with 2-15 minute delay (normal for Google Monitoring).
    
    Returns (api_requests, uploaded_bytes) or None on error.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        # Don't log here - this is expected if GOOGLE_CLOUD_PROJECT_ID is not set
        return None
    
    try:
        from google.cloud import monitoring_v3
        from google.oauth2 import service_account
        import google.auth
        from google.protobuf import timestamp_pb2
    except ImportError as e:
        # Library not installed
        log(f"Monitoring API: required library not installed ({e})")
        return None
    
    try:
        # Get credentials
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if credentials_path and os.path.exists(credentials_path):
            credentials = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/monitoring.read"]
            )
        else:
            # Try to use Application Default Credentials
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/monitoring.read"]
            )
        
        # Create Monitoring API client
        client = monitoring_v3.MetricServiceClient(credentials=credentials)
        project_name = f"projects/{project_id}"
        
        # Calculate time range: from start of day PST to current time
        pst = pytz.timezone("America/Los_Angeles")
        now_pst = datetime.now(pst)
        today_start_pst = datetime.combine(now_pst.date(), dt_time.min)
        today_start_pst = pst.localize(today_start_pst)
        
        # Convert to UTC for API
        today_start_utc = today_start_pst.astimezone(pytz.UTC)
        now_utc = now_pst.astimezone(pytz.UTC)
        
        # Create time interval via _pb (protobuf object)
        interval = monitoring_v3.TimeInterval()
        end_timestamp = timestamp_pb2.Timestamp(seconds=int(now_utc.timestamp()))
        start_timestamp = timestamp_pb2.Timestamp(seconds=int(today_start_utc.timestamp()))
        interval._pb.end_time.CopyFrom(end_timestamp)
        interval._pb.start_time.CopyFrom(start_timestamp)
        
        # Create metric request
        # Correct metric: serviceruntime.googleapis.com/api/request_count
        # Filter by service: photoslibrary.googleapis.com
        request = monitoring_v3.ListTimeSeriesRequest()
        request.name = project_name
        request.filter = (
            'metric.type = "serviceruntime.googleapis.com/api/request_count" '
            'AND resource.labels.service = "photoslibrary.googleapis.com"'
        )
        request.interval = interval
        
        # Configure aggregation via _pb
        from google.protobuf import duration_pb2
        alignment_period = duration_pb2.Duration(seconds=3600)  # 1 hour
        request._pb.aggregation.alignment_period.CopyFrom(alignment_period)
        request._pb.aggregation.per_series_aligner = monitoring_v3.Aggregation.Aligner.ALIGN_SUM
        request._pb.aggregation.cross_series_reducer = monitoring_v3.Aggregation.Reducer.REDUCE_SUM
        request.view = monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL
        
        # Execute request with timeout (30 seconds)
        # timeout parameter accepts float (seconds) or None
        response = client.list_time_series(request=request, timeout=30.0)
        
        # Sum values from time series
        # REDUCE_SUM should sum all series, but if metric is split by methods,
        # there may be multiple series - sum all points from all series
        total_requests = 0
        series_count = 0
        for time_series in response:
            series_count += 1
            if not time_series.points:
                continue
            # Sum all points in the series (each point represents aggregated data for a period)
            for point in time_series.points:
                if hasattr(point.value, 'int64_value') and point.value.int64_value:
                    total_requests += point.value.int64_value
                elif hasattr(point.value, 'double_value') and point.value.double_value:
                    total_requests += int(point.value.double_value)
        
        if total_requests > 0:
            # Metric updates with 2-15 minute delay, but this is real usage
            # Don't log every API call - only log errors
            return total_requests, None
        
        # No data is normal (no requests today or metric not updated yet)
        if series_count == 0:
            log("Monitoring API: no time series found (no requests today or metric not updated yet)")
            return None
        
        # If we have series but total_requests is 0, return None (no data yet)
        return None
    except Exception as e:
        # Log error for diagnostics
        error_type = type(e).__name__
        error_msg = str(e)
        import traceback
        
        if "DefaultCredentialsError" in error_type or "credentials" in error_msg.lower():
            # Credentials not configured
            log(f"Monitoring API: credentials not configured ({error_type})")
            return None
        
        # Log other errors with full traceback for diagnostics
        log(f"Monitoring API error: {error_type}: {error_msg}")
        # Save traceback to log for debugging
        tb_lines = traceback.format_exc().split('\n')
        for line in tb_lines[:5]:  # First 5 lines of traceback
            if line.strip():
                log(f"  {line}")
        return None


def sync_quota_from_api():
    """Syncs local counter with real value from Google Cloud Monitoring API.
    
    Returns True if sync successful, False otherwise.
    Preserves uploaded_bytes value (not available from Monitoring API).
    """
    real_usage = get_real_quota_usage()
    if real_usage is None:
        return False
    
    api_requests, uploaded_bytes = real_usage
    if api_requests is None:
        return False
    
    # Load current data
    current_date = get_current_pst_date()
    date_str = current_date.isoformat()
    
    if DAILY_QUOTA_FILE.exists():
        try:
            with open(DAILY_QUOTA_FILE, "r") as f:
                quota_data = json.load(f)
            
            if quota_data.get("date") == date_str:
                old_requests = quota_data.get("api_requests", 0)
                old_uploaded_bytes = quota_data.get("uploaded_bytes", 0)
                # Sync if value changed or first sync
                if api_requests != old_requests:
                    diff = api_requests - old_requests
                    # Only log significant changes (>10 requests) to reduce noise
                    if abs(diff) > 10:
                        log(f"Quota sync: {old_requests} → {api_requests} requests (diff: {diff:+d})")
                    quota_data["api_requests"] = api_requests
                    quota_data["quota_source"] = "api"
                    # Preserve uploaded_bytes (not available from Monitoring API)
                    if "uploaded_bytes" not in quota_data:
                        quota_data["uploaded_bytes"] = old_uploaded_bytes
                    save_daily_quota(quota_data)
                    return True
                else:
                    # Value unchanged, but update source
                    quota_data["quota_source"] = "api"
                    # Preserve uploaded_bytes
                    if "uploaded_bytes" not in quota_data:
                        quota_data["uploaded_bytes"] = old_uploaded_bytes
                    save_daily_quota(quota_data)
                    return True
        except Exception as e:
            log(f"Warning: Failed to sync quota: {e}")
            return False
    else:
        # File doesn't exist, create new with API data
        quota_data = {
            "date": date_str,
            "api_requests": api_requests,
            "uploaded_bytes": 0,  # New file, start from 0
            "quota_source": "api"
        }
        save_daily_quota(quota_data)
        return True
    
    return False


def load_daily_quota():
    """Loads daily quotas, resets if new day.
    
    Sync priority:
    1. Real value from Google Cloud Monitoring API (if configured)
    2. Local saved value
    
    Preserves uploaded_bytes on restart (only resets on new day).
    """
    current_date = get_current_pst_date()
    date_str = current_date.isoformat()

    # Check for manual synchronization from environment variable
    initial_requests = os.getenv("INITIAL_API_REQUESTS")
    if initial_requests is not None:
        try:
            initial_requests = int(initial_requests)
            log(f"Manual sync: using {initial_requests} as initial API requests count")
        except (ValueError, TypeError):
            log(f"Warning: Invalid manual sync value, ignoring")
            initial_requests = None

    # Attempt to get real value from API at startup
    real_usage = get_real_quota_usage()
    api_requests_from_api = None
    if real_usage is not None:
        api_requests_from_api, _ = real_usage
        # Only log at startup if we got a value (silent sync during runtime)
        # Note: If real_usage is None, error details are already logged in get_real_quota_usage()

    if DAILY_QUOTA_FILE.exists():
        try:
            with open(DAILY_QUOTA_FILE, "r") as f:
                quota_data = json.load(f)

            # Check if it's a new day
            if quota_data.get("date") == date_str:
                api_requests = quota_data.get("api_requests", 0)
                uploaded_bytes = quota_data.get("uploaded_bytes", 0)  # Preserve on restart
                
                # Priority: API > manual sync > local value
                # Ensure uploaded_bytes is preserved in quota_data
                if "uploaded_bytes" not in quota_data:
                    quota_data["uploaded_bytes"] = uploaded_bytes
                
                if api_requests_from_api is not None:
                    # Always update quota_source to "api" if Monitoring API is available
                    needs_save = False
                    if api_requests_from_api != api_requests:
                        # Only log significant changes (>10 requests) or at startup
                        diff = api_requests_from_api - api_requests
                        if abs(diff) > 10:
                            log(f"Quota sync: {api_requests} → {api_requests_from_api} requests (diff: {diff:+d})")
                        api_requests = api_requests_from_api
                        quota_data["api_requests"] = api_requests
                        needs_save = True
                    # Update quota_source to "api" even if values match (Monitoring API is working)
                    if quota_data.get("quota_source") != "api":
                        quota_data["quota_source"] = "api"
                        needs_save = True
                    if needs_save:
                        save_daily_quota(quota_data)
                    # Return updated values
                    return api_requests, uploaded_bytes
                elif initial_requests is not None and initial_requests > api_requests:
                    log(f"Syncing from manual: {api_requests} → {initial_requests} requests")
                    api_requests = initial_requests
                    quota_data["api_requests"] = api_requests
                    quota_data["quota_source"] = "manual"
                    save_daily_quota(quota_data)
                elif "quota_source" not in quota_data:
                    quota_data["quota_source"] = "local"
                    save_daily_quota(quota_data)
                
                return api_requests, uploaded_bytes
        except Exception:
            pass

    # New day or file doesn't exist - reset or use sync values
    # For new day, uploaded_bytes should be 0 (correct)
    # For missing file, uploaded_bytes should be 0 (correct)
    if api_requests_from_api is not None:
        api_requests = api_requests_from_api
        quota_source = "api"
    elif initial_requests is not None:
        api_requests = initial_requests
        quota_source = "manual"
    else:
        api_requests = 0
        quota_source = "local"
    
    quota_data = {
        "date": date_str,
        "api_requests": api_requests,
        "uploaded_bytes": 0,  # New day or new file - reset to 0 (correct)
        "quota_source": quota_source
    }
    save_daily_quota(quota_data)
    
    if quota_source == "local":
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        if project_id:
            log("Warning: Using local quota counter. Monitoring API sync unavailable (check credentials/permissions).")
            log("Note: Monitoring API provides real usage with 2-15 min delay. Fallback to local counter.")
        else:
            log("Warning: Using local quota counter. Set GOOGLE_CLOUD_PROJECT_ID for Monitoring API sync.")
    
    return api_requests, 0


def save_daily_quota(quota_data=None):
    """Saves daily quotas."""
    if quota_data is None:
        quota_data = {
            "date": get_current_pst_date().isoformat(),
            "api_requests": 0,
            "uploaded_bytes": 0,
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
        "uploaded_bytes": uploaded_bytes,
    }
    save_daily_quota(quota_data)
    return api_requests


def decrement_api_requests(count):
    """Decrements API request counter by count (for rollback on errors)."""
    api_requests, uploaded_bytes = load_daily_quota()
    api_requests = max(0, api_requests - count)  # Don't go below 0
    quota_data = {
        "date": get_current_pst_date().isoformat(),
        "api_requests": api_requests,
        "uploaded_bytes": uploaded_bytes,
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
        "uploaded_bytes": uploaded_bytes,
    }
    save_daily_quota(quota_data)
    return uploaded_bytes


def check_api_quota(requests_needed=1):
    """Checks API request limit, returns True if can continue.

    Args:
        requests_needed: Number of requests planned to make (for projection only)
    """
    api_requests, _ = load_daily_quota()
    # Calculate percentage from ACTUAL usage, not projected
    percentage = api_requests / API_QUOTA_LIMIT
    effective_limit = API_QUOTA_LIMIT - SAFETY_RESERVE  # Account for reserve
    stop_threshold_limit = int(API_QUOTA_LIMIT * STOP_THRESHOLD)  # 95% = 9500
    projected_requests = api_requests + requests_needed  # For projection only

    # Check STOP_THRESHOLD first (95% = 9500 requests) - based on actual usage
    if api_requests >= stop_threshold_limit:
        log(
            f"STOP_THRESHOLD ({STOP_THRESHOLD*100:.0f}%) reached: {api_requests}/{API_QUOTA_LIMIT} ({percentage*100:.1f}%)"
        )
        log(f"Stopping work to prevent quota overrun")
        return False

    # Check effective_limit (with safety reserve) - based on actual usage
    if api_requests >= effective_limit:
        log(
            f"Safe API request limit exceeded: {api_requests}/{API_QUOTA_LIMIT} (reserve: {SAFETY_RESERVE} requests)"
        )
        log(f"Stopping work to prevent quota overrun")
        return False

    # Check projected limit (for planning future operations)
    if projected_requests >= stop_threshold_limit:
        log(
            f"Projected requests would exceed STOP_THRESHOLD: {api_requests}/{API_QUOTA_LIMIT} → {projected_requests} after operation"
        )
        log(f"Stopping work to prevent quota overrun")
        return False

    # Warning and critical levels based on ACTUAL usage
    if percentage >= CRITICAL_THRESHOLD:
        remaining = API_QUOTA_LIMIT - api_requests
        log(
            f"Critical API request level: {api_requests}/{API_QUOTA_LIMIT} ({percentage*100:.1f}%)"
        )
        log(f"Warning: {remaining} requests remaining until limit")
        return True

    if percentage >= WARNING_THRESHOLD:
        remaining = API_QUOTA_LIMIT - api_requests
        log(
            f"Warning: API requests at {percentage*100:.1f}% ({api_requests}/{API_QUOTA_LIMIT})"
        )
        log(f"Warning: {remaining} requests remaining")
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
        log(
            f"Upload volume limit reached: {uploaded_mb:.1f}/{limit_mb:.1f} MB ({percentage*100:.1f}%)"
        )
        log(f"Stopping work to prevent quota overrun")
        return False

    if percentage >= CRITICAL_THRESHOLD:
        uploaded_mb = uploaded_bytes / (1024 * 1024)
        limit_mb = UPLOAD_QUOTA_LIMIT / (1024 * 1024)
        remaining_mb = (UPLOAD_QUOTA_LIMIT - uploaded_bytes) / (1024 * 1024)
        log(
            f"Critical upload volume level: {uploaded_mb:.1f}/{limit_mb:.1f} MB ({percentage*100:.1f}%)"
        )
        log(f"Warning: {remaining_mb:.1f} MB remaining until limit")
        return True

    if percentage >= WARNING_THRESHOLD:
        uploaded_mb = uploaded_bytes / (1024 * 1024)
        limit_mb = UPLOAD_QUOTA_LIMIT / (1024 * 1024)
        remaining_mb = (UPLOAD_QUOTA_LIMIT - uploaded_bytes) / (1024 * 1024)
        log(
            f"Warning: upload volume at {percentage*100:.1f}% ({uploaded_mb:.1f}/{limit_mb:.1f} MB)"
        )
        log(f"Warning: {remaining_mb:.1f} MB remaining")
        return True

    return True


def is_nonrecoverable_media_error(message: str) -> bool:
    """Return True when Google Photos reports a permanent media creation failure."""
    if not message:
        return False
    # Normalize quotes to handle both regular and typographic quotes
    # Replace typographic quotes (U+2019) with regular apostrophe (U+0027)
    msg_normalized = message.lower().replace("\u2019", "'").replace("\u2018", "'")
    known_patterns = [
        "error while trying to create this media item",
        "upload failed: failed: there was an error while trying to create this media item",
        "it may be damaged or use a file format that preview doesn't recognize",
    ]
    return any(pattern in msg_normalized for pattern in known_patterns)


def run_cmd(cmd, retries=3, cooldown=5, is_gphotos_api=False, estimated_requests=1):
    """Safe rclone call with 429/Quota exceeded handling.
    
    Uses real value from Google Cloud Monitoring API.
    Periodically syncs with API to get current value.
    On errors (except quota errors), does NOT count requests (they weren't successful).

    Args:
        estimated_requests: Legacy parameter, kept for API compatibility (not used)
    Returns:
        tuple: (stdout, 0)
    """
    global LAST_SYNC_TIME, LAST_SYNC_UPLOADS
    
    if is_gphotos_api:
        # Sync with API before check (periodically)
        current_time = time.time()
        should_sync = False
        
        if LAST_SYNC_TIME is None:
            # First sync
            should_sync = True
        elif (current_time - LAST_SYNC_TIME) >= SYNC_INTERVAL_SECONDS:
            # Enough time passed
            should_sync = True
        elif (LAST_SYNC_UPLOADS + 1) >= SYNC_INTERVAL_UPLOADS:
            # Enough uploads passed
            should_sync = True
        
        if should_sync:
            if sync_quota_from_api():
                LAST_SYNC_TIME = current_time
                LAST_SYNC_UPLOADS = 0
        
        # Check quota before operation (use real value from Monitoring API)
        if not check_api_quota(0):  # 0 because real value is already in counter
            reset_time, seconds_until_reset = get_quota_reset_time()
            raise QuotaExceededError(reset_time, seconds_until_reset)

    last_error = None
    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            # SUCCESS - do NOT increment counter, it's updated via API sync
            if is_gphotos_api:
                # Increment upload counter for periodic sync
                LAST_SYNC_UPLOADS += 1
                
                # Periodic sync after successful operation
                current_time = time.time()
                if (LAST_SYNC_TIME is None or 
                    (current_time - LAST_SYNC_TIME) >= SYNC_INTERVAL_SECONDS or
                    LAST_SYNC_UPLOADS >= SYNC_INTERVAL_UPLOADS):
                    if sync_quota_from_api():
                        LAST_SYNC_TIME = current_time
                        LAST_SYNC_UPLOADS = 0
            
            return result.stdout.strip(), 0

        stderr = result.stderr
        last_error = stderr

        # Check for daily quota limit
        if is_daily_quota_exceeded(stderr):
            # Quota exceeded - sync to get real value
            if is_gphotos_api:
                sync_quota_from_api()
            
            reset_time, seconds_until_reset = get_quota_reset_time()
            hours_until_reset = seconds_until_reset / 3600
            api_requests, _ = load_daily_quota()
            log(f"Daily quota limit reached (10,000 requests/day)")
            log(f"Current usage: {api_requests}/{API_QUOTA_LIMIT}")
            log(
                f"Quota will reset at: {reset_time.strftime('%Y-%m-%d %H:%M:%S PST')}"
            )
            log(
                f"Wait time remaining: {hours_until_reset:.1f} hours ({seconds_until_reset // 60} minutes)"
            )
            log(f"Saving state to continue after quota reset...")
            raise QuotaExceededError(reset_time, seconds_until_reset)

        stderr_lower = stderr.lower()

        # Check for non-recoverable media errors (damaged/unsupported files)
        if is_nonrecoverable_media_error(stderr):
            log(f"Non-recoverable media error detected, stopping retries")
            # Don't count requests - operation failed before completion
            raise RuntimeError(stderr or "rclone error: non-recoverable media error")

        # Check quota after failed attempt (for planning next retry)
        if is_gphotos_api:
            if not check_api_quota(0):  # Use real value
                reset_time, seconds_until_reset = get_quota_reset_time()
                raise QuotaExceededError(reset_time, seconds_until_reset)

        # Detect temporary quota errors (rate limit)
        if (
            "quota exceeded" in stderr_lower
            or "too many requests" in stderr_lower
            or "rate limit" in stderr_lower
        ):
            wait = cooldown * attempt * 2
            log(
                f"Warning: Temporary rate limit — pausing {wait} sec before retry (attempt {attempt}/{retries})"
            )
            time.sleep(wait)
            continue

        # If error is different — just retry
        log(f"Warning: Error: {stderr.strip()} (attempt {attempt}/{retries})")
        time.sleep(cooldown * attempt)

    # All retries failed - don't count requests (operation didn't succeed)
    raise RuntimeError(last_error or "rclone error")


def list_drive_files():
    """List all photos/videos in Google Drive. Returns list of tuples (path, size)."""
    cmd = ["rclone", "lsjson", f"{GDRIVE}:{SOURCE_PATH}", "--recursive", "--files-only"]
    try:
        out, _ = run_cmd(cmd, is_gphotos_api=False)  # Request to Google Drive, don't count
        data = json.loads(out)
    except Exception as e:
        log(f"Error getting file list: {e}")
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
        log(
            f"Album {album_name} → will be created on first upload (if doesn't exist)"
        )
        return True  # First use of album
    return False  # Album already used


def upload_file(relpath, album, file_size):
    """Uploads file from GDrive → GPhotos.
    
    Uses real quota value from Google Cloud Monitoring API.
    """
    # Check upload volume quota before upload
    if not check_upload_quota(file_size):
        reset_time, seconds_until_reset = get_quota_reset_time()
        raise QuotaExceededError(reset_time, seconds_until_reset)

    src = f"{GDRIVE}:{SOURCE_PATH}/{relpath}"
    dest = f"{GPHOTOS}:album/{album}/"  # ← added 'album/'

    # Determine if this is the first file in album
    is_first_in_album = ensure_album(album)

    cmd = [
        "rclone",
        "copy",
        src,
        dest,
        "--checkers",
        "8",
        "--transfers",
        str(MAX_PARALLEL_UPLOADS),
        "--timeout",
        f"{UPLOAD_TIMEOUT}s",
        "--low-level-retries",
        "5",
        "--retries",
        "3",
        "--bwlimit",
        "2M",
        "--quiet",
    ]

    try:
        _ = run_cmd(
            cmd,
            retries=5,
            cooldown=30,
            is_gphotos_api=True,
            estimated_requests=1,  # Legacy parameter, kept for compatibility
        )
        # Increment uploaded bytes counter after successful upload
        increment_upload_bytes(file_size)
        DONE.add(relpath)
        with open(STATE_FILE, "w") as state_file:
            json.dump(list(DONE), state_file)
        METRICS["uploaded_files"] += 1
        METRICS["by_album"][album] += 1
        log(f"✓ Success: {relpath} → {album}")
    except QuotaExceededError:
        # Re-raise QuotaExceededError to stop the loop
        raise
    except Exception as e:
        METRICS["errors"] += 1
        log(f"Error uploading {relpath}: {e}")
        error_message = str(e)
        if is_nonrecoverable_media_error(error_message):
            reason = (
                "Google Photos rejected the media item as damaged or unsupported. "
                "Re-encode or inspect the original file before retrying."
            )
            FAILED[relpath] = {
                "reason": reason,
                "timestamp": datetime.now().isoformat(),
            }
            METRICS["failed_files"][relpath] = FAILED[relpath]
            with open(FAILED_FILE, "w") as failed_file:
                json.dump(FAILED, failed_file, indent=2, ensure_ascii=False)
            DONE.add(relpath)
            with open(STATE_FILE, "w") as state_file:
                json.dump(list(DONE), state_file)
            log(f"Skipping {relpath}: {reason}")


def save_summary():
    METRICS["finished"] = datetime.now().isoformat()
    METRICS["duration_sec"] = round(time.time() - START_TIME, 2)
    # Save current quotas to metrics
    api_requests, uploaded_bytes = load_daily_quota()
    METRICS["api_requests_used"] = api_requests
    METRICS["uploaded_bytes"] = uploaded_bytes
    METRICS["failed_files"] = FAILED
    # Create a copy for JSON serialization (sets are not JSON serializable)
    metrics_copy = METRICS.copy()
    metrics_copy["albums_created"] = sorted(list(METRICS["albums_created"]))
    with open(SUMMARY_PATH, "w") as f:
        json.dump(metrics_copy, f, indent=2, ensure_ascii=False)
    log(f"Report saved: {SUMMARY_PATH}")


def get_upload_statistics(total_files=None):
    """Calculates and returns upload statistics.
    
    Args:
        total_files: Total number of files to process (if None, only shows uploaded/failed)
    
    Returns:
        dict with statistics
    """
    uploaded_count = len(DONE)
    failed_count = len(FAILED)
    
    stats = {
        "uploaded": uploaded_count,
        "failed": failed_count,
    }
    
    if total_files is not None:
        remaining = total_files - uploaded_count - failed_count
        progress = (uploaded_count / total_files * 100) if total_files > 0 else 0
        stats["remaining"] = remaining
        stats["progress"] = progress
        stats["total"] = total_files
    
    # Count files by extension
    extensions = {}
    for file in DONE:
        if '.' in file:
            ext = '.' + file.split('.')[-1].lower()
            extensions[ext] = extensions.get(ext, 0) + 1
    
    # Sort by count and get top file types
    top_extensions = sorted(extensions.items(), key=lambda x: x[1], reverse=True)[:7]
    stats["top_file_types"] = top_extensions
    
    return stats


def log_upload_statistics(total_files=None):
    """Logs upload statistics in a formatted way."""
    stats = get_upload_statistics(total_files)
    
    log("=== Upload Statistics ===")
    log(f"Uploaded: {stats['uploaded']:,} files")
    log(f"Failed: {stats['failed']:,} files")
    
    if total_files is not None:
        log(f"Remaining: {stats['remaining']:,} files")
        log(f"Progress: {stats['progress']:.1f}%")
    
    if stats["top_file_types"]:
        file_types_str = ", ".join([f"{ext} ({count:,})" for ext, count in stats["top_file_types"]])
        log(f"Top file types: {file_types_str}")


def main():
    global START_TIME, LAST_SYNC_TIME
    START_TIME = time.time()
    LAST_SYNC_TIME = None  # Initialize sync

    log(f"Log: {LOG_PATH}")

    # Load daily quotas at startup (API sync happens in load_daily_quota)
    api_requests, uploaded_bytes = load_daily_quota()
    uploaded_mb = uploaded_bytes / (1024 * 1024)
    
    # Determine quota data source (read after load_daily_quota updates it)
    quota_source = "local"
    if DAILY_QUOTA_FILE.exists():
        try:
            with open(DAILY_QUOTA_FILE, "r") as f:
                quota_data = json.load(f)
                quota_source = quota_data.get("quota_source", "local")
        except Exception:
            pass
    
    log(
        f"Current quotas: {api_requests}/{API_QUOTA_LIMIT} requests, {uploaded_mb:.1f} MB uploaded"
    )
    
    if quota_source == "api":
        log("Quota tracking: using Google Cloud Monitoring API (real usage, 2-15 min delay)")
    elif quota_source == "manual":
        log("Quota tracking: using manual sync value")
    else:
        # quota_source is "local" - check if Monitoring API is actually unavailable
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        if project_id:
            # Try to check if Monitoring API is available (quick test)
            test_result = get_real_quota_usage()
            if test_result is None:
                log("Quota tracking: using local counter")
                log("Note: Monitoring API sync unavailable. Check credentials/permissions.")
            else:
                # Monitoring API works but quota_source wasn't updated yet - will sync next time
                log("Quota tracking: using local counter (Monitoring API available, syncing...)")
        else:
            log("Quota tracking: using local counter (set GOOGLE_CLOUD_PROJECT_ID for Monitoring API sync)")

    # Log initial statistics (without total files count)
    log_upload_statistics()

    files = list_drive_files()
    total = len(files)
    METRICS["processed_files"] = total
    log(f"Files found: {total}")
    
    # Log full statistics with total files count
    log_upload_statistics(total_files=total)

    try:
        processed_count = 0  # Count of files actually processed (not skipped)
        for i, (relpath, file_size) in enumerate(files, 1):
            if relpath in DONE:
                continue
            if relpath in FAILED:
                reason = FAILED[relpath].get("reason", "previous failure")
                log(f"Skipping previously failed file {relpath}: {reason}")
                continue

            low = relpath.lower()
            is_photo = low.endswith(PHOTO_EXT)
            is_video = low.endswith(VIDEO_EXT)
            if not (is_photo or is_video):
                continue

            processed_count += 1  # Count only files that are actually processed
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
                log(f"Stopping due to daily quota limit reached")
                log(
                    f"Progress saved: {METRICS['uploaded_files']} files uploaded, {METRICS['errors']} errors"
                )
                log(
                    f"Run script again after {e.reset_time.strftime('%Y-%m-%d %H:%M:%S PST')} to continue"
                )
                raise

            if processed_count % 100 == 0 or i == total:
                elapsed = time.time() - START_TIME
                rate = processed_count / elapsed if elapsed > 0 else 0
                remaining_files = total - i  # Files remaining in the list
                eta = remaining_files / rate / 60 if rate > 0 else 0
                # Show current quotas in progress
                api_requests, uploaded_bytes = load_daily_quota()
                uploaded_mb = uploaded_bytes / (1024 * 1024)
                log(
                    f"Progress: {i}/{total} ({i/total*100:.1f}%) | Processed: {processed_count} | ETA ≈ {eta:.1f} min | Quotas: {api_requests}/{API_QUOTA_LIMIT} requests, {uploaded_mb:.1f} MB"
                )

        save_summary()
        log(
            f"Completed: {METRICS['uploaded_files']} files, {METRICS['errors']} errors."
        )
    except QuotaExceededError:
        # Exception already handled above, just re-raise
        raise


if __name__ == "__main__":
    try:
        main()
    except QuotaExceededError as e:
        log(f"Exiting due to daily quota limit")
        log(f"Quota will reset at: {e.reset_time.strftime('%Y-%m-%d %H:%M:%S PST')}")
        # State already saved in main()
    except KeyboardInterrupt:
        log("Stopped by user.")
        save_summary()
