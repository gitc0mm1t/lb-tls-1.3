"""Сведения о сети машины: проверка файла (mylb/hostinfo.py) и скрипт сбора (scripts/host-info.sh).

Настоящие ip и ifconfig на каждой машине отвечают по-своему, поэтому скрипту
подсовываем подставные команды с заранее известным выводом.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from mylb import hostinfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "host-info.sh")
TOOLS = ["bash", "awk", "hostname", "date", "tr", "mkdir", "chmod", "mv", "dirname"]   # что нужно скрипту

IP_OUTPUT = r"""1: lo    inet 127.0.0.1/8 scope host lo\       valid_lft forever preferred_lft forever
2: eth0    inet 10.0.1.15/24 brd 10.0.1.255 scope global dynamic eth0\       valid_lft 3000sec preferred_lft 3000sec
2: eth0    inet 10.0.1.16/24 scope global secondary eth0:1\       valid_lft forever preferred_lft forever
3: eth1    inet 10.0.2.15/20 brd 10.0.15.255 scope global eth1\       valid_lft forever preferred_lft forever
"""

MACOS_IFCONFIG_OUTPUT = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
gif0: flags=8010<POINTOPOINT,MULTICAST> mtu 1280
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether aa:bb:cc:dd:ee:ff
\tinet6 fe80::1c2b:3d4e:5f60:7a8b%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.168.50.173 netmask 0xffffff00 broadcast 192.168.50.255
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xfffffe00
"""

LINUX_IFCONFIG_OUTPUT = """eth0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500
        inet 172.31.5.9  netmask 255.255.240.0  broadcast 172.31.15.255

lo: flags=73<UP,LOOPBACK,RUNNING>  mtu 65536
        inet 127.0.0.1  netmask 255.0.0.0
"""

GOOD = {"collected_at": "2026-11-16T22:40:00Z", "hostname": "srv-1", "source": "ip",
        "items": [{"name": "eth0", "addresses": [{"address": "10.0.1.15", "prefix": 24}]}]}


class LoadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "host.json")

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_good_file(self):
        self.write(GOOD)
        self.assertEqual(hostinfo.load(self.path), GOOD)

    def test_unknown_fields_are_dropped(self):
        self.write({**GOOD, "secret": "x",
                    "items": [{"name": "eth0", "mac": "aa:bb", "addresses": [
                        {"address": "10.0.1.15", "prefix": 24, "note": "y"}]}]})
        self.assertEqual(hostinfo.load(self.path), GOOD)

    def test_missing_file(self):
        with self.assertRaises(hostinfo.HostInfoMissing):
            hostinfo.load(self.path)
        with self.assertRaises(hostinfo.HostInfoMissing):
            hostinfo.load("")

    def test_broken_files(self):
        addr = {"address": "10.0.1.15", "prefix": 24}
        bad = [
            "", "не json", "[]", '"строка"',
            {**GOOD, "collected_at": "вчера"},
            {**GOOD, "source": "nmap"},
            {**GOOD, "hostname": "a b<script>"},
            {**GOOD, "items": "eth0"},
            {**GOOD, "items": [{"name": "eth0; rm -rf /", "addresses": [addr]}]},
            {**GOOD, "items": [{"name": "eth0"}]},
            {**GOOD, "items": [{"name": "eth0", "addresses": [{"address": "999.1.1.1", "prefix": 24}]}]},
            {**GOOD, "items": [{"name": "eth0", "addresses": [{"address": "fe80::1", "prefix": 24}]}]},
            {**GOOD, "items": [{"name": "eth0", "addresses": [{"address": "10.0.1.15", "prefix": 33}]}]},
            {**GOOD, "items": [{"name": "eth0", "addresses": [{"address": "10.0.1.15", "prefix": True}]}]},
            {**GOOD, "items": [{"name": "eth0", "addresses": ["10.0.1.15"]}]},
        ]
        for data in bad:
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(hostinfo.HostInfoInvalid):
                    hostinfo.load(self.path)

    def test_too_big_file(self):
        self.write(" " * (hostinfo.MAX_SIZE + 1) + json.dumps(GOOD))
        with self.assertRaises(hostinfo.HostInfoInvalid):
            hostinfo.load(self.path)


@unittest.skipUnless(all(shutil.which(t) for t in TOOLS), "скрипту нужны bash и awk")
class ScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        # PATH только из этой папки: настоящие ip и ifconfig скрипт не найдёт.
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        for tool in TOOLS:
            os.symlink(shutil.which(tool), os.path.join(self.bin, tool))
        self.out = os.path.join(self.dir, "out", "host.json")

    def fake(self, name, output):
        """Подставная команда: печатает заранее заданный вывод."""
        data = os.path.join(self.dir, name + ".txt")
        with open(data, "w", encoding="utf-8") as f:
            f.write(output)
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"#!{shutil.which('bash')}\nwhile IFS= read -r line; do printf '%s\\n' \"$line\"; done < '{data}'\n")
        os.chmod(path, 0o755)

    def run_script(self):
        return subprocess.run([shutil.which("bash"), SCRIPT, self.out], env={"PATH": self.bin},
                              capture_output=True, text=True, timeout=30)

    def items(self):
        return {i["name"]: [(a["address"], a["prefix"]) for a in i["addresses"]]
                for i in hostinfo.load(self.out)["items"]}     # load заодно проверяет формат файла

    def test_linux_ip(self):
        self.fake("ip", IP_OUTPUT)
        self.fake("ifconfig", "этот вывод не должен понадобиться\n")      # ip важнее
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.items(), {"lo": [("127.0.0.1", 8)],
                                        "eth0": [("10.0.1.15", 24), ("10.0.1.16", 24)],
                                        "eth1": [("10.0.2.15", 20)]})
        self.assertEqual(hostinfo.load(self.out)["source"], "ip")
        self.assertEqual(res.stdout.splitlines(),
                         ["lo 127.0.0.1/8", "eth0 10.0.1.15/24", "eth0 10.0.1.16/24", "eth1 10.0.2.15/20"])

    def test_macos_ifconfig(self):
        self.fake("ifconfig", MACOS_IFCONFIG_OUTPUT)
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        # gif0 без IPv4-адреса в список не попадает; IPv6-адреса пропускаются
        self.assertEqual(self.items(), {"lo0": [("127.0.0.1", 8)], "en0": [("192.168.50.173", 24)],
                                        "utun3": [("10.8.0.2", 23)]})
        self.assertEqual(hostinfo.load(self.out)["source"], "ifconfig")

    def test_linux_ifconfig_with_dotted_mask(self):
        self.fake("ifconfig", LINUX_IFCONFIG_OUTPUT)
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.items(), {"eth0": [("172.31.5.9", 20)], "lo": [("127.0.0.1", 8)]})

    def test_no_addresses_gives_empty_list(self):
        self.fake("ip", "")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(hostinfo.load(self.out)["items"], [])

    def test_file_is_readable_by_other_users(self):
        self.fake("ip", IP_OUTPUT)
        self.run_script()
        self.assertEqual(os.stat(self.out).st_mode & 0o777, 0o644)     # в контейнере читает пользователь app

    def test_no_tools_is_clear_error(self):
        res = self.run_script()
        self.assertEqual(res.returncode, 1)
        self.assertIn("ifconfig", res.stderr)
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
