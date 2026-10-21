import argparse
import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from html import escape
import ipaddress
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from collections import deque
from time import perf_counter
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
import psutil
import uvicorn

from bibliteka import run_server
from rich.console import Console
from rich.text import Text
from sql import DatabaseError, initialize_database


console = Console(force_terminal=True, color_system="truecolor")
PROJECT_DIR = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = Path(__file__).resolve()
TUNNEL_SCRIPT = Path(__file__).resolve().parent / "global.py"
ACCESS_KEY_PATH = Path(__file__).resolve().parent / ".inventory_access.json"
LOCAL_URL_PATTERN = re.compile(r"https://\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}\b")
TUNNEL_URL_PATTERN = re.compile(
    r"https://[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.trycloudflare\.com\b"
)


class GuiControlService:
    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.urls = {"server": "", "tunnel": ""}
        self.events: deque[dict[str, object]] = deque(maxlen=2000)
        self.next_event_id = 0
        self.lock = threading.Lock()

    def log(self, source: str, message: str) -> None:
        with self.lock:
            self.next_event_id += 1
            self.events.append(
                {
                    "id": self.next_event_id,
                    "source": source,
                    "message": message,
                }
            )

    def is_running(self, key: str) -> bool:
        with self.lock:
            process = self.processes.get(key)
            running = process is not None and process.poll() is None
            if not running:
                self.urls[key] = ""
            return running

    def start(self, key: str) -> None:
        script = SERVER_SCRIPT if key == "server" else TUNNEL_SCRIPT
        with self.lock:
            process = self.processes.get(key)
            if process is not None and process.poll() is None:
                raise HTTPException(status_code=409, detail=f"{key} is already running.")
            if key == "tunnel":
                server = self.processes.get("server")
                if server is None or server.poll() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Start the inventory server before the tunnel.",
                    )
            if not script.is_file():
                raise HTTPException(status_code=500, detail=f"Script not found: {script}")

            command = [sys.executable, "-X", "utf8", "-u", str(script)]
            if key == "tunnel":
                command.append("--gui-api-events")
            environment = os.environ.copy()
            if key == "tunnel":
                environment["INVENTORY_GUI_MANAGED"] = "1"
            try:
                process = subprocess.Popen(
                    command,
                    cwd=PROJECT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as error:
                raise HTTPException(status_code=500, detail=str(error)) from error

            self.processes[key] = process
            self.urls[key] = ""
        self.log(key, f"{key} запускается.")
        threading.Thread(
            target=self._read_output,
            args=(key, process),
            daemon=True,
        ).start()

    def _read_output(self, key: str, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                if key == "tunnel" and line.startswith("\x1eINVENTORY_GUI_EVENT "):
                    try:
                        event = json.loads(line.partition(" ")[2])
                        if not isinstance(event, dict):
                            raise ValueError("Событие должно быть JSON-объектом.")
                        if event.get("type") == "tunnel_url":
                            self.set_url(key, str(event["url"]))
                    except (KeyError, TypeError, ValueError) as error:
                        self.log(key, f"Не удалось прочитать событие Global: {error}")
                    continue
                self.log(key, line)
                pattern = LOCAL_URL_PATTERN if key == "server" else TUNNEL_URL_PATTERN
                match = pattern.search(line)
                if match:
                    self.set_url(key, match.group(0))
        return_code = process.wait()
        with self.lock:
            if self.processes.get(key) is process:
                self.urls[key] = ""
        self.log(key, f"{key} завершил работу (код {return_code}).")

    def set_url(self, key: str, value: str) -> None:
        with self.lock:
            self.urls[key] = value

    def status(self) -> dict[str, object]:
        server_running = self.is_running("server")
        tunnel_running = self.is_running("tunnel")
        with self.lock:
            return {
                "server_running": server_running,
                "server_url": self.urls["server"],
                "tunnel_running": tunnel_running,
                "tunnel_url": self.urls["tunnel"],
            }

    def read_events(self, after: int) -> list[dict[str, object]]:
        with self.lock:
            return [event for event in self.events if int(event["id"]) > after]

    def stop(self, key: str) -> None:
        with self.lock:
            process = self.processes.get(key)
        if process is None or process.poll() is not None:
            return

        try:
            children = psutil.Process(process.pid).children(recursive=True)
            for child in children:
                child.terminate()
            process.terminate()
            _, remaining = psutil.wait_procs(children, timeout=3)
            for child in remaining:
                child.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        except psutil.Error as error:
            self.log(key, f"Не удалось полностью остановить {key}: {error}")
            if process.poll() is None:
                process.terminate()

    def stop_all(self) -> None:
        self.stop("tunnel")
        self.stop("server")


control_service = GuiControlService()


@asynccontextmanager
async def control_lifespan(app: FastAPI):
    app.state.control_service = control_service
    try:
        yield
    finally:
        control_service.stop_all()


control_app = FastAPI(lifespan=control_lifespan)


def verify_gui_token(x_inventory_gui_token: str = Header(default="")) -> None:
    expected_token = os.environ.get("INVENTORY_GUI_API_TOKEN", "")
    if not expected_token or not hmac.compare_digest(x_inventory_gui_token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid GUI API token.")


@control_app.get("/api/status")
async def get_gui_status(_: None = Depends(verify_gui_token)):
    return control_service.status()


@control_app.get("/api/events")
async def get_gui_events(
    after: int = Query(default=0, ge=0),
    _: None = Depends(verify_gui_token),
):
    return {"events": control_service.read_events(after)}


@control_app.get("/api/inventory")
async def get_gui_inventory(
    limit: int = Query(default=500, ge=1, le=500),
    _: None = Depends(verify_gui_token),
):
    try:
        database = await asyncio.to_thread(initialize_database)
        records = await asyncio.to_thread(database.list_inventory, limit)
    except DatabaseError as error:
        raise HTTPException(
            status_code=503,
            detail=f"Не удалось загрузить таблицу инвентаризации: {error}",
        ) from error
    return {"records": records}


@control_app.post("/api/access-key/rotate")
async def rotate_inventory_access_key(
    key_hash: str = Body(..., embed=True),
    _: None = Depends(verify_gui_token),
):
    if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
        raise HTTPException(status_code=422, detail="Некорректный хеш ключа доступа.")

    temporary_path = ACCESS_KEY_PATH.with_name(
        f"{ACCESS_KEY_PATH.name}.{secrets.token_hex(6)}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps({"key_hash": key_hash}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, ACCESS_KEY_PATH)
    except OSError as error:
        temporary_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось сохранить ключ доступа: {error}",
        ) from error
    return {"status": "updated"}


@control_app.post("/api/server/start")
async def start_gui_server(_: None = Depends(verify_gui_token)):
    control_service.start("server")
    return {"status": "starting"}


@control_app.post("/api/server/stop")
async def stop_gui_server(_: None = Depends(verify_gui_token)):
    await asyncio.to_thread(control_service.stop, "tunnel")
    await asyncio.to_thread(control_service.stop, "server")
    return {"status": "stopping"}


@control_app.post("/api/tunnel/start")
async def start_gui_tunnel(_: None = Depends(verify_gui_token)):
    control_service.start("tunnel")
    return {"status": "starting"}


@control_app.post("/api/tunnel/stop")
async def stop_gui_tunnel(_: None = Depends(verify_gui_token)):
    await asyncio.to_thread(control_service.stop, "tunnel")
    return {"status": "stopping"}


@control_app.post("/api/shutdown")
async def shutdown_gui_api(
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_gui_token),
):
    background_tasks.add_task(setattr, control_app.state.server, "should_exit", True)
    return {"status": "shutting_down"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        app.state.inventory_database = await asyncio.to_thread(initialize_database)
    except DatabaseError as error:
        console.print(f"[bold red]Ошибка базы данных:[/] {error}")
        raise

    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()

    def handle_loop_exception(
        current_loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        exception = context.get("exception")
        if (
            isinstance(exception, ConnectionResetError)
            and getattr(exception, "winerror", None) == 10054
        ):
            console.print(
                "[dim yellow]Клиент закрыл соединение (Windows 10054).[/]"
            )
            return

        if previous_exception_handler is not None:
            previous_exception_handler(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    app.state.connection_reset_handler = handle_loop_exception
    loop.set_exception_handler(handle_loop_exception)
    try:
        yield
    finally:
        if loop.get_exception_handler() is app.state.connection_reset_handler:
            loop.set_exception_handler(previous_exception_handler)
        del app.state.connection_reset_handler


app = FastAPI(lifespan=lifespan)


def read_inventory_access_key_hash() -> str:
    try:
        config = json.loads(ACCESS_KEY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=503,
            detail="Ключ доступа не настроен. Создайте его в панели управления.",
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=503,
            detail="Не удалось прочитать настройки ключа доступа.",
        ) from error

    key_hash = config.get("key_hash") if isinstance(config, dict) else None
    if not isinstance(key_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", key_hash):
        raise HTTPException(
            status_code=503,
            detail="Настройки ключа доступа повреждены.",
        )
    return key_hash


@app.get("/api/mobile/inventory")
async def get_mobile_inventory(
    request: Request,
    limit: int = Query(default=500, ge=1, le=500),
    x_inventory_access_key: str = Header(default=""),
):
    configured_key_hash = read_inventory_access_key_hash()
    supplied_key_hash = hashlib.sha256(
        x_inventory_access_key.encode("utf-8")
    ).hexdigest()
    if not x_inventory_access_key or not hmac.compare_digest(
        supplied_key_hash,
        configured_key_hash,
    ):
        raise HTTPException(status_code=401, detail="Неверный ключ доступа.")

    try:
        records = await asyncio.to_thread(
            request.app.state.inventory_database.list_inventory,
            limit,
        )
    except DatabaseError as error:
        raise HTTPException(
            status_code=503,
            detail=f"Не удалось загрузить таблицу инвентаризации: {error}",
        ) from error
    return {"records": records}


def is_cloudflare_request(request: Request) -> bool:
    return (
        "cf-connecting-ip" in request.headers
        or "cf-ray" in request.headers
    )


@app.middleware("http")
async def log_request(request: Request, call_next):
    started_at = perf_counter()
    response = await call_next(request)
    elapsed_ms = (perf_counter() - started_at) * 1000

    status_style = (
        "green"
        if response.status_code < 400
        else "yellow"
        if response.status_code < 500
        else "bold red"
    )
    log_line = Text()
    client_ip = request.client.host if request.client is not None else "неизвестный IP"
    log_line.append(client_ip, style="magenta")
    log_line.append("  ")
    log_line.append(request.method, style="bold cyan")
    log_line.append(" ")
    log_line.append(request.url.path)
    log_line.append("  ")
    log_line.append(str(response.status_code), style=status_style)
    log_line.append(f"  {elapsed_ms:.1f} мс", style="dim")
    console.print(log_line)
    return response


@app.post("/stop")
async def stop_server(request: Request, background_tasks: BackgroundTasks):
    if is_cloudflare_request(request):
        raise HTTPException(
            status_code=403,
            detail="Server shutdown is disabled through the public tunnel.",
        )

    client = request.client
    try:
        client_ip = ipaddress.ip_address(client.host) if client is not None else None
    except ValueError:
        client_ip = None

    trusted_ips = request.app.state.trusted_stop_ips
    if client_ip not in trusted_ips:
        raise HTTPException(
            status_code=403,
            detail="IP is not allowed to stop the server.",
        )

    try:
        form = parse_qs((await request.body()).decode("utf-8"), strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid confirmation form.") from error

    token_values = form.get("token", [])
    stop_tokens = request.app.state.stop_tokens
    stored_token = stop_tokens.get(str(client_ip))
    if (
        not token_values
        or stored_token is None
        or stored_token[1] < perf_counter()
        or not secrets.compare_digest(token_values[0], stored_token[0])
    ):
        raise HTTPException(
            status_code=403,
            detail="Open /stop in your browser and confirm the stop request.",
        )
    del stop_tokens[str(client_ip)]

    server = getattr(request.app.state, "server", None)
    if server is None:
        raise HTTPException(status_code=503, detail="Server is not ready to stop.")

    background_tasks.add_task(setattr, server, "should_exit", True)
    return HTMLResponse(
        content=(
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<title>Сервер остановлен</title>"
            "<body style='font:16px sans-serif;max-width:600px;margin:4rem auto'>"
            "<h1>Сервер останавливается</h1>"
            "<p>Это окно можно закрыть.</p></body></html>"
        ),
        headers={"Connection": "close"},
        background=background_tasks,
    )

@app.get("/stop")
async def explain_stop_method(request: Request):
    if is_cloudflare_request(request):
        raise HTTPException(
            status_code=403,
            detail="Server shutdown is disabled through the public tunnel.",
        )

    client = request.client
    try:
        client_ip = ipaddress.ip_address(client.host) if client is not None else None
    except ValueError:
        client_ip = None

    if client_ip not in request.app.state.trusted_stop_ips:
        raise HTTPException(status_code=403, detail="IP is not allowed to stop the server.")

    stop_tokens = request.app.state.stop_tokens
    now = perf_counter()
    for address, (_, expires_at) in list(stop_tokens.items()):
        if expires_at < now:
            del stop_tokens[address]

    token = secrets.token_urlsafe(32)
    stop_tokens[str(client_ip)] = (token, now + 300)
    safe_token = escape(token, quote=True)
    return HTMLResponse(
        content=(
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Остановка сервера</title>"
            "<body style='font:16px sans-serif;max-width:600px;margin:4rem auto;padding:0 1rem'>"
            "<h1>Остановить сервер?</h1>"
            "<p>Сервер инвентаризации будет выключен для всех подключённых устройств.</p>"
            "<form method='post' action='/stop'>"
            f"<input type='hidden' name='token' value='{safe_token}'>"
            "<button style='padding:.7rem 1rem;background:#b42318;color:white;"
            "border:0;border-radius:6px;cursor:pointer' type='submit'>"
            "Да, остановить сервер</button></form></body></html>"
        )
    )

@app.get("/")
async def root(request: Request):
    client = request.client
    try:
        client_ip = ipaddress.ip_address(client.host) if client is not None else None
    except ValueError:
        client_ip = None

    trusted_ips = request.app.state.trusted_stop_ips
    is_trusted = client_ip in trusted_ips and not is_cloudflare_request(request)
    page_path = Path(__file__).resolve().parent / "index.html"
    try:
        page = page_path.read_text(encoding="utf-8")
    except OSError as error:
        raise HTTPException(status_code=500, detail="Главная страница недоступна.") from error

    access_badge = (
        '<span class="badge trusted">Доверенное устройство</span>'
        if is_trusted
        else '<span class="badge">Обычный доступ</span>'
    )
    stop_button = (
        '<a class="button danger" href="/stop">Остановить сервер</a>'
        if is_trusted
        else ""
    )
    page = page.replace("{{ACCESS_BADGE}}", access_badge)
    page = page.replace("{{STOP_BUTTON}}", stop_button)
    return HTMLResponse(page)

@app.get("/download")
async def download_sender():
    executable_path = (
        Path(__file__).resolve().parent.parent / "Inventarizatsiya.exe"
    )
    if not executable_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Файл Inventarizatsiya.exe пока не собран.",
        )
    return FileResponse(
        executable_path,
        media_type="application/vnd.microsoft.portable-executable",
        filename=executable_path.name,
    )

@app.get("/status/")
async def get_status():
    return {
        "key": "Ineventorizatsiya",
        "status": 200,
        "message": "Сервер работает!",
    }

@app.post("/send-dict/")
async def receive_dict(request: Request, data: dict[str, object] = Body(...)):
    print("Тип данных:", type(data))
    print("Полученный словарь (dict) на сервере:", data)

    item_name = data.get("name", "Неизвестно")
    try:
        record_id = await asyncio.to_thread(
            request.app.state.inventory_database.save_inventory,
            data,
        )
    except DatabaseError as error:
        console.print(f"[bold red]Не удалось сохранить инвентаризацию:[/] {error}")
        raise HTTPException(
            status_code=503,
            detail="Не удалось сохранить данные инвентаризации.",
        ) from error
    return {
        "status": "success",
        "message": f"Словарь для товара '{item_name}' успешно принят!",
        "record_id": record_id,
        "received_dict": data,
    }

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui-api", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    arguments = parser.parse_args()

    if arguments.gui_api:
        if not os.environ.get("INVENTORY_GUI_API_TOKEN"):
            parser.error("INVENTORY_GUI_API_TOKEN is required with --gui-api.")
        if not 1 <= arguments.port <= 65535:
            parser.error("--port must be between 1 and 65535 with --gui-api.")
        server = uvicorn.Server(
            uvicorn.Config(
                control_app,
                host="127.0.0.1",
                port=arguments.port,
                access_log=False,
                log_level="warning",
            )
        )
        control_app.state.server = server
        server.run()
        return

    run_server(app, console)


if __name__ == "__main__":
    main()
