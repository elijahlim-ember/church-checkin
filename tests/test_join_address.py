"""The address, Wi-Fi name and QR code that *Add a one-time device* shows: read afresh each time, so the dialog is
right on whatever network the computer is on today (a hotel Wi-Fi at camp, a phone hotspot).

Run:  python -m unittest discover -s tests -v
"""

import base64
import os

os.environ["CHECKIN_NO_USB"] = "1"
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkin import network, qr  # noqa: E402

# `http://192.0.2.10:8080/`, using the documentation-only address reserved for examples.
KNOWN_URL = "http://192.0.2.10:8080/"
KNOWN_SQUARE = [
    "#######.##..#..#..#######",
    "#.....#..##...#...#.....#",
    "#.###.#..##.#..##.#.###.#",
    "#.###.#.#.#.##.#..#.###.#",
    "#.###.#.##.....#..#.###.#",
    "#.....#.#.##.##.#.#.....#",
    "#######.#.#.#.#.#.#######",
    "........###....##........",
    "#...#.####.##.#.######..#",
    ".####..####..#.#.#.###.#.",
    "###...#...###..###.#.##..",
    "#####..##..##....##.#.##.",
    "#.##.##.#.#.#.##..#..####",
    "#...#....#.....#...##..#.",
    "...#..#####....##.##.##..",
    ".........#..#...###..###.",
    "###.######.#..#.#######..",
    "........#.#.##.##...#.#..",
    "#######.##.##...#.#.##...",
    "#.....#...##...##...###.#",
    "#.###.#.#...#.#.#####.###",
    "#.###.#...#...#.#.#..####",
    "#.###.#..##..##...#....#.",
    "#.....#...#.#.##.#.#####.",
    "#######.##.####...#...###",
]

NETSH_WIFI = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 00000000-0000-0000-0000-000000000000
    Physical address       : 00:11:22:33:44:55
    State                  : connected
    SSID                   : Example-WiFi
    BSSID                  : 66:77:88:99:aa:bb
    Network type           : Infrastructure
    Authentication         : WPA2-Personal
    Profile                : Example-WiFi

    Hosted network status  : Not available
"""
NETSH_CABLE = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : disconnected
    Radio status           : Hardware On

"""
NETSH_REFUSED = "Network shell commands need location permission to access WLAN information.\n"


class TestQr(unittest.TestCase):
    def test_a_known_address_draws_the_square_that_was_read_back(self):
        drawn = ["".join("#" if cell else "." for cell in row) for row in qr.matrix(KNOWN_URL)]
        self.assertEqual(drawn, KNOWN_SQUARE)

    def test_error_correction_matches_the_published_example(self):
        # "HELLO WORLD" at 1-M, the worked example in every QR tutorial.
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr._error_bytes(data, 10), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_every_size_is_square_with_its_three_corners(self):
        corner = ["#######", "#.....#", "#.###.#", "#.###.#", "#.###.#", "#.....#", "#######"]
        for length, side in ((1, 21), (14, 21), (15, 25), (26, 25), (27, 29), (42, 29), (43, 33), (62, 33),
                             (63, 37), (84, 37)):
            rows = ["".join("#" if cell else "." for cell in row) for row in qr.matrix("x" * length)]
            self.assertEqual((len(rows), {len(row) for row in rows}), (side, {side}), length)
            self.assertEqual([row[:7] for row in rows[:7]], corner, length)
            self.assertEqual([row[-7:] for row in rows[:7]], corner, length)
            self.assertEqual([row[:7] for row in rows[-7:]], corner, length)
            self.assertEqual(rows[side - 8][8], "#", length)  # the one module that is always dark

    def test_too_long_is_refused_not_drawn_wrong(self):
        with self.assertRaises(ValueError):
            qr.matrix("x" * 85)

    def test_the_picture_is_an_svg_with_one_mark_per_dark_module_and_no_script(self):
        picture = qr.svg(KNOWN_URL)
        self.assertEqual(picture.count("h1v1h-1z"), sum(row.count("#") for row in KNOWN_SQUARE))
        self.assertIn('viewBox="0 0 33 33"', picture)  # 25 modules and 4 light ones each side
        self.assertNotIn("script", picture.lower())
        self.assertNotIn(KNOWN_URL, picture)
        uri = qr.data_uri(KNOWN_URL)
        self.assertTrue(uri.startswith("data:image/svg+xml;base64,"))
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]).decode(), picture)


class TestWifiName(unittest.TestCase):
    def name_on_windows(self, text):
        with mock.patch.object(network.sys, "platform", "win32"), mock.patch.object(network, "_run", return_value=text):
            return network.wifi_name()

    def test_windows_reads_the_name_not_the_access_point(self):
        self.assertEqual(self.name_on_windows(NETSH_WIFI), "Example-WiFi")

    def test_on_a_cable_or_when_windows_refuses_there_is_no_name(self):
        self.assertIsNone(self.name_on_windows(NETSH_CABLE))
        self.assertIsNone(self.name_on_windows(NETSH_REFUSED))
        self.assertIsNone(self.name_on_windows(""))

    def test_two_adapters_on_two_networks_give_no_name_rather_than_a_guess(self):
        second = NETSH_WIFI.replace("Example-WiFi", "Example-Router")
        self.assertIsNone(self.name_on_windows(NETSH_WIFI + second))
        self.assertEqual(self.name_on_windows(NETSH_WIFI + NETSH_WIFI), "Example-WiFi")

    def test_a_name_that_came_through_damaged_is_not_shown(self):
        self.assertIsNone(self.name_on_windows(NETSH_WIFI.replace("Example-WiFi", "Example\ufffd")))
        self.assertIsNone(self.name_on_windows(NETSH_WIFI.replace("Example-WiFi", "x" * 33)))

    def test_a_command_that_fails_or_hangs_gives_no_name(self):
        import subprocess
        for error in (FileNotFoundError("netsh"), subprocess.TimeoutExpired("netsh", 3)):
            with mock.patch.object(network.sys, "platform", "win32"), \
                    mock.patch.object(network, "_run", side_effect=error):
                self.assertIsNone(network.wifi_name())

    def test_mac_reads_the_name_when_macos_gives_it(self):
        replies = ["Current Wi-Fi Network: Example-WiFi\n", "en1 is not a Wi-Fi interface.\n"]
        with mock.patch.object(network.sys, "platform", "darwin"), mock.patch.object(network, "_run", side_effect=replies):
            self.assertEqual(network.wifi_name(), "Example-WiFi")
        with mock.patch.object(network.sys, "platform", "darwin"), \
                mock.patch.object(network, "_run", return_value="You are not associated with an AirPort network.\n"):
            self.assertIsNone(network.wifi_name())

    def test_only_the_name_is_asked_for_never_the_password(self):
        source = (Path(network.__file__)).read_text(encoding="utf-8")
        for word in ("key=clear", "show profile", "find-generic-password", "security"):
            self.assertNotIn(word, source)


class TestAddresses(unittest.TestCase):
    def addresses(self, known, main):
        probe = mock.MagicMock()
        probe.__enter__.return_value.getsockname.return_value = (main, 0)
        if main is None:
            probe.__enter__.return_value.connect.side_effect = OSError("no route")
        with mock.patch.object(network.socket, "getaddrinfo", return_value=[(2, 1, 6, "", (a, 0)) for a in known]), \
                mock.patch.object(network.socket, "socket", return_value=probe):
            return network.lan_addresses()

    def test_the_address_the_computer_reaches_its_network_by_comes_first(self):
        self.assertEqual(self.addresses(["10.0.0.5", "192.168.56.1", "172.20.10.2"], "172.20.10.2"),
                         ["172.20.10.2", "10.0.0.5", "192.168.56.1"])

    def test_tailscale_never_comes_first_and_dead_addresses_are_dropped(self):
        self.assertEqual(self.addresses(["100.101.102.103", "127.0.0.1", "169.254.7.7", "192.168.8.20"], "100.101.102.103"),
                         ["192.168.8.20", "100.101.102.103"])

    def test_no_network_gives_no_address(self):
        self.assertEqual(self.addresses(["127.0.0.1"], None), [])


if __name__ == "__main__":
    unittest.main()
