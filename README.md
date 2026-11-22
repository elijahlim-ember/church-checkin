# Church Check-in

Church Check-in is a local-first check-in kiosk for congregations. It keeps its roster and attendance on a local computer, serves a touch-friendly kiosk to devices on the local network, and can print name labels directly to a USB-connected Brother QL printer. An internet connection is not needed for normal check-in.

## Requirements

- Python 3.10 or later
- Windows, macOS or Linux
- A Brother QL label printer and USB connection for label printing

On macOS or Linux, USB printing also needs `pyusb` and the system `libusb` package. See [printer setup](docs/PRINTER-TEST.md).

## Setup

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py serve
```

On Windows, activate with `.venv\Scripts\Activate.ps1` and run `python run.py serve`.

Open `http://localhost:8080` on the host computer. On first launch, create an administrator account, volunteer PIN and backup passphrase, and set the organisation name. Register each kiosk device from its browser. For Windows installation and network access, see [Windows setup](docs/SETUP-WINDOWS.md).

## Configuration

Copy `.env.example` to `.env` to document local settings, then set the variables in your shell or service environment. The app does not load `.env` files automatically.

- `ORG_NAME` sets the initial organisation name. The name can also be changed in Admin settings and is stored in the database.
- `CHECKIN_DATA` sets the data directory. By default, data is stored in `./data`.
- `CHECKIN_NO_USB=1` disables USB printer discovery, which is useful for development and automated tests.
- `CHECKIN_UPGRADE_WITHOUT_BACKUP=1` and `CHECKIN_RESTORE_WITHOUT_BACKUP=1` are maintenance overrides for upgrades and restores. Use them only when a backup cannot be made.

Keep the data directory private. It contains the roster, settings, encryption keys, backups and logs. Back up through the Admin screen or run `python run.py backup`.

Breeze ChMS sync is an optional legacy integration. It is off until configured in Admin. Local check-in does not depend on it.

## Run and maintain

```sh
python run.py serve
python run.py printers
python run.py backup
python run.py sample-roster --help
```

See [recovery](docs/RECOVERY.md), [sync](docs/SYNC.md), and [updating](docs/UPDATING-AND-GROWING.md) for further guidance.

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests use temporary directories and fake roster data.

## Licence

This project is available under the MIT Licence. See [LICENSE](LICENSE).
