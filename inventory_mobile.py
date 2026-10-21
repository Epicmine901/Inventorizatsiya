from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import secrets
import ssl
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPSHandler,
    HTTPRedirectHandler,
    Request,
    build_opener,
)


def load_config(path: Path) -> tuple[str, str, ssl.SSLContext]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось прочитать файл доступа {path}: {error}") from error
    if not isinstance(config, dict):
        raise RuntimeError("Файл доступа должен содержать JSON-объект.")

    server_url = config.get("server_url")
    access_key = config.get("access_key")
    ca_certificate = config.get("ca_certificate", "")
    if not isinstance(server_url, str) or not isinstance(access_key, str):
        raise RuntimeError("В файле доступа отсутствуют server_url или access_key.")
    parsed_url = urlsplit(server_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.path not in ("", "/")
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RuntimeError("Адрес сервера должен быть корневым HTTPS URL.")
    if not access_key:
        raise RuntimeError("Ключ доступа в файле пуст.")
    if not isinstance(ca_certificate, str):
        raise RuntimeError("Сертификат CA в файле доступа имеет неверный формат.")

    try:
        context = ssl.create_default_context(
            cadata=ca_certificate if ca_certificate else None
        )
    except ssl.SSLError as error:
        raise RuntimeError(f"Не удалось проверить сертификат сервера: {error}") from error
    return server_url.rstrip("/"), access_key, context


def fetch_records(server_url: str, access_key: str, context: ssl.SSLContext) -> list[dict[str, object]]:
    request = Request(
        f"{server_url}/api/mobile/inventory?limit=500",
        headers={"X-Inventory-Access-Key": access_key},
    )
    class NoRedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = build_opener(HTTPSHandler(context=context), NoRedirectHandler())
    try:
        with opener.open(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
            detail = body.get("detail", error.reason) if isinstance(body, dict) else error.reason
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = error.reason
        raise RuntimeError(f"Сервер вернул ошибку {error.code}: {detail}") from error
    except (URLError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Не удалось подключиться к серверу: {error}") from error

    records = result.get("records") if isinstance(result, dict) else None
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise RuntimeError("Сервер вернул некорректный список записей.")
    return records


GROUP_LABELS = {
    "Network Hardware": "Сетевые адаптеры",
    "Motherboard": "Материнская плата",
    "RAM": "Оперативная память",
    "Storages": "Накопители",
    "CPU": "Процессоры",
    "GPUs": "Видеокарты",
    "Monitors": "Мониторы",
    "OS": "Операционная система",
    "ActiveDirectory": "Домен и пользователь",
    "mouse_keyboard": "Клавиатура и мышь",
}
GROUP_ORDER = tuple(GROUP_LABELS)
FIELD_LABELS = {
    "manufacturer": "Производитель",
    "model": "Модель",
    "name": "Имя",
    "type": "Тип",
    "ip": "IP-адрес",
    "ip_address": "IP-адрес",
    "mac": "MAC-адрес",
    "mac_address": "MAC-адрес",
    "capacity": "Объём",
    "capacity_gb": "Объём, ГБ",
    "size": "Объём",
    "frequency": "Частота",
    "frequency_mhz": "Частота, МГц",
    "speed_mhz": "Частота, МГц",
    "slot": "Слот",
    "serial": "Серийный номер",
    "serial_number": "Серийный номер",
    "uuid": "UUID",
    "cores": "Физические ядра",
    "threads": "Логические потоки",
    "clock": "Частота",
    "socket": "Сокет",
    "driver_version": "Версия драйвера",
    "status": "Статус",
    "interface": "Интерфейс",
    "volume_decimal_gb": "Объём (десятичный)",
    "volume_windows_gb": "Объём (Windows)",
    "volume_bytes": "Объём, байт",
}
RECORD_COLUMNS = (
    ("name", "Устройство"),
    ("fio", "Ответственный"),
    ("location", "Местоположение"),
    ("received_at", "Дата получения"),
    ("in_stock", "На складе"),
)
_INITIAL_DEFAULT_SETTINGS: dict[str, object] = {
  "columns": {
    "10": False,
    "14": False,
    "15": False
  },
  "fields": {
    "2": {
      "uuid": False,
      "serialnumber": False
    },
    "3": {
      "serialnumber": False,
      "slot": False
    },
    "4": {
      "serialnumber": False,
      "interface": False,
      "capacitydecimal": False,
      "capacitybytes": False
    },
    "5": {
      "processorid": False,
      "socket": False,
      "threads": False
    },
    "6": {
      "status": False,
      "driverversion": False
    },
    "8": {
      "registereduser": False,
      "installdate": False,
      "manufacturer": False,
      "architecture": False,
      "build": False
    },
    "9": {
      "isindomain": True,
      "domainorworkgroupname": False,
      "primaryowner": False
    }
  },
  "textSize": 10
}
default_config = _INITIAL_DEFAULT_SETTINGS
default_settings = default_config


def inventory_sysinfo(record: dict[str, object]) -> dict[str, object]:
    raw_payload = record.get("payload", "{}")
    try:
        payload = json.loads(str(raw_payload))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    sysinfo = payload.get("SysInfo")
    return sysinfo if isinstance(sysinfo, dict) else {}


def humanize_field(key: object) -> str:
    text = str(key)
    normalized = text.replace(" ", "_").lower()
    if normalized in FIELD_LABELS:
        return FIELD_LABELS[normalized]
    return text.replace("_", " ").strip().capitalize()


def render_table_value(value: object, column_index: int) -> str:
    if isinstance(value, dict):
        return "".join(
            "<div class='cell-detail' data-column='"
            + str(column_index)
            + "' data-field='"
            + escape(str(key), quote=True)
            + "'><span>"
            + escape(humanize_field(key))
            + ":</span> "
            + render_table_value(item, column_index)
            + "</div>"
            for key, item in value.items()
            if item is not None and item != ""
        ) or "—"
    if isinstance(value, list):
        return "".join(
            "<div class='cell-item'><b>"
            + str(index)
            + ".</b> "
            + render_table_value(item, column_index)
            + "</div>"
            for index, item in enumerate(value, 1)
            if item is not None and item != ""
        ) or "—"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if value is None or value == "":
        return "—"
    return escape(str(value))


def render_page(records: list[dict[str, object]]) -> bytes:
    group_keys = {
        key
        for record in records
        for key in inventory_sysinfo(record)
    }
    ordered_group_keys = [key for key in GROUP_ORDER if key in group_keys]
    ordered_group_keys.extend(sorted(group_keys - set(ordered_group_keys)))
    headers = ["<th class='id-column' data-column='0'>ID</th>"]
    headers.extend(
        "<th data-column='"
        + str(index)
        + "'>"
        + escape(GROUP_LABELS.get(key, humanize_field(key)))
        + "</th>"
        for index, key in enumerate(ordered_group_keys, 1)
    )
    headers.extend(
        "<th data-column='"
        + str(index)
        + "'>"
        + escape(label)
        + "</th>"
        for index, (_, label) in enumerate(
            RECORD_COLUMNS,
            len(ordered_group_keys) + 1,
        )
    )
    rows = []
    for record in records:
        cells = [
            "<td class='id-column' data-column='0'>"
            + escape(str(record.get("id", "")))
            + "</td>"
        ]
        sysinfo = inventory_sysinfo(record)
        cells.extend(
            "<td data-column='"
            + str(index)
            + "'>"
            + render_table_value(sysinfo.get(key), index)
            + "</td>"
            for index, key in enumerate(ordered_group_keys, 1)
        )
        cells.extend(
            "<td data-column='"
            + str(index)
            + "'>"
            + render_table_value(
                (
                    "Да" if bool(record.get("in_stock")) else "Нет"
                )
                if field == "in_stock"
                else record.get(field),
                index,
            )
            + "</td>"
            for index, (field, _) in enumerate(
                RECORD_COLUMNS,
                len(ordered_group_keys) + 1,
            )
        )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    table_markup = (
        "<div class='table-wrap'><table><thead><tr>"
        + "".join(headers)
        + "</tr></thead><tbody>"
        + ("".join(rows) if rows else "")
        + "</tbody></table></div>"
        if rows
        else "<p class='empty'>В таблице пока нет записей.</p>"
    )
    configured_settings = default_config
    if (
        default_config is _INITIAL_DEFAULT_SETTINGS
        and default_settings is not _INITIAL_DEFAULT_SETTINGS
    ):
        configured_settings = default_settings
    default_settings_json = json.dumps(
        configured_settings,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>Инвентаризация</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f1f5f9;color:#182536;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{width:min(100%,1280px);margin:auto;padding:20px 14px 48px}}
.top{{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:18px}}
h1{{font-size:clamp(22px,5vw,32px);margin:0}}
.muted{{color:#66758a;font-size:14px;margin:4px 0 0}}
button{{border:0;border-radius:10px;background:#147d8c;color:white;padding:11px 16px;font:inherit;font-weight:650}}
.actions{{display:flex;gap:8px;align-items:center}}
.secondary-button{{background:white;color:#147d8c;border:1px solid #b9d4d9}}
.custom-panel{{margin:0 0 14px;padding:16px;background:white;border:1px solid #dce4ec;border-radius:10px;box-shadow:0 8px 24px #18253612}}
.panel-heading{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;font-weight:700}}
.panel-actions{{display:flex;flex-wrap:wrap;gap:6px}}
.settings-message{{margin:0 0 10px;color:#147d8c;font-size:13px}}
.settings-message:empty{{display:none}}
.text-size-control{{display:grid;grid-template-columns:auto minmax(120px,320px) auto;align-items:center;gap:12px;margin-bottom:14px;padding:12px;background:#f5f8fb;border-radius:8px;font-size:14px}}
.text-size-control input{{width:100%;accent-color:#147d8c}}
.text-size-value{{min-width:42px;text-align:right;color:#147d8c;font-weight:700}}
.options-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}}
.column-option{{min-width:0;padding:10px;border:1px solid #e3eaf0;border-radius:8px}}
.option-main,.field-option{{display:flex;align-items:flex-start;gap:8px;font-size:14px}}
.option-main{{font-weight:650}}
.field-options{{display:grid;gap:5px;margin:8px 0 0 24px}}
.field-option{{color:#66758a;font-weight:400}}
.option-main input,.field-option input{{margin-top:4px;accent-color:#147d8c}}
.reset-button{{padding:6px 10px;font-size:13px}}
.table-wrap{{width:100%;overflow:auto;background:white;border:1px solid #dce4ec;border-radius:8px}}
table{{width:100%;min-width:100%;table-layout:fixed;border-collapse:separate;border-spacing:0;font-size:var(--table-font-size,14px)}}
th,td{{min-width:var(--table-min-column-width,76px);padding:calc(var(--table-font-size,14px)*.7) calc(var(--table-font-size,14px)*.8);text-align:left;vertical-align:top;border-right:1px solid #e3eaf0;border-bottom:1px solid #e3eaf0;overflow-wrap:anywhere}}
th{{position:sticky;top:0;z-index:2;background:#14263a;color:white;font-weight:650}}
th.id-column,td.id-column{{position:sticky;left:0;z-index:3;min-width:64px;width:64px;background:#e7f1f4;color:#147d8c;font-weight:700}}
th.id-column{{z-index:4;background:#14263a;color:white}}
tbody tr:nth-child(even){{background:#f5f8fb}}
tbody tr:last-child td{{border-bottom:0}}
td:last-child,th:last-child{{border-right:0}}
.cell-detail{{margin:0 0 5px}}
.cell-detail:last-child{{margin-bottom:0}}
.cell-detail span{{color:#718096}}
.cell-item{{padding-bottom:6px}}
.cell-item:last-child{{padding-bottom:0}}
.empty{{padding:24px;text-align:center;color:#66758a}}
footer{{margin-top:16px;color:#718096;font-size:13px}}
@media(max-width:600px){{.top{{align-items:flex-start;flex-direction:column}}.actions{{width:100%;justify-content:flex-end}}.text-size-control{{grid-template-columns:1fr auto}}.text-size-control input{{grid-column:1/-1;grid-row:2}}}}
</style>
</head>
<body><main>
<div class="top"><div><h1>Таблица инвентаризации</h1>
<p class="muted">Записей: {len(records)} · Обновлено: {escape(datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S"))}</p>
</div><div class="actions"><button class="secondary-button" id="customize-button" type="button" aria-expanded="false">Настроить таблицу</button><button onclick="location.reload()">Обновить</button></div></div>
<section class="custom-panel" id="customize-panel" hidden>
<div class="panel-heading"><span>Столбцы и поля</span><div class="panel-actions"><button class="reset-button" id="copy-view" type="button">Скопировать</button><button class="reset-button" id="paste-view" type="button">Вставить</button><button class="reset-button" id="reset-view" type="button">Сбросить настройки</button></div></div>
<p class="settings-message" id="settings-message" role="status" aria-live="polite"></p>
<label class="text-size-control" for="text-size"><span>Размер текста и столбцов</span><input id="text-size" type="range" min="8" max="16" step="1" value="14"><output class="text-size-value" id="text-size-value" for="text-size">14 px</output></label>
<div class="options-grid" id="column-options"></div>
</section>
{table_markup}
<footer>Локальный просмотр защищённого сервера инвентаризации.</footer>
</main>
<script>
(() => {{
  const table = document.querySelector("table");
  const panel = document.getElementById("customize-panel");
  const button = document.getElementById("customize-button");
  const options = document.getElementById("column-options");
  const textSize = document.getElementById("text-size");
  const textSizeValue = document.getElementById("text-size-value");
  const settingsMessage = document.getElementById("settings-message");
  const storageKey = "inventory-table-view-v1";
  const defaults = {default_settings_json};
  const normalizeFieldKey = (key) => {{
    const normalized = String(key).toLowerCase().replace(/[^a-z0-9]/g, "");
    return normalized === "serial" ? "serialnumber" : normalized;
  }};
  const cloneSettings = (settings) => ({{
    ...settings,
    columns: {{...(settings.columns || {{}})}},
    fields: Object.fromEntries(
      Object.entries(settings.fields || {{}}).map(([column, fields]) => [
        column,
        Object.fromEntries(
          Object.entries(fields || {{}}).map(([field, visible]) => [
            normalizeFieldKey(field),
            visible,
          ]),
        ),
      ]),
    ),
  }});
  const mergeSettings = (base, override) => {{
    const merged = cloneSettings(base);
    Object.assign(merged, override);
    merged.columns = {{...(base.columns || {{}}), ...(override.columns || {{}})}};
    for (const [column, fields] of Object.entries(override.fields || {{}})) {{
      merged.fields[column] = {{...(merged.fields[column] || {{}}), ...fields}};
    }}
    return merged;
  }};
  let saved = cloneSettings(defaults);
  let currentTextSize = 14;
  try {{
    const stored = JSON.parse(localStorage.getItem(storageKey) || "{{}}");
    if (stored && typeof stored === "object" && !Array.isArray(stored)) {{
      saved = mergeSettings(defaults, stored);
    }}
  }} catch (error) {{
    localStorage.removeItem(storageKey);
  }}

  const save = () => localStorage.setItem(storageKey, JSON.stringify(saved));
  const applyTextSize = (value) => {{
    const size = Math.min(16, Math.max(8, Number(value) || 14));
    currentTextSize = size;
    document.documentElement.style.setProperty("--table-font-size", size + "px");
    document.documentElement.style.setProperty(
      "--table-min-column-width",
      Math.max(48, Math.round(size * 5.5)) + "px",
    );
    textSize.value = String(size);
    textSizeValue.value = size + " px";
    textSizeValue.textContent = size + " px";
  }};
  const applyTableWidth = () => {{
    if (!table) return;
    const visibleColumns = [...table.querySelectorAll("thead th:not(.id-column)")]
      .filter((header) => !header.hidden).length;
    const minColumnWidth = Math.max(48, Math.round(currentTextSize * 5.5));
    const preferredWidth = 64 + visibleColumns * minColumnWidth;
    table.style.minWidth =
      Math.max(table.parentElement.clientWidth, preferredWidth) + "px";
  }};
  const apply = () => {{
    if (!table) return;
    table.querySelectorAll("[data-column]").forEach((cell) => {{
      const column = cell.dataset.column;
      cell.hidden = saved.columns?.[column] === false;
    }});
    table.querySelectorAll(".cell-detail[data-field]").forEach((field) => {{
      const column = field.dataset.column;
      const key = normalizeFieldKey(field.dataset.field);
      field.hidden = saved.fields?.[column]?.[key] === false;
    }});
    applyTableWidth();
  }};
  const validateSettings = (value) => {{
    if (!value || typeof value !== "object" || Array.isArray(value)) {{
      throw new Error("Настройки должны быть JSON-объектом.");
    }}
    const result = {{}};
    if (value.textSize !== undefined) {{
      const size = Number(value.textSize);
      if (!Number.isInteger(size) || size < 8 || size > 16) {{
        throw new Error("Размер текста должен быть целым числом от 8 до 16.");
      }}
      result.textSize = size;
    }}
    for (const section of ["columns", "fields"]) {{
      if (value[section] === undefined) continue;
      if (!value[section] || typeof value[section] !== "object" || Array.isArray(value[section])) {{
        throw new Error("Раздел " + section + " должен быть JSON-объектом.");
      }}
      result[section] = {{}};
      for (const [column, setting] of Object.entries(value[section])) {{
        if (section === "columns") {{
          if (typeof setting !== "boolean") {{
            throw new Error("Для столбцов используйте true или false.");
          }}
          result.columns[column] = setting;
          continue;
        }}
        if (!setting || typeof setting !== "object" || Array.isArray(setting)) {{
          throw new Error("Поля столбца должны быть JSON-объектом.");
        }}
        result.fields[column] = {{}};
        for (const [field, visible] of Object.entries(setting)) {{
          if (typeof visible !== "boolean") {{
            throw new Error("Для полей используйте true или false.");
          }}
          result.fields[column][field] = visible;
        }}
      }}
    }}
    return result;
  }};
  const syncControls = () => {{
    options.querySelectorAll("input[data-setting-type]").forEach((checkbox) => {{
      const {{settingType, column, field}} = checkbox.dataset;
      checkbox.checked = settingType === "column"
        ? saved.columns?.[column] !== false
        : saved.fields?.[column]?.[normalizeFieldKey(field)] !== false;
    }});
    applyTextSize(saved.textSize);
    apply();
  }};
  const useSettings = (settings) => {{
    saved = mergeSettings(defaults, settings);
    syncControls();
    save();
  }};
  applyTextSize(saved.textSize);
  applyTableWidth();
  textSize.addEventListener("input", () => {{
    applyTextSize(textSize.value);
    saved.textSize = Number(textSize.value);
    applyTableWidth();
    save();
  }});
  window.addEventListener("resize", applyTableWidth);
  const addCheckbox = (parent, labelText, checked, onChange, className) => {{
    const label = document.createElement("label");
    label.className = className;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = checked;
    if (className === "option-main") {{
      checkbox.dataset.settingType = "column";
      checkbox.dataset.column = parent.dataset.column;
    }} else {{
      checkbox.dataset.settingType = "field";
      checkbox.dataset.column = parent.dataset.column;
    }}
    checkbox.addEventListener("change", () => onChange(checkbox.checked));
    label.append(checkbox, document.createTextNode(labelText));
    parent.append(label);
    return checkbox;
  }};

  if (table) {{
    table.querySelectorAll("thead th[data-column]").forEach((header) => {{
      const column = header.dataset.column;
      const group = document.createElement("div");
      group.className = "column-option";
      group.dataset.column = column;
      const labelText = header.textContent.trim();
      addCheckbox(
        group,
        labelText,
        saved.columns?.[column] !== false,
        (checked) => {{
          saved.columns ??= {{}};
          saved.columns[column] = checked;
          save();
          apply();
        }},
        "option-main",
      );
      const fields = new Map();
      table.querySelectorAll(
        '.cell-detail[data-column="' + column + '"][data-field]',
      ).forEach((field) => {{
        const key = field.dataset.field;
        if (!fields.has(key)) {{
          fields.set(key, field.querySelector("span")?.textContent.replace(/:$/, "") || key);
        }}
      }});
      if (fields.size) {{
        const fieldOptions = document.createElement("div");
        fieldOptions.className = "field-options";
        fieldOptions.dataset.column = column;
        fields.forEach((fieldLabel, key) => {{
          const fieldCheckbox = addCheckbox(
            fieldOptions,
            fieldLabel,
            saved.fields?.[column]?.[normalizeFieldKey(key)] !== false,
            (checked) => {{
              saved.fields ??= {{}};
              saved.fields[column] ??= {{}};
              saved.fields[column][normalizeFieldKey(key)] = checked;
              save();
              apply();
            }},
            "field-option",
          );
          fieldCheckbox.dataset.field = key;
        }});
        group.append(fieldOptions);
      }}
      options.append(group);
    }});
  }} else {{
    options.textContent = "В таблице пока нет записей.";
  }}

  apply();
  button.addEventListener("click", () => {{
    panel.hidden = !panel.hidden;
    button.setAttribute("aria-expanded", String(!panel.hidden));
  }});
  document.getElementById("copy-view").addEventListener("click", async () => {{
    const text = JSON.stringify(saved, null, 2);
    try {{
      if (!navigator.clipboard?.writeText) throw new Error("Буфер обмена недоступен.");
      await navigator.clipboard.writeText(text);
      settingsMessage.textContent = "Настройки скопированы. Вставьте их на другом устройстве.";
    }} catch (error) {{
      window.prompt("Скопируйте настройки из окна:", text);
      settingsMessage.textContent = "Если копирование не произошло автоматически, скопируйте JSON из окна.";
    }}
  }});
  document.getElementById("paste-view").addEventListener("click", async () => {{
    let text;
    try {{
      if (!navigator.clipboard?.readText) throw new Error("Буфер обмена недоступен.");
      text = await navigator.clipboard.readText();
    }} catch (error) {{
      text = window.prompt("Вставьте JSON настроек, скопированный из таблицы:");
    }}
    if (text === null || text === undefined) return;
    try {{
      useSettings(validateSettings(JSON.parse(text)));
      settingsMessage.textContent = "Настройки вставлены и сохранены в этом браузере.";
    }} catch (error) {{
      settingsMessage.textContent = "Не удалось вставить настройки: " + error.message;
    }}
  }});
  document.getElementById("reset-view").addEventListener("click", () => {{
    saved = cloneSettings(defaults);
    localStorage.removeItem(storageKey);
    syncControls();
    settingsMessage.textContent = "Настройки браузера сброшены до значений default_config.";
  }});
}})();
</script>
</body></html>"""
    return page.encode("utf-8")


def open_browser(url: str) -> None:
    for command in ("termux-open-url", "termux-open"):
        executable = shutil.which(command)
        if executable is not None:
            subprocess.run([executable, url], check=True)
            return
    import webbrowser

    if not webbrowser.open(url):
        raise RuntimeError("Не удалось открыть браузер. Откройте ссылку вручную: " + url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Открыть таблицу инвентаризации в браузере.")
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("inventory_access.json"),
        help="путь к inventory_access.json (по умолчанию рядом с этим файлом)",
    )
    arguments = parser.parse_args()

    server_url, access_key, context = load_config(arguments.config)

    class BrowserHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != f"/{browser_token}":
                self.send_error(404)
                return
            try:
                records = fetch_records(server_url, access_key, context)
                content = render_page(records)
                status = 200
            except RuntimeError as error:
                content = (
                    "<!doctype html><html lang='ru'><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>Ошибка</title><body style='font:16px system-ui;padding:24px'>"
                    "<h1>Не удалось загрузить таблицу</h1><p>"
                    + escape(str(error))
                    + "</p><button onclick='location.reload()'>Повторить</button></body></html>"
                ).encode("utf-8")
                status = 502
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            return

    browser_token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), BrowserHandler)
    url = f"http://127.0.0.1:{server.server_address[1]}/{browser_token}"
    print("Открываю таблицу в браузере. Для завершения вернитесь сюда и нажмите Ctrl+C.")
    try:
        open_browser(url)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nКлиент остановлен.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
