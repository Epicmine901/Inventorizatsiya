import ipaddress
import socket
import subprocess
from pathlib import Path

import psutil
from fastapi import FastAPI
from rich.console import Console
from rich.panel import Panel
import uvicorn


def ensure_lan_certificate() -> tuple[str, int]:
    project_dir = Path(__file__).resolve().parent
    env_path = project_dir / ".env"
    env_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    env_values = {}

    for line in env_lines:
        stripped_line = line.strip()
        if stripped_line and not stripped_line.startswith("#") and "=" in stripped_line:
            key, value = stripped_line.split("=", 1)
            env_values[key.strip()] = value.strip().strip("\"'")

    port_value = env_values.get("PORT")
    if not port_value:
        raise RuntimeError("PORT is missing from .env.")
    try:
        port = int(port_value)
    except ValueError as error:
        raise RuntimeError("PORT in .env must be a number.") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT in .env must be between 1 and 65535.")

    interface_addresses = psutil.net_if_addrs()
    interface_stats = psutil.net_if_stats()
    usable_addresses = {
        interface_name: next(
            (
                address.address
                for address in addresses
                if address.family == socket.AF_INET
                and not ipaddress.ip_address(address.address).is_loopback
                and not ipaddress.ip_address(address.address).is_link_local
            ),
            None,
        )
        for interface_name, addresses in interface_addresses.items()
        if interface_stats.get(interface_name) is not None
        and interface_stats[interface_name].isup
    }
    usable_addresses = {
        interface_name: address
        for interface_name, address in usable_addresses.items()
        if address is not None
    }

    lan_ip = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route_socket:
            route_socket.connect(("192.0.2.1", 80))
            route_ip = route_socket.getsockname()[0]
        if route_ip in usable_addresses.values():
            lan_ip = route_ip
    except OSError:
        pass

    if lan_ip is None:
        saved_lan_ip = env_values.get("LAN_IP")
        lan_ip = next(
            (
                address
                for address in usable_addresses.values()
                if address == saved_lan_ip
            ),
            next(iter(usable_addresses.values()), None),
        )

    if lan_ip is None:
        raise RuntimeError("No usable IPv4 address found on an active interface.")

    cert_path = project_dir / "Sertifikat\\localhost+1.pem"
    key_path = project_dir / "Sertifikat\\localhost+1-key.pem"
    mkcert_path = project_dir / "Sertifikat\\mkcert.exe"
    if not mkcert_path.is_file():
        raise RuntimeError(f"mkcert executable was not found: {mkcert_path}")

    if (
        env_values.get("LAN_IP") != lan_ip
        or not cert_path.is_file()
        or not key_path.is_file()
    ):
        install_result = subprocess.run(
            [str(mkcert_path), "-install"],
            cwd=project_dir,
            check=False,
        )
        if install_result.returncode != 0:
            raise RuntimeError("mkcert could not install its local CA.")

        cert_result = subprocess.run(
            [
                str(mkcert_path),
                "-cert-file",
                str(cert_path),
                "-key-file",
                str(key_path),
                lan_ip,
                "localhost",
                "127.0.0.1",
            ],
            cwd=project_dir,
            check=False,
        )
        if cert_result.returncode != 0:
            raise RuntimeError("mkcert could not generate the LAN certificate.")

    updated_lines = []
    updated_lan_ip = False
    for line in env_lines:
        if "=" not in line or line.lstrip().startswith("#"):
            updated_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key == "NETWORK_INTERFACE":
            continue
        if key == "LAN_IP":
            if not updated_lan_ip:
                updated_lines.append(f"LAN_IP={lan_ip}")
                updated_lan_ip = True
            continue
        updated_lines.append(line)

    if not updated_lan_ip:
        updated_lines.append(f"LAN_IP={lan_ip}")

    env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return lan_ip, port


def read_trusted_stop_ips(
    env_path: Path,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    trusted_ips: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = {
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("::1"),
    }
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped_line = line.strip()
        if (
            not stripped_line
            or stripped_line.startswith("#")
            or "=" not in stripped_line
        ):
            continue
        key, value = stripped_line.split("=", 1)
        if key.strip() != "TRUSTED_STOP_IPS":
            continue
        for address in value.split(","):
            address = address.strip()
            if not address:
                continue
            try:
                trusted_ips.add(ipaddress.ip_address(address))
            except ValueError as error:
                raise RuntimeError(
                    f"Invalid IP address '{address}' in TRUSTED_STOP_IPS."
                ) from error
    return trusted_ips


def create_server_sockets(host: str, port: int) -> list[socket.socket]:
    sockets = []
    try:
        for bind_host in (host, "127.0.0.1"):
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
            server_socket.bind((bind_host, port))
            server_socket.listen(2048)
            server_socket.setblocking(False)
            sockets.append(server_socket)
    except OSError:
        for server_socket in sockets:
            server_socket.close()
        if "server_socket" in locals():
            server_socket.close()
        raise
    return sockets


def run_server(app: FastAPI, console: Console) -> None:
    project_dir = Path(__file__).resolve().parent
    try:
        lan_ip, port = ensure_lan_certificate()
        trusted_stop_ips = read_trusted_stop_ips(project_dir / ".env")
        trusted_stop_ips.add(ipaddress.ip_address(lan_ip))
    except RuntimeError as error:
        console.print(
            Panel(
                f"[red]{error}[/]",
                title="[bold red]Не удалось подготовить сервер[/]",
                border_style="red",
            )
        )
        raise SystemExit(1) from error
    except OSError as error:
        console.print(
            Panel(
                f"[red]Не удалось прочитать настройки: {error}[/]",
                title="[bold red]Ошибка настроек[/]",
                border_style="red",
            )
        )
        raise SystemExit(1) from error

    console.print(
        Panel(
            f"[bold cyan]https://{lan_ip}:{port}[/]\n"
            "[dim]Сервер доступен в локальной сети.[/]",
            title="[bold green]Запуск сервера инвентаризации[/]",
            border_style="green",
        )
    )

    server_config = uvicorn.Config(
        app,
        host=lan_ip,
        port=port,
        ssl_certfile=str(project_dir / "Sertifikat" / "localhost+1.pem"),
        ssl_keyfile=str(project_dir / "Sertifikat" / "localhost+1-key.pem"),
        access_log=False,
        timeout_graceful_shutdown=3,
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"rich": {"format": "%(message)s"}},
            "handlers": {
                "rich": {
                    "class": "rich.logging.RichHandler",
                    "console": console,
                    "show_time": True,
                    "show_level": True,
                    "show_path": False,
                    "markup": False,
                    "rich_tracebacks": True,
                    "formatter": "rich",
                }
            },
            "loggers": {
                "uvicorn": {
                    "handlers": ["rich"],
                    "level": "INFO",
                    "propagate": False,
                },
                "uvicorn.error": {
                    "handlers": ["rich"],
                    "level": "INFO",
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": [],
                    "level": "WARNING",
                    "propagate": False,
                },
            },
        },
    )
    server = uvicorn.Server(server_config)
    app.state.server = server
    app.state.trusted_stop_ips = trusted_stop_ips
    app.state.stop_tokens = {}

    try:
        server_sockets = create_server_sockets(lan_ip, port)
    except OSError as error:
        console.print(
            Panel(
                f"[red]Не удалось открыть порт {port}: {error}[/]",
                title="[bold red]Не удалось запустить сервер[/]",
                border_style="red",
            )
        )
        raise SystemExit(1) from error

    console.print(
        "[dim]Остановить можно запросом POST /stop с этого компьютера "
        "или с доверенного IP.[/]"
    )
    try:
        server.run(sockets=server_sockets)
    finally:
        for server_socket in server_sockets:
            server_socket.close()
