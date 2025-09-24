"""
Export native .shapr packages from both active Projects and TempState workspaces.

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
"""

from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple
from zipfile import ZipFile, ZIP_DEFLATED

DEFAULT_ADD_REVISION = True
DEFAULT_INCLUDE_TEMPSTATE = False


def sanitize(name: str) -> str:
    """
    Return a filesystem-safe version of a string by replacing path-breaking characters.

    Args:
        name (str): The string to sanitize.

    Returns:
        str: A safe string for use as a folder or file name.
    """
    return "".join((c if c not in "/\\:" else "_") for c in name)


def ensure_dir(path: Path) -> None:
    """
    Ensure that a directory exists, creating parents if necessary.

    Args:
        path (Path): Directory path to create.
    """
    path.mkdir(parents=True, exist_ok=True)


def extract_jpg_image(src: Path, dst: Path) -> None:
    """
    Extract the first embedded JPEG image from a binary file.

    Args:
        src (Path): Source binary file containing embedded JPG data.
        dst (Path): Destination file path where the extracted JPG will be written.
    """
    jpg_start = b"\xff\xd8"
    jpg_end = b"\xff\xd9"
    data = src.read_bytes()
    start = data.find(jpg_start)
    if start == -1:
        return
    end = data.find(jpg_end, start)
    if end == -1:
        return
    end += len(jpg_end)
    dst.write_bytes(data[start:end])


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
    """
    Attempt to open a SQLite database.

    Args:
        db_path (Path): Path to the SQLite database.

    Returns:
        sqlite3.Connection | None: Connection object if successful, else None.
    """
    try:
        return sqlite3.connect(str(db_path))
    except Exception:
        return None


def _first_row(cur: sqlite3.Cursor, sql: str, params: Sequence[object]) -> Optional[Tuple]:
    """
    Execute a query safely and return the first row.

    Args:
        cur (sqlite3.Cursor): Database cursor.
        sql (str): SQL query to execute.
        params (Sequence[object]): Query parameters.

    Returns:
        tuple | None: First row result or None if no row was returned or query failed.
    """
    try:
        cur.execute(sql, params)
        return cur.fetchone()
    except Exception:
        return None


def read_project_meta(db_path: Path, project_id: str) -> ProjectMeta:
    """
    Read metadata for a project from the Shapr3D projectStorage.db.

    Args:
        db_path (Path): Path to the projectStorage.db SQLite file.
        project_id (str): Project ID to look up.

    Returns:
        ProjectMeta: Metadata object containing title, folder, revision, and thumbnail info.
    """
    title = project_id
    folder = ""
    revision = 0
    thumb: Optional[str] = None
    conn = open_db(db_path)
    if conn is None:
        return ProjectMeta(project_id, title, folder, revision, thumb)
    try:
        cur = conn.cursor()
        candidates = [
            ("Projects", "title", "folderPath", "revisionID", "thumbnailLight", "thumbnailDark"),
            ("projects", "title", "folderpath", "revisionid", "thumbnaillight", "thumbnaildark"),
        ]
        row = None
        for table, c_title, c_folder, c_rev, c_thl, c_thd in candidates:
            row = _first_row(
                cur,
                f"SELECT IFNULL({c_title}, projectID), IFNULL({c_folder}, ''), IFNULL({c_rev}, 0), {c_thl}, {c_thd} "
                f"FROM {table} WHERE projectID = ?",
                (project_id,),
            )
            if row:
                break
        if row:
            title, folder, revision, th_light, th_dark = row
            thumb = th_dark or th_light
            title = (title or project_id).strip() or project_id
            folder = folder or ""
            revision = int(revision or 0)
    finally:
        conn.close()
    return ProjectMeta(project_id, title, folder, revision, thumb)


def make_zip_name(base_dir: Path, title: str, folder: str, revision: int, add_revision: bool) -> Path:
    """
    Construct a path for the exported .shapr file.

    Args:
        base_dir (Path): Root directory for exports.
        title (str): Project title.
        folder (str): Folder name associated with the project.
        revision (int): Project revision ID.
        add_revision (bool): Whether to append [rev-X] to filename.

    Returns:
        Path: Full path to the .shapr file.
    """
    folder_safe = sanitize(folder).strip(" _")
    title_safe = sanitize(title) or "untitled"
    out_dir = base_dir / (folder_safe if folder_safe else title_safe)
    ensure_dir(out_dir)
    name = title_safe
    if add_revision and revision > 0:
        name += f" [rev-{revision}]"
    return out_dir / f"{name}.shapr"


def build_metadata(project_id: str, revision: int) -> bytes:
    """
    Build JSON metadata for embedding inside the .shapr file.

    Args:
        project_id (str): Project identifier.
        revision (int): Revision number.

    Returns:
        bytes: UTF-8 encoded JSON metadata.
    """
    obj = {"remoteID": project_id, "revisionID": int(revision or 0), "localChangeCount": 0}
    return json.dumps(obj, indent=2).encode("utf-8")


def write_shapr_zip(target: Path, workspace_path: Path, project_id: str, revision: int) -> None:
    """
    Write a .shapr ZIP file containing workspace, metadata, and an empty .export_log.

    Args:
        target (Path): Output .shapr path.
        workspace_path (Path): Path to the workspace file to include.
        project_id (str): Project identifier.
        revision (int): Revision number for metadata.
    """
    with ZipFile(target, mode="w", compression=ZIP_DEFLATED) as zf:
        zf.writestr(".export_log", b"")
        zf.writestr(".metadata", build_metadata(project_id, revision))
        zf.write(workspace_path, arcname="workspace")


def save_thumbnail_if_any(root_dir: Path, resources_dir: Path, thumb_rel: Optional[str]) -> None:
    """
    Extract and save a thumbnail image if available.

    Args:
        root_dir (Path): Directory where thumbnail.jpg should be written.
        resources_dir (Path): Base resources folder containing thumbnails.
        thumb_rel (str | None): Relative thumbnail path to extract.
    """
    if not thumb_rel:
        return
    src = resources_dir / thumb_rel
    if not src.exists():
        return
    dst = root_dir / "thumbnail.jpg"
    try:
        extract_jpg_image(src, dst)
    except Exception:
        pass


def iter_active_workspaces(projects_root: Path) -> Iterator[Tuple[str, Path]]:
    """
    Yield project IDs and workspace paths for active projects.

    Args:
        projects_root (Path): Root path to the Shapr3D package.

    Yields:
        Tuple[str, Path]: (project_id, workspace_path)
    """
    for pid_dir in (projects_root / "LocalState" / "projects").iterdir():
        if not pid_dir.is_dir():
            continue
        ws = pid_dir / "project" / "workspace"
        if ws.exists() and ws.is_file():
            yield (pid_dir.name, ws)


def iter_tempstate_workspaces(tempstate_root: Path) -> Iterator[Tuple[str, Path]]:
    """
    Yield GUIDs and workspace paths for TempState projects.

    Args:
        tempstate_root (Path): Root TempState directory.

    Yields:
        Tuple[str, Path]: (guid, workspace_path)
    """
    for sub in tempstate_root.iterdir():
        if not sub.is_dir():
            continue
        ws = sub / "workspace"
        if ws.exists() and ws.is_file():
            yield (sub.name, ws)


def export_active_projects(packages_root: Path, base_export_dir: Path, add_revision: bool) -> None:
    """
    Export all active projects into subdirectory 'Current'.

    Args:
        packages_root (Path): Shapr3D package root directory.
        base_export_dir (Path): Base export directory.
        add_revision (bool): Whether to append [rev-X] to filenames.
    """
    storage_dir = packages_root / "LocalState" / "storage"
    db_path = storage_dir / "projectStorage.db"
    resources_dir = storage_dir / "resources"
    export_dir = base_export_dir / "Current"
    ensure_dir(export_dir)

    for project_id, ws_path in iter_active_workspaces(packages_root):
        meta = read_project_meta(db_path, project_id)
        target = make_zip_name(export_dir, meta.title, meta.folder, meta.revision_id, add_revision)
        if target.exists():
            print(f"Skip (exists): {target}")
            continue
        print(f"Exporting: {target}")
        write_shapr_zip(target, ws_path, meta.project_id, meta.revision_id)
        save_thumbnail_if_any(target.parent, resources_dir, meta.thumb_rel)


def export_tempstate_projects(packages_root: Path, base_export_dir: Path, add_revision: bool) -> None:
    """
    Export all TempState (trashed) projects into subdirectory 'Trashed'.

    Args:
        packages_root (Path): Shapr3D package root directory.
        base_export_dir (Path): Base export directory.
        add_revision (bool): Whether to append [rev-X] to filenames.
    """
    temp_root = packages_root / "TempState"
    if not temp_root.exists():
        return
    export_dir = base_export_dir / "Trashed"
    ensure_dir(export_dir)

    storage_dir = packages_root / "LocalState" / "storage"
    db_path = storage_dir / "projectStorage.db"
    for guid, ws_path in iter_tempstate_workspaces(temp_root):
        meta = read_project_meta(db_path, guid)
        title = meta.title if meta.title != guid else f"Temp_{guid}"
        folder = meta.folder
        revision = meta.revision_id
        target = make_zip_name(export_dir, title, folder, revision if add_revision else 0, add_revision)
        if target.exists():
            print(f"Skip (exists): {target}")
            continue
        print(f"Exporting (TempState): {target}")
        write_shapr_zip(target, ws_path, guid, revision)


def find_packages_root() -> Path:
    """
    Locate the Shapr3D package directory for the current user.

    Returns:
        Path: Path to the first matching Shapr3D package folder found.

    Raises:
        FileNotFoundError: If no matching package folder is found.
    """
    current_user = getpass.getuser()
    root_folder = Path(fr"C:\Users\{current_user}\AppData\Local\Packages")
    matches = [p for p in root_folder.rglob("Shapr3D.Shapr3D*") if p.is_dir()]
    if not matches:
        raise FileNotFoundError("Shapr3D package folder not found.")
    return matches[0]


def parse_args() -> argparse.Namespace:
    """
    Parse CLI arguments.

    Returns:
        argparse.Namespace: Parsed arguments including export_dir, include_tempstate, add_revision.
    """
    p = argparse.ArgumentParser(description="Export Shapr3D .shapr from active and TempState workspaces.")
    p.add_argument("--export-dir", type=str, required=True, help="Destination directory for exports.")
    p.add_argument("--include-tempstate", action="store_true", default=DEFAULT_INCLUDE_TEMPSTATE,
                   help="Include TempState (trashed) projects in the export.")
    p.add_argument("--add-revision", action="store_true", default=DEFAULT_ADD_REVISION,
                   help="Append [rev-X] to exported filenames when available.")
    return p.parse_args()


def main() -> None:
    """
    Main entry point for the script.

    - Parses CLI arguments.
    - Finds Shapr3D package root.
    - Exports current and (optionally) trashed projects.
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
