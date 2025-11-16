# Google Drive → Google Photos Auto Sync

A simple tool that automatically copies your photos and videos from Google Drive to Google Photos. Everything happens in the cloud - no need to download files to your computer.

## What It Does

- Copies photos and videos from Google Drive to Google Photos
- Organizes them into albums by date (like `2023_05_photo` or `2023_06_video`)
- Works automatically - you just run it and it handles everything
- Stops safely if it hits Google's daily limits, then you can continue the next day

## Quick Start

### Step 1: Install Required Software

**On macOS:**
```bash
brew install rclone
pip install -r requirements.txt
```

**On Linux:**
```bash
# Install rclone from https://rclone.org/install/
pip install -r requirements.txt
```

### Step 2: Get Google API Keys

You need to create API keys so the tool can access your Google Drive and Google Photos.

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click "Create Project" and name it (e.g., "photos-sync")
3. Enable these APIs:
   - Go to "APIs & Services" → "Library"
   - Search for and enable "Google Drive API"
   - Search for and enable "Google Photos Library API"
4. Create credentials:
   - Go to "APIs & Services" → "Credentials"
   - Click "Create Credentials" → "OAuth client ID"
   - Choose "Desktop App"
   - Name it (e.g., "photos-sync")
   - Copy the **Client ID** and **Client Secret** (you'll need these)

**Important:** If you see "Access blocked", go to "OAuth consent screen" → "Test users" and add your email address.

### Step 3: Set Up rclone

rclone is a tool that connects to your Google accounts. Run this command:

```bash
rclone config
```

**For Google Drive:**
1. Type `n` (new remote)
2. Name it `gdrive`
3. Choose `drive` (number for Google Drive)
4. Paste your Client ID when asked
5. Paste your Client Secret when asked
6. Choose `1` for full access
7. Type `y` to confirm - your browser will open to authorize

**For Google Photos:**
1. Type `n` (new remote)
2. Name it `gphotos`
3. Choose `google photos` (number for Google Photos)
4. Paste the same Client ID
5. Paste the same Client Secret
6. Choose `false` when asked if read-only
7. Type `y` to confirm - your browser will open to authorize

**Test it works:**
```bash
rclone lsd gdrive:
rclone lsf gphotos:
```

If both commands show folders without errors, you're good!

### Step 4: Configure the Script

Create a file named `.env` in the project folder with this content:

```env
GDRIVE_REMOTE=gdrive
GPHOTOS_REMOTE=gphotos
SOURCE_PATH=Photo
```

Change `SOURCE_PATH` to the name of the folder in your Google Drive where your photos are stored.

### Step 5: Run It

```bash
python3 main.py
```

That's it! The script will:
- Find all photos and videos in your Google Drive folder
- Copy them to Google Photos
- Organize them into albums by date
- Show you progress as it works

## How It Works

1. The script looks at all files in your Google Drive folder
2. It picks out photos and videos (ignores other files)
3. It figures out the date from the filename
4. It creates albums in Google Photos (like `2023_05_photo`)
5. It copies files to the right albums

The script remembers what it's already copied, so you can run it multiple times safely.

## Daily Limits

Google limits how much you can upload per day:
- **10,000 API requests per day**
- **50 GB of uploads per day**

The script automatically stops before hitting these limits. If it stops, just wait until the next day (quota resets at midnight Pacific Time) and run it again - it will continue where it left off.

## If Something Goes Wrong

### "Access blocked" or "App not verified"
- Go to Google Cloud Console → OAuth consent screen → Test users
- Add your email address

### "Token expired" or "Invalid Credentials"
Your login expired. Refresh it:
```bash
rclone config reconnect gdrive:
rclone config reconnect gphotos:
```
This will open your browser to log in again.

### "Quota exceeded"
You've hit Google's daily limit. Wait until tomorrow (after midnight Pacific Time) and run the script again.

### "Directory not found"
Check that your `SOURCE_PATH` in `.env` matches the folder name in Google Drive:
```bash
rclone lsf gdrive:
```

### Script stops but you want to continue
Just run `python3 main.py` again. The script remembers what it already copied and continues from where it stopped.

## Files Created

The script creates some files to track progress (in `~/gphoto_logs/` by default):
- `state.json` - remembers which files were already copied
- `sync_*.log` - detailed log of what happened
- `summary_*.json` - summary report
- `failed.json` - list of files that couldn't be copied (usually damaged files)

## License

This project is provided as-is for personal use.
