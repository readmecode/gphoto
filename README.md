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
  - Safety reserve (100 requests)
  - Proactive stop at 98% limit

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
| `SOURCE_PATH` | `Photo` | Path in Google Drive to sync from |
| `LOG_DIR` | `~/gphoto_logs` | Directory for logs and reports |
| `MAX_PARALLEL_UPLOADS` | `2` | Maximum parallel uploads |
| `UPLOAD_TIMEOUT` | `600` | Upload timeout in seconds |
| `PHOTO_EXT` | `.jpg,.jpeg,.png,.heic,.cr2` | Photo file extensions (comma-separated) |
| `VIDEO_EXT` | `.mp4,.mov,.avi,.mkv` | Video file extensions (comma-separated) |
| `IGNORED_EXT` | `.thm,.lrv,.json` | Extensions to ignore (comma-separated) |

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
- **Stop Threshold**: 98% usage (with 100 request safety reserve)

The script tracks:
- API requests (estimated per operation: 2 for upload, 3 for first file in album)
- Uploaded bytes
- Automatic quota reset detection (midnight PST)

When quota is exceeded, the script:
- Saves current state
- Logs reset time
- Provides instructions for resuming after quota reset

## 📁 Output Files

All output files are saved to `LOG_DIR` (default: `~/gphoto_logs/`):

- `sync_YYYYMMDD_HHMMSS.log` - Detailed operation log
- `summary_YYYYMMDD_HHMMSS.json` - Final report with metrics:
  - Processed and uploaded file counts
  - Created albums list
  - Quota usage (API requests and bytes)
  - Execution duration
  - Quota limit information (if reached)
- `state.json` - Sync state (processed files)
- `daily_quota.json` - Daily quota tracking

## 🛡️ Error Handling

The script automatically handles:
- **Rate limiting (429 errors)**: Exponential backoff with retries
- **Daily quota exceeded**: Graceful stop with state preservation
- **Temporary errors**: Automatic retries with increasing delays
- **Network issues**: Configurable retries and timeouts

## 📊 Example Output

```
[10:30:15] 📄 Лог: ~/gphoto_logs/sync_20240115_103015.log
[10:30:15] 📊 Текущие квоты: 0/10000 запросов, 0.0 МБ загружено
[10:30:16] 📂 Найдено файлов: 1250
[10:30:20] ✅ IMG_2023_05_15.jpg → 2023_05_photo
[10:30:25] ✅ VID_2023_06_20.mp4 → 2023_06_video
...
[10:45:30] 📈 Прогресс: 100/1250 (8.0%) ETA ≈ 125.5 мин | Квоты: 200/10000 запросов, 1250.5 МБ
```

## 🔄 Resuming After Quota Exceeded

If the script stops due to quota limits:

1. Wait until the reset time (shown in logs, typically midnight PST / 10:00 Turkey time)
2. Run the script again:
   ```bash
   python3 main.py
   ```
3. The script will automatically resume from where it stopped using `state.json`

📅 Quota resets at **00:00 PST (10:00 Turkey time)**

## License

This project is provided as-is for personal use.

## 🩺 Troubleshooting

| Problem | Solution |
|---------|----------|
| `Quota exceeded for quota metric 'all requests per day'` | Wait until 10:00 Turkey time (00:00 PST) — quota will reset |
| `rclone: can't upload files here` | Use `gphotos:upload` or `gphotos:album/...`, not just `gphotos:` |
| `directory not found` | Verify path exists: `rclone lsf gdrive:Photo` |
| `Access blocked: app not verified` | Add yourself as **tester** in Google Cloud → OAuth consent screen → Test users |
| `401: Invalid Credentials` | Re-authenticate: `rclone config reconnect gdrive:` and `rclone config reconnect gphotos:` |
| `403: Forbidden` | Check that APIs are enabled in Google Cloud Console |

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
# Run at 10:05 AM daily (after quota reset at 10:00)
5 10 * * * /usr/local/bin/python3 /path/to/gphoto/main.py >> ~/gphoto_cron.log 2>&1
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
🎉 Завершено: 1250 файлов, ошибок 0.
```

— everything is working perfectly 💪

## Notes

- The script uses PST timezone for quota reset calculations
- Album names are derived from file names using regex pattern matching
- Files already processed are skipped (tracked in `state.json`)
- The script is designed for large collections and can handle interruptions gracefully
- The script automatically creates albums in format `YYYY_MM_photo` / `YYYY_MM_video` based on file dates

