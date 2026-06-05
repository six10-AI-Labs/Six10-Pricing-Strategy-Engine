"""
drive_sync.py — Sync data feed files from Google Drive to local Data Feeds folder.

Called at pipeline startup so the engine always reads the latest files from Drive
rather than relying on a manual sync of the local folder.

Strategy:
  - Sellerise XLSX:   download any file NOT already present locally (immutable; new
                      files are added weekly by Amber — never overwrite existing ones)
  - FBA Inventory CSV: same approach (each snapshot is a distinct dated file)
  - COGS Sheet:       always re-export as XLSX (the sheet can be edited at any time)

Requires the same service-account credentials used by sheets_writer.py.
"""

import io
import logging
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
_XLSX_EXPORT_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

# Characters illegal in Windows filenames
_WIN_ILLEGAL = str.maketrans({
    "\t": " ", "/": "-", "\\": "-", ":": ".", "*": "_",
    "?": "_", '"': "_", "<": "_", ">": "_", "|": "_",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """Return a Windows-safe version of a Drive filename.

    Drive allows characters that Windows forbids (tabs, slashes, colons).
    Sanitised names are used for both local storage and existence checks so
    repeated runs produce the same path and the skip-if-present logic works.
    """
    return name.translate(_WIN_ILLEGAL).strip()


def _drive_newer(drive_file: dict, local_path: Path) -> bool:
    """Return True if the Drive file was modified more recently than the local copy.

    Used so that files updated in-place on Drive (same filename, new content)
    are re-downloaded rather than silently skipped by the name-exists check.
    """
    if not local_path.exists():
        return False  # file is absent — caller handles the download
    drive_mtime_str = drive_file.get("modifiedTime", "")
    if not drive_mtime_str:
        return False  # no metadata → be conservative and keep local copy
    try:
        drive_mtime = datetime.fromisoformat(drive_mtime_str.replace("Z", "+00:00"))
        local_mtime = datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc)
        return drive_mtime > local_mtime
    except Exception:
        return False


def _build_service(creds_path: str):
    creds = Credentials.from_service_account_file(creds_path, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds)


def _list_folder(service, folder_id: str) -> list:
    """Return list of file metadata dicts for all non-trashed items in folder.

    Includes modifiedTime so callers can re-download files that have been
    updated in Drive since the local copy was written.
    """
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType,modifiedTime)",
        pageSize=200,
    ).execute()
    return results.get("files", [])


def _download(service, file_id: str, dest: Path, export_mime: str = None) -> bool:
    """Download a Drive file to *dest*.  If *export_mime* is given, exports a
    Google Workspace document (e.g. Sheets → XLSX) instead of raw download.
    Returns True on success, False on failure.
    """
    try:
        if export_mime:
            request = service.files().export_media(
                fileId=file_id, mimeType=export_mime
            )
        else:
            request = service.files().get_media(fileId=file_id)

        dest.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        dest.write_bytes(buf.getvalue())
        logger.debug("Downloaded %s → %s", file_id, dest)
        return True
    except Exception as exc:
        logger.error("Drive download failed for %s: %s", dest.name, exc)
        return False


# ---------------------------------------------------------------------------
# Per-source sync functions
# ---------------------------------------------------------------------------

def _sync_sellerise(service, config: dict, base_dir: Path) -> int:
    """Download any Sellerise XLSX files missing from local brand folders.

    Drive folder IDs come from config['drive']['sellerise_brand_folders']
    which maps brand_key → Drive folder ID.  The local target directory mirrors
    the structure expected by data_loader.load_sellerise_brand():
        <base_dir>/SellerRise Sales Data/<brand_folder_name>/
    """
    drive_brand_ids: dict = (
        config.get("drive", {}).get("sellerise_brand_folders", {})
    )
    local_folder_names: dict = config.get("data", {}).get("brand_folders", {})

    downloaded = 0
    for brand_key, drive_folder_id in drive_brand_ids.items():
        local_name = local_folder_names.get(brand_key, brand_key)
        local_dir = base_dir / "SellerRise Sales Data" / local_name
        local_dir.mkdir(parents=True, exist_ok=True)

        existing = {p.name for p in local_dir.glob("*.xlsx")}
        remote_files = _list_folder(service, drive_folder_id)

        for f in remote_files:
            if not f["name"].lower().endswith(".xlsx"):
                continue
            safe = _safe_name(f["name"])
            local_path = local_dir / safe
            if safe in existing and not _drive_newer(f, local_path):
                continue  # already present and not updated — skip
            reason = "updated in Drive" if safe in existing else "new file"
            logger.info(
                "Drive sync [Sellerise] %s / %s — downloading (%s)...",
                brand_key, safe, reason,
            )
            if _download(service, f["id"], local_dir / safe):
                downloaded += 1

    return downloaded


def _sync_fba_inventory(service, config: dict, base_dir: Path) -> int:
    """Download any FBA Inventory CSV files missing from local brand folders.

    Drive folder IDs come from config['drive']['fba_brand_folders'] which maps
    the Drive folder display name (e.g. "AquaDoc") → Drive folder ID.
    The local directory is: <base_dir>/Amazon FBA Inventory report/<folder_name>/
    """
    drive_brand_ids: dict = (
        config.get("drive", {}).get("fba_brand_folders", {})
    )

    downloaded = 0
    for folder_name, drive_folder_id in drive_brand_ids.items():
        local_dir = base_dir / "Amazon FBA Inventory report" / folder_name
        local_dir.mkdir(parents=True, exist_ok=True)

        existing = {p.name for p in local_dir.glob("*.csv")}
        remote_files = _list_folder(service, drive_folder_id)

        for f in remote_files:
            if not f["name"].lower().endswith(".csv"):
                continue
            safe = _safe_name(f["name"])
            local_path = local_dir / safe
            if safe in existing and not _drive_newer(f, local_path):
                continue
            reason = "updated in Drive" if safe in existing else "new file"
            logger.info(
                "Drive sync [FBA] %s / %s — downloading (%s)...",
                folder_name, safe, reason,
            )
            if _download(service, f["id"], local_dir / safe):
                downloaded += 1

    return downloaded


def _sync_folder_brand_files(
    service, config: dict, base_dir: Path,
    config_key: str, local_parent: str,
    extensions: tuple = (".xlsx", ".csv"),
    label: str = "",
) -> int:
    """Generic helper: download missing files from per-brand Drive folders.

    config['drive'][config_key] maps folder_name → Drive folder ID.
    Files are saved to: <base_dir>/<local_parent>/<folder_name>/
    Only files with an extension in *extensions* are considered.
    Already-present files (by sanitised name) are skipped.
    """
    drive_brand_ids: dict = config.get("drive", {}).get(config_key, {})
    if not drive_brand_ids:
        return 0

    downloaded = 0
    for folder_name, drive_folder_id in drive_brand_ids.items():
        local_dir = base_dir / local_parent / folder_name
        local_dir.mkdir(parents=True, exist_ok=True)

        existing = set()
        for ext in extensions:
            existing.update(p.name for p in local_dir.glob(f"*{ext}"))

        remote_files = _list_folder(service, drive_folder_id)
        for f in remote_files:
            name_lower = f["name"].lower()
            if not any(name_lower.endswith(ext) for ext in extensions):
                continue
            safe = _safe_name(f["name"])
            local_path = local_dir / safe
            if safe in existing and not _drive_newer(f, local_path):
                continue
            reason = "updated in Drive" if safe in existing else "new file"
            logger.info(
                "Drive sync [%s] %s / %s — downloading (%s)...",
                label or config_key, folder_name, safe, reason,
            )
            if _download(service, f["id"], local_dir / safe):
                downloaded += 1

    return downloaded


def _sync_helium10(service, config: dict, base_dir: Path) -> int:
    """Download Helium10 files: root CSVs + per-brand XLSX sub-folders.

    Root CSVs go to: <base_dir>/Helium10/
    Brand XLSXs go to: <base_dir>/Helium10/<brand>/
    """
    drive_cfg = config.get("drive", {})
    downloaded = 0

    # Root CSV folder
    csv_folder_id = drive_cfg.get("helium10_csv_folder")
    if csv_folder_id:
        local_dir = base_dir / "Helium10"
        local_dir.mkdir(parents=True, exist_ok=True)
        existing = {p.name for p in local_dir.glob("*.csv")}
        for f in _list_folder(service, csv_folder_id):
            if not f["name"].lower().endswith(".csv"):
                continue
            safe = _safe_name(f["name"])
            local_path = local_dir / safe
            if safe in existing and not _drive_newer(f, local_path):
                continue
            reason = "updated in Drive" if safe in existing else "new file"
            logger.info("Drive sync [Helium10] root / %s — downloading (%s)...", safe, reason)
            if _download(service, f["id"], local_dir / safe):
                downloaded += 1

    # Per-brand XLSX folders
    brand_folders: dict = drive_cfg.get("helium10_brand_folders", {})
    for brand_name, folder_id in brand_folders.items():
        local_dir = base_dir / "Helium10" / brand_name
        local_dir.mkdir(parents=True, exist_ok=True)
        existing = {p.name for p in local_dir.glob("*.xlsx")}
        for f in _list_folder(service, folder_id):
            if not f["name"].lower().endswith(".xlsx"):
                continue
            safe = _safe_name(f["name"])
            local_path = local_dir / safe
            if safe in existing and not _drive_newer(f, local_path):
                continue
            reason = "updated in Drive" if safe in existing else "new file"
            logger.info("Drive sync [Helium10] %s / %s — downloading (%s)...", brand_name, safe, reason)
            if _download(service, f["id"], local_dir / safe):
                downloaded += 1

    return downloaded


def _sync_cogs_sheet(service, config: dict, base_dir: Path) -> bool:
    """Export the COGS Google Sheet as XLSX, overwriting the local copy.

    Always re-exports so that any edits to the live sheet (e.g. adding a new ASIN
    cost) are picked up without a manual download step.
    """
    sheet_id = config.get("drive", {}).get("cogs_sheet_id")
    if not sheet_id:
        return False

    dest = base_dir / "COGS Sheet.xlsx"
    logger.info("Drive sync [COGS Sheet] exporting from Google Sheets...")
    ok = _download(service, sheet_id, dest, export_mime=_XLSX_EXPORT_MIME)
    if ok:
        logger.info("Drive sync [COGS Sheet] exported successfully")
    return ok


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def sync_data_feeds(config: dict) -> None:
    """Sync all configured Drive sources to the local Data Feeds folder.

    Safe to call on every run:
      - Sellerise / FBA files are only downloaded if absent locally.
      - COGS Sheet is always re-exported (cheap, ensures latest costs).
      - Any download failure is logged but does NOT abort the pipeline.

    Called from main.py before build_master_dataset() so data_loader.py
    reads the latest files without any change to its own logic.
    """
    drive_cfg = config.get("drive", {})
    if not drive_cfg:
        logger.debug("No 'drive' section in config — skipping Drive sync")
        return

    creds_path: str = config.get("google_sheets", {}).get(
        "credentials_path", "pricing-strategy-engine-4983eab4d919.json"
    )
    if not Path(creds_path).exists():
        logger.warning(
            "Drive sync skipped: service account credentials not found at '%s'",
            creds_path,
        )
        return

    base_dir = Path(config["data"]["base_dir"])

    try:
        service = _build_service(creds_path)
    except Exception as exc:
        logger.error("Drive sync: failed to build Drive service — %s", exc)
        return

    logger.info("Drive sync starting...")

    # 1. Sellerise (most critical — new files added weekly)
    n_sel = _sync_sellerise(service, config, base_dir)
    logger.info(
        "Drive sync [Sellerise] done — %d new file(s) downloaded", n_sel,
    )

    # 2. FBA Inventory snapshots
    n_fba = _sync_fba_inventory(service, config, base_dir)
    logger.info(
        "Drive sync [FBA Inventory] done — %d new file(s) downloaded", n_fba,
    )

    # 3. Fee Preview reports (new files only)
    n_fee = _sync_folder_brand_files(
        service, config, base_dir,
        config_key="fee_preview_brand_folders",
        local_parent="Amazon Fee Preview",
        extensions=(".xlsx", ".csv"),
        label="Fee Preview",
    )
    logger.info("Drive sync [Fee Preview] done — %d new file(s) downloaded", n_fee)

    # 4. Storage Fees reports (new files only)
    n_storage = _sync_folder_brand_files(
        service, config, base_dir,
        config_key="storage_fees_brand_folders",
        local_parent="Amazon Monthly Storage Fees Report",
        extensions=(".xlsx", ".csv"),
        label="Storage Fees",
    )
    logger.info("Drive sync [Storage Fees] done — %d new file(s) downloaded", n_storage)

    # 5. Return Reports (new files only)
    n_returns = _sync_folder_brand_files(
        service, config, base_dir,
        config_key="return_report_brand_folders",
        local_parent="Return Reports",
        extensions=(".xlsx", ".csv"),
        label="Return Reports",
    )
    logger.info("Drive sync [Return Reports] done — %d new file(s) downloaded", n_returns)

    # 6. Helium10 (root CSVs + brand XLSXs)
    n_h10 = _sync_helium10(service, config, base_dir)
    logger.info("Drive sync [Helium10] done — %d new file(s) downloaded", n_h10)

    # 7. COGS Sheet (always refresh — edits picked up on every run)
    _sync_cogs_sheet(service, config, base_dir)

    total_new = n_sel + n_fba + n_fee + n_storage + n_returns + n_h10
    logger.info("Drive sync complete — %d total new file(s) downloaded", total_new)
