import getpass
import platform
import re
import shutil
import socket
import subprocess
from pathlib import Path

import psutil


UNKNOWN = "Неизвестно"
_SYS = Path("/sys")
_PROC = Path("/proc")


def _read_text(path, default=UNKNOWN):
    try:
        value = Path(path).read_text(encoding="utf-8", errors="replace").strip()
        return value or default
    except (OSError, UnicodeError):
        return default


def _dmi_value(name):
    return _read_text(_SYS / "class" / "dmi" / "id" / name)


def _capacity_text(size_bytes):
    size_gb_decimal = round(size_bytes / 1_000_000_000, 2)
    size_gb_binary = round(size_bytes / (1024**3), 2)
    if size_gb_decimal >= 1000:
        decimal = f"{round(size_gb_decimal / 1000, 2)} ТБ ({size_gb_decimal} ГБ)"
    else:
        decimal = f"{size_gb_decimal} ГБ"
    return decimal, f"{size_gb_binary} ГБ"


def _manufacturer(model, vendor=UNKNOWN):
    if vendor not in (UNKNOWN, "", "0x0000"):
        return vendor
    for brand in (
        "samsung", "western digital", "kingston", "crucial", "seagate",
        "toshiba", "intel", "corsair", "adata", "lexar", "sandisk", "wd",
    ):
        if brand in model.lower():
            return brand.upper() if len(brand) <= 2 else brand.title()
    return model.split()[0] if model != UNKNOWN else UNKNOWN


def get_network_hardware_info():
    ip_by_interface = {}
    for interface, addresses in psutil.net_if_addrs().items():
        ipv4 = next(
            (address.address for address in addresses if address.family == socket.AF_INET),
            None,
        )
        mac = next(
            (address.address for address in addresses if address.family == psutil.AF_LINK),
            None,
        )
        if mac and ipv4 and not ipv4.startswith(("127.", "169.254.")):
            ip_by_interface[interface] = (ipv4, mac)

    devices = []
    excluded = ("lo", "docker", "veth", "virbr", "br-", "tun", "tap", "wg")
    for name, (ip, mac) in ip_by_interface.items():
        name_lower = name.lower()
        if any(name_lower == item or name_lower.startswith(item) for item in excluded):
            continue

        interface_path = _SYS / "class" / "net" / name
        wireless = (interface_path / "wireless").exists()
        driver_path = interface_path / "device" / "driver"
        try:
            driver = driver_path.resolve(strict=True).name
        except OSError:
            driver = UNKNOWN

        devices.append({
            "name": name,
            "model": driver,
            "type": "Wi-Fi" if wireless else "Ethernet",
            "ip": ip,
            "mac": mac,
        })
    return devices


def get_motherboard():
    return {
        "manufacturer": _dmi_value("board_vendor"),
        "model": _dmi_value("board_name"),
        "name": _dmi_value("board_name"),
        "serial": _dmi_value("board_serial"),
        "uuid": _dmi_value("product_uuid"),
    }


def get_RAMs():
    meminfo = {}
    for line in _read_text(_PROC / "meminfo", "").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            meminfo[key] = value.strip()

    try:
        total_kb = int(meminfo.get("MemTotal", "0 kB").split()[0])
    except (ValueError, IndexError):
        total_kb = 0
    if not total_kb:
        try:
            total_kb = psutil.virtual_memory().total // 1024
        except (OSError, AttributeError):
            return []

    return [{
        "manufacturer": UNKNOWN,
        "model": "Оперативная память (суммарно)",
        "capacity_gb": round(total_kb / (1024**2), 2),
        "speed_mhz": UNKNOWN,
        "slot": UNKNOWN,
        "serial": UNKNOWN,
    }]


def get_storages():
    storage_devices = []
    block_path = _SYS / "block"
    try:
        block_devices = sorted(block_path.iterdir())
    except OSError:
        return storage_devices

    ignored_prefixes = ("loop", "ram", "zram", "dm-", "md")
    for device in block_devices:
        name = device.name
        if name.startswith(ignored_prefixes) or (device / "partition").exists():
            continue

        try:
            size_bytes = int(_read_text(device / "size", "0")) * 512
        except ValueError:
            continue
        if size_bytes <= 0:
            continue

        model = _read_text(device / "device" / "model")
        vendor = _read_text(device / "device" / "vendor")
        manufacturer = _manufacturer(model, vendor)
        model_lower = model.lower()
        rotational = _read_text(device / "queue" / "rotational", "1")
        if name.startswith("nvme") or any(token in model_lower for token in ("nvme", "m.2", "pcie")):
            drive_type = "NVMe SSD"
        elif rotational == "0":
            drive_type = "SATA SSD"
        else:
            drive_type = "HDD"

        capacity_decimal, capacity_binary = _capacity_text(size_bytes)
        transport = _read_text(device / "device" / "transport")
        storage_devices.append({
            "manufacturer": manufacturer,
            "model": model,
            "type": drive_type,
            "capacity_decimal": capacity_decimal,
            "capacity_binary": capacity_binary,
            "capacity_bytes": size_bytes,
            "interface": transport,
            "serial": _read_text(device / "device" / "serial"),
        })
    return storage_devices


def get_CPU():
    cpuinfo = {}
    for line in _read_text(_PROC / "cpuinfo", "").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() not in cpuinfo:
            cpuinfo[key.strip()] = value.strip()

    logical = psutil.cpu_count(logical=True) or 0
    physical = psutil.cpu_count(logical=False) or logical
    frequency = _read_text(
        _SYS / "devices" / "system" / "cpu" / "cpu0" / "cpufreq" / "cpuinfo_max_freq",
        "",
    )
    try:
        clock_mhz = round(int(frequency) / 1000)
    except ValueError:
        try:
            current = psutil.cpu_freq()
            clock_mhz = round(current.max or current.current) if current else 0
        except (OSError, AttributeError):
            clock_mhz = 0

    model = cpuinfo.get("model name", cpuinfo.get("Hardware", UNKNOWN))
    clock_speed = (
        f"{round(clock_mhz / 1000, 2)} ГГц ({clock_mhz} МГц)"
        if clock_mhz else UNKNOWN
    )
    return [{
        "manufacturer": cpuinfo.get("vendor_id", UNKNOWN),
        "model": model,
        "cores": physical,
        "threads": logical,
        "clock_speed": clock_speed,
        "socket": UNKNOWN,
        "processor_id": UNKNOWN,
    }]


def get_GPUs():
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            if result.returncode == 0:
                gpu_list = []
                for line in result.stdout.splitlines():
                    parts = [part.strip() for part in line.split(",", 2)]
                    if len(parts) != 3:
                        continue
                    name, memory, driver = parts
                    gpu_list.append({
                        "manufacturer": "NVIDIA",
                        "model": name,
                        "vram": f"{memory} МБ" if memory.isdigit() else UNKNOWN,
                        "driver_version": driver or UNKNOWN,
                        "status": "OK",
                    })
                if gpu_list:
                    return gpu_list
        except (OSError, subprocess.SubprocessError):
            pass

    gpu_list = []
    drm_path = _SYS / "class" / "drm"
    try:
        cards = sorted(drm_path.glob("card[0-9]*"))
    except OSError:
        return gpu_list
    for card in cards:
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        vendor_id = _read_text(device / "vendor")
        device_id = _read_text(device / "device")
        driver_path = device / "driver"
        try:
            driver = driver_path.resolve(strict=True).name
        except OSError:
            driver = UNKNOWN
        try:
            vram_bytes = int(_read_text(device / "mem_info_vram_total", "0"))
        except ValueError:
            vram_bytes = 0
        vram = f"{round(vram_bytes / 1024**3, 2)} ГБ" if vram_bytes else UNKNOWN
        gpu_list.append({
            "manufacturer": vendor_id,
            "model": f"{vendor_id} {device_id}",
            "vram": vram,
            "driver_version": driver,
            "status": "OK",
        })
    return gpu_list


def _decode_edid(edid):
    manufacturer_code = (edid[8] << 8) | edid[9]
    manufacturer = "".join(
        chr(((manufacturer_code >> shift) & 0x1F) + 64)
        for shift in (10, 5, 0)
    )
    product_code = int.from_bytes(edid[10:12], "little")
    model = UNKNOWN
    serial = UNKNOWN
    for offset in range(54, min(len(edid) - 17, 126), 18):
        descriptor = edid[offset : offset + 18]
        if descriptor[:5] == b"\x00\x00\x00\xfc\x00":
            model = descriptor[5:18].decode("ascii", errors="ignore").strip(" \x00\n") or UNKNOWN
        elif descriptor[:5] == b"\x00\x00\x00\xff\x00":
            serial = descriptor[5:18].decode("ascii", errors="ignore").strip(" \x00\n") or UNKNOWN
    if model == UNKNOWN:
        model = f"{product_code:04X}"
    return manufacturer or UNKNOWN, model, serial


def get_Monitors():
    monitor_list = []
    drm_path = _SYS / "class" / "drm"
    try:
        connectors = drm_path.glob("card*-*")
        for connector in connectors:
            if _read_text(connector / "status", "") != "connected":
                continue
            try:
                edid = (connector / "edid").read_bytes()
            except OSError:
                continue
            if len(edid) < 128:
                continue
            manufacturer, model, serial = _decode_edid(edid)
            monitor_list.append({
                "manufacturer": manufacturer,
                "model": model,
                "serial": serial,
            })
    except OSError:
        pass
    return monitor_list


def get_monitors_fallback():
    return get_Monitors()


def get_OS():
    try:
        release = platform.freedesktop_os_release()
        name = release.get("PRETTY_NAME", platform.system())
        version = release.get("VERSION_ID", platform.release())
    except (AttributeError, OSError):
        name = platform.system()
        version = platform.release()
    return {
        "name": name,
        "version": version,
        "build": platform.release(),
        "architecture": platform.machine() or UNKNOWN,
        "manufacturer": "Linux",
        "install_date": UNKNOWN,
        "registered_user": getpass.getuser(),
    }


def get_ActiveDirectory():
    domain = ""
    realm = shutil.which("realm")
    if realm:
        try:
            result = subprocess.run(
                [realm, "list", "--name-only"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            domain = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError, IndexError):
            pass

    current_user = getpass.getuser()
    return {
        "is_in_domain": bool(domain),
        "domain_or_workgroup_name": domain or platform.node() or UNKNOWN,
        "type": "Домен Active Directory (realmd)" if domain else "Локальная система Linux",
        "current_user": current_user,
        "primary_owner": current_user,
    }


def get_mouse_keyboard():
    devices = {"keyboards": [], "mice": []}
    try:
        contents = (_PROC / "bus" / "input" / "devices").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return devices

    for block in contents.split("\n\n"):
        name_match = re.search(r'^N: Name="(.*)"$', block, re.MULTILINE)
        handlers_match = re.search(r"^H: Handlers=(.*)$", block, re.MULTILINE)
        if not name_match or not handlers_match:
            continue
        name = name_match.group(1)
        handlers = handlers_match.group(1).split()
        name_lower = name.lower()
        if "kbd" in handlers or "keyboard" in name_lower:
            devices["keyboards"].append({
                "manufacturer": UNKNOWN,
                "model": name,
                "description": " ".join(handlers),
                "status": "Доступно",
            })
        elif any(handler.startswith("mouse") for handler in handlers) or any(
            keyword in name_lower for keyword in ("mouse", "touchpad", "trackpoint")
        ):
            devices["mice"].append({
                "manufacturer": UNKNOWN,
                "model": name,
                "status": "Доступно",
            })
    return devices