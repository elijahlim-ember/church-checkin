"""Direct USB access to Brother printers on Windows through usbprint.sys.

Windows loads its built-in usbprint.sys driver for every USB printer-class
device. Opening the device interface (GUID_DEVINTERFACE_USBPRINT) gives a
two-way byte pipe to the printer: we write raster data and read the 32-byte
status replies. No Windows print queue, spooler, dialog, AirPrint or Zadig
driver swap is involved, and the Brother Windows driver can stay installed.

Only importable on Windows; everything here is plain ctypes (no extra packages).
"""

import ctypes
import re
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - imported only on Windows
    raise ImportError("winusb backend is Windows-only")

GUID_DEVINTERFACE_USBPRINT = "{28D78FAD-5A12-11D1-AE5B-0000F803A8C2}"
BROTHER_VID = 0x04F9

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
ERROR_NO_MORE_ITEMS = 259
ERROR_INSUFFICIENT_BUFFER = 122
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
IOCTL_USBPRINT_GET_1284_ID = 0x220034


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        a, b, c, d, e = text.strip("{}").split("-")
        guid = cls(int(a, 16), int(b, 16), int(c, 16))
        for i, byte in enumerate(bytes.fromhex(d + e)):
            guid.Data4[i] = byte
        return guid


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p), ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


_setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)

_cfgmgr32.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
_cfgmgr32.CM_Locate_DevNodeW.restype = wintypes.DWORD
_cfgmgr32.CM_Get_Parent.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG]
_cfgmgr32.CM_Get_Parent.restype = wintypes.DWORD
_cfgmgr32.CM_Get_Device_IDW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]
_cfgmgr32.CM_Get_Device_IDW.restype = wintypes.DWORD

_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
_setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                   wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
_setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                        ctypes.c_void_p, wintypes.DWORD,
                                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
_setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
_kernel32.ResetEvent.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                ctypes.POINTER(OVERLAPPED)]
_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                               ctypes.POINTER(OVERLAPPED)]
_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                      ctypes.POINTER(OVERLAPPED)]
_kernel32.DeviceIoControl.restype = wintypes.BOOL
_kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                          ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
_kernel32.GetOverlappedResult.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
_kernel32.CancelIoEx.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class UsbPrintError(OSError):
    pass


def _parse_path(path: str) -> dict:
    match = re.search(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})(?:&mi_[0-9a-f]{2})?#([^#]+)#", path, re.I)
    if not match:
        return {"vid": None, "pid": None, "serial": ""}
    instance = match.group(3)
    # Composite devices (the QL-820NWB is one) get a generated instance id like "7&1a2b3c&0&0000" on the
    # printer interface; the serial number is on the parent USB device.
    serial = _parent_serial(path) if "&" in instance else instance.upper()
    return {"vid": int(match.group(1), 16), "pid": int(match.group(2), 16), "serial": serial}


def _parent_serial(path: str) -> str:
    # \\?\usb#vid_04f9&pid_209d&mi_00#7&2c9fa817&0&0000#{guid} -> USB\VID_04F9&PID_209D&MI_00\7&2C9FA817&0&0000
    parts = path.split("#")
    if len(parts) < 4:
        return ""
    instance_id = "\\".join([parts[0].rsplit("\\", 1)[-1]] + parts[1:3]).upper()
    node, parent = wintypes.DWORD(0), wintypes.DWORD(0)
    if _cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(node), instance_id, 0) != 0:
        return ""
    if _cfgmgr32.CM_Get_Parent(ctypes.byref(parent), node, 0) != 0:
        return ""
    buffer = ctypes.create_unicode_buffer(512)
    if _cfgmgr32.CM_Get_Device_IDW(parent, buffer, 512, 0) != 0:
        return ""
    serial = buffer.value.rsplit("\\", 1)[-1]
    return "" if "&" in serial else serial.upper()


def list_device_paths() -> list[str]:
    guid = GUID.parse(GUID_DEVINTERFACE_USBPRINT)
    info = _setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if info in (None, INVALID_HANDLE_VALUE):
        raise UsbPrintError(ctypes.get_last_error(), "SetupDiGetClassDevs failed")
    paths = []
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not _setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), index, ctypes.byref(iface)):
                if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                    break
                raise UsbPrintError(ctypes.get_last_error(), "SetupDiEnumDeviceInterfaces failed")
            index += 1
            required = wintypes.DWORD(0)
            _setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(iface), None, 0, ctypes.byref(required),
                                                       None)
            if not required.value:
                continue
            buffer = ctypes.create_string_buffer(required.value)
            # cbSize is the size of the fixed part of SP_DEVICE_INTERFACE_DETAIL_DATA_W.
            ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not _setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(iface), buffer, required,
                                                              None, None):
                continue
            paths.append(ctypes.wstring_at(ctypes.addressof(buffer) + 4))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(info)
    return paths


class UsbPrintDevice:
    def __init__(self, path: str):
        self.path = path
        self.handle = _kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                            None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self.handle in (None, INVALID_HANDLE_VALUE):
            raise UsbPrintError(ctypes.get_last_error(), f"Cannot open printer (Windows error {ctypes.get_last_error()})")
        self.event = _kernel32.CreateEventW(None, True, False, None)

    def close(self) -> None:
        if self.handle not in (None, INVALID_HANDLE_VALUE):
            _kernel32.CloseHandle(self.handle)
            self.handle = None
        if self.event:
            _kernel32.CloseHandle(self.event)
            self.event = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _wait(self, overlapped: OVERLAPPED, timeout: float, what: str) -> int:
        result = _kernel32.WaitForSingleObject(self.event, int(timeout * 1000))
        transferred = wintypes.DWORD(0)
        if result == WAIT_TIMEOUT:
            _kernel32.CancelIoEx(self.handle, ctypes.byref(overlapped))
            _kernel32.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(transferred), True)
            return -1
        if result != WAIT_OBJECT_0:
            raise UsbPrintError(ctypes.get_last_error(), f"{what} wait failed")
        if not _kernel32.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(transferred), False):
            raise UsbPrintError(ctypes.get_last_error(), f"{what} failed (Windows error {ctypes.get_last_error()})")
        return transferred.value

    def write(self, data: bytes, timeout: float = 10.0, chunk: int = 16384) -> None:
        for start in range(0, len(data), chunk):
            part = data[start:start + chunk]
            buffer = ctypes.create_string_buffer(part, len(part))
            overlapped = OVERLAPPED(hEvent=self.event)
            _kernel32.ResetEvent(self.event)
            ok = _kernel32.WriteFile(self.handle, buffer, len(part), None, ctypes.byref(overlapped))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                raise UsbPrintError(ctypes.get_last_error(), f"USB write failed (Windows error {ctypes.get_last_error()})")
            written = self._wait(overlapped, timeout, "USB write")
            if written < 0:
                raise TimeoutError("The printer stopped accepting data")
            if written != len(part):
                raise UsbPrintError(0, f"Short USB write ({written} of {len(part)} bytes)")

    def read(self, size: int = 64, timeout: float = 0.5) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        overlapped = OVERLAPPED(hEvent=self.event)
        _kernel32.ResetEvent(self.event)
        ok = _kernel32.ReadFile(self.handle, buffer, size, None, ctypes.byref(overlapped))
        if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
            raise UsbPrintError(ctypes.get_last_error(), f"USB read failed (Windows error {ctypes.get_last_error()})")
        count = self._wait(overlapped, timeout, "USB read")
        return buffer.raw[:count] if count > 0 else b""

    def device_id(self) -> str:
        buffer = ctypes.create_string_buffer(1024)
        overlapped = OVERLAPPED(hEvent=self.event)
        _kernel32.ResetEvent(self.event)
        returned = wintypes.DWORD(0)
        ok = _kernel32.DeviceIoControl(self.handle, IOCTL_USBPRINT_GET_1284_ID, None, 0, buffer, 1024,
                                       ctypes.byref(returned), ctypes.byref(overlapped))
        if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
            return ""
        count = self._wait(overlapped, 2.0, "Device ID")
        if count < 2:
            return ""
        length = int.from_bytes(buffer.raw[:2], "big")
        return buffer.raw[2:min(count, length)].decode("ascii", "replace")


def find_printers() -> list[dict]:
    devices = []
    for path in list_device_paths():
        info = _parse_path(path)
        if info["vid"] not in (None, BROTHER_VID):
            continue
        model = ""
        try:
            with UsbPrintDevice(path) as device:
                ident = device.device_id()
            fields = dict(part.split(":", 1) for part in ident.split(";") if ":" in part)
            model = fields.get("MDL") or fields.get("MODEL") or ""
            if info["vid"] is None and "brother" not in (fields.get("MFG") or "").lower():
                continue
        except OSError:
            pass
        devices.append({"backend": "usb", "address": info["serial"] or path, "serial": info["serial"],
                        "model": model, "path": path, "pid": info["pid"]})
    return devices


def resolve(address: str | None) -> str:
    """Find the device path for a serial number (or path); with no address, the only Brother printer."""
    printers = find_printers()
    if address:
        wanted = address.upper()
        for p in printers:
            if (p["serial"] and p["serial"].upper() == wanted) or p["path"].upper() == wanted:
                return p["path"]
        raise FileNotFoundError(f"Printer {address} is not connected")
    if not printers:
        raise FileNotFoundError("No Brother printer is connected by USB")
    if len(printers) > 1:
        raise FileNotFoundError("Several Brother printers are connected; set the serial number in Printers")
    return printers[0]["path"]


def open_device(address: str | None) -> UsbPrintDevice:
    deadline = time.time() + 1
    while True:
        try:
            return UsbPrintDevice(resolve(address))
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.2)
