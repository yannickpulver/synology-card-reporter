# Synology SD Card Backup Checker

Check if files on your SD card are backed up to your Synology NAS.

## Features

- Compares SD card files against NAS using Synology FileStation API (fast!)
- Matches by filename + modification time
- Supports photos, RAW, videos, and sidecar files
- Early exit when all files found
- Scans latest folders first
- 1Password CLI support for credentials

## Setup

```bash
pip install -r requirements.txt
cp config.example .env
# Edit .env with your NAS credentials
```

## Usage

```bash
# Uses default folders from .env
python checker.py

# Specify folders
python checker.py /myaccount/Photos/2025 /myaccount/Photos/2024

# Specify SD card volume
python checker.py --volume /Volumes/EOS_DIGITAL
```

## Config (.env)

```
SYNOLOGY_HOST=192.168.1.100
SYNOLOGY_PORT=5000
SYNOLOGY_USER=admin
SYNOLOGY_PASS=op://Private/Synology NAS/password
SYNOLOGY_SECURE=false
SYNOLOGY_FOLDERS=/myaccount/Photos/2026 /myaccount/Photos/2025
```

## Output

```
💾 Scanning SD card: /Volumes/EOS_DIGITAL
   Found 247 media files

🔌 Connecting to NAS: 192.168.1.100
📂 Scanning /myaccount/Photos/2025 (recursive)...
   🎉 All files found! Scanned 12 folders

🔍 Comparing...

✅ 243 files backed up

📁 Matching folders on NAS (2):
   /myaccount/Photos/2025/Adventure/2025.12.21 - Foggy Bern (150 files)
   /myaccount/Photos/2025/Work/2025.01.20 - Shoot (93 files)

❌ 4 files missing:

  2025-01-22 (4 files): IMG_4521.CR2, IMG_4521.JPG, IMG_4522.CR2, IMG_4522.JPG
```
