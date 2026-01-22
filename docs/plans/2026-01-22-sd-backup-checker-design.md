# SD Card → Synology NAS Backup Checker

## Overview

Python CLI tool that compares files on an SD card against a Synology NAS to identify files not yet backed up.

## Flow

```
SD Card                          Synology NAS
────────                         ────────────
DCIM/                            /photos/2024/adventure/...
  IMG_001.JPG    ──compare──►    /photos/2024/work/...
  IMG_002.ARW                    /photos/2023/...
  MOV_003.MP4
         │
         ▼
    Report: "Missing files:"
    - IMG_002.ARW
    - MOV_003.MP4
```

1. Load NAS credentials from `.env`
2. Scan SD card → collect all media files with name + mtime
3. Connect to NAS via FileStation API
4. Recursively list target folder → collect all files with name + mtime
5. Compare: find SD card files not on NAS (by name + mtime match)
6. Output report

## File Matching

**Supported extensions:**
- Photos: `.jpg`, `.jpeg`, `.heic`, `.heif`, `.png`, `.tiff`, `.tif`
- RAW: `.cr2`, `.cr3`, `.arw`, `.nef`, `.dng`, `.raf`, `.orf`, `.rw2`
- Video: `.mp4`, `.mov`, `.avi`, `.mts`, `.m4v`
- Sidecars: `.xmp`, `.json`, `.aae`

**Matching logic:**
- Search NAS files for same filename (case-insensitive)
- If found: compare modification timestamps (2-second tolerance)
- Same filename in different NAS subfolders → match ANY occurrence

**Unsupported files:** Ignored silently. Use `--show-skipped` to list.

## CLI Interface

```bash
# Interactive volume picker
python checker.py /photos

# Specify volume directly
python checker.py /photos --volume /Volumes/EOS_DIGITAL

# Positional args
python checker.py /Volumes/EOS_DIGITAL /photos

# With options
python checker.py /photos --volume /Volumes/SD --show-skipped --output report.txt
```

**Arguments:**
- `nas_path` (required) — NAS folder to search
- `--volume PATH` — SD card path (skips interactive picker)
- `--show-skipped` — List ignored non-media files
- `--output FILE` — Write report to file

**Volume picker (when no --volume):**
```
Available volumes:
  /Volumes/EOS_DIGITAL (32GB)
  /Volumes/SD_CARD (64GB)
Select volume (or Ctrl+C to cancel): _
```

## Configuration

**.env file:**
```
SYNOLOGY_HOST=192.168.1.100
SYNOLOGY_PORT=5000
SYNOLOGY_USER=admin
SYNOLOGY_PASS=yourpassword
SYNOLOGY_SECURE=false
```

## Output Format

```
Scanning SD card: /Volumes/EOS_DIGITAL
Found 247 media files

Connecting to NAS: 192.168.1.100
Scanning /photos (recursive)...
Found 12,847 files on NAS

Comparing...

✓ 243 files backed up
✗ 4 files missing:

  IMG_4521.CR2      (24.3 MB, 2024-01-15 14:32)
  IMG_4521.JPG      (8.1 MB, 2024-01-15 14:32)
  MOV_4522.MP4      (1.2 GB, 2024-01-15 14:35)
  IMG_4523.ARW      (25.1 MB, 2024-01-15 14:40)

Skipped 12 non-media files (use --show-skipped to list)
```

## Project Structure

```
synology-nas-checker/
├── .env                 # NAS credentials (gitignored)
├── .env.example         # Template for credentials
├── .gitignore
├── requirements.txt     # synology-api, python-dotenv
├── checker.py           # Main script
└── README.md            # Usage instructions
```

## Dependencies

- `synology-api` — Synology FileStation API client
- `python-dotenv` — Load .env credentials

## Technical Notes

- Uses Synology FileStation API (not filesystem access) for speed
- Session-based auth, handles self-signed certs
- Recursive folder listing with pagination for large libraries
