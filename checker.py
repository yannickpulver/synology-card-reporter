#!/usr/bin/env python3
"""SD Card → Synology NAS Backup Checker"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


def resolve_op_reference(value: str | None) -> str | None:
    """Resolve 1Password reference (op://...) to actual value."""
    if not value or not value.startswith('op://'):
        return value
    try:
        result = subprocess.run(
            ['op', 'read', value],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
        print(f"Warning: Failed to resolve 1Password reference: {result.stderr.strip()}")
        return None
    except FileNotFoundError:
        print("Error: 1Password CLI (op) not found. Install it or use plain credentials.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("Error: 1Password CLI timed out. Are you signed in?")
        sys.exit(1)


from synology_api.filestation import FileStation

# Supported media extensions
PHOTO_EXTS = {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.tiff', '.tif'}
RAW_EXTS = {'.cr2', '.cr3', '.arw', '.nef', '.dng', '.raf', '.orf', '.rw2'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mts', '.m4v'}
SIDECAR_EXTS = {'.xmp', '.json', '.aae'}
MEDIA_EXTS = PHOTO_EXTS | RAW_EXTS | VIDEO_EXTS | SIDECAR_EXTS

# System volumes to exclude from picker (macOS)
SYSTEM_VOLUMES = {'Macintosh HD', 'Macintosh HD - Data', 'Recovery', 'Preboot', 'VM', 'Update'}


def get_available_volumes() -> list[tuple[str, int]]:
    """Get list of mounted volumes with sizes (macOS)."""
    volumes = []
    volumes_path = Path('/Volumes')

    if not volumes_path.exists():
        return volumes

    for vol in volumes_path.iterdir():
        if vol.name in SYSTEM_VOLUMES:
            continue
        if not vol.is_dir():
            continue
        try:
            stat = os.statvfs(vol)
            size_gb = (stat.f_blocks * stat.f_frsize) // (1024 ** 3)
            volumes.append((str(vol), size_gb))
        except (OSError, PermissionError):
            continue

    return sorted(volumes)


def select_volume() -> str:
    """Interactive volume picker."""
    volumes = get_available_volumes()

    if not volumes:
        print("No removable volumes found in /Volumes/")
        sys.exit(1)

    print("\nAvailable volumes:")
    for i, (path, size) in enumerate(volumes, 1):
        print(f"  {i}. {path} ({size}GB)")

    while True:
        try:
            choice = input(f"\nSelect volume [1-{len(volumes)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(volumes):
                return volumes[idx][0]
            print(f"Please enter a number between 1 and {len(volumes)}")
        except ValueError:
            print("Please enter a valid number")
        except KeyboardInterrupt:
            print("\nCancelled")
            sys.exit(0)


def is_media_file(path: Path) -> bool:
    """Check if file is a supported media type."""
    return path.suffix.lower() in MEDIA_EXTS


def scan_sd_card(sd_path: str) -> dict[str, tuple[int, float]]:
    """Scan SD card for media files. Returns {filename: (size, mtime)}."""
    files = {}
    sd_root = Path(sd_path)

    if not sd_root.exists():
        print(f"Error: Path does not exist: {sd_path}")
        sys.exit(1)

    for path in sd_root.rglob('*'):
        if path.is_file() and is_media_file(path):
            stat = path.stat()
            # Use lowercase filename as key for case-insensitive matching
            files[path.name.lower()] = (path.name, stat.st_size, stat.st_mtime, path)

    return files


def scan_nas_folder(fs: FileStation, nas_path: str) -> dict[str, tuple[int, float]]:
    """Recursively scan NAS folder. Returns {filename: (size, mtime)}."""
    files = {}
    folders_to_scan = [nas_path]

    while folders_to_scan:
        folder = folders_to_scan.pop()
        try:
            result = fs.get_file_list(
                folder_path=folder,
                additional=['size', 'time'],
                limit=5000
            )

            if not result or 'data' not in result:
                continue

            for item in result['data'].get('files', []):
                if item.get('isdir'):
                    folders_to_scan.append(item['path'])
                else:
                    name = item['name'].lower()
                    size = item.get('additional', {}).get('size', 0)
                    mtime = item.get('additional', {}).get('time', {}).get('mtime', 0)
                    # Store first occurrence (any match is fine)
                    if name not in files:
                        files[name] = (size, mtime)
        except Exception as e:
            print(f"Warning: Could not scan {folder}: {e}")

    return files


def format_size(size: int) -> str:
    """Format file size in human-readable form."""
    if size >= 1024 ** 3:
        return f"{size / (1024 ** 3):.1f} GB"
    elif size >= 1024 ** 2:
        return f"{size / (1024 ** 2):.1f} MB"
    elif size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def format_time(timestamp: float) -> str:
    """Format timestamp as readable date."""
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')


def compare_files(
    sd_files: dict,
    nas_files: dict,
    time_tolerance: int = 2
) -> tuple[list, list]:
    """Compare SD files against NAS. Returns (backed_up, missing)."""
    backed_up = []
    missing = []

    for key, (name, size, mtime, path) in sd_files.items():
        if key in nas_files:
            nas_size, nas_mtime = nas_files[key]
            # Match if mtime within tolerance
            if abs(mtime - nas_mtime) <= time_tolerance:
                backed_up.append((name, size, mtime, path))
            else:
                missing.append((name, size, mtime, path))
        else:
            missing.append((name, size, mtime, path))

    return backed_up, missing


def main():
    parser = argparse.ArgumentParser(
        description='Check if SD card files are backed up to Synology NAS'
    )
    parser.add_argument('nas_path', help='NAS folder to search (e.g., /photos)')
    parser.add_argument('sd_path', nargs='?', help='SD card path (optional, uses picker)')
    parser.add_argument('--volume', '-v', help='SD card path (alternative to positional)')
    parser.add_argument('--show-skipped', action='store_true', help='List skipped non-media files')
    parser.add_argument('--output', '-o', help='Write report to file')

    args = parser.parse_args()

    # Load credentials (supports 1Password references like op://vault/item/field)
    load_dotenv()
    host = resolve_op_reference(os.getenv('SYNOLOGY_HOST'))
    port = int(os.getenv('SYNOLOGY_PORT', 5000))
    user = resolve_op_reference(os.getenv('SYNOLOGY_USER'))
    password = resolve_op_reference(os.getenv('SYNOLOGY_PASS'))
    secure = os.getenv('SYNOLOGY_SECURE', 'false').lower() == 'true'

    if not all([host, user, password]):
        print("Error: Missing NAS credentials. Create .env file from config.example")
        sys.exit(1)

    # Determine SD card path
    sd_path = args.volume or args.sd_path
    if not sd_path:
        sd_path = select_volume()

    # Output setup
    output_lines = []
    def log(msg: str = ''):
        print(msg)
        output_lines.append(msg)

    # Scan SD card
    log(f"Scanning SD card: {sd_path}")
    sd_files = scan_sd_card(sd_path)
    log(f"Found {len(sd_files)} media files")

    # Count skipped files if requested
    if args.show_skipped:
        sd_root = Path(sd_path)
        all_files = list(sd_root.rglob('*'))
        skipped = [f for f in all_files if f.is_file() and not is_media_file(f)]

    # Connect to NAS
    log(f"\nConnecting to NAS: {host}")
    try:
        fs = FileStation(
            ip_address=host,
            port=port,
            username=user,
            password=password,
            secure=secure,
            cert_verify=False,
            dsm_version=7
        )
    except Exception as e:
        print(f"Error connecting to NAS: {e}")
        sys.exit(1)

    # Scan NAS
    log(f"Scanning {args.nas_path} (recursive)...")
    nas_files = scan_nas_folder(fs, args.nas_path)
    log(f"Found {len(nas_files)} files on NAS")

    # Compare
    log("\nComparing...")
    backed_up, missing = compare_files(sd_files, nas_files)

    log(f"\n✓ {len(backed_up)} files backed up")

    if missing:
        log(f"✗ {len(missing)} files missing:\n")
        # Sort by path
        missing.sort(key=lambda x: str(x[3]))
        for name, size, mtime, path in missing:
            log(f"  {name:<30} ({format_size(size)}, {format_time(mtime)})")
    else:
        log("\nAll files are backed up!")

    # Show skipped files
    if args.show_skipped and skipped:
        log(f"\nSkipped {len(skipped)} non-media files:")
        for f in sorted(skipped)[:20]:  # Limit to 20
            log(f"  {f.name}")
        if len(skipped) > 20:
            log(f"  ... and {len(skipped) - 20} more")
    elif args.show_skipped:
        log("\nNo non-media files skipped")

    # Write to file if requested
    if args.output:
        with open(args.output, 'w') as f:
            f.write('\n'.join(output_lines))
        print(f"\nReport written to {args.output}")


if __name__ == '__main__':
    main()
