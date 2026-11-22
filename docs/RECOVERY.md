# Backup and recovery

The app creates encrypted backup files containing the local database, settings, label templates and required keys. Protect the backup passphrase separately from the backup file.

## Create a backup

Use **Admin → Backups** or run:

```sh
python run.py backup
```

Copy a recent backup to a separate, access-controlled location. Do not place backups in a public folder.

## Restore

1. Install the same or a newer app version on the replacement computer.
2. Start the app and open the restore workflow, or run `python run.py restore PATH_TO_BACKUP`.
3. Select the encrypted backup and enter its passphrase.
4. Register each kiosk device again if needed.
5. Check the roster, settings and printer before opening the kiosk for use.

Keep the original data directory unchanged until the restored copy has been checked. If a restore fails, preserve the backup and consult the error message before trying again.
