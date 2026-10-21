from concurrent.futures import ThreadPoolExecutor
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import webbrowser

import pythoncom
import requests

try:
  import webview
except ImportError as error:
  webview = None
  WEBVIEW_IMPORT_ERROR = error
else:
  WEBVIEW_IMPORT_ERROR = None

from sysinfo import (
    get_ActiveDirectory,
    get_CPU,
    get_GPUs,
    get_Monitors,
    get_OS,
    get_RAMs,
    get_mouse_keyboard,
    get_motherboard,
    get_network_hardware_info,
    get_storages,
)


BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = (
  Path(sys.executable).resolve().parent
  if getattr(sys, "frozen", False)
  else Path(__file__).resolve().parent
)
WEB_DIR = BASE_DIR / "web"
HTML_PATH = WEB_DIR / "index.html"
WEB_ASSETS = {
  "/styles.css": (WEB_DIR / "styles.css", "text/css; charset=utf-8"),
  "/styles_ledger.css": (WEB_DIR / "styles_ledger.css", "text/css; charset=utf-8"),
  "/styles_neon.css": (WEB_DIR / "styles_neon.css", "text/css; charset=utf-8"),
  "/styles_harbor.css": (WEB_DIR / "styles_harbor.css", "text/css; charset=utf-8"),
  "/app.js": (WEB_DIR / "app.js", "text/javascript; charset=utf-8"),
}
CONFIG_PATH = (
  BASE_DIR / "server_config.json"
  if getattr(sys, "frozen", False)
  else APP_DIR / ".env"
)
COLLECTION_POOL = ThreadPoolExecutor(max_workers=5, thread_name_prefix="sysinfo")

COLLECTORS = {
  "Network Hardware": (get_network_hardware_info, []),
  "Motherboard": (get_motherboard, {}),
  "RAM": (get_RAMs, []),
  "Storages": (get_storages, []),
  "CPU": (get_CPU, []),
  "GPUs": (get_GPUs, []),
  "Monitors": (get_Monitors, []),
  "OS": (get_OS, {}),
  "ActiveDirectory": (get_ActiveDirectory, {}),
  "mouse_keyboard": (get_mouse_keyboard, {"keyboards": [], "mice": []}),
}


def _run_collector(function):
  pythoncom.CoInitialize()
  try:
    return function()
  finally:
    pythoncom.CoUninitialize()


def _load_server_urls():
  try:
    if getattr(sys, "frozen", False):
      config_values = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
      if not isinstance(config_values, dict):
        raise ValueError("Embedded server configuration must be a JSON object.")
    else:
      config_values = {}
      for line_number, line in enumerate(
        CONFIG_PATH.read_text(encoding="utf-8").splitlines(),
        start=1,
      ):
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
          continue
        if "=" not in stripped_line:
          raise ValueError(
            f"Invalid configuration at {CONFIG_PATH}:{line_number}; "
            "expected KEY=VALUE."
          )
        key, value = stripped_line.split("=", 1)
        config_values[key.strip()] = value.strip().strip("\"'")
  except (OSError, json.JSONDecodeError, ValueError) as error:
    raise RuntimeError(f"Cannot read server configuration {CONFIG_PATH}: {error}") from error

  local_server_url = str(config_values.get("INVENTORY_LOCAL_URL", "")).strip().rstrip("/")
  global_server_url = str(config_values.get("INVENTORY_GLOBAL_URL", "")).strip().rstrip("/")

  servers = []
  for scope, url in (
    ("local", local_server_url),
    ("global", global_server_url),
  ):
    if not url:
      continue
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
      raise RuntimeError(
        f"Invalid {scope} server URL '{url}' in {CONFIG_PATH}; "
        "use a full http:// or https:// URL."
      )
    if parsed_url.username or parsed_url.password:
      raise RuntimeError(f"Do not put credentials in the {scope} server URL.")
    if any(existing_url == url for _, existing_url in servers):
      continue
    servers.append((scope, url))

  return servers


def _normalize_server_url(value):
  value = value.strip()
  if not value:
    return None

  candidate = value if "://" in value else f"https://{value}"
  try:
    parsed_url = urlsplit(candidate)
    hostname = parsed_url.hostname
    port = parsed_url.port
  except ValueError as error:
    raise ValueError(f"Invalid server address: {error}") from error

  if parsed_url.scheme not in ("http", "https") or not hostname:
    raise ValueError("Enter an IP address or a full http:// or https:// URL.")
  if parsed_url.username or parsed_url.password:
    raise ValueError("Credentials are not allowed in the server address.")
  if parsed_url.path not in ("", "/") or parsed_url.query or parsed_url.fragment:
    raise ValueError("Enter only the server address, without a path or query.")

  if port is None and parsed_url.scheme == "https":
    try:
      ip_address = ipaddress.ip_address(hostname)
    except ValueError:
      ip_address = None
    if ip_address is not None and (ip_address.is_private or ip_address.is_loopback):
      candidate = f"{candidate}:8000"

  return candidate.rstrip("/")


def _verify_server_url(url):
  parsed_url = urlsplit(url)
  if parsed_url.scheme != "https":
    return True
  try:
    ip_address = ipaddress.ip_address(parsed_url.hostname or "")
  except ValueError:
    return True
  if ip_address.is_private or ip_address.is_loopback or ip_address.is_link_local:
    return str(BASE_DIR / "rootCA.pem")
  return True


class InventoryApi:
  def __init__(self):
    self.inventory = None
    self.base_url = None
    self.base_verify = True
    self.manual_server_url = None
    self.http = requests.Session()

  def check_connection(self, server_url=None):
    if server_url is not None:
      try:
        self.manual_server_url = _normalize_server_url(server_url)
      except ValueError as error:
        self.manual_server_url = ""
        self.base_url = None
        return {"ok": False, "error": str(error)}

    if self.manual_server_url is not None:
      if not self.manual_server_url:
        self.base_url = None
        return {"ok": False, "error": "Enter a valid server IP address or URL."}
      servers = [
        (
          "global" if _verify_server_url(self.manual_server_url) is True else "local",
          self.manual_server_url,
        )
      ]
    else:
      try:
        servers = _load_server_urls()
      except RuntimeError as error:
        self.base_url = None
        return {"ok": False, "error": str(error)}

      if not servers:
        self.base_url = None
        return {
          "ok": False,
          "error": f"No server URLs configured in {CONFIG_PATH}.",
        }

    errors = []
    for scope, base_url in servers:
      verify = _verify_server_url(base_url) if self.manual_server_url else (
        str(BASE_DIR / "rootCA.pem") if scope == "local" else True
      )
      try:
        response = self.http.get(
          f"{base_url}/status/",
          verify=verify,
          timeout=2,
        )
        response.raise_for_status()
        status = response.json()
        if isinstance(status, dict) and status.get("key") == "Ineventorizatsiya":
          self.base_url = base_url
          self.base_verify = verify
          return {
            "ok": True,
            "scope": scope,
            "server": base_url,
            "message": status.get("message", "Сервер доступен"),
          }
        errors.append(f"{scope}: неверный ключ статуса")
      except (requests.RequestException, ValueError) as error:
        errors.append(f"{scope}: {error}")

    self.base_url = None
    return {"ok": False, "error": "; ".join(errors)}

  def get_inventory(self):
    futures = {
      name: COLLECTION_POOL.submit(_run_collector, collector)
      for name, (collector, _) in COLLECTORS.items()
    }
    inventory = {}
    errors = {}
    for name, future in futures.items():
      try:
        inventory[name] = future.result()
      except Exception as error:
        inventory[name] = COLLECTORS[name][1]
        errors[name] = str(error)
    if errors:
      inventory["Collection Errors"] = errors

    self.inventory = inventory
    return self.inventory

  def send_inventory(self, fio=None, location=None, additional=None):
    if self.base_url is None:
      connection = self.check_connection()
      if not connection["ok"]:
        return {
          "ok": False,
          "error": f"Сервер инвентаризации недоступен: {connection['error']}",
        }

    server_url = self.base_url
    if server_url is None:
      return {"ok": False, "error": "Не выбран сервер инвентаризации."}

    inventory = self.inventory or self.get_inventory()
    fio = fio.strip() if isinstance(fio, str) and fio.strip() else None
    location = location.strip() if isinstance(location, str) and location.strip() else None
    additional = (
      additional.strip()
      if isinstance(additional, str) and additional.strip()
      else None
    )
    payload = {
      "name": "Noutbuk",
      "fio": fio,
      "location": location,
      "additional": additional,
      "SysInfo": inventory,
      "in_stock": True,
    }

    try:
      response = self.http.post(
        f"{server_url}/send-dict/",
        json=payload,
        verify=self.base_verify,
        timeout=20,
      )
      response.raise_for_status()
      return {
        "ok": True,
        "status_code": response.status_code,
        "response": response.json(),
      }
    except requests.RequestException as error:
      self.base_url = None
      return {"ok": False, "error": str(error)}
    except ValueError as error:
      return {"ok": False, "error": f"Сервер вернул некорректный JSON: {error}"}


class BrowserRequestHandler(BaseHTTPRequestHandler):
  api = None
  html_path = None
  server_port = None

  def _send_json(self, status_code, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    self.send_response(status_code)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)

  def _is_local_request(self):
    allowed_hosts = {
      f"127.0.0.1:{self.server_port}",
      f"localhost:{self.server_port}",
    }
    if self.headers.get("Host") not in allowed_hosts:
      return False

    origin = self.headers.get("Origin")
    return origin is None or origin in {
      f"http://127.0.0.1:{self.server_port}",
      f"http://localhost:{self.server_port}",
    }

  def do_GET(self):
    if not self._is_local_request():
      self._send_json(403, {"error": "Local requests only"})
      return

    request_path = urlsplit(self.path).path
    if request_path in ("/", "/index.html"):
      try:
        body = self.html_path.read_bytes()
      except OSError as error:
        self._send_json(500, {"error": f"Cannot read UI file: {error}"})
        return
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)
      return

    asset = WEB_ASSETS.get(request_path)
    if asset is not None:
      asset_path, content_type = asset
      try:
        body = asset_path.read_bytes()
      except OSError as error:
        self._send_json(500, {"error": f"Cannot read UI asset: {error}"})
        return
      self.send_response(200)
      self.send_header("Content-Type", content_type)
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)
      return

    if request_path == "/api/check-connection":
      self._send_json(200, self.api.check_connection())
      return

    if request_path == "/api/inventory":
      self._send_json(200, self.api.get_inventory())
      return

    self._send_json(404, {"error": "Not found"})

  def do_POST(self):
    if not self._is_local_request():
      self._send_json(403, {"error": "Local requests only"})
      return

    if self.path not in ("/api/send-inventory", "/api/check-connection"):
      self._send_json(404, {"error": "Not found"})
      return

    try:
      content_length = int(self.headers.get("Content-Length", "0"))
      if content_length <= 0:
        self._send_json(400, {"error": "Request body is empty"})
        return
      if content_length > 1_000_000:
        self._send_json(413, {"error": "Request body is too large"})
        return
      payload = json.loads(self.rfile.read(content_length))
      if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    except (ValueError, UnicodeDecodeError) as error:
      self._send_json(400, {"error": f"Invalid JSON request: {error}"})
      return

    if self.path == "/api/check-connection":
      server_url = payload.get("server_url")
      if server_url is not None and not isinstance(server_url, str):
        self._send_json(400, {"error": "server_url must be a string"})
        return
      result = self.api.check_connection(server_url)
    else:
      result = self.api.send_inventory(
        payload.get("fio"),
        payload.get("location"),
        payload.get("additional"),
      )
    self._send_json(200, result)

  def log_message(self, format_string, *args):
    print(f"Browser UI: {format_string % args}")


def run_browser_fallback(api):
  try:
    port = int(os.environ.get("INVENTORY_UI_PORT", "8765"))
  except ValueError as error:
    raise RuntimeError("INVENTORY_UI_PORT must be a valid TCP port number.") from error
  if not 1 <= port <= 65535:
    raise RuntimeError("INVENTORY_UI_PORT must be between 1 and 65535.")

  handler = type(
    "ConfiguredBrowserRequestHandler",
    (BrowserRequestHandler,),
    {
      "api": api,
      "html_path": HTML_PATH,
      "server_port": port,
    },
  )

  try:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
  except OSError as error:
    raise RuntimeError(
      f"Could not start browser UI at http://127.0.0.1:{port}: {error}. "
      "Set INVENTORY_UI_PORT to another available port."
    ) from error

  url = f"http://127.0.0.1:{port}/"
  print(f"WebView could not start. Open the browser UI at {url}")
  webbrowser.open(url)
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\nBrowser UI stopped.")
  finally:
    server.server_close()


if __name__ == "__main__":
  api = InventoryApi()
  try:
    if webview is None:
      raise RuntimeError(f"pywebview could not be imported: {WEBVIEW_IMPORT_ERROR}")
    webview.create_window(
      "Инвентаризация",
      HTML_PATH.as_uri(),
      js_api=api,
      width=1280,
      height=860,
      min_size=(980, 640),
    )
    webview.start()
  except Exception as error:
    print(f"WebView failed to start: {error}")
    run_browser_fallback(api)
