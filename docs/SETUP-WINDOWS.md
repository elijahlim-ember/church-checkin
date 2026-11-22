# Windows setup

This guide describes a typical local installation. Choose the computer and network settings that suit your congregation.

## Install the app

1. Install Python 3.10 or later and select the option to add Python to PATH.
2. Copy the project folder to a location such as `C:\ChurchCheckin`.
3. Open PowerShell in that folder and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python run.py serve
   ```

4. On the same computer, open `http://localhost:8080` and complete first-run setup.
5. In Windows Firewall, allow the app's selected port on the private network only. Find the host computer's current address in Admin and use it to open the kiosk from other devices.

Use a stable local address for the host if your network supports DHCP reservations. Do not copy a data folder containing real roster details into source control or a shared public folder.

## USB label printing

Connect a Brother QL printer by USB, install its driver if required by Windows, and select it in Admin. Use **Test Print** to check the label roll, orientation and cut position before opening check-in.

## Optional shortcuts

The scripts in `scripts/` can install Windows shortcuts and start the app. Review the script paths and task names before running them. The kiosk can also be opened manually in a browser.
