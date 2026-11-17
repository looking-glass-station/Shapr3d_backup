"""
Export native .shapr packages from both active Projects and TempState workspaces.

Now keeps only the last N versions (default: 3) of each project per export folder.

Note: This only works for files that you've downloaded to desktop, if you created files on your iPad and haven't
synced them to your desktop you're out of luck.

- Creates a folder per exported .shapr file
- Extracts a JPG thumbnail from Shapr3D's resources and saves it alongside
- Two subdirectories in export dir:
    - "Current" -> active projects
    - "Trashed" -> TempState (previously deleted) projects
- Argparse flags:
    --export-dir (path)
    --include-tempstate (bool)
    --add-revision (bool) - Shapr3D keeps a version history for files, you might want to set this to false if you
    start running out of space.
- Skips export if target .shapr already exists. Will take a while to run the first time.
- Retention: after each export, the script prunes older .shapr files for the same project, keeping the most recent
  by revision ID (fallback to mtime) up to KEEP_VERSIONS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple, List
from zipfile import ZipFile, ZIP_DEFLATED

# =============================
# Defaults / policy
# =============================
DEFAULT_ADD_REVISION = True
DEFAULT_INCLUDE_TEMPSTATE = True
KEEP_VERSIONS = 3  # number of .shapr versions to retain per project folder


def sanitize(name: str) -> str:
    """Return a filesystem-safe version of a string by replacing path-breaking characters."""
    return "".join((c if c not in "/\\:" else "_") for c in name)


def ensure_dir(path: Path) -> None:
    """Ensure that a directory exists, creating parents if necessary."""
    path.mkdir(parents=True, exist_ok=True)


def extract_jpg_image(src: Path, dst: Path) -> bool:
    """
    Extract the first embedded JPEG image from a binary file.

    Returns:
        bool: True if a JPG was found and written; False otherwise.
    """
    try:
        jpg_start = b"\xff\xd8"
        jpg_end = b"\xff\xd9"
        data = src.read_bytes()
        start = data.find(jpg_start)
        if start == -1:
            return False
        end = data.find(jpg_end, start)
        if end == -1:
            return False
        end += len(jpg_end)
        dst.write_bytes(data[start:end])
        return True
    except Exception:
        return False


@dataclass
class ProjectMeta:
    """
    Metadata about a Shapr3D project retrieved from the database.

    Attributes:
        project_id (str): Unique identifier for the project.
        title (str): Human-readable title of the project.
        folder (str): Optional folder path associated with the project.
        revision_id (int): Revision number of the project.
        thumb_rel (Optional[str]): Relative path to thumbnail image in resources folder.
    """
    project_id: str
    title: str
    folder: str
    revision_id: int
    thumb_rel: Optional[str]


def open_db(db_path: Path) -> Optional[sqlite3.Connection]:
    """Attempt to open a SQLite database."""
    try:
        return sqlite3.connect(str(db_path))
    except Exception:
        return None


def _first_row(cur: sqlite3.Cursor, sql: str, params: Sequence[object]) -> Optional[Tuple]:
    """Execute a query safely and return the first row."""
    try:
        cur.execute(sql, params)
        return cur.fetchone()
    except Exception:
        return None


def _read_project_meta_from_connection(conn: sqlite3.Connection, project_id: str) -> Optional[ProjectMeta]:
    """
    Try common table/column spellings to fetch project metadata from an open SQLite connection.
    Returns ProjectMeta on success, or None if not found.
    """
    cur = conn.cursor()
    candidates = [
        ("Projects", "title", "folderPath", "revisionID", "thumbnailLight", "thumbnailDark"),
        ("projects", "title", "folderpath", "revisionid", "thumbnaillight", "thumbnaildark"),
    ]
    for table, c_title, c_folder, c_rev, c_thl, c_thd in candidates:
        row = _first_row(
            cur,
            f"SELECT IFNULL({c_title}, projectID), IFNULL({c_folder}, ''), IFNULL({c_rev}, 0), {c_thl}, {c_thd} "
            f"FROM {table} WHERE projectID = ?",
            (project_id,),
        )
        if row:
            title, folder, revision, th_light, th_dark = row
            thumb = th_dark or th_light
            title = (title or project_id).strip() or project_id
            folder = folder or ""
            revision = int(revision or 0)
            return ProjectMeta(project_id, title, folder, revision, thumb)
    return None


def read_project_meta(db_path: Path, project_id: str) -> ProjectMeta:
    """Read metadata for a project from the primary projectStorage.db, with safe defaults."""
    title = project_id
    folder = ""
    revision = 0
    thumb: Optional[str] = None
    conn = open_db(db_path)
    if conn is None:
        return ProjectMeta(project_id, title, folder, revision, thumb)
    try:
        found = _read_project_meta_from_connection(conn, project_id)
        if found:
            return found
    finally:
        conn.close()
    return ProjectMeta(project_id, title, folder, revision, thumb)


def read_project_meta_from_any_db(storage_dir: Path, project_id: str) -> ProjectMeta:
    """
    Try to read metadata from ANY .db in LocalState/storage (useful for TempState items that
    were removed from the main projectStorage.db).
    """
    # Try the canonical DB first
    ps = storage_dir / "projectStorage.db"
    if ps.exists():
        meta = read_project_meta(ps, project_id)
        if meta.title != project_id or meta.revision_id != 0 or meta.thumb_rel:
            return meta

    # Fall back to scanning all *.db files in storage
    for db in sorted(storage_dir.glob("*.db")):
        if db.name == "projectStorage.db":
            continue
        conn = open_db(db)
        if conn is None:
            continue
        try:
            found = _read_project_meta_from_connection(conn, project_id)
            if found:
                return found
        finally:
            conn.close()

    # Nothing found; return a default placeholder
    return ProjectMeta(project_id, project_id, "", 0, None)


def make_zip_name(base_dir: Path, title: str, folder: str, revision: int, add_revision: bool) -> Path:
    """Construct a path for the exported .shapr file."""
    folder_safe = sanitize(folder).strip(" _")
    title_safe = sanitize(title) or "untitled"
    out_dir = base_dir / (folder_safe if folder_safe else title_safe)
    ensure_dir(out_dir)
    name = title_safe
    if add_revision and revision > 0:
        name += f" [rev-{revision}]"
    return out_dir / f"{name}.shapr"


def build_metadata(project_id: str, revision: int) -> bytes:
    """Build JSON metadata for embedding inside the .shapr file."""
    obj = {"remoteID": project_id, "revisionID": int(revision or 0), "localChangeCount": 0}
    return json.dumps(obj, indent=2).encode("utf-8")


def write_shapr_zip(target: Path, workspace_path: Path, project_id: str, revision: int) -> None:
    """
    Write a .shapr ZIP file containing workspace, metadata, and an empty .export_log.

    The workspace is a SQLite DB in WAL mode; recent edits may live only in workspace-wal.
    Copying the raw file would lose them, so take a consistent snapshot with the SQLite
    backup API (read-only on the source) and zip that instead.
    """
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "workspace"
        src = sqlite3.connect(f"{workspace_path.as_uri()}?mode=ro", uri=True)
        dst = sqlite3.connect(str(snapshot))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        partial = target.with_suffix(target.suffix + ".partial")
        with ZipFile(partial, mode="w", compression=ZIP_DEFLATED) as zf:
            zf.writestr(".export_log", b"")
            zf.writestr(".metadata", build_metadata(project_id, revision))
            zf.write(snapshot, arcname="workspace")
        partial.replace(target)


def save_thumbnail_with_fallback(root_dir: Path, resources_dir: Path, thumb_rel: Optional[str], workspace_path: Path) -> None:
    """
    Save a thumbnail if available.

    Resources thumbnails are plain JPEG files (~10-120 KB), so copy them as-is. If there isn't one,
    fall back to carving the first JPEG out of the workspace, discarding tiny fragments that are
    almost certainly false matches on the JPEG markers.

    Args:
        root_dir (Path): Folder to save thumbnail into.
        resources_dir (Path): Base resources folder.
        thumb_rel (Optional[str]): Relative path to thumbnail resource.
        workspace_path (Path): Path to workspace file for fallback extraction.
    """
    dst = root_dir / "thumbnail.jpg"

    # Try resources first
    if thumb_rel:
        src = resources_dir / thumb_rel
        try:
            data = src.read_bytes()
            if data.startswith(b"\xff\xd8"):
                dst.write_bytes(data)
                return
        except OSError:
            pass

    # Fallback: extract from workspace
    if extract_jpg_image(workspace_path, dst):
        if dst.stat().st_size < 4 * 1024:
            dst.unlink(missing_ok=True)


# =============================
# Retention helpers
# =============================

_REV_RE = re.compile(r"\[rev-(\d+)\]", re.IGNORECASE)


def _read_remote_and_revision_from_shapr(path: Path) -> Tuple[Optional[str], Optional[int]]:
    """Return (remoteID, revisionID) stored in a .shapr zip; None if unreadable/missing."""
    try:
        with ZipFile(path, "r") as zf:
            with zf.open(".metadata") as mf:
                meta = json.loads(mf.read().decode("utf-8"))
                rid = meta.get("remoteID")
                rev = meta.get("revisionID")
                if isinstance(rev, str) and rev.isdigit():
                    rev = int(rev)
                if isinstance(rev, (int,)):
                    return rid, rev
                return rid, None
    except Exception:
        # Try to infer revision from filename
        m = _REV_RE.search(path.stem)
        rev = int(m.group(1)) if m else None
        return None, rev


def prune_project_versions(project_dir: Path, keep: int, project_id: str) -> None:
    """
    Keep only the newest `keep` .shapr files in `project_dir` belonging to `project_id`.

    Newness order:
      1) Higher revisionID from .metadata (preferred)
      2) Newer modification time as tie-breaker / fallback
    """
    if not project_dir.exists():
        return

    candidates: List[Tuple[Path, int, float]] = []
    for f in project_dir.glob("*.shapr"):
        remote, rev = _read_remote_and_revision_from_shapr(f)
        if remote is not None and remote != project_id:
            continue  # different project
        # For missing rev, use -1 so they're oldest
        rev_int = int(rev) if rev is not None else -1
        candidates.append((f, rev_int, f.stat().st_mtime))

    if len(candidates) <= keep:
        return

    # Sort by revision desc, then mtime desc
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    to_keep = {p for p, _, _ in candidates[:keep]}
    for p, _, _ in candidates:
        if p not in to_keep:
            try:
                p.unlink()
                print(f"Pruned old version: {p}")
            except Exception as e:
                print(f"Failed to prune {p}: {e}")


# =============================
# Discovery / export
# =============================

def _read_slot_ids(db_path: Path) -> dict:
    """Return {projectID: slotID} from projectStorage.db (empty dict if unavailable)."""
    conn = open_db(db_path)
    if conn is None:
        return {}
    try:
        return {pid: slot for pid, slot in conn.execute("SELECT projectID, slotID FROM Projects") if slot}
    except Exception:
        return {}
    finally:
        conn.close()


def iter_active_workspaces(projects_root: Path, db_path: Path) -> Iterator[Tuple[str, Path]]:
    """
    Yield project IDs and workspace paths for active projects.

    Current layout: LocalState/projects/<projectID>/<slotID>/workspace, where slotID comes from
    Projects.slotID. Older layout used a fixed "project" subfolder. If the DB doesn't name a slot,
    fall back to the most recently modified subfolder that contains a workspace.
    Offloaded (cloud-only) projects have an empty folder and are skipped.
    """
    projects_dir = projects_root / "LocalState" / "projects"
    if not projects_dir.exists():
        return
    slot_ids = _read_slot_ids(db_path)
    for pid_dir in projects_dir.iterdir():
        if not pid_dir.is_dir():
            continue
        slot = slot_ids.get(pid_dir.name)
        ws = pid_dir / slot / "workspace" if slot else None
        if ws is None or not ws.is_file():
            found = [p for p in pid_dir.glob("*/workspace") if p.is_file()]
            ws = max(found, key=lambda p: p.stat().st_mtime) if found else None
        if ws is not None:
            yield (pid_dir.name, ws)


def iter_tempstate_workspaces(tempstate_root: Path) -> Iterator[Tuple[str, Path]]:
    """Yield GUIDs and workspace paths for TempState projects."""
    if not tempstate_root.exists():
        return
    for sub in tempstate_root.iterdir():
        if not sub.is_dir():
            continue
        ws = sub / "workspace"
        if ws.exists() and ws.is_file():
            yield (sub.name, ws)


def export_active_projects(packages_root: Path, base_export_dir: Path, add_revision: bool) -> None:
    """
    Export all active projects into subdirectory 'Current'.
    Also enforce retention for each project's folder after export.
    """
    storage_dir = packages_root / "LocalState" / "storage"
    db_path = storage_dir / "projectStorage.db"
    resources_dir = storage_dir / "resources"
    export_dir = base_export_dir / "Current"
    ensure_dir(export_dir)

    for project_id, ws_path in iter_active_workspaces(packages_root, db_path):
        meta = read_project_meta(db_path, project_id)
        target = make_zip_name(export_dir, meta.title, meta.folder, meta.revision_id, add_revision)
        if target.exists():
            print(f"Skip (exists): {target}")
        else:
            print(f"Exporting: {target}")
            write_shapr_zip(target, ws_path, meta.project_id, meta.revision_id)
            save_thumbnail_with_fallback(target.parent, resources_dir, meta.thumb_rel, ws_path)

        # Retention: always prune this project's folder
        prune_project_versions(target.parent, KEEP_VERSIONS, meta.project_id)


def export_tempstate_projects(packages_root: Path, base_export_dir: Path, add_revision: bool) -> None:
    """
    Export all TempState (trashed) projects into subdirectory 'Trashed'.

    - Title/thumbnail are recovered by scanning *all* storage DBs.
    - If still no title is found, fallback to naming Temp_<GUID>.
    - Enforce retention per project folder after export.
    """
    temp_root = packages_root / "TempState"
    if not temp_root.exists():
        return
    export_dir = base_export_dir / "Trashed"
    ensure_dir(export_dir)

    storage_dir = packages_root / "LocalState" / "storage"
    resources_dir = storage_dir / "resources"

    for guid, ws_path in iter_tempstate_workspaces(temp_root):
        # Scan all DBs for a matching projectID
        meta = read_project_meta_from_any_db(storage_dir, guid)

        # Use title if we got one, otherwise fallback to Temp_GUID
        title = meta.title if meta.title and meta.title != guid else f"Temp_{guid}"
        folder = meta.folder
        revision = meta.revision_id

        target = make_zip_name(export_dir, title, folder, revision if add_revision else 0, add_revision)
        if target.exists():
            print(f"Skip (exists): {target}")
        else:
            print(f"Exporting (TempState): {target}")
            write_shapr_zip(target, ws_path, guid, revision)
            save_thumbnail_with_fallback(target.parent, resources_dir, meta.thumb_rel, ws_path)

        # Retention for this project's folder, keyed by original guid (remoteID)
        prune_project_versions(target.parent, KEEP_VERSIONS, guid)


# =============================
# Entrypoint / args
# =============================

def find_packages_root() -> Path:
    """Locate the Shapr3D package directory for the current user."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        root_folder = Path(local_appdata) / "Packages"
    else:
        root_folder = Path.home() / "AppData" / "Local" / "Packages"
    # Package folders sit directly under Packages; rglob would walk every app's data tree.
    matches = [p for p in root_folder.glob("Shapr3D.Shapr3D*") if p.is_dir()]
    if not matches:
        raise FileNotFoundError("Shapr3D package folder not found.")

    print(f"Found Shapr3D installation directory: {matches[0]}")
    return matches[0]


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(description="Export Shapr3D .shapr from active and TempState workspaces.")
    p.add_argument("--export-dir", type=str, required=True, help="Destination directory for exports.")
    # BooleanOptionalAction gives --flag / --no-flag; store_true with default=True could never be turned off.
    p.add_argument("--include-tempstate", action=argparse.BooleanOptionalAction, default=DEFAULT_INCLUDE_TEMPSTATE,
                   help="Include TempState (trashed) projects in the export.")
    p.add_argument("--add-revision", action=argparse.BooleanOptionalAction, default=DEFAULT_ADD_REVISION,
                   help="Append [rev-X] to exported filenames when available.")
    return p.parse_args()


def main() -> None:
    """
    Main entry point for the script:
      - Parses CLI arguments.
      - Finds Shapr3D package root.
      - Exports current and (optionally) trashed projects.
      - After each export, prunes old .shapr files for that project, keeping only the newest KEEP_VERSIONS.
    """
    args = parse_args()
    export_dir = Path(args.export_dir).expanduser().resolve()
    ensure_dir(export_dir)
    packages_root = find_packages_root()
    export_active_projects(packages_root, export_dir, add_revision=args.add_revision)

    if args.include_tempstate:
        export_tempstate_projects(packages_root, export_dir, add_revision=args.add_revision)

    print("Done.")


if __name__ == "__main__":
    main()
