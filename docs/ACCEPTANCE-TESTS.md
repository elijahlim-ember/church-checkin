# Acceptance checks

Run the automated suite from the project root:

```sh
python -m unittest discover -s tests -v
```

Before using the kiosk with a congregation, check the following on its own equipment:

- Complete first-run setup and register at least one kiosk device.
- Import a small, clearly labelled test roster and check that search finds the expected households.
- Check in and undo a test attendance record.
- Print and collect a test label. Confirm the roll size and cut position.
- Restart the app and confirm that settings and attendance remain available.
- Create a backup and confirm that the restore workflow can read it.
- If using Breeze, preview and verify a test sync before enabling regular sync.
