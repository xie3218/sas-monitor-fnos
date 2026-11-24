"""Run with python3 -m unittest discover -s tests. No hardware or privileges required."""
import contextlib
import errno
import http.client
import http.server
import json
import mmap
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
sys.path.insert(0, str(ROOT / "fpk/app"))
import scan
import collect
import server

DISK = dict(device="/dev/sda", controllerType="scsi", model="DEMO SAS", serial="EXAMPLE",
            wwn="0x5000000000000001", logicalBlock=512, bytes=8192)
KEY = collect.scan_key(DISK)


def empty(sector=512, total=8, first=0):
    return dict(checkedSectors=first, sectorBytes=sector, totalSectors=total, unreadable=0, ranges=[])


def surface(record, read, check=lambda: None, stop=lambda: False):
    scan.scan_surface(record, read, check, stop, lambda: None)


class Reading(unittest.TestCase):
    def test_read_long_offsets_and_short_aligned_reads(self):
        first = 2**32 + 200
        for sector in (512, 4096):
            with self.subTest(sector=sector):
                record = empty(sector, first + 20, first)
                offsets = []
                def read(offset, size):
                    offsets.append(offset)
                    return min(3 * sector, size)
                surface(record, read)
                self.assertEqual(offsets, [(first + n) * sector for n in range(0, 20, 3)])
                self.assertEqual(record["checkedSectors"], first + 20)
                self.assertFalse(record["ranges"])

    def test_actual_direct_read_tempfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture"
            contents = bytes(range(256)) * (scan.CHUNK // 256 + 2)
            path.write_bytes(contents)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
            except OSError as error:
                if error.errno in (errno.EINVAL, errno.EOPNOTSUPP):
                    self.skipTest("Filesystem does not support O_DIRECT")
                raise
            with os.fdopen(fd, "rb", buffering=0), mmap.mmap(-1, scan.CHUNK) as buf, memoryview(buf) as view:
                record = empty(512, len(contents) // 512)
                def read(offset, size):
                    with view[:size] as target:
                        got = os.preadv(fd, [target], offset)
                        self.assertEqual(target[:got], contents[offset:offset + got])
                    return got
                surface(record, read)
                self.assertEqual(record["checkedSectors"], record["totalSectors"])

    def test_sector_errors_and_transient_failure(self):
        record = empty(total=28)
        failures = {1, 2, 13, 27}
        singles = []
        def read(offset, size):
            first, length = offset // 512, size // 512
            if length == 1:
                singles.append(first)
            if (any(first <= bad < first + length for bad in failures)
                    or (first <= 8 < first + length and (length > 1 or singles.count(8) == 1))):
                raise OSError(errno.EIO, "injected")
            return size
        surface(record, read)
        self.assertEqual(record["ranges"], [[1, 2], [13, 13], [27, 27]])
        self.assertEqual(record["checkedSectors"], 28)
        self.assertEqual(record["unreadable"], 4)
        self.assertTrue(all(singles.count(i) == 2 for i in failures | {8}))

    def test_stop_offline_or_invalid_short_read_does_not_add_bad_sectors(self):
        def io(offset, size):
            raise OSError(errno.EIO, "injected")
        def offline():
            raise scan.ScanError("offline")
        for read, check, stop, reason in [
                (io, offline, lambda: False, "offline"),
                (io, lambda: None, lambda: True, "stopped"),
                (lambda o, n: 0, lambda: None, lambda: False, "short_read"),
                (lambda o, n: 1, lambda: None, lambda: False, "short_read")]:
            record = empty()
            with self.assertRaisesRegex(scan.ScanError, reason):
                surface(record, read, check, stop)
            self.assertEqual(record["unreadable"], 0)
            self.assertEqual(record["checkedSectors"], 0)

    def test_stop_at_error_limit_and_keep_unscanned_tail(self):
        record = empty(total=scan.ERROR_LIMIT + 9)
        def bad(offset, size):
            raise OSError(errno.EIO, "injected")
        with self.assertRaisesRegex(scan.ScanError, "limited"):
            surface(record, bad)
        self.assertEqual(record["ranges"], [[0, scan.ERROR_LIMIT - 1]])
        self.assertEqual(record["checkedSectors"], scan.ERROR_LIMIT)
        self.assertEqual(record["unreadable"], scan.ERROR_LIMIT)


class Target(unittest.TestCase):
    def test_reused_device_number_and_offline_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = Path(tmp)
            (block / "device").mkdir()
            (block / "device/state").write_text("running")
            (block / "diskseq").write_text("24")
            scan.verify_online(block, 24)
            (block / "diskseq").write_text("25")
            with self.assertRaisesRegex(scan.ScanError, "offline"):
                scan.verify_online(block, 24)
            (block / "device/state").write_text("offline")
            with self.assertRaisesRegex(scan.ScanError, "offline"):
                scan.verify_online(block, 25)

    def test_wwn_and_serial_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            scsi = Path(tmp)
            (scsi / "wwid").write_text("naa.5000000000000001\n")
            self.assertTrue(scan.matches_identity(DISK, scsi))
            self.assertFalse(scan.matches_identity(dict(DISK, wwn="0x5000000000000002"), scsi))
            (scsi / "wwid").write_text("t10.example")
            ident = bytes.fromhex(DISK["wwn"][2:])
            (scsi / "vpd_pg83").write_bytes(bytes([0, 0x83, 0, 12, 1, 0x13, 0, 8]) + ident)
            self.assertFalse(scan.matches_identity(DISK, scsi), "A matching port address is insufficient")
            (scsi / "vpd_pg83").write_bytes(bytes([0, 0x83, 0, 12, 1, 0x03, 0, 8]) + ident)
            self.assertTrue(scan.matches_identity(DISK, scsi))
            no_wwn = dict(DISK, wwn=None)
            (scsi / "vpd_pg80").write_bytes(b"\x00\x80\x00\x07EXAMPLE")
            (scsi / "vendor").write_text("DEMO ")
            (scsi / "model").write_text("SAS   ")
            self.assertTrue(scan.matches_identity(no_wwn, scsi))
            self.assertFalse(scan.matches_identity(dict(no_wwn, model="OTHER"), scsi))
            (scsi / "vpd_pg80").write_bytes(b"\x00\x80\x00\x07CHANGED")
            self.assertFalse(scan.matches_identity(no_wwn, scsi))

    def test_only_physical_sas_block_or_unique_generic_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / "devices/host0/port-0:1/end_device-0:1/target0:0:0/0:0:0:0"
            block = device / "block/sda"
            block.mkdir(parents=True)
            (block / "device").symlink_to(device)
            cls = root / "class"
            (cls / "block").mkdir(parents=True)
            (cls / "block/sda").symlink_to(block)
            (cls / "sas_end_device/end_device-0:1").mkdir(parents=True)
            for sc, leaf, source in [("scsi_generic", "sg1", "/dev/sg1"), ("bsg", "0:0:0:0", "/dev/bsg/0:0:0:0")]:
                entry = cls / sc / leaf
                entry.mkdir(parents=True)
                (entry / "device").symlink_to(device)
                self.assertEqual(collect.scan_block(dict(DISK, device=source), root), "sda")
            self.assertEqual(collect.scan_block(DISK, root), "sda")
            self.assertIsNone(collect.scan_block(dict(DISK, controllerType="megaraid,1"), root))
            self.assertIsNone(collect.scan_block(dict(DISK, device="/dev/sda1"), root))
            (cls / "sas_end_device/end_device-0:1").rmdir()
            self.assertIsNone(collect.scan_block(DISK, root))


class ScanIntegration(unittest.TestCase):
    def test_api_worker_cancel_persistence_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps({"state": "ok", "disks": [DISK]}))
            stack.enter_context(patch.object(collect, "SNAPSHOT", snapshot))
            stack.enter_context(patch.object(collect, "scan_block", return_value="sda"))
            state = root / "results.json"
            service = scan.Scanner(state)
            started, release = threading.Event(), threading.Event()

            @contextlib.contextmanager
            def fake_disk(disk):
                def read(offset, length):
                    started.set()
                    if not release.wait(3):
                        raise TimeoutError()
                    return 512
                yield DISK["bytes"], 512, read, lambda: None
            stack.enter_context(patch.object(scan, "open_disk", fake_disk))
            address = str(root / "scan.sock")
            stack.enter_context(patch.object(server, "SCAN_SOCKET", address))
            try:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                endpoint = address
            except PermissionError:
                # Some CI environments allow only loopback TCP. Exercise the same framing.
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                endpoint = ("127.0.0.1", 0)
                class LoopbackClient(socket.socket):
                    def __init__(self, family, kind):
                        super().__init__(socket.AF_INET, kind)
                    def connect(self, path):
                        if path != address:
                            raise ValueError("unexpected scan socket")
                        super().connect(listener.getsockname())
                stack.enter_context(patch.object(server, "socket", types.SimpleNamespace(
                    socket=LoopbackClient, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM)))
            stack.enter_context(listener)
            listener.bind(endpoint)
            listener.listen(4)
            done = threading.Event()
            def serve():
                while not done.is_set():
                    peer, _ = listener.accept()
                    with peer:
                        if not done.is_set():
                            scan.handle_request(peer, service)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            def close():
                done.set()
                with socket.socket(listener.family, socket.SOCK_STREAM) as wake:
                    wake.connect(listener.getsockname())
                thread.join(3)
            stack.callback(close)
            httpd = stack.enter_context(http.server.HTTPServer(("127.0.0.1", 0), server.Handler))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            stack.callback(httpd.shutdown)
            admin = {"X-Trim-Isadmin": "true", "X-Trim-Userid": "1000"}
            write = {**admin, "X-SAS-Scan": "1", "Sec-Fetch-Site": "same-origin"}
            def request(method, suffix="", headers=None, key=KEY):
                client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=4)
                try:
                    client.request(method, server.BASE + "/api/scan/" + key + suffix, headers=headers or {})
                    r = client.getresponse()
                    content = r.read()
                    return r.status, json.loads(content) if r.headers.get_content_type() == "application/json" else {}
                finally:
                    client.close()

            self.assertEqual(request("GET")[0], 403)
            self.assertEqual(request("POST", "/start", admin)[0], 403)
            self.assertEqual(request("POST", "/start", {**write, "Sec-Fetch-Site": "cross-site"})[0], 403)
            self.assertEqual(request("GET", "/start", admin)[0], 404)
            self.assertEqual(request("GET", headers=admin), (200, {"result": None, "active": None}))
            code, data = request("POST", "/start", write)
            self.assertEqual(code, 202)
            job = data["active"]["job"]
            self.assertTrue(started.wait(2))
            self.assertEqual(request("POST", "/start", write)[0], 409)
            self.assertEqual(request("POST", "/stop/" + "0" * 32, write)[0], 409)
            self.assertEqual(request("POST", "/stop/" + job, write, key="0" * 64)[0], 409)
            self.assertEqual(request("POST", "/stop/" + job, write)[1]["result"]["state"], "stopping")
            release.set()
            for _ in range(100):
                status, data = request("GET", headers=admin)
                if not data["active"]:
                    break
                time.sleep(.01)
            self.assertEqual(data["result"]["state"], "stopped")
            self.assertLess(data["result"]["checkedSectors"], data["result"]["totalSectors"])
            self.assertEqual(scan.Scanner(state).command(["status", KEY])[1]["result"], data["result"])
            saved = json.loads(state.read_bytes())
            saved[KEY]["state"] = "running"
            state.write_text(json.dumps(saved))
            recovered = scan.Scanner(state)
            self.assertEqual(recovered.command(["status", KEY])[1]["result"]["state"], "interrupted")
            self.assertIsNone(recovered.active)


if __name__ == "__main__":
    unittest.main()
