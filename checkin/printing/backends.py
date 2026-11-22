"""Printer transports and the shared Brother job/status logic.

Backends:
  usb      Direct USB. Windows: usbprint.sys (winusb.py). macOS/Linux: libusb via pyusb.
  tcp      Raw TCP port 9100 to a fixed IP (optional; not used for the Sunday setup).
  preview  No hardware. Decodes the exact raster bytes back into PNG files, for
           training, testing and checking label layouts. A file named 'simulate'
           in the folder can force: offline, no_media, cover_open, fail, slow.
"""

import os
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .raster import STATUS_REQUEST, Media, PrinterStatus, check_media, parse_status, take_packets

BROTHER_VID = 0x04F9


@dataclass
class PrintResult:
    status: str                      # printed | failed | unknown
    message: str = ""
    printer_status: PrinterStatus | None = None
    confirmed: int = 0
    log: list = field(default_factory=list)
    sent: bool = True                # False when the job was refused before any data went to the printer


def offline(message: str) -> PrinterStatus:
    return PrinterStatus(ok=False, state="offline", message=message, errors=[])


class Transport:
    def write(self, data: bytes, timeout: float = 10.0) -> None:
        raise NotImplementedError

    def read(self, size: int = 64, timeout: float = 0.5) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class BrotherBackend:
    """Status request and print-with-confirmation over any two-way transport."""

    two_way = True

    def __init__(self, address: str | None = None):
        self.address = address or None

    def open(self) -> Transport:
        raise NotImplementedError

    def _query(self, dev: Transport, timeout: float = 2.0, reset: bool = True) -> PrinterStatus:
        """reset=False sends only ESC i S, without the invalidate/initialise that could cancel labels still printing."""
        dev.read(64, 0.05)  # drop anything stale from an earlier job
        dev.write(STATUS_REQUEST if reset else b"\x1b\x69\x53", timeout=3.0)
        deadline = time.time() + timeout
        buffer = b""
        last = None
        while time.time() < deadline:
            buffer += dev.read(64, 0.3)
            packets, buffer = take_packets(buffer)
            for packet in packets:
                status = parse_status(packet)
                if status and status.status_type == "reply":
                    return status
                last = status or last
        return last or offline("Printer is connected but not answering. Turn it off and on again.")

    def status(self) -> PrinterStatus:
        try:
            with self.open() as dev:
                return self._query(dev)
        except FileNotFoundError as exc:
            return offline(f"Printer offline: {exc}")
        except (OSError, TimeoutError) as exc:
            return offline(f"Printer offline: {exc}")

    def print_job(self, data: bytes, pages: int, confirm: str = "per_label", media: Media | None = None) -> PrintResult:
        log = []
        try:
            with self.open() as dev:
                before = self._query(dev) if self.two_way else None
                if before is not None:
                    if media:
                        before = check_media(before, media)
                    log.append(("before", before.status_type, before.state, before.errors))
                    if not before.ok:
                        return PrintResult("failed", before.message, before, 0, log, sent=False)
                try:
                    dev.write(data, timeout=10.0)
                except TimeoutError:
                    after = self._try_status(dev)
                    message = after.message if after and not after.ok else "The printer stopped accepting data"
                    return PrintResult("failed", message, after, 0, log)
                if not self.two_way or confirm == "none":
                    return PrintResult("printed", "Sent (not confirmed by printer)", before, pages, log)
                return self._await_completion(dev, pages, confirm, log)
        except FileNotFoundError as exc:
            return PrintResult("failed", f"Printer offline: {exc}", offline(str(exc)), 0, log, sent=False)
        except (OSError, TimeoutError) as exc:
            return PrintResult("failed", f"Printer offline: {exc}", offline(str(exc)), 0, log)

    def _try_status(self, dev: Transport) -> PrinterStatus | None:
        try:
            return self._query(dev, timeout=1.5)
        except (OSError, TimeoutError):
            return None

    def _await_completion(self, dev: Transport, pages: int, confirm: str, log: list) -> PrintResult:
        """Called once the whole job has been written.

        The QL-820NWB takes data only as fast as it prints, so the write returns while the last label is
        printing. It reports every label, but on longer jobs it drops the early reports. So: a completion,
        then 'ready', then no new 'printing' within a moment means the last label is done.
        """
        deadline = time.time() + 8 + 4 * pages
        quiet_window = 0.8
        ready_since = None
        buffer = b""
        completed = 0
        last = None
        while time.time() < deadline:
            if ready_since and time.time() - ready_since >= quiet_window:
                return PrintResult("printed", "Printed", last, pages, log)
            chunk = dev.read(64, 0.2)
            if not chunk:
                continue
            packets, buffer = take_packets(buffer + chunk)
            for packet in packets:
                status = parse_status(packet)
                if not status:
                    continue
                last = status
                log.append((round(time.time(), 3), status.status_type, status.phase, status.errors))
                if status.errors and status.state not in ("ready", "busy"):
                    note = f" after {completed} of {pages} labels" if completed else ""
                    return PrintResult("failed", status.message + note, status, completed, log)
                if status.status_type == "turned_off":
                    return PrintResult("failed", "Printer turned off", status, completed, log)
                if status.status_type == "printing_completed":
                    completed += 1
                    if confirm == "job" or completed >= pages:
                        return PrintResult("printed", "Printed", status, pages, log)
                    ready_since = None
                elif status.status_type == "phase_change":
                    # 'ready' after a completion may be the end of the job; 'printing' means another label began.
                    ready_since = time.time() if (status.phase == "ready" and completed) else None
        if completed:
            return self._verify_rest(dev, pages, completed, last, log)
        return PrintResult("unknown", f"Printer confirmed {completed} of {pages} labels. Check the labels.", last,
                           completed, log)

    def _verify_rest(self, dev: Transport, pages: int, completed: int, last, log: list) -> PrintResult:
        """After a partial confirmation: no error reported and the printer answers 'ready' means it printed."""
        try:
            status = self._query(dev, timeout=1.5, reset=False)
        except (OSError, TimeoutError):
            status = None
        if status is None:
            return PrintResult("unknown", f"Printer confirmed {completed} of {pages} labels, then stopped answering. "
                               "Check the labels.", last, completed, log)
        log.append((round(time.time(), 3), "verify", status.state, status.errors))
        if not status.ok:
            return PrintResult("failed", f"{status.message} (some of the {pages} labels may have printed)", status,
                               completed, log)
        return PrintResult("printed", "Printed", status, pages, log)


# ---- USB ------------------------------------------------------------------------------
class WindowsUsbBackend(BrotherBackend):
    def open(self) -> Transport:
        from . import winusb
        device = winusb.open_device(self.address)

        class _T(Transport):
            def write(self, data, timeout=10.0):
                device.write(data, timeout=timeout)

            def read(self, size=64, timeout=0.5):
                return device.read(size, timeout)

            def close(self):
                device.close()

        return _T()


def _libusb_backend():
    """pyusb backend: on Windows the libusb DLL bundled in libusb-package, elsewhere the system libusb (None)."""
    if sys.platform != "win32":
        return None
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:  # noqa: BLE001 - package or DLL missing
        return None


WINUSB_HINT = ("Windows' own USB printer driver is using this printer, and it sends everything to the IPP channel. "
               "Switch the printer's Interface 0 to WinUSB with Zadig (docs/SETUP-WINDOWS.md).")


class LibUsbBackend(BrotherBackend):
    """Direct USB through libusb. On Windows the printer's interface 0 must use the WinUSB driver (Zadig).

    The QL-820NWB's interface 0 has two alternate settings: 0 is the Brother raster channel (07/01/02) and
    1 is IPP over USB (07/01/04). Windows 11's usbprint.sys selects the IPP one, where raster data and status
    requests are silently dropped, so we select setting 0 ourselves.
    """

    def open(self) -> Transport:
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise FileNotFoundError("pyusb is not installed (pip install pyusb) and libusb is required") from exc
        devices = list(usb.core.find(find_all=True, idVendor=BROTHER_VID, backend=_libusb_backend()) or [])
        device = None
        for candidate in devices:
            serial = _usb_serial(candidate)
            if not self.address or (serial and serial.upper() == self.address.upper()):
                device = candidate
                break
        if device is None:
            if sys.platform == "win32" and _on_usbprint(self.address):
                raise OSError(WINUSB_HINT)
            raise FileNotFoundError(f"Printer {self.address or ''} is not connected".replace("  ", " "))
        try:
            if device.is_kernel_driver_active(0):
                device.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            device.set_configuration()
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            config = device.get_active_configuration()
            interface = config[(0, 0)]
            usb.util.claim_interface(device, interface.bInterfaceNumber)
        except (NotImplementedError, usb.core.USBError) as exc:
            if sys.platform == "win32":
                raise OSError(WINUSB_HINT) from exc
            raise
        try:
            device.set_interface_altsetting(interface=interface.bInterfaceNumber, alternate_setting=0)
        except (NotImplementedError, usb.core.USBError):
            pass
        ep_out = usb.util.find_descriptor(interface, custom_match=lambda e: usb.util.endpoint_direction(
            e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        ep_in = usb.util.find_descriptor(interface, custom_match=lambda e: usb.util.endpoint_direction(
            e.bEndpointAddress) == usb.util.ENDPOINT_IN)

        class _T(Transport):
            def write(self, data, timeout=10.0):
                try:
                    for start in range(0, len(data), 16384):
                        ep_out.write(data[start:start + 16384], int(timeout * 1000))
                except usb.core.USBTimeoutError as exc:
                    raise TimeoutError("The printer stopped accepting data") from exc
                except usb.core.USBError as exc:
                    raise OSError(str(exc)) from exc

            def read(self, size=64, timeout=0.5):
                try:
                    return bytes(ep_in.read(size, max(1, int(timeout * 1000))))
                except usb.core.USBTimeoutError:
                    return b""
                except usb.core.USBError as exc:
                    if "timed out" in str(exc).lower():
                        return b""
                    raise OSError(str(exc)) from exc

            def close(self):
                try:
                    usb.util.release_interface(device, interface.bInterfaceNumber)
                    usb.util.dispose_resources(device)
                except usb.core.USBError:
                    pass

        return _T()


def _on_usbprint(address: str | None) -> bool:
    try:
        from . import winusb
        return any(not address or p["serial"].upper() == address.upper() for p in winusb.find_printers())
    except OSError:
        return False


def _usb_serial(device) -> str:
    try:
        import usb.util
        return usb.util.get_string(device, device.iSerialNumber) or ""
    except Exception:  # noqa: BLE001 - permissions or detached device
        return ""


def UsbBackend(address=None) -> BrotherBackend:
    if sys.platform == "win32" and _libusb_backend() is None:
        return WindowsUsbBackend(address)
    return LibUsbBackend(address)


def discover_usb() -> list[dict]:
    """List Brother printers connected by USB to this computer (none when CHECKIN_NO_USB is set, as in tests)."""
    if os.environ.get("CHECKIN_NO_USB"):
        return []
    found = _discover_libusb()
    if sys.platform == "win32":
        # libusb reads serial numbers only from WinUSB printers; the ones still on usbprint.sys come from winusb.py.
        found = [d for d in found if d["serial"]]
        known = {d["serial"].upper() for d in found}
        from . import winusb
        for device in winusb.find_printers():
            if not device["serial"] or device["serial"].upper() not in known:
                found.append({**device, "driver": "usbprint"})
    return found


def _discover_libusb() -> list[dict]:
    try:
        import usb.core
        import usb.util
    except ImportError:
        return []
    found = []
    try:
        for device in usb.core.find(find_all=True, idVendor=BROTHER_VID, backend=_libusb_backend()) or []:
            try:
                model = usb.util.get_string(device, device.iProduct) or ""
            except Exception:  # noqa: BLE001
                model = ""
            serial = _usb_serial(device)
            found.append({"backend": "usb", "address": serial, "serial": serial, "model": model,
                          "path": f"bus {device.bus} address {device.address}", "pid": device.idProduct,
                          "driver": "libusb"})
    except Exception:  # noqa: BLE001 - libusb missing
        return []
    return found


# ---- TCP --------------------------------------------------------------------------------
class TcpBackend(BrotherBackend):
    def open(self) -> Transport:
        host = self.address or ""
        port = 9100
        if ":" in host:
            host, port_text = host.rsplit(":", 1)
            port = int(port_text)
        if not host:
            raise FileNotFoundError("No printer IP address configured")
        sock = socket.create_connection((host, port), timeout=3)

        class _T(Transport):
            def write(self, data, timeout=10.0):
                sock.settimeout(timeout)
                try:
                    sock.sendall(data)
                except socket.timeout as exc:
                    raise TimeoutError("The printer stopped accepting data") from exc

            def read(self, size=64, timeout=0.5):
                sock.settimeout(timeout)
                try:
                    return sock.recv(size)
                except socket.timeout:
                    return b""

            def close(self):
                sock.close()

        return _T()


# ---- Preview (no hardware) ------------------------------------------------------------------
class PreviewBackend(BrotherBackend):
    two_way = False

    def __init__(self, address: str | None = None, media_key: str = "62"):
        super().__init__(address)
        self.folder = Path(address or "label-preview")
        self.media_key = media_key

    def _simulation(self) -> str:
        try:
            return (self.folder / "simulate").read_text().strip().lower()
        except OSError:
            return ""

    def status(self) -> PrinterStatus:
        from .raster import MEDIA
        sim = self._simulation()
        media = MEDIA.get(self.media_key) or MEDIA["62"]
        if sim == "offline":
            return offline("Printer offline (simulated)")
        if sim == "no_media":
            return PrinterStatus(False, "no_media", "Out of labels", ["No media"], media.width_mm)
        if sim == "cover_open":
            return PrinterStatus(False, "cover_open", "Cover open", ["Cover open"], media.width_mm)
        return PrinterStatus(True, "ready", "Ready (preview printer)", [], media.width_mm, media.length_mm,
                             "die-cut" if media.die_cut else "continuous", "Preview", "reply", "ready")

    def print_job(self, data: bytes, pages: int, confirm: str = "per_label", media: Media | None = None) -> PrintResult:
        from .raster import decode_job
        status = self.status()
        if media:
            status = check_media(status, media)
        if not status.ok:
            return PrintResult("failed", status.message, status, sent=False)
        images = decode_job(data)
        if len(images) != pages:
            return PrintResult("failed", f"Encoded {len(images)} labels, expected {pages}", status)
        sim = self._simulation()
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for index, image in enumerate(images, start=1):
            if sim == "fail" and index == 2:
                return PrintResult("failed", "Out of labels after 1 of %d labels (simulated)" % pages,
                                   PrinterStatus(False, "no_media", "Out of labels", ["No media"]), 1)
            image.save(self.folder / f"{stamp}-{int(time.time() * 1000) % 100000:05d}-{index}.png")
            time.sleep(1.0 if sim == "slow" else 0.05)
        return PrintResult("printed", "Printed (preview)", status, pages)


def make_backend(kind: str, address: str | None, media_key: str = "62") -> BrotherBackend:
    if kind == "usb":
        return UsbBackend(address)
    if kind == "tcp":
        return TcpBackend(address)
    if kind == "preview":
        return PreviewBackend(address, media_key)
    raise ValueError(f"Unknown printer connection type: {kind}")
