# Updating and extending the app

Keep the project code and the private data directory separate. Before updating, create an encrypted backup from Admin or with `python run.py backup`. Follow the release instructions for the version you are installing, then check the kiosk, database and printer before use.

The local roster and settings stay in the data directory. Do not replace that directory when copying in new project files. Database migrations run when the app starts; a backup is made before an upgrade migration.

## Configuration options

- Add check-in stations by opening the kiosk address on another device and registering it.
- Add printers and service schedules from Admin.
- Adjust label templates in Admin or in the copied files under `data/templates/`. See [template format](../templates/README.md).
- Use the sample roster command to create fictitious import data.
- Configure Breeze sync only if the congregation still uses Breeze. See [optional sync](SYNC.md).

## Code map

| Area | Location |
|---|---|
| Database and migrations | `checkin/db.py` |
| Roster import and validation | `checkin/roster.py` |
| Optional Breeze integration | `checkin/breeze.py`, `checkin/breeze_sync.py` |
| Check-in and visitor workflows | `checkin/service.py` |
| Reports and retention | `checkin/reports.py` |
| HTTP API | `checkin/server.py` |
| Brother printer support | `checkin/printing/` |
| Label rendering and templates | `checkin/labels.py`, `templates/` |
| Kiosk and admin screens | `web/` |
| Automated tests | `tests/` |
