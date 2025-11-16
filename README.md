# Google Drive → Google Photos Auto Sync

Automated synchronization tool that transfers photos and videos from Google Drive to Google Photos using `rclone`.

Works **cloud-to-cloud**, without local downloads.

---

## 🚀 Features

- 🔄 **Direct transfer (Drive → Photos)** — No local downloads required
- 📅 **Auto sorting by date/type** — Photos and videos organized into `YYYY_MM_photo` / `YYYY_MM_video` albums
- 🧠 **Quota-safe uploading** — Monitors daily Google API limits
- 🗂 **Album creation** — Automatically creates albums in Google Photos
- 🧾 **Detailed logs and reports** — Saves `.log`, `.json` and `.state` files
- ⚙️ **Resumable sync** — Automatically continues after quota/errors
- ⚡ **Optimizations**:
  - Album caching (avoids repeated existence checks)
  - Precise API request counting (accounts for different operation types)
  - Safety reserve (300 requests for Google Photos SDK overhead)
  - Proactive stop at 95% limit (9,500 requests)

## 🧰 Requirements

### Python Packages
```bash
pip install -r requirements.txt
```

### External Tools
```bash
brew install rclone ffmpeg jq
```

## ⚙️ Configuration

The project uses environment variables (via `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `GDRIVE_REMOTE` | `gdrive` | rclone remote name for Google Drive |
| `GPHOTOS_REMOTE` | `gphotos` | rclone remote name for Google Photos |
| `SOURCE_PATH` | `Photo` | Path in Google Drive to sync from (e.g. `Photo`, the top-level folder that holds your photo subfolders) |
| `LOG_DIR` | `~/gphoto_logs` | Directory for logs and reports |
| `MAX_PARALLEL_UPLOADS` | `2` | Maximum parallel uploads |
| `UPLOAD_TIMEOUT` | `600` | Upload timeout in seconds |
| `PHOTO_EXT` | `.jpg,.jpeg,.png,.heic,.cr2` | Photo file extensions (comma-separated) |
| `VIDEO_EXT` | `.mp4,.mov,.avi,.mkv` | Video file extensions (comma-separated) |
| `IGNORED_EXT` | `.thm,.lrv,.json` | Extensions to ignore (comma-separated) |
| `INITIAL_API_REQUESTS` | (none) | Manual sync: initial API requests count from Google Cloud Console (see Quota Synchronization) |

### Example `.env` file

```env
GDRIVE_REMOTE=gdrive
GPHOTOS_REMOTE=gphotos
SOURCE_PATH=Photo
LOG_DIR=~/gphoto_logs
MAX_PARALLEL_UPLOADS=2
UPLOAD_TIMEOUT=600
PHOTO_EXT=.jpg,.jpeg,.png,.heic,.cr2
VIDEO_EXT=.mp4,.mov,.avi,.mkv
IGNORED_EXT=.thm,.lrv,.json

# Optional: Manual quota synchronization (see Quota Management section)
# INITIAL_API_REQUESTS=9918
```

## 🔑 How to Get Google API Keys (Client ID & Secret)

To allow `rclone` to work on your behalf, you need to create an OAuth client in Google Cloud Console.

### Step 1. Create a Project in [Google Cloud Console](https://console.cloud.google.com/projectcreate)

- Name it, for example: `rclone-photos-sync`
- Click **Create**

### Step 2. Enable APIs

Navigate to:

- **APIs & Services → Library**

  Find and enable:

  - ✅ **Google Drive API**
  - ✅ **Google Photos Library API**

### Step 3. Create OAuth 2.0 Client ID

1. Go to: **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Desktop App**
3. Name it, for example: `rclone-photos-sync`
4. Save — you'll get:
   - `Client ID`
   - `Client Secret`

**Important**: If you see "Access blocked: app not verified", add yourself as a **tester** in **OAuth consent screen → Test users**.

## 🔧 Configure rclone

Open terminal:

```bash
rclone config
```

This opens an interactive menu:

### 1️⃣ Add Google Drive

```
n) New remote
name> gdrive
Storage> drive
```

- **Client ID** → paste your ID
- **Client Secret** → paste your Secret
- Full access (scopes): `drive`
- At the end, say `y` — browser will open for authorization

Verify:

```bash
rclone lsd gdrive:
```

### 2️⃣ Add Google Photos

```
n) New remote
name> gphotos
Storage> google photos
```

- **Client ID / Secret** → you can use the same ones
- When asked:

  ```
  Read only? (true/false)
  ```

  choose **false**
- During authorization, make sure redirect URL matches what rclone shows:

  ```
  http://127.0.0.1:53682/
  ```
- Confirm access in browser

Verify:

```bash
rclone lsf gphotos:
```

You should see:

```
album/
feature/
media/
shared-album/
upload/
```

## 🧭 Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   brew install rclone ffmpeg jq
   ```

2. **Get Google API keys** (see section above)

3. **Configure rclone** (see section above)

4. **Create `.env` file** in project root:
   ```bash
   cp .env.example .env  # if you have an example file
   # or create manually with the example from Configuration section
   ```

5. **Quick test** (see section below)

6. **Run the script:**
   ```bash
   python3 main.py
   ```

## 🧪 Quick Test

Before running the full sync, verify access:

```bash
# List directories in Google Drive
rclone lsd gdrive:

# List directories in Google Photos
rclone lsd gphotos:

# Test copy (dry-run)
rclone copy gdrive:Photo/test.jpg gphotos:upload --dry-run --progress
```

If all commands work without errors, you're ready to proceed.

## 🔄 How It Works

1. **File Discovery**: Lists all files from Google Drive using `rclone lsjson`
2. **Filtering**: Filters files by extension (photos/videos only)
3. **Album Detection**: Extracts date from filename using regex pattern `(19|20\d{2})[-_.]?(0[1-9]|1[0-2])`
4. **Upload**: Transfers files via `rclone copy` with quota checks
5. **State Management**: Saves progress to `state.json` for idempotency
6. **Reporting**: Logs progress every 100 files with ETA and quota usage

## 🕒 Quota Management

The script implements sophisticated quota management to prevent exceeding Google Photos API limits:

- **API Quota**: 10,000 requests per day
- **Upload Quota**: 50 GB per day
- **Warning Threshold**: 80% usage
- **Critical Threshold**: 90% usage
- **Stop Threshold**: 95% usage (9,500 requests) with 300 request safety reserve

### Request Counting Rules

The script counts API requests accurately to match Google's actual quota usage:

- **Each HTTP call to Google Photos API = 1 request**, even if it fails
- **Upload operation**: 2 requests (`mediaItems.upload` + `mediaItems.batchCreate`)
- **First file in album**: 3 requests (`albums.list` + `upload` + `batchCreate`)
- **Retries count as separate requests** - each retry attempt makes real API calls that Google counts
- **Requests are counted for EACH attempt** in retry loops, not just the first one
- **Requests are counted BEFORE execution** to prevent quota overruns
- **Safety reserve**: 300 requests reserved for Google Photos SDK overhead

**Important**: Real usage shows ~1.28 requests/file average, but the script uses conservative estimates (2 requests/file) to account for retries and rclone internal operations.

The script tracks:
- API requests (counted per operation: 2 for upload, 3 for first file in album)
- Uploaded bytes
- Automatic quota reset detection (midnight PST)
- Failed files (recorded in `failed.json` for damaged/unsupported files)
- Progress with separate counters for processed vs. total files

When quota is exceeded, the script:
- Saves current state
- Logs reset time
- Provides instructions for resuming after quota reset

### Quota Synchronization

If you notice a discrepancy between the script's request count and Google Cloud Console (e.g., script shows 584 but Console shows 9,918), you can manually synchronize the counter:

1. **Check actual usage** in [Google Cloud Console](https://console.cloud.google.com/apis/api/photoslibrary.googleapis.com/quotas):
   - Go to **APIs & Services → Quotas**
   - Find "All requests per day" quota
   - Note the "Current usage" value

2. **Set the initial count** via environment variable:
   ```bash
   export INITIAL_API_REQUESTS=9918
   python3 main.py
   ```

   Or add to `.env` file:
   ```env
   INITIAL_API_REQUESTS=9918
   ```

3. **The script will**:
   - Automatically sync the counter with the provided value (if higher than current)
   - Continue counting requests from that point
   - Properly control quota usage going forward

**Why is synchronization needed?**
- Requests made in previous sessions may not be fully tracked
- The script's counter resets daily, but Google's quota accumulates throughout the day
- Manual sync ensures accurate quota control for the rest of the day

## 📁 Output Files

All output files are saved to `LOG_DIR` (default: `~/gphoto_logs/`):

- `sync_YYYYMMDD_HHMMSS.log` - Detailed operation log
- `summary_YYYYMMDD_HHMMSS.json` - Final report with metrics:
  - Processed and uploaded file counts
  - Created albums list
  - Quota usage (API requests and bytes)
  - Execution duration
  - Quota limit information (if reached)
  - Failed files list
- `state.json` - Sync state (processed files)
- `daily_quota.json` - Daily quota tracking
- `failed.json` - Files that failed to upload (damaged/unsupported files) with reasons and timestamps

## 🛡️ Error Handling

The script automatically handles:
- **Rate limiting (429 errors)**: Exponential backoff with retries
- **Daily quota exceeded**: Graceful stop with state preservation
- **Temporary errors**: Automatic retries with increasing delays
- **Network issues**: Configurable retries and timeouts
- **Non-recoverable media errors**: Automatically skips damaged/unsupported files and records them in `failed.json`

## 🔑 Token Refresh

If you encounter authentication errors like `invalid_grant: maybe token expired?`, you need to refresh your OAuth tokens for rclone remotes.

### Refresh Google Photos Token

```bash
rclone config reconnect gphotos:
```

This will:
1. Open your browser for re-authentication
2. Ask you to confirm access to Google Photos
3. Save the new token automatically

### Refresh Google Drive Token

```bash
rclone config reconnect gdrive:
```

This will:
1. Open your browser for re-authentication
2. Ask you to confirm access to Google Drive
3. Save the new token automatically

### When to Refresh Tokens

Tokens may expire in the following situations:
- **After 7 days of inactivity** (Google OAuth tokens have limited lifetime)
- **After revoking app access** in your Google Account settings
- **After changing OAuth client credentials** in Google Cloud Console
- **When you see errors** like:
  - `invalid_grant: maybe token expired?`
  - `401: Invalid Credentials`
  - `couldn't fetch token`

### Verify Token Refresh

After refreshing, verify the connection works:

```bash
# Test Google Photos access
rclone lsf gphotos:

# Test Google Drive access
rclone lsd gdrive:
```

If both commands work without errors, your tokens are valid and the sync script should work correctly.

## 📊 Example Output

```
[10:30:15] 📄 Log: ~/gphoto_logs/sync_20240115_103015.log
[10:30:15] 📊 Current quotas: 0/10000 requests, 0.0 MB uploaded
[10:30:16] 📂 Files found: 1250
[10:30:20] ✅ IMG_2023_05_15.jpg → 2023_05_photo
[10:30:25] ✅ VID_2023_06_20.mp4 → 2023_06_video
...
[10:45:30] 📈 Progress: 100/1250 (8.0%) | Processed: 100 | ETA ≈ 125.5 min | Quotas: 200/10000 requests, 1250.5 MB
```

**Progress format explanation**:
- `Progress: 100/1250` - Total files processed in the list (including skipped files)
- `Processed: 100` - Files actually uploaded (not skipped)
- This helps understand the ratio between total files and actual uploads

## 🔄 Resuming After Quota Exceeded

If the script stops due to quota limits:

1. **Check actual quota usage** in Google Cloud Console to verify
2. **Optionally sync the counter** if there's a large discrepancy:
   ```bash
   export INITIAL_API_REQUESTS=<actual_usage_from_console>
   python3 main.py
   ```
3. Wait until the reset time (shown in logs, typically midnight PST / 11:00 GMT+3)
4. Run the script again:
   ```bash
   python3 main.py
   ```
5. The script will automatically resume from where it stopped using `state.json`

📅 Quota resets at **00:00 PST (11:00 GMT+3)**

**Note**: If you see a large discrepancy between script's count and Google Cloud Console (e.g., script shows 584 but Console shows 9,918), use `INITIAL_API_REQUESTS` to sync before resuming.

## License

This project is provided as-is for personal use.

## 🩺 Troubleshooting

| Problem | Solution |
|---------|----------|
| `Quota exceeded for quota metric 'all requests per day'` | Wait until 11:00 GMT+3 (00:00 PST) — quota will reset |
| `rclone: can't upload files here` | Use `gphotos:upload` or `gphotos:album/...`, not just `gphotos:` |
| `directory not found` | Verify path exists: `rclone lsf gdrive:Photo` |
| `Access blocked: app not verified` | Add yourself as **tester** in Google Cloud → OAuth consent screen → Test users |
| `401: Invalid Credentials` | Re-authenticate: `rclone config reconnect gdrive:` and `rclone config reconnect gphotos:` |
| `403: Forbidden` | Check that APIs are enabled in Google Cloud Console |
| `invalid_grant: maybe token expired?` | Run `rclone config reconnect gphotos:` and reauthorize the Google Photos remote |
| `There was an error while trying to create this media item. (3)` | File is damaged or unsupported — the script skips it and records the failure in `failed.json`; inspect or re-encode the original file |
| Large discrepancy between script's request count and Google Cloud Console | Use `INITIAL_API_REQUESTS` environment variable to manually sync the counter (see Quota Synchronization section) |

## ☁️ Multi-Project Hack (Bypass Daily Limits)

Want to upload more than 10,000 files per day?

Create multiple projects:

```
rclone-photos-sync-1
rclone-photos-sync-2
rclone-photos-sync-3
```

In each, enable Photos API and configure separate `client_id`.

In `.rclone.conf` you can quickly switch:

```ini
[gphotos1]
type = google photos
client_id = ...
client_secret = ...
token = {...}

[gphotos2]
type = google photos
client_id = ...
client_secret = ...
token = {...}
```

Then upload by years:

```bash
rclone copy gdrive:Photo/2010 gphotos1:upload
rclone copy gdrive:Photo/2011 gphotos2:upload
```

## 📅 Automation (cron)

For automatic daily runs:

```bash
crontab -e
```

Add:

```bash
# Run at 11:05 AM daily (after quota reset at 11:00, GMT+3)
5 11 * * * /usr/local/bin/python3 /path/to/gphoto/main.py >> ~/gphoto_cron.log 2>&1
```

Adjust the path to match your installation.

## ✅ Final Check

After running the sync, verify results:

```bash
# List created albums
rclone lsf gphotos:album/ --dirs-only | head

# Check latest log
tail -n 20 ~/gphoto_logs/sync_*.log
```

If you see lines like:

```
✅ IMG_2020_07_01.jpg → 2020_07_photo
🎉 Completed: 1250 files, 0 errors.
```

— everything is working perfectly 💪

## Notes

- The script uses PST timezone for quota reset calculations (as per Google Cloud documentation)
- Album names are derived from file names using regex pattern matching
- Files already processed are skipped (tracked in `state.json`)
- Failed files (damaged/unsupported) are automatically skipped and recorded in `failed.json`
- The script is designed for large collections and can handle interruptions gracefully
- The script automatically creates albums in format `YYYY_MM_photo` / `YYYY_MM_video` based on file dates
- API requests are counted for EACH attempt in retry loops (not just the first one)
- Each retry attempt makes real API calls that Google counts, so they're all tracked
- The script stops at 95% quota usage (9,500 requests) to prevent exceeding the daily limit
- Progress shows both total files processed and actually uploaded files separately
- If you notice a large discrepancy between script's count and Google Cloud Console, use `INITIAL_API_REQUESTS` to sync

