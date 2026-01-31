#!/usr/bin/env python3
"""SD Card → Synology NAS Backup Checker"""

import argparse
import os
import subprocess
import sys
from collections import defaultdict
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


def open_finder_with_files(file_paths: list[Path]) -> None:
    """Open Finder with the specified files selected."""
    if not file_paths:
        return

    # Build AppleScript to select multiple files
    posix_files = ', '.join(f'POSIX file "{p}"' for p in file_paths)
    script = f'''
    tell application "Finder"
        activate
        reveal {{{posix_files}}}
        select {{{posix_files}}}
    end tell
    '''
    subprocess.run(['osascript', '-e', script], capture_output=True)


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


def scan_local_folder(local_path: str, verbose: bool = True) -> dict[str, tuple[int, float, str]]:
    """Scan local folder for media files. Returns {filename: (size, mtime, folder_path)}."""
    files = {}
    root = Path(local_path)

    if not root.exists():
        if verbose:
            print(f"   Warning: Local path does not exist: {local_path}")
        return files

    for path in root.rglob('*'):
        if path.is_file() and is_media_file(path):
            stat = path.stat()
            name = path.name.lower()
            if name not in files:
                files[name] = (stat.st_size, stat.st_mtime, str(path.parent))

    return files


def scan_nas_folder(
    fs: FileStation,
    nas_path: str,
    verbose: bool = True,
    target_files: dict[str, float] | None = None,
    time_tolerance: int = 10
) -> tuple[dict[str, tuple[int, float, str]], bool]:
    """Recursively scan NAS folder. Returns ({filename: (size, mtime, folder_path)}, was_interrupted).

    If target_files provided (dict of filename -> mtime), stops early when all targets found with matching mtime.
    Press Ctrl+C to stop scan early and proceed with results found so far.
    """
    files = {}
    folders_to_scan = [nas_path]
    scanned_count = 0
    remaining = set(target_files.keys()) if target_files else None

    try:
        while folders_to_scan:
            folder = folders_to_scan.pop()
            scanned_count += 1
            if verbose:
                print(f"\r   🔎 {folder[:70]:<70} (Ctrl+C to stop)", end="", flush=True)
            try:
                # Paginate through all files in folder
                offset = 0
                limit = 5000
                subdirs = []

                while True:
                    result = fs.get_file_list(
                        folder_path=folder,
                        additional=['size', 'time'],
                        limit=limit,
                        offset=offset
                    )

                    if not result or 'data' not in result:
                        break

                    items = result['data'].get('files', [])
                    if not items:
                        break

                    for item in items:
                        if item.get('isdir'):
                            subdirs.append(item['path'])
                        else:
                            name = item['name'].lower()
                            size = item.get('additional', {}).get('size', 0)
                            mtime = item.get('additional', {}).get('time', {}).get('mtime', 0)
                            # Check if this file matches target mtime better than existing
                            if target_files and name in target_files:
                                target_mtime = target_files[name]
                                is_match = abs(mtime - target_mtime) <= time_tolerance
                                # Store if: not found yet, OR this one matches and previous didn't
                                if name not in files:
                                    files[name] = (size, mtime, folder)
                                    if is_match and remaining:
                                        remaining.discard(name)
                                elif is_match and remaining and name in remaining:
                                    # Found a matching version, update
                                    files[name] = (size, mtime, folder)
                                    remaining.discard(name)
                            elif name not in files:
                                files[name] = (size, mtime, folder)

                    # Check if we got fewer items than limit (last page)
                    if len(items) < limit:
                        break
                    offset += limit

                # Early exit if all target files found
                if remaining is not None and len(remaining) == 0:
                    if verbose:
                        print(f"\r   🎉 All files found! Scanned {scanned_count} folders{' ' * 30}")
                    return files, False

                # Add subdirs sorted ascending (pop takes from end, so latest scanned first)
                folders_to_scan.extend(sorted(subdirs))
            except Exception as e:
                if isinstance(e, KeyboardInterrupt):
                    raise
                print(f"\nWarning: Could not scan {folder}: {e}")
    except KeyboardInterrupt:
        if verbose:
            print(f"\r   ⏹ Stopped early. Scanned {scanned_count} folders{' ' * 40}")
        return files, True

    if verbose:
        print(f"\r   ✔ Scanned {scanned_count} folders{' ' * 60}")
    return files, False


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


def find_matching_date_folder(dest_path: str, date_str: str) -> Path | None:
    """Find folder starting with date string (YYYY-MM-DD or YYYY.MM.DD)."""
    dest = Path(dest_path)
    if not dest.exists():
        return None
    # Try both YYYY-MM-DD and YYYY.MM.DD formats
    date_dot = date_str.replace('-', '.')
    for folder in dest.iterdir():
        if folder.is_dir() and (folder.name.startswith(date_str) or folder.name.startswith(date_dot)):
            return folder
    return None


def copy_files_to_folder(files: list[Path], dest_folder: Path) -> tuple[int, int]:
    """Copy files, skip existing. Returns (copied, skipped)."""
    import shutil
    dest_folder.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    for src in files:
        dest = dest_folder / src.name
        if dest.exists():
            skipped += 1
        else:
            shutil.copy2(src, dest)
            copied += 1
    return copied, skipped


LAST_DEST_FILE = Path.home() / '.sd-checker-last-dest'


def load_last_dest() -> str | None:
    """Load last used destination from file."""
    if LAST_DEST_FILE.exists():
        return LAST_DEST_FILE.read_text().strip() or None
    return None


def save_last_dest(dest: str) -> None:
    """Save last used destination to file."""
    LAST_DEST_FILE.write_text(dest)


def prompt_copy_missing(by_date: dict[str, list]) -> None:
    """Interactive copy flow for missing files grouped by date."""
    try:
        choice = input("\nCopy missing files? [y/n]: ").strip().lower()
        if choice != 'y':
            return

        # Suggest last used destination
        suggested = load_last_dest()
        if suggested:
            dest_input = input(f"Destination folder [{suggested}]: ").strip()
            dest_path = dest_input if dest_input else suggested
        else:
            dest_path = input("Destination folder: ").strip()

        if not dest_path:
            print("No destination specified.")
            return
        if not Path(dest_path).exists():
            print(f"Destination does not exist: {dest_path}")
            return

        save_last_dest(dest_path)
        remaining_dates = sorted(by_date.keys(), reverse=True)
        copied_folders: list[Path] = []

        while remaining_dates:
            # Auto-select if only one date
            if len(remaining_dates) == 1:
                selected_date = remaining_dates[0]
                print(f"\nOne date remaining: {selected_date} ({len(by_date[selected_date])} files)")
            else:
                print(f"\nRemaining dates ({len(remaining_dates)}):")
                for i, date_key in enumerate(remaining_dates, 1):
                    print(f"  {i}. {date_key} ({len(by_date[date_key])} files)")

                choice = input(f"\nCopy from which date? [1-{len(remaining_dates)}, q=quit]: ").strip()
                if choice.lower() == 'q':
                    break

                try:
                    idx = int(choice) - 1
                    if not (0 <= idx < len(remaining_dates)):
                        print(f"Please enter 1-{len(remaining_dates)} or q")
                        continue
                except ValueError:
                    print(f"Please enter 1-{len(remaining_dates)} or q")
                    continue

                selected_date = remaining_dates[idx]

            files = by_date[selected_date]
            file_paths = [path for name, size, mtime, path in files]

            # Check for existing folder with matching date
            existing = find_matching_date_folder(dest_path, selected_date)
            if existing:
                use_existing = input(f"Found '{existing.name}'. Copy there? [y/n]: ").strip().lower()
                if use_existing == 'y':
                    dest_folder = existing
                else:
                    dest_folder = Path(dest_path) / selected_date.replace('-', '.')
            else:
                dest_folder = Path(dest_path) / selected_date.replace('-', '.')

            print(f"Copying {len(file_paths)} files to {dest_folder}...")
            copied, skipped = copy_files_to_folder(file_paths, dest_folder)
            print(f"Done ({copied} copied, {skipped} skipped)")
            copied_folders.append(dest_folder)

            remaining_dates.remove(selected_date)

        # Open destination folder in Finder
        if copied_folders:
            subprocess.run(['open', str(Path(dest_path))], capture_output=True)

        print("Done.")

    except KeyboardInterrupt:
        print("\nCancelled")


def compare_files(
    sd_files: dict,
    nas_files: dict,
    local_files: dict | None = None,
    time_tolerance: int = 10
) -> tuple[list, list, list]:
    """Compare SD files against NAS and local. Returns (backed_up_nas, backed_up_local, missing)."""
    backed_up_nas = []
    backed_up_local = []
    missing = []
    local_files = local_files or {}

    for key, (name, size, mtime, path) in sd_files.items():
        found = False
        # Check NAS first
        if key in nas_files:
            nas_size, nas_mtime, nas_folder = nas_files[key]
            if abs(mtime - nas_mtime) <= time_tolerance:
                backed_up_nas.append((name, size, mtime, path, nas_folder))
                found = True
        # Check local if not found on NAS
        if not found and key in local_files:
            local_size, local_mtime, local_folder = local_files[key]
            if abs(mtime - local_mtime) <= time_tolerance:
                backed_up_local.append((name, size, mtime, path, local_folder))
                found = True
        if not found:
            missing.append((name, size, mtime, path))

    return backed_up_nas, backed_up_local, missing


def main():
    parser = argparse.ArgumentParser(
        description='Check if SD card files are backed up to Synology NAS'
    )
    parser.add_argument('nas_paths', nargs='*', help='NAS folder(s) to search (defaults to SYNOLOGY_FOLDERS env)')
    parser.add_argument('sd_path', nargs='?', help='SD card path (optional, uses picker)')
    parser.add_argument('--volume', '-v', help='SD card path (alternative to positional)')
    parser.add_argument('--local', '-l', nargs='*', help='Local folder(s) to check (defaults to LOCAL_FOLDERS env)')
    parser.add_argument('--show-skipped', action='store_true', help='List skipped non-media files')
    parser.add_argument('--output', '-o', help='Write report to file')
    parser.add_argument('--open-missing', action='store_true', help='Open Finder with missing files for a selected date')

    args = parser.parse_args()

    # Load credentials (supports 1Password references like op://vault/item/field)
    load_dotenv()
    host = resolve_op_reference(os.getenv('SYNOLOGY_HOST'))
    port = int(os.getenv('SYNOLOGY_PORT', 5000))
    user = resolve_op_reference(os.getenv('SYNOLOGY_USER'))
    password = resolve_op_reference(os.getenv('SYNOLOGY_PASS'))
    secure = os.getenv('SYNOLOGY_SECURE', 'false').lower() == 'true'

    if not all([host, user, password]):
        print("❌ Error: Missing NAS credentials. Create .env file from config.example")
        sys.exit(1)

    # Get NAS paths from args or env
    nas_paths = args.nas_paths
    if not nas_paths:
        default_folders = os.getenv('SYNOLOGY_FOLDERS', '')
        nas_paths = default_folders.split() if default_folders else []
    if not nas_paths:
        print("❌ Error: No NAS folders specified. Use args or set SYNOLOGY_FOLDERS in .env")
        sys.exit(1)

    # Get local folders from args or env
    local_paths = args.local if args.local is not None else []
    if not local_paths:
        default_local = os.getenv('LOCAL_FOLDERS', '')
        local_paths = default_local.split() if default_local else []

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
    log(f"💾 Scanning SD card: {sd_path}")
    sd_files = scan_sd_card(sd_path)
    log(f"   Found {len(sd_files)} media files")

    if not sd_files:
        log("\n✅ No media files found on SD card. Nothing to check.")
        sys.exit(0)

    # Count skipped files if requested
    if args.show_skipped:
        sd_root = Path(sd_path)
        all_files = list(sd_root.rglob('*'))
        skipped = [f for f in all_files if f.is_file() and not is_media_file(f)]

    # Connect to NAS
    log(f"\n🔌 Connecting to NAS: {host}")
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

    # Scan NAS (with early exit when all SD files found with matching mtime)
    # Build dict of filename -> mtime for matching
    sd_file_mtimes = {name: data[2] for name, data in sd_files.items()}
    nas_files = {}
    scan_interrupted = False
    for nas_path in nas_paths:
        log(f"📂 Scanning {nas_path} (recursive)...")
        # Only look for files not yet matched
        remaining_mtimes = {k: v for k, v in sd_file_mtimes.items() if k not in nas_files or abs(nas_files[k][1] - v) > 10}
        folder_files, interrupted = scan_nas_folder(fs, nas_path, target_files=remaining_mtimes)
        nas_files.update(folder_files)
        if interrupted:
            scan_interrupted = True
            break
        # Check if all found with matching mtime
        all_matched = all(
            name in nas_files and abs(nas_files[name][1] - mtime) <= 10
            for name, mtime in sd_file_mtimes.items()
        )
        if all_matched:
            break
    if scan_interrupted:
        log(f"   ⚠️  Scan stopped early - results may be incomplete")
    log(f"   Found {len(nas_files)} matching files on NAS")

    # Scan local folders
    local_files = {}
    if local_paths:
        log(f"\n💻 Scanning local folders...")
        for local_path in local_paths:
            log(f"   📂 {local_path}")
            folder_files = scan_local_folder(local_path, verbose=False)
            local_files.update(folder_files)
        log(f"   Found {len(local_files)} media files locally")

    # Compare
    log("\n🔍 Comparing...")
    backed_up_nas, backed_up_local, missing = compare_files(sd_files, nas_files, local_files)

    total_backed = len(backed_up_nas) + len(backed_up_local)
    log(f"\n✅ {total_backed} files backed up")

    # Show matching folders on NAS
    if backed_up_nas:
        matching_folders = sorted(set(item[4] for item in backed_up_nas))
        log(f"\n📁 On NAS ({len(backed_up_nas)} files in {len(matching_folders)} folders):")
        for folder in matching_folders:
            count = sum(1 for item in backed_up_nas if item[4] == folder)
            log(f"   {folder} ({count} files)")

    # Show matching folders locally
    if backed_up_local:
        matching_folders = sorted(set(item[4] for item in backed_up_local))
        log(f"\n💻 On local ({len(backed_up_local)} files in {len(matching_folders)} folders):")
        for folder in matching_folders:
            count = sum(1 for item in backed_up_local if item[4] == folder)
            log(f"   {folder} ({count} files)")

    if missing:
        log(f"\n❌ {len(missing)} files missing:\n")
        # Group by date
        by_date: dict[str, list] = defaultdict(list)
        for name, size, mtime, path in missing:
            date_key = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
            by_date[date_key].append((name, size, mtime, path))

        sorted_dates = sorted(by_date.keys(), reverse=True)
        for date_key in sorted_dates:
            files = by_date[date_key]
            names = ", ".join(name for name, size, mtime, path in sorted(files, key=lambda x: x[0]))
            log(f"  {date_key} ({len(files)} files): {names}")
            log("")

        # Open Finder with missing files for selected date
        if args.open_missing:
            print("\nSelect a date to open in Finder:")
            for i, date_key in enumerate(sorted_dates, 1):
                print(f"  {i}. {date_key} ({len(by_date[date_key])} files)")

            try:
                choice = input(f"\nSelect date [1-{len(sorted_dates)}] or Enter to skip: ").strip()
                if choice:
                    idx = int(choice) - 1
                    if 0 <= idx < len(sorted_dates):
                        selected_date = sorted_dates[idx]
                        paths = [path for name, size, mtime, path in by_date[selected_date]]
                        print(f"\n📂 Opening {len(paths)} files from {selected_date} in Finder...")
                        open_finder_with_files(paths)
            except (ValueError, KeyboardInterrupt):
                pass

        # Prompt to copy missing files
        prompt_copy_missing(by_date)
    else:
        log("\n🎉 All files are backed up!")

    # Show skipped files
    if args.show_skipped and skipped:
        log(f"\n⏭️  Skipped {len(skipped)} non-media files:")
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
