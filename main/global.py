import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel


TUNNEL_URL_PATTERN = re.compile(
    r"https://[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.trycloudflare\.com\b"
)
console = Console(force_terminal=True, color_system="truecolor")


def show_cloudflared_log(line: str) -> None:
    log_match = re.match(
        r"^(?:\S+\s+)?(INF|WRN|ERR|DBG)\s+(.*)$",
        line,
        re.IGNORECASE,
    )
    if log_match is None:
        if any(word in line.lower() for word in ("error", "failed", "unable")):
            console.print(f"[red]Ошибка Cloudflare:[/] {escape(line)}")
        return

    level, message = log_match.groups()
    level = level.upper()
    message_lower = message.lower()

    if "registered tunnel connection" in message_lower:
        location_match = re.search(r"\blocation=(\S+)", message)
        protocol_match = re.search(r"\bprotocol=(\S+)", message)
        connection_details = []
        if location_match:
            connection_details.append(f"регион: {location_match.group(1)}")
        if protocol_match:
            connection_details.append(f"протокол: {protocol_match.group(1).upper()}")
        details = f" ({', '.join(connection_details)})" if connection_details else ""
        console.print(f"[green]Соединение с Cloudflare установлено{details}.[/]")
    elif level == "INF" and "retrying connection" in message_lower:
        console.print("[yellow]Связь прервана. Cloudflare пытается подключиться снова.[/]")
    elif level == "WRN":
        console.print(f"[yellow]Предупреждение Cloudflare:[/] {escape(message)}")
    elif level == "ERR":
        console.print(f"[red]Ошибка Cloudflare:[/] {escape(message)}")


def main(gui_api_events: bool = False) -> int:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        print("Сначала запустите main.py, чтобы определить локальный IP-адрес.")
        return 1

    env_values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped_line = line.strip()
        if stripped_line and not stripped_line.startswith("#") and "=" in stripped_line:
            key, value = stripped_line.split("=", 1)
            env_values[key.strip()] = value.strip().strip("\"'")

    lan_ip = env_values.get("LAN_IP")
    if not lan_ip:
        console.print("[bold red]В файле .env не найден LAN_IP.[/]")
        console.print("Сначала запустите main.py.")
        return 1

    port_value = env_values.get("PORT")
    if not port_value:
        console.print("[bold red]В файле .env не найден PORT.[/]")
        return 1
    try:
        port = int(port_value)
    except ValueError:
        console.print("[bold red]Значение PORT в файле .env должно быть числом.[/]")
        return 1
    if not 1 <= port <= 65535:
        console.print("[bold red]PORT должен быть в диапазоне от 1 до 65535.[/]")
        return 1

    cloudflared = shutil.which("cloudflared")
    if cloudflared is None:
        console.print("[bold red]Не найден cloudflared.[/]")
        console.print(
            "Установите его с сайта "
            "[link=https://developers.cloudflare.com/tunnel/downloads/]"
            "https://developers.cloudflare.com/tunnel/downloads/[/link] "
            "и запустите этот скрипт снова."
        )
        return 1

    root_ca_path = Path(__file__).resolve().parent / "rootCA.pem"
    if not root_ca_path.is_file():
        console.print(f"[bold red]Не найден сертификат CA:[/] {root_ca_path}")
        return 1

    origin = f"https://{lan_ip}:{port}"
    console.print(Panel(f"[bold cyan]{origin}[/]", title="Запуск публичной ссылки"))

    try:
        process = subprocess.Popen(
            [
                cloudflared,
                "tunnel",
                "--url",
                origin,
                "--origin-ca-pool",
                str(root_ca_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        console.print(f"[bold red]Не удалось запустить cloudflared:[/] {error}")
        return 1

    recent_output = deque(maxlen=5)
    tunnel_url = None

    try:
        if process.stdout is None:
            process.terminate()
            process.wait()
            console.print("[bold red]Не удалось прочитать вывод cloudflared.[/]")
            return 1

        for output_line in process.stdout:
            line = output_line.strip()
            if not line:
                continue

            recent_output.append(line)
            url_match = TUNNEL_URL_PATTERN.search(line)
            if url_match and tunnel_url is None:
                tunnel_url = url_match.group(0)
                console.print(
                    Panel(
                        f"[bold green underline]{tunnel_url}[/]",
                        title="[bold green]ПУБЛИЧНАЯ ССЫЛКА[/]",
                        border_style="green",
                    )
                )
                console.log("[bold green]Публичная ссылка создана. Туннель запущен.[/]")
                if gui_api_events:
                    print(
                        "\x1eINVENTORY_GUI_EVENT "
                        + json.dumps(
                            {"type": "tunnel_url", "url": tunnel_url},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                console.print(
                    "Не закрывайте это окно. Нажмите Ctrl+C, чтобы остановить ссылку."
                )
            elif tunnel_url is not None:
                show_cloudflared_log(line)

        return_code = process.wait()
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
        process.wait()
        console.print("\n[bold yellow]Публичная ссылка остановлена.[/]")
        return 0

    if tunnel_url is None:
        console.print("\n[bold red]Не удалось получить публичную ссылку.[/]")
        if recent_output:
            console.print("[yellow]Последние сообщения cloudflared:[/]")
            for line in recent_output:
                console.print(line, markup=False)
    else:
        message = (
            "Публичная ссылка отключена."
            if return_code == 0
            else f"Туннель завершился с ошибкой (код {return_code}). Публичная ссылка отключена."
        )
        console.log(
            "[yellow]" + escape(message) + "[/]"
            if return_code == 0
            else "[red]" + escape(message) + "[/]"
        )

    return return_code if tunnel_url is not None or return_code != 0 else 1


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--gui-api-events", action="store_true")
    arguments = argument_parser.parse_args()
    sys.exit(main(gui_api_events=arguments.gui_api_events))
