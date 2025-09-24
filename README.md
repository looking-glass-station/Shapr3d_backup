# Shapr3D Backup

Shapr3D stores project files in the cloud and locally in a SQL Lite database. 

This project creates an incremental backup of your Shapr3D project files as Shapr3d Files. 


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



### Minimal example:
python.exe main.py --export-dir C:\Documents\Shapr3d_backup

