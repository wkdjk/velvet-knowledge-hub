# ingest_from_drive.py — Drive folder wrapper for all VKH ingest scripts.
#
# Downloads files from a Google Drive folder and dispatches to the correct
# ingest script. Each folder maps to a specific ingest_*.py script.
#
# Usage:
#   PYTHONPATH=. python scripts/ingest_from_drive.py --folder FOLDER_NAME [--dry-run]
#
# FOLDER_NAME: qia | nz | mfds_price | mfds_records | kstat | all
#
# L-1:  PYTHONPATH=. ensures repo root is importable.
# L-2:  .env must be at repo root (velvet-knowledge-hub/.env).
# L-3:  GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
#
# Security: no credentials in this file. All secrets from environment only.

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

REPO_ROOT = Path(__file__).resolve().parent.parent

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# ---------------------------------------------------------------------------
# Folder → script mapping.
#
# Each entry: folder_id, primary_script, file_arg_flag.
# file_arg_flag = None means the script takes no file argument (mfds_annual).
# file_arg_flag = "--historical" or "--mfds" for vfi_records (auto-detected below).
#
# mfds_price folder also dispatches ingest_vfi_price.py — handled in _extra_scripts.
# ---------------------------------------------------------------------------

FOLDER_MAP: dict[str, tuple[str, str, str | None]] = {
    "qia":          ("1L2z3xYlpvkHzlO4JrpYfJFpGltzUfa58", "scripts/ingest_qia.py",          "--file"),
    "nz":           ("10jlxeYND28jbiI49XXi3H3NOnbttOK0Q", "scripts/ingest_nz_export.py",     "--file"),
    "mfds_price":   ("1UUf55PlJjbCPzVxpcHSERnjC5Ah0Za3g", "scripts/ingest_mfds_annual.py",   None),
    "mfds_records": ("1xZI1MFMMVS09OUdXSvsuYjYQwi2OPXCg", "scripts/ingest_vfi_records.py",  "--auto"),
    "kstat":        ("1ebc4WfgBh-egMQOTX0fujdH6EJpBq-BI", "scripts/ingest_kstat.py",         "--file"),
}

# Extra scripts dispatched for a folder in addition to the primary script.
# Each entry: (script_path, file_arg_flag)
_EXTRA_SCRIPTS: dict[str, list[tuple[str, str]]] = {
    "mfds_price": [("scripts/ingest_vfi_price.py", "--file")],
}

# Files to skip regardless of folder.
_SKIP_NAMES = {"HOW_TO_UPDATE.txt"}

# File suffixes to skip regardless of folder (e.g. PDF reference files in mfds_price).
_SKIP_SUFFIXES = frozenset({".pdf"})

# Sleep between folder dispatches when --folder all is used.
# Sheets API quota: 60 read requests per minute per user.
# Each folder runs ~10 scripts × 3 reads = 30 reads in ~20s.
# 65s sleep guarantees the quota window fully resets before the next folder.
_INTER_FOLDER_SLEEP = 65


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _build_drive_service():
    """Load service account from env and return a Drive v3 service object."""
    load_dotenv(REPO_ROOT / ".env")

    sa_json_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_raw:
        print(
            "ERROR: GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.\n"
            "  Local dev: add it to .env at the repo root (single-line JSON — L-3).\n"
            "  GitHub Actions: add it to repository Secrets.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        sa_info = json.loads(sa_json_raw)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON — {exc}\n"
            "  Tip: minify to one line with:\n"
            '  python -c "import json,sys; print(json.dumps(json.load(sys.stdin), '
            'separators=(\',\',\':\')))" < key.json',
            file=sys.stderr,
        )
        sys.exit(1)

    creds = Credentials.from_service_account_info(sa_info, scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def _list_files(drive, folder_id: str) -> list[dict]:
    """Return list of {id, name, mimeType} dicts for non-trashed files in folder."""
    try:
        result = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,mimeType)",
        ).execute()
    except Exception as exc:
        print(f"ERROR: Could not list files in folder {folder_id} — {exc}", file=sys.stderr)
        print(
            "  Check: (1) Drive API is enabled on the GCP project, "
            "(2) service account has access to the folder.",
            file=sys.stderr,
        )
        return []
    return result.get("files", [])


def _download_file(drive, file_id: str, dest_path: Path) -> bool:
    """Download a Drive file to dest_path. Returns True on success."""
    try:
        request = drive.files().get_media(fileId=file_id)
        with dest_path.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    except Exception as exc:
        print(f"  WARNING: Failed to download {dest_path.name} — {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# File-arg resolution for ingest_vfi_records.py (--auto flag)
# ---------------------------------------------------------------------------

def _resolve_vfi_records_flag(filename: str) -> str:
    """
    ingest_vfi_records.py accepts --historical (Excel archive) or --mfds
    (MFDS portal download). Detect by filename pattern.
    MFDS portal files match: 수입식품조회YYYYMMDD.xlsx
    Everything else is treated as --historical.
    """
    if filename.startswith("수입식품조회") and filename.endswith(".xlsx"):
        return "--mfds"
    return "--historical"


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

def _build_cmd(script: str, file_flag: str | None, file_path: Path, dry_run: bool) -> list[str]:
    """Build the subprocess argv for a single ingest dispatch."""
    cmd = [sys.executable, str(REPO_ROOT / script)]
    if file_flag is not None:
        cmd += [file_flag, str(file_path)]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def _run_cmd(cmd: list[str]) -> bool:
    """Run command; print stderr on failure. Returns True on success."""
    result = subprocess.run(cmd, cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)})
    if result.returncode != 0:
        print(f"  ERROR: script exited with code {result.returncode}: {' '.join(cmd)}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Core: process one folder
# ---------------------------------------------------------------------------

def _process_folder(folder_key: str, dry_run: bool) -> tuple[int, int, int]:
    """
    Download all files from a Drive folder and dispatch ingest scripts.

    Returns (files_downloaded, scripts_run, errors).
    """
    folder_id, primary_script, file_flag = FOLDER_MAP[folder_key]
    extra_scripts = _EXTRA_SCRIPTS.get(folder_key, [])

    print(f"\n--- Folder: {folder_key} (Drive ID: {folder_id}) ---")
    print(f"    Primary script: {primary_script}")
    if extra_scripts:
        print(f"    Extra scripts:  {[s for s, _ in extra_scripts]}")

    drive = _build_drive_service()
    files = _list_files(drive, folder_id)

    if not files:
        print("  No files found (or folder inaccessible).")
        return 0, 0, 0

    # Filter skipped files (by name or suffix).
    files = [
        f for f in files
        if f["name"] not in _SKIP_NAMES
        and Path(f["name"]).suffix.lower() not in _SKIP_SUFFIXES
    ]
    print(f"  Found {len(files)} file(s) to process:")
    for f in files:
        print(f"    {f['name']}")

    # Special case: primary script takes no file argument (mfds_annual).
    # Dispatch it exactly once, then still download files for extra scripts.
    if file_flag is None:
        # Download files for extra scripts (if any), dispatch primary once.
        tmpdir = Path(tempfile.mkdtemp(prefix="vkh_drive_"))
        try:
            downloaded = 0
            errors = 0
            scripts_run = 0

            # Dispatch primary script once (no file arg).
            cmd = _build_cmd(primary_script, None, Path("/dev/null"), dry_run)
            if dry_run:
                print(f"  [DRY-RUN] Would run: {' '.join(cmd)}")
                scripts_run += 1
            else:
                print(f"  Running: {' '.join(cmd)}")
                if _run_cmd(cmd):
                    scripts_run += 1
                else:
                    errors += 1

            # Download files and dispatch extra scripts.
            for file_meta in files:
                dest = tmpdir / file_meta["name"]
                if dry_run:
                    print(f"  [DRY-RUN] Would download: {file_meta['name']}")
                    downloaded += 1
                else:
                    print(f"  Downloading: {file_meta['name']}")
                    if not _download_file(drive, file_meta["id"], dest):
                        errors += 1
                        continue
                    downloaded += 1

                for extra_script, extra_flag in extra_scripts:
                    if extra_flag == "--auto":
                        extra_flag = _resolve_vfi_records_flag(file_meta["name"])
                    cmd = _build_cmd(extra_script, extra_flag, dest, dry_run)
                    if dry_run:
                        print(f"  [DRY-RUN] Would run: {' '.join(cmd)}")
                        scripts_run += 1
                    else:
                        print(f"  Running: {' '.join(cmd)}")
                        if _run_cmd(cmd):
                            scripts_run += 1
                        else:
                            errors += 1

            return downloaded, scripts_run, errors
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # Normal case: download each file, dispatch primary + extra scripts.
    tmpdir = Path(tempfile.mkdtemp(prefix="vkh_drive_"))
    try:
        downloaded = 0
        errors = 0
        scripts_run = 0

        for file_meta in files:
            dest = tmpdir / file_meta["name"]

            if dry_run:
                print(f"  [DRY-RUN] Would download: {file_meta['name']}")
                downloaded += 1
            else:
                print(f"  Downloading: {file_meta['name']}")
                if not _download_file(drive, file_meta["id"], dest):
                    errors += 1
                    continue
                downloaded += 1

            # Resolve --auto flag for vfi_records.
            resolved_flag = file_flag
            if file_flag == "--auto":
                resolved_flag = _resolve_vfi_records_flag(file_meta["name"])

            # Archive files (e.g. historical Excel) resolve to --historical, which
            # expects a directory — not a single file. Skip them: they stay in Drive
            # for reference but are not re-ingested once already loaded.
            if file_flag == "--auto" and resolved_flag == "--historical":
                print(f"  Skipping archive file (kept in Drive, not re-ingested): {file_meta['name']}")
                continue

            # Primary script dispatch.
            cmd = _build_cmd(primary_script, resolved_flag, dest, dry_run)
            if dry_run:
                print(f"  [DRY-RUN] Would run: {' '.join(cmd)}")
                scripts_run += 1
            else:
                print(f"  Running: {' '.join(cmd)}")
                if _run_cmd(cmd):
                    scripts_run += 1
                else:
                    errors += 1

            # Extra scripts (mfds_price: also run ingest_vfi_price.py).
            for extra_script, extra_flag in extra_scripts:
                if extra_flag == "--auto":
                    extra_flag = _resolve_vfi_records_flag(file_meta["name"])
                cmd = _build_cmd(extra_script, extra_flag, dest, dry_run)
                if dry_run:
                    print(f"  [DRY-RUN] Would run: {' '.join(cmd)}")
                    scripts_run += 1
                else:
                    print(f"  Running: {' '.join(cmd)}")
                    if _run_cmd(cmd):
                        scripts_run += 1
                    else:
                        errors += 1

        return downloaded, scripts_run, errors
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download files from a Drive folder and dispatch VKH ingest scripts."
    )
    parser.add_argument(
        "--folder",
        required=True,
        choices=[*FOLDER_MAP.keys(), "all"],
        help="Drive folder to process (qia | nz | mfds_price | mfds_records | kstat | all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be downloaded and which commands would run. No changes made.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("[DRY-RUN MODE] No files will be downloaded, no scripts will be executed.")

    folders = list(FOLDER_MAP.keys()) if args.folder == "all" else [args.folder]

    total_downloaded = 0
    total_scripts = 0
    total_errors = 0

    for i, folder_key in enumerate(folders):
        dl, sr, err = _process_folder(folder_key, args.dry_run)
        total_downloaded += dl
        total_scripts += sr
        total_errors += err
        if not args.dry_run and i < len(folders) - 1:
            print(f"\n  Sleeping {_INTER_FOLDER_SLEEP}s to stay within Sheets API quota...")
            time.sleep(_INTER_FOLDER_SLEEP)

    print(
        f"\n=== Summary: {total_downloaded} file(s) downloaded, "
        f"{total_scripts} script(s) run, {total_errors} error(s) ==="
    )
    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
