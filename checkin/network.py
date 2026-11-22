"""What network this computer is on right now: its addresses, and the Wi-Fi's name where it can be read.

Asked afresh whenever a volunteer adds a one-time device, because a computer taken to a camp or moved to another
Wi-Fi keeps running with a new address. Only the name of the Wi-Fi is ever read: never its password.
"""

import ipaddress
import socket
import subprocess
import sys

_SHARED = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and the like: not the Wi-Fi a phone in the room is on


def lan_addresses():
    """This computer's IPv4 addresses, the one other devices on its Wi-Fi should use first."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    main = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # sends nothing; only asks which address would be used
            main = probe.getsockname()[0]
            found.add(main)
    except OSError:
        pass
    usable = [a for a in found if not a.startswith(("127.", "169.254.", "0."))]
    return sorted(usable, key=lambda a: (ipaddress.ip_address(a) in _SHARED, a != main, ipaddress.ip_address(a)))


def wifi_name():
    """The name of the Wi-Fi this computer is joined to, or None on a cable or when it cannot be read."""
    try:
        if sys.platform == "win32":
            return _one_name(_windows_names(_run(["netsh", "wlan", "show", "interfaces"])))
        if sys.platform == "darwin":
            return _one_name(_mac_names(_run(["networksetup", "-getairportnetwork", device]) for device in ("en0", "en1")))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _run(command):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no black window flashing up on the Windows desk
    done = subprocess.run(command, capture_output=True, timeout=3, creationflags=flags, stdin=subprocess.DEVNULL)
    encoding = "oem" if sys.platform == "win32" else "utf-8"
    return done.stdout.decode(encoding, errors="replace")


def _windows_names(text):
    """The SSID lines of `netsh wlan show interfaces`: one per joined Wi-Fi adapter."""
    names = []
    for line in text.splitlines():
        label, colon, value = line.partition(":")
        if colon and label.strip() == "SSID":  # not "BSSID" or "AP BSSID"
            names.append(value.strip())
    return names


def _mac_names(texts):
    marker = "Current Wi-Fi Network: "
    return [line[len(marker):].strip() for text in texts for line in text.splitlines() if line.startswith(marker)]


def _one_name(names):
    names = {n for n in names if n}
    if len(names) != 1:  # none, or two adapters on two networks: say nothing rather than guess
        return None
    name = names.pop()
    if len(name) > 32 or "�" in name or not name.isprintable():  # a name that did not come through whole
        return None
    return name
