"""Collect only SAS telemetry and publish one atomic snapshot."""
import concurrent.futures
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SMARTCTL = next((p for p in (Path("/usr/sbin/smartctl"), Path("/usr/bin/smartctl")) if p.is_file()), None)
SNAPSHOT = Path("/run/sas-monitor/snapshot.json")
BASELINES = Path("/var/lib/sas-monitor/non-medium.json")
ALERT_WINDOW = 24 * 60 * 60
BASELINE_RETENTION = 30 * ALERT_WINDOW
DIRECT = re.compile(r"/dev/(?:sd[a-z]+|sg[0-9]+|bsg/[0-9]+:[0-9]+:[0-9]+:[0-9]+)")
RAID_DEVICE = re.compile(r"/dev/(?:sd[a-z]+|bus/[0-9]+)")
RAID_TYPE = re.compile(r"megaraid,[0-9]{1,4}")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def smartctl(*args, timeout):
    result = subprocess.run(
        [str(SMARTCTL), *args], capture_output=True, timeout=timeout, env=ENV,
        encoding="utf-8", errors="replace",
    )
    return json.loads(result.stdout), result.returncode


def supported(name, kind):
    return bool(
        (kind == "scsi" and DIRECT.fullmatch(name))
        or (RAID_TYPE.fullmatch(kind) and RAID_DEVICE.fullmatch(name))
    )


def first_number(lines, pattern):
    for line in lines:
        match = re.match(pattern, line)
        if match:
            return int(match[1])
    return None


def read_disk(data, status, name, kind):
    if data.get("device", {}).get("protocol", "").upper() != "SCSI":
        return None
    if data.get("device_type", {}).get("scsi_value", 0) not in (0, 0x14):
        return None
    lines = data.get("smartctl", {}).get("output", [])
    transport = data.get("scsi_transport_protocol", {})
    if transport.get("value") != 6 and not any(re.match(r"Transport protocol:\s*SAS\b", line) for line in lines):
        return None

    defects = first_number(lines, r"Elements in grown defect list:\s*(\d+)")
    non_medium = first_number(lines, r"Non-medium error count:\s*(\d+)")
    errors = {}
    for operation in ("read", "write", "verify"):
        counter = data.get("scsi_error_counter_log", {}).get(operation, {})
        errors[operation] = {
            "corrected": counter.get("total_errors_corrected"),
            "uncorrected": counter.get("total_uncorrected_errors"),
        }

    return {
        "device": name, "controllerType": kind,
        "model": data.get("model_name") or data.get("scsi_model_name") or
                 " ".join(filter(None, [data.get("scsi_vendor"), data.get("scsi_product")])),
        "serial": data.get("serial_number"),
        "wwn": data.get("logical_unit_id"),
        "firmware": data.get("firmware_version") or data.get("scsi_revision"),
        "bytes": data.get("user_capacity", {}).get("bytes"),
        "logicalBlock": data.get("logical_block_size"),
        "physicalBlock": data.get("physical_block_size"),
        "temperature": data.get("temperature", {}).get("current"),
        "hours": data.get("power_on_time", {}).get("hours"),
        "grownDefects": defects, "nonMedium": non_medium, "errors": errors,
        "smartPassed": data.get("smart_status", {}).get("passed"), "exitStatus": status,
        "selfTests": [line.strip() for line in lines if re.match(r"\s*#\s*\d+\s+", line)][:20],
        "report": "\n".join(lines)[:24000],
    }


def probe(candidate):
    name, kind = candidate
    try:
        data, status = smartctl("--json=o", "-x", "-d", kind, name, timeout=12)
    except subprocess.TimeoutExpired:
        return None, {"device": name, "type": kind, "message": "读取超时"}
    except (OSError, ValueError):
        return None, {"device": name, "type": kind, "message": "无法读取 SMART 输出"}
    disk = read_disk(data, status, name, kind)
    if disk is None and status & 3:
        return None, {"device": name, "type": kind, "message": "无法打开设备"}
    return disk, None


def collect():
    if not SMARTCTL:
        return {"state": "missing_tool", "disks": []}
    try:
        scan, status = smartctl("--scan-open", "--json", timeout=45)
    except subprocess.TimeoutExpired:
        return {"state": "scan_timeout", "disks": []}
    except (OSError, ValueError):
        return {"state": "scan_failed", "disks": []}

    devices = scan.get("devices", [])
    candidates = []
    for device in devices:
        name, kind = device.get("name", ""), device.get("type", "")
        if supported(name, kind) and (name, kind) not in candidates:
            candidates.append((name, kind))
    if status and not candidates:
        return {"state": "scan_failed", "disks": []}

    disks, issues, seen = [], [], set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for disk, issue in pool.map(probe, candidates):
            if issue:
                issues.append(issue)
            if disk:
                identity = disk_identity(disk)
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                disks.append(disk)
    return {"state": "ok", "disks": disks, "issues": issues, "candidates": len(candidates)}


def disk_identity(disk):
    if disk["wwn"]:
        return "wwn:" + disk["wwn"]
    if disk["serial"]:
        return "serial:" + json.dumps([disk["model"], disk["serial"]], ensure_ascii=False)
    return None


def measure_non_medium(disk, baselines, timestamp):
    identity = disk_identity(disk)
    current = disk["nonMedium"]
    if identity is None or current is None or disk["exitStatus"] & 3:
        return None, None

    previous = baselines.get(identity)
    delta, increased_at = None, None
    if previous and current >= previous["count"]:
        delta = current - previous["count"]
        increased_at = timestamp if delta else previous["increasedAt"]
    baselines[identity] = {"count": current, "seenAt": timestamp, "increasedAt": increased_at}
    return delta, increased_at


def apply_health(data, baselines, timestamp):
    for disk in data["disks"]:
        delta, increased_at = measure_non_medium(disk, baselines, timestamp)
        disk["nonMediumDelta"] = delta
        disk["nonMediumIncreasedAt"] = (
            datetime.datetime.fromtimestamp(increased_at, datetime.timezone.utc).isoformat()
            if increased_at is not None else None
        )

        reasons = []
        if (disk["grownDefects"] or 0) > 0:
            reasons.append("grown_defects")
        if any((entry["uncorrected"] or 0) > 0 for entry in disk["errors"].values()):
            reasons.append("uncorrected")
        status = disk["exitStatus"]
        if status & 128:
            reasons.append("self_test")
        if increased_at is not None and 0 <= timestamp - increased_at < ALERT_WINDOW:
            reasons.append("non_medium_increased")
        passed = disk["smartPassed"]
        if passed is False or status & 8:
            health = "failed"
            reasons.insert(0, "smart_failed")
        elif status & 3 or passed is not True:
            health = "unknown"
            reasons.insert(0, "smart_unknown")
        else:
            health = "attention" if reasons else "passed"
        disk["health"] = health
        disk["healthReasons"] = reasons


def load_baselines(timestamp):
    try:
        contents = json.loads(BASELINES.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        print(f"Unable to load SAS counter baselines: {error}", file=sys.stderr)
        return {}
    if not isinstance(contents, dict):
        return {}
    cutoff = timestamp - BASELINE_RETENTION
    return {
        key: entry for key, entry in contents.items()
        if isinstance(entry, dict) and type(entry.get("count")) is int and entry["count"] >= 0
        and type(entry.get("seenAt")) is int and cutoff <= entry["seenAt"] <= timestamp
        and "increasedAt" in entry and (entry["increasedAt"] is None or
             (type(entry["increasedAt"]) is int and 0 <= entry["increasedAt"] <= entry["seenAt"]))
    }


def write_json(path, data, mode):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        staging = Path(temp.name)
        try:
            json.dump(data, temp, ensure_ascii=True)
            temp.flush()
            os.fchmod(temp.fileno(), mode)
            os.fsync(temp.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)


def publish(data):
    now = datetime.datetime.now(datetime.timezone.utc)
    data["collectedAt"] = now.isoformat()
    if data["state"] == "ok":
        timestamp = int(now.timestamp())
        baselines = load_baselines(timestamp)
        apply_health(data, baselines, timestamp)
        try:
            write_json(BASELINES, baselines, 0o600)
        except OSError as error:
            print(f"Unable to save SAS counter baselines: {error}", file=sys.stderr)
            data["baselineUnavailable"] = True
    write_json(SNAPSHOT, data, 0o640)


if __name__ == "__main__":
    publish(collect())
