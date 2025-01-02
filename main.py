import argparse
import getpass
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from fs import mirror
from typing import Tuple, Optional


def backup_files(src: Path, dest: Path) -> None:
    """
    Backup files from source to destination.

    Args:
        src (Path): The source directory to back up.
        dest (Path): The destination directory to back up to.
    """
    dest.mkdir(parents=True, exist_ok=True)
    mirror.mirror(str(src), str(dest))
    #dest.unlink(missing_ok=True)
    #dest.mkdir(parents=True, exist_ok=True)
    #shutil.copytree(src, dest, dirs_exist_ok=True)
    print(f"Backup completed: {src} -> {dest}")


def get_project_info(projects_db: Path, project_id: str) -> Tuple[str, Optional[datetime], Optional[str], Optional[str]]:
    """
    Retrieve project information from the database.

    Args:
        projects_db (Path): Path to the projects' database.
        project_id (str): ID of the project to retrieve information for.

    Returns:
        Tuple[str, Optional[datetime], Optional[str], Optional[str]]: Project information including title, last modified time,
            light thumbnail, and dark thumbnail.
    """
    conn = sqlite3.connect(str(projects_db))
    cursor = conn.cursor()
    cursor.execute("SELECT title, lastModifiedAtMsec, thumbnailLight, thumbnailDark FROM Projects WHERE projectID = ?", (project_id,))
    result = cursor.fetchone()
    conn.close()

    if result:
        title, last_modified_time, thumbnail_light, thumbnail_dark = result
        title = title.strip() if title else project_id
        mod_date = datetime.utcfromtimestamp(int(last_modified_time) / 1000) if last_modified_time else None
        return title, mod_date, thumbnail_light, thumbnail_dark
    else:
        return project_id, None, None, None


def extract_jpg_image(path: Path, out_path: Path) -> None:
    """
    Extract JPG image from a file.

    Thanks, Mr Che Fisher
    # https://gist.github.com/GrayedFox/8cabb5bc81312cbff0a0a9244683d06c

    Args:
        path (Path): Path to the input file.
        out_path (Path): Path to save the extracted JPG image.
    """
    jpg_byte_start = b'\xff\xd8'
    jpg_byte_end = b'\xff\xd9'
    jpg_image = bytearray()

    with open(path, 'rb') as f:
        req_data = f.read()

        start = req_data.find(jpg_byte_start)
        if start == -1:
            print('Could not find JPG start of image marker!')
            return

        end = req_data.find(jpg_byte_end, start) + len(jpg_byte_end)
        jpg_image += req_data[start:end]

    with open(out_path, 'wb') as f:
        f.write(jpg_image)


def export_shapes_to_parasolid(backup_path: Path, workspace_path: Path, parasolid_folder: Path) -> Tuple[int, int]:
    """
    Export shapes to Parasolid format.

    Args:
        backup_path (Path): Path to the backup directory.
        workspace_path (Path): Path to the workspace directory.
        parasolid_folder (Path): Path to the folder to save the exported shapes.

    Returns:
        Tuple[int, int]: A tuple containing the count of exported shapes and skipped shapes.
    """
    exported_count = 0
    skipped_count = 0
    resources_folder = backup_path / 'LocalState' / 'storage' / 'resources'
    projects_db = backup_path / 'LocalState' / 'storage' / 'projectStorage.db'
    illegal_characters_pattern = re.compile(r'[<>:"/\\|?*]')

    for path in workspace_path.rglob('workspace'):
        project_id = path.parent.parent.name
        title, mod_date, thumbnail_light, thumbnail_dark = get_project_info(projects_db, project_id)
        print(f"Retrieved project info: {title}, {mod_date}")

        out_folder = parasolid_folder / title
        out_folder.mkdir(parents=True, exist_ok=True)

        if thumbnail_dark:
            shutil.copy(resources_folder / thumbnail_dark, out_folder / thumbnail_dark)
            extract_jpg_image(out_folder / thumbnail_dark, out_folder / 'thumbnail.jpg')

        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            results = cursor.execute('SELECT cast(ShapeName as text), ShapeData FROM main.Shapes')

            for row in results:
                name, binary = row
                sanitized_filename = illegal_characters_pattern.sub('_', name)
                output_file_path = out_folder / f"{sanitized_filename}.x_b"

                if mod_date and (not output_file_path.exists() or output_file_path.stat().st_mtime < mod_date.timestamp()):
                    shutil.copy(path, out_folder / 'workspace.shapr')

                    with output_file_path.open('wb') as file:
                        file.write(binary)

                    print(f"    Exported: {sanitized_filename}.x_b")
                    exported_count += 1
                else:
                    print(f"    Skipped: {sanitized_filename}.x_b (File hasn't been modified)")
                    skipped_count += 1

    return exported_count, skipped_count


def main() -> None:
    parser = argparse.ArgumentParser(description='Backup and export Shapr3D shapes to Parasolid format.')
    parser.add_argument('backup_path', type=str, help='Path to the backup destination')
    args = parser.parse_args()

    current_user = getpass.getuser()
    root_folder = Path(fr'C:\Users\{current_user}\AppData\Local\Packages')
    backup_path = Path(args.backup_path)
    shapr3d_path = [p for p in root_folder.rglob('Shapr3D.Shapr3D*') if p.is_dir()][0]

    if shapr3d_path.exists():
        shapr3d_path_backup = backup_path.joinpath('Shapr3d_files')
        backup_files(shapr3d_path, shapr3d_path_backup)

        # New version change
        #workspace_directories = shapr3d_path.joinpath('LocalState', 'workspaces')
        workspace_directories = shapr3d_path

        parasolid_folder = backup_path.joinpath('parasolid_backups')

        exported_count, skipped_count = export_shapes_to_parasolid(shapr3d_path_backup, workspace_directories,
                                                                   parasolid_folder)

        print("Completed")
        print(f"Exported: {exported_count}, skipped: {skipped_count}")

        print("Waiting for you to read this.")
        time.sleep(5)
        print("...done")
    else:
        print("Shapr3D installation not found.")


if __name__ == "__main__":
    main()
