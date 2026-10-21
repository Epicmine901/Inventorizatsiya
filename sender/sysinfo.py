import datetime
import getpass
import socket

import psutil
import wmi


_EXCLUDED_NETWORK_KEYWORDS = (
    "bluetooth",
    "virtual",
    "vethernet",
    "loopback",
    "tunnel",
    "tap",
    "vpn",
    "microsoft wi-fi direct",
    "wan miniport",
)
_WIRELESS_KEYWORDS = ("wi-fi", "wireless", "802.11", "wlan")
_STORAGE_BRANDS = (
    "samsung",
    "wd",
    "western digital",
    "kingston",
    "crucial",
    "seagate",
    "toshiba",
    "intel",
    "corsair",
    "adata",
    "hp",
    "lenovo",
    "lexar",
    "sandisk",
)


def _normalize_mac(address):
    return address.replace("-", ":").lower()


def _decode_wmi_bytes(byte_array):
    if not byte_array:
        return "Неизвестно"
    try:
        return "".join(chr(value) for value in byte_array if value != 0).strip()
    except (TypeError, ValueError):
        return "Неизвестно"

def get_network_hardware_info():
    c = wmi.WMI()
    devices = []
    # Получаем физические сетевые адаптеры через WMI (Win32_NetworkAdapter)
    # Фильтруем только те, у которых есть MAC-адрес и они физические (не виртуальные/минипорты)
    wmi_adapters = c.Win32_NetworkAdapter(NetEnabled=True)
    # Индексируем адреса один раз вместо полного перебора для каждого адаптера.
    ip_by_mac = {}
    for addrs in psutil.net_if_addrs().values():
        mac = next(
            (addr.address for addr in addrs if addr.family == psutil.AF_LINK),
            None,
        )
        ip = next(
            (addr.address for addr in addrs if addr.family == socket.AF_INET),
            None,
        )
        if mac and ip:
            ip_by_mac.setdefault(_normalize_mac(mac), ip)

    for item in wmi_adapters:
        # Пропускаем виртуальные адаптеры, Bluetooth и WAN-минипорты по имени устройства
        name = item.Name or ""
        desc = item.Description or ""
        name_lower = name.lower()
        desc_lower = desc.lower()
        if any(
            keyword in name_lower or keyword in desc_lower
            for keyword in _EXCLUDED_NETWORK_KEYWORDS
        ):
            continue
        mac = item.MACAddress
        if not mac:
            continue
        ip = ip_by_mac.get(_normalize_mac(mac))
        # Пропускаем, если нет реального IP (или это APIPA / локалхост)
        if not ip or ip.startswith('127.') or ip.startswith('169.254.'):
            continue

        # Определяем тип (Wi-Fi или Ethernet) по описанию драйвера
        if any(
            keyword in desc_lower or keyword in name_lower
            for keyword in _WIRELESS_KEYWORDS
        ):
            net_type = "Wi-Fi"
        else:
            net_type = "Ethernet"
        devices.append({
            "name": name,
            "model": desc,
            "type": net_type,
            "ip": ip,
            "mac": mac
        })

    return devices


def get_motherboard():
    c = wmi.WMI()
    mb_data = {}

    # Получаем данные о материнской плате
    for board in c.Win32_BaseBoard():
        mb_data["manufacturer"] = board.Manufacturer  # Производитель (кто изготовил)
        mb_data["model"] = board.Product             # Модель / Название платы
        mb_data["name"] = board.Name                 # Системное имя
        mb_data["serial"] = board.SerialNumber       # Серийный номер

    # Получаем UUID системы (тот самый уникальный адрес-идентификатор железа)
    for product in c.Win32_ComputerSystemProduct():
        mb_data["uuid"] = product.UUID

    return mb_data

def get_RAMs():
    c = wmi.WMI()
    ram_modules = []

    for mem in c.Win32_PhysicalMemory():
        # Объем памяти приходит в байтах, переводим в гигабайты (ГБ)
        capacity_bytes = int(mem.Capacity) if mem.Capacity else 0
        capacity_gb = round(capacity_bytes / (1024 ** 3), 2)

        ram_modules.append({
            "manufacturer": mem.Manufacturer.strip() if mem.Manufacturer else "Неизвестно",
            "model": mem.PartNumber.strip() if mem.PartNumber else "Неизвестно",
            "capacity_gb": capacity_gb,
            "speed_mhz": mem.Speed,
            "slot": mem.DeviceLocator,
            "serial": mem.SerialNumber.strip() if mem.SerialNumber else "Неизвестно"
        })

    return ram_modules

def get_storages():
    c = wmi.WMI()
    storage_devices = []

    for disk in c.Win32_DiskDrive():
        size_bytes = int(disk.Size) if disk.Size else 0
        
        # 1. Десятичный объем (как у производителей: 1 ГБ = 1,000,000,000 байт)
        size_gb_decimal = round(size_bytes / (1000 ** 3), 2)
        if size_gb_decimal >= 1000:
            capacity_decimal = f"{round(size_gb_decimal / 1000, 2)} ТБ ({size_gb_decimal} ГБ)"
        else:
            capacity_decimal = f"{size_gb_decimal} ГБ"

        # 2. Двоичный объем (как видит Windows: 1 ГБ = 1,073,741,824 байт)
        size_gb_binary = round(size_bytes / (1024 ** 3), 2)
        
        model = disk.Model.strip() if disk.Model else "Неизвестно"
        model_lower = model.lower()
        
        # Определяем производителя
        manufacturer = "Неизвестно"
        for brand in _STORAGE_BRANDS:
            if brand in model_lower:
                manufacturer = brand.upper() if len(brand) <= 2 else brand.title()
                break
        if manufacturer == "Неизвестно" and model != "Неизвестно":
            manufacturer = model.split()[0]

        # Определяем тип накопителя
        if 'nvme' in model_lower or 'm.2' in model_lower or 'pcie' in model_lower:
            drive_type = "NVMe SSD"
        elif 'ssd' in model_lower or 'solid' in model_lower:
            drive_type = "SATA SSD"
        else:
            drive_type = "HDD"

        storage_devices.append({
            "manufacturer": manufacturer,
            "model": model,
            "type": drive_type,
            "capacity_decimal": capacity_decimal,   # Десятичный (производительский)
            "capacity_binary": f"{size_gb_binary} ГБ",  # Двоичный (для Windows)
            "capacity_bytes": size_bytes,           # Точный размер в байтах
            "interface": disk.InterfaceType,
            "serial": disk.SerialNumber.strip() if disk.SerialNumber else "Неизвестно"
        })

    return storage_devices

def get_CPU():
    c = wmi.WMI()
    cpu_list = []

    for cpu in c.Win32_Processor():
        # Частота процессора (обычно приходит в МГц)
        clock_mhz = cpu.MaxClockSpeed or cpu.CurrentClockSpeed or 0
        clock_ghz = round(clock_mhz / 1000, 2) if clock_mhz else "Неизвестно"

        cpu_list.append({
            "manufacturer": cpu.Manufacturer.strip() if cpu.Manufacturer else "Неизвестно",
            "model": cpu.Name.strip() if cpu.Name else "Неизвестно",
            "cores": cpu.NumberOfCores,
            "threads": cpu.NumberOfLogicalProcessors,
            "clock_speed": f"{clock_ghz} ГГц ({clock_mhz} МГц)" if clock_mhz else "Неизвестно",
            "socket": cpu.SocketDesignation.strip() if cpu.SocketDesignation else "Неизвестно",
            "processor_id": cpu.ProcessorId.strip() if cpu.ProcessorId else "Неизвестно"
        })

    return cpu_list

def get_GPUs():
    c = wmi.WMI()
    gpu_list = []

    for gpu in c.Win32_VideoController():
        # Обрабатываем объем видеопамяти с учетом возможного переполнения 32-бит (>4 ГБ)
        vram_bytes = int(gpu.AdapterRAM) if gpu.AdapterRAM else 0
        if vram_bytes < 0:
            vram_bytes += 4294967296  # Исправление переполнения для памяти >4 ГБ
            
        vram_mb = round(vram_bytes / (1024 ** 2), 2) if vram_bytes > 0 else 0
        vram_gb = round(vram_bytes / (1024 ** 3), 2) if vram_bytes > 0 else 0

        # Форматируем вывод памяти
        if vram_gb >= 1:
            vram_readable = f"{vram_gb} ГБ ({vram_mb} МБ)"
        elif vram_mb > 0:
            vram_readable = f"{vram_mb} МБ"
        else:
            vram_readable = "Неизвестно / Общая память"

        gpu_list.append({
            "manufacturer": gpu.AdapterCompatibility.strip() if gpu.AdapterCompatibility else "Неизвестно",
            "model": gpu.Name.strip() if gpu.Name else "Неизвестно",
            "vram": vram_readable,
            "driver_version": gpu.DriverVersion.strip() if gpu.DriverVersion else "Неизвестно",
            "status": gpu.Status.strip() if gpu.Status else "Неизвестно"
        })

    return gpu_list

def get_Monitors():
    # Подключаемся к пространству имен root\wmi для чтения EDID данных мониторов
    try:
        c = wmi.WMI(namespace="wmi")
        monitors = c.WmiMonitorID()
    except Exception:
        # Если вдруг WMI root\wmi недоступен, пробуем стандартный метод
        return get_monitors_fallback()

    monitor_list = []

    for mon in monitors:
        model = _decode_wmi_bytes(mon.UserFriendlyName)
        manufacturer = _decode_wmi_bytes(mon.ManufacturerName)
        serial = _decode_wmi_bytes(mon.SerialNumberID)

        # Если имя пользователя не заполнено, берем зашифрованное имя продукта
        if not model or model == "Неизвестно":
            model = _decode_wmi_bytes(mon.ProductCodeID)

        monitor_list.append({
            "manufacturer": manufacturer if manufacturer != "Неизвестно" else "Внешний / PnP монитор",
            "model": model if model else "Неизвестная модель",
            "serial": serial if serial != "Неизвестно" else "Отсутствует / Заблокирован"
        })

    return monitor_list

def get_monitors_fallback():
    # Запасной метод через стандартный Win32_DesktopMonitor
    c = wmi.WMI()
    monitor_list = []
    for mon in c.Win32_DesktopMonitor():
        monitor_list.append({
            "manufacturer": mon.MonitorManufacturer if mon.MonitorManufacturer else "Неизвестно",
            "model": mon.Name if mon.Name else "Неизвестно",
            "serial": "Недоступно"
        })
    return monitor_list

def get_OS():
    c = wmi.WMI()
    os_info = {}

    for os in c.Win32_OperatingSystem():
        # Преобразуем WMI-дату установки в нормальный формат (YYYY-MM-DD HH:MM:SS)
        install_date_str = os.InstallDate
        install_date = "Неизвестно"
        if install_date_str:
            try:
                dt_part = install_date_str.split('.')[0]
                install_date = datetime.datetime.strptime(dt_part, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                install_date = install_date_str

        os_info = {
            "name": os.Caption.strip() if os.Caption else "Неизвестно",
            "version": os.Version.strip() if os.Version else "Неизвестно",
            "build": os.BuildNumber.strip() if os.BuildNumber else "Неизвестно",
            "architecture": os.OSArchitecture.strip() if os.OSArchitecture else "Неизвестно",
            "manufacturer": os.Manufacturer.strip() if os.Manufacturer else "Неизвестно",
            "install_date": install_date,
            "registered_user": os.RegisteredUser.strip() if os.RegisteredUser else "Неизвестно"
        }

    return os_info


def get_ActiveDirectory():
    c = wmi.WMI()
    ad_info = {}

    for cs in c.Win32_ComputerSystem():
        is_domain = cs.PartOfDomain
        wmi_user = cs.UserName
        
        # Если WMI не возвращает активного пользователя, берем через getpass
        current_user = wmi_user.strip() if wmi_user else getpass.getuser()

        ad_info = {
            "is_in_domain": is_domain,
            "domain_or_workgroup_name": cs.Domain if is_domain else cs.Workgroup,
            "type": "Active Directory Домен" if is_domain else "Рабочая группа (Workgroup)",
            "current_user": current_user,
            "primary_owner": cs.PrimaryOwnerName.strip() if cs.PrimaryOwnerName else "Неизвестно"
        }

    return ad_info

def get_mouse_keyboard():
    c = wmi.WMI()
    input_devices = {
        "keyboards": [],
        "mice": []
    }

    # 1. Получаем клавиатуры
    try:
        for kb in c.Win32_Keyboard():
            name = kb.Name.strip() if kb.Name else "Клавиатура"
            desc = kb.Description.strip() if kb.Description else ""
            
            # Пробуем запросить ProviderName через try-except
            manufacturer = "Неизвестно"
            try:
                if kb.ProviderName:
                    manufacturer = kb.ProviderName.strip()
            except Exception:
                pass  # Пропускаем, если свойство отсутствует в системе

            # Если через ProviderName не вышло, определяем по описанию/названию или бренду
            if manufacturer == "Неизвестно":
                full_text = f"{name} {desc}".lower()
                if "usb" in full_text:
                    manufacturer = "USB Device"
                elif "ps/2" in full_text:
                    manufacturer = "PS/2 Standard"
                    
                known_brands = ['logitech', 'razer', 'corsair', 'hyperx', 'asus', 'msi', 'dell', 'hp', 'lenovo', 'acer']
                for brand in known_brands:
                    if brand in full_text:
                        manufacturer = brand.title()
                        break

            input_devices["keyboards"].append({
                "manufacturer": manufacturer,
                "model": name,
                "description": desc,
                "status": kb.Status.strip() if kb.Status else "Неизвестно"
            })
    except Exception:
        pass

    # 2. Получаем мыши, тачпады и прочие указывающие устройства
    try:
        for mouse in c.Win32_PointingDevice():
            name = mouse.Name.strip() if mouse.Name else "Указывающее устройство"
            manufacturer = mouse.Manufacturer.strip() if mouse.Manufacturer else "Неизвестно"
            
            if manufacturer == "Неизвестно" or manufacturer == "Microsoft":
                name_lower = name.lower()
                if "elan" in name_lower:
                    manufacturer = "ELAN"
                elif "synaptics" in name_lower:
                    manufacturer = "Synaptics"
                elif "alps" in name_lower:
                    manufacturer = "ALPS"

            input_devices["mice"].append({
                "manufacturer": manufacturer,
                "model": name,
                "status": mouse.Status.strip() if mouse.Status else "Неизвестно"
            })
    except Exception:
        pass

    return input_devices
