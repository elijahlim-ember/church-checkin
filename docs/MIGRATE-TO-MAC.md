# Move the app to macOS

1. Copy the project code to the Mac without copying `.venv`.
2. Install Python 3.10 or later, then create an environment and install the dependencies:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

3. Install `libusb` with your preferred package manager if USB printing is required.
4. Restore an encrypted app backup using the passphrase, or set `CHECKIN_DATA` to an existing protected data directory.
5. Run `python run.py serve`, then check the kiosk and printer before use.

Do not copy private roster data into the project source folder. Keep the backup passphrase separate from the backup file.
