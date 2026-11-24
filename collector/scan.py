"""Read-only SAS surface scans. Run as the service behind sas-monitor-scan.socket."""
import contextlib
import copy
import errno
import fcntl
import json
import mmap
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import threading
import time
import uuid

# python -I omits the script directory. This directory is installed root-owned.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect

RESULTS = Path("/var/lib/sas-monitor-scan/results.json")
CHUNK = 1024 * 1024
ERROR_LIMIT = 64
READ_ERRORS = {errno.EIO, errno.EREMOTEIO, errno.EILSEQ}
ACTIVE = {"starting", "running", "stopping"}
DISK_ID = re.compile(r"[0-9a-f]{64}")
JOB_ID = re.compile(r"[0-9a-f]{32}")


class ScanError(Exception):
    """A stable error code for the dashboard."""


def snapshot_disk(key):
    try:
        snapshot = json.loads(collect.SNAPSHOT.read_bytes())
    except (OSError, ValueError):
        raise ScanError("snapshot")
    matches = [disk for disk in snapshot.get("disks", []) if collect.scan_key(disk) == key]
    if snapshot.get("state") != "ok" or len(matches) != 1:
        raise ScanError("disk_changed")
    disk = matches[0]
    if disk["controllerType"] != "scsi":
        raise ScanError("raid")
    return disk


def wwn_hex(value):
    value = re.sub(r"^(?:0x|naa\.|eui\.)", "", str(value).lower().strip())
    return value if re.fullmatch(r"(?:[0-9a-f]{8}){2,4}", value) and int(value, 16) else None


def matches_identity(disk, scsi):
    if disk.get("wwn"):
        wanted = wwn_hex(disk["wwn"])
        if not wanted:
            return False
        try:
            if wanted == wwn_hex((scsi / "wwid").read_text()):
                return True
        except FileNotFoundError:
            pass
        # Some kernels expose a T10 name in wwid. Match only LU-associated
        # binary EUI/NAA descriptors, never a target port's SAS address.
        try:
            page = (scsi / "vpd_pg83").read_bytes()
        except FileNotFoundError:
            return False
        end = 4 + int.from_bytes(page[2:4], "big")
        if len(page) < 4 or page[1] != 0x83 or end > len(page):
            return False
        pos = 4
        while pos + 4 <= end:
            length = page[pos + 3]
            after = pos + 4 + length
            if after > end:
                break
            if (page[pos] & 15 == 1 and page[pos + 1] & 0x3f in (2, 3)
                    and page[pos + 4:after].hex() == wanted):
                return True
            pos = after
        return False
    # SCSI VPD page 0x80 contains the device's unit serial number.
    page = (scsi / "vpd_pg80").read_bytes()
    length = int.from_bytes(page[2:4], "big")
    if len(page) < 4 or page[1] != 0x80 or length == 0 or len(page) < 4 + length:
        return False
    serial = page[4:4 + length].decode("ascii", errors="replace").strip(" \0")
    product = (scsi / "model").read_text().strip()
    vendor = (scsi / "vendor").read_text().strip()
    model = " ".join(str(disk.get("model") or "").split())
    return bool(serial and serial == disk.get("serial") and model in
                {" ".join(product.split()), " ".join((vendor + " " + product).split())})


def verify_online(block, sequence):
    try:
        same = int((block / "diskseq").read_text()) == sequence
        online = (block / "device/state").read_text().strip() == "running"
    except (OSError, ValueError):
        raise ScanError("offline")
    if not same or not online:
        raise ScanError("offline")


@contextlib.contextmanager
def open_disk(disk):
    name = collect.scan_block(disk)
    if not name:
        raise ScanError("path")
    with contextlib.ExitStack() as stack:
        fd = os.open("/dev/" + name, os.O_RDONLY | os.O_DIRECT | os.O_CLOEXEC | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        details = os.fstat(fd)
        if not stat.S_ISBLK(details.st_mode):
            raise ScanError("path")
        devnum = f"{os.major(details.st_rdev)}:{os.minor(details.st_rdev)}"
        block = Path("/sys/dev/block", devnum).resolve(strict=True)
        if block.name != name or (block / "partition").exists():
            raise ScanError("disk_changed")
        # Bind the open descriptor to this sysfs generation even if sdX is reused.
        try:
            sequence, = struct.unpack("=Q", fcntl.ioctl(fd, 0x80081280, bytes(8)))  # BLKGETDISKSEQ
        except OSError as error:
            raise ScanError("diskseq") from error

        def check_online():
            verify_online(block, sequence)

        check_online()
        try:
            verified = matches_identity(disk, block / "device")
        except OSError:
            verified = False
        if not verified:
            raise ScanError("disk_changed")
        sector, = struct.unpack("=I", fcntl.ioctl(fd, 0x1268, bytes(4)))  # BLKSSZGET
        size = os.lseek(fd, 0, os.SEEK_END)
        if (sector not in (512, 1024, 2048, 4096) or sector != disk.get("logicalBlock")
                or size <= 0 or size != disk.get("bytes") or size % sector):
            raise ScanError("geometry")
        check_online()
        with mmap.mmap(-1, CHUNK) as buffer, memoryview(buffer) as view:
            def read(offset, length):
                with view[:length] as destination:
                    return os.preadv(fd, [destination], offset)
            yield size, sector, read, check_online


def scan_surface(result, read, check_online, cancelled, progress):
    """Classify sectors in ascending LBA order, subdividing only a failed read."""
    sector = result["sectorBytes"]
    total = result["totalSectors"]

    def inspect(first, length):
        while length:
            if cancelled():
                raise ScanError("stopped")
            try:
                received = read(first * sector, length * sector)
            except OSError as error:
                if error.errno not in READ_ERRORS:
                    raise
                check_online()
                if length > 1:
                    half = length // 2
                    inspect(first, half)
                    first += half
                    length -= half
                    continue
                # Confirm the individual LBA before recording an unreadable sector.
                if cancelled():
                    raise ScanError("stopped")
                try:
                    received = read(first * sector, sector)
                except OSError as retry:
                    if retry.errno not in READ_ERRORS:
                        raise
                    check_online()
                    ranges = result["ranges"]
                    if ranges and ranges[-1][1] == first - 1:
                        ranges[-1][1] = first
                    else:
                        ranges.append([first, first])
                    result["unreadable"] += 1
                    received = sector
            if received <= 0 or received > length * sector or received % sector:
                raise ScanError("short_read")
            advanced = received // sector
            first += advanced
            length -= advanced
            result["checkedSectors"] = first
            progress()
            if result["unreadable"] >= ERROR_LIMIT and first < total:
                raise ScanError("limited")

    while result["checkedSectors"] < total:
        first = result["checkedSectors"]
        inspect(first, min(CHUNK // sector, total - first))
    check_online()


class Scanner:
    def __init__(self, path=RESULTS):
        self.path = path
        self.lock = threading.Lock()
        self.active = None
        self.stop = threading.Event()
        try:
            self.results = json.loads(path.read_bytes())
        except FileNotFoundError:
            self.results = {}
        recovered = False
        for record in self.results.values():
            if record["state"] in ACTIVE:
                record.update(state="interrupted", error="restart", endedAt=int(time.time()))
                recovered = True
        if recovered:
            self.save()

    def save(self):
        collect.write_json(self.path, self.results, 0o600)

    def status(self, key):
        current = self.results.get(self.active) or {}
        active = {k: current[k] for k in ("disk", "job", "label")} if current else None
        return {"result": copy.deepcopy(self.results.get(key)), "active": active}

    def command(self, words):
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            return 400, {"error": "invalid"}
        if len(words) not in (2, 3) or not DISK_ID.fullmatch(words[1]):
            return 400, {"error": "invalid"}
        action, key = words[:2]
        with self.lock:
            if action == "status" and len(words) == 2:
                return 200, self.status(key)
            if action == "stop" and len(words) == 3 and JOB_ID.fullmatch(words[2]):
                record = self.results.get(key)
                if self.active != key or not record or words[2] != record["job"]:
                    return 409, {"error": "job_changed"}
                self.stop.set()
                record["state"] = "stopping"
                return 202, self.status(key)
            if action != "start" or len(words) != 2:
                return 400, {"error": "invalid"}
            if self.active:
                return 409, {"error": "busy"}
            disk = snapshot_disk(key)
            if not collect.scan_block(disk):
                raise ScanError("path")
            label = " · ".join(str(x) for x in (disk.get("model"), disk.get("serial"), disk["device"]) if x)
            record = dict(disk=key, job=uuid.uuid4().hex, label=label, state="starting",
                          startedAt=int(time.time()), endedAt=None, error=None, checkedSectors=0,
                          totalSectors=0, sectorBytes=0, unreadable=0, ranges=[])
            previous = self.results.get(key)
            self.results[key] = record
            try:
                self.save()
            except OSError:
                if previous is None:
                    del self.results[key]
                else:
                    self.results[key] = previous
                raise
            self.active = key
            self.stop.clear()
            threading.Thread(target=self.run, args=(disk, copy.deepcopy(record)), daemon=True).start()
            return 202, self.status(key)

    def run(self, disk, result):
        key = result["disk"]
        last_update, last_save, last_check = 0.0, time.monotonic(), 0.0
        try:
            if self.stop.is_set():
                raise ScanError("stopped")
            with open_disk(disk) as (size, sector, read, check_online):
                result.update(totalSectors=size // sector, sectorBytes=sector, state="running")

                def progress():
                    nonlocal last_update, last_save, last_check
                    now = time.monotonic()
                    if now - last_update < 1:
                        return
                    if now - last_check >= 5:
                        check_online()
                        last_check = now
                    with self.lock:
                        published = copy.deepcopy(result)
                        if self.stop.is_set():
                            published["state"] = "stopping"
                        self.results[key] = published
                        if now - last_save >= 60:
                            self.save()
                            last_save = now
                    last_update = now

                progress()
                scan_surface(result, read, check_online, self.stop.is_set, progress)
                result["state"] = "complete"
        except ScanError as error:
            code = str(error)
            result["state"] = code if code in ("limited", "stopped") else "interrupted"
            result["error"] = code
        except Exception as error:
            print(f"Scan {result['job']}: {error}", file=sys.stderr, flush=True)
            result.update(state="interrupted", error="io")
        finally:
            result["endedAt"] = int(time.time())
            with self.lock:
                self.results[key] = result
                self.active = None
                try:
                    self.save()
                except OSError as error:
                    result["saveFailed"] = True
                    print(f"Cannot persist scan result: {error}", file=sys.stderr, flush=True)


def handle_request(conn, scanner):
    conn.settimeout(2)
    try:
        with conn.makefile("rb") as reader:
            line = reader.readline(256)
        if not line.endswith(b"\n"):
            raise ValueError("incomplete request")
        status, data = scanner.command(json.loads(line))
    except (ValueError, TypeError):
        status, data = 400, {"error": "invalid"}
    except ScanError as error:
        status, data = 409, {"error": str(error)}
    except OSError as error:
        print(f"Scan service: {error}", file=sys.stderr, flush=True)
        status, data = 503, {"error": "service"}
    try:
        conn.sendall(json.dumps([status, data]).encode() + b"\n")
    except OSError:
        pass




def serve(listener, scanner):
    while True:
        conn, _ = listener.accept()
        with conn:
            handle_request(conn, scanner)


if __name__ == "__main__":
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        sys.exit("Start via sas-monitor-scan.socket")
    with socket.socket(fileno=3) as listener:
        serve(listener, Scanner())
