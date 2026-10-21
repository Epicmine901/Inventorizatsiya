    "use strict";

    const collectButton = document.getElementById("collect-button");
    const sendButton = document.getElementById("send-button");
    const serverControl = document.getElementById("server-control");
    const serverInput = document.getElementById("server-input");
    const serverRefresh = document.getElementById("server-refresh");
    const summary = document.getElementById("summary");
    const itemCount = document.getElementById("item-count");
    const result = document.getElementById("result");
    const connectionLabel = document.getElementById("connection-label");
    const connectionDot = document.getElementById("connection-dot");
    const themeStylesheet = document.getElementById("theme-stylesheet");
    const themeButtons = [...document.querySelectorAll("[data-theme-choice]")];
    const themeStylesheets = {
      ledger: "styles_ledger.css",
      neon: "styles_neon.css",
      harbor: "styles_harbor.css",
    };

    let inventory = null;
    let initialized = false;

    function selectTheme(theme, animate = true) {
      const stylesheet = themeStylesheets[theme];
      if (!stylesheet) return;
      document.documentElement.dataset.theme = theme;
      themeStylesheet.href = stylesheet;
      for (const button of themeButtons) {
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.themeChoice === theme),
        );
      }
      if (!animate || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        return;
      }
      document.body.classList.remove("theme-changing");
      void document.body.offsetWidth;
      document.body.classList.add("theme-changing");
      window.setTimeout(() => document.body.classList.remove("theme-changing"), 650);
    }

    for (const button of themeButtons) {
      button.addEventListener("click", () => {
        selectTheme(button.dataset.themeChoice);
      });
    }
    selectTheme("neon", false);

    const fieldLabels = {
      type: "Тип", name: "Имя", model: "Модель", manufacturer: "Производитель",
      ip: "IP-адрес", mac: "MAC-адрес", serial: "Серийный номер", uuid: "UUID",
      capacity_gb: "Объем, ГБ", speed_mhz: "Частота, МГц", slot: "Слот",
      capacity_decimal: "Объем (десятичный)", capacity_binary: "Объем (Windows)",
      capacity_bytes: "Объем, байт", interface: "Интерфейс", cores: "Физические ядра",
      threads: "Логические потоки", clock_speed: "Частота", socket: "Сокет",
      processor_id: "ID процессора", vram: "Видеопамять", driver_version: "Версия драйвера",
      status: "Статус", version: "Версия", build: "Сборка", architecture: "Разрядность",
      install_date: "Дата установки", registered_user: "Зарегистрированный пользователь",
      is_in_domain: "В домене", domain_or_workgroup_name: "Домен / рабочая группа",
      current_user: "Текущий пользователь", primary_owner: "Владелец ПК",
      description: "Описание", manufacturer_id: "Код производителя",
    };

    function setBusy(isBusy) {
      collectButton.disabled = isBusy;
      sendButton.disabled = isBusy;
    }

    function setStatus(message, state = "") {
      result.textContent = message;
      result.className = `result${state ? ` ${state}` : ""}`;
    }

    function setConnection(message, state = "") {
      connectionLabel.textContent = message;
      connectionDot.className = `dot${state ? ` ${state}` : ""}`;
    }

    function displayValue(value) {
      if (value === null || value === undefined || value === "") return "Неизвестно";
      return typeof value === "object" ? JSON.stringify(value) : String(value);
    }

    function createDetailRows(container, item) {
      for (const [key, value] of Object.entries(item || {})) {
        const row = document.createElement("div");
        row.className = "detail-row";

        const label = document.createElement("span");
        label.className = "detail-key";
        label.textContent = fieldLabels[key] || key;

        const detail = document.createElement("span");
        detail.className = "detail-value";
        detail.textContent = displayValue(value);

        row.append(label, detail);
        container.append(row);
      }
    }

    function addHardwareCard(iconName, title, value, details) {
      const card = document.createElement("details");
      card.className = "hardware-card";
      card.style.setProperty(
        "--card-index",
        String(summary.querySelectorAll(".hardware-card").length),
      );

      const heading = document.createElement("summary");
      const iconBox = document.createElement("span");
      iconBox.className = "hardware-icon";
      iconBox.dataset.icon = iconName;

      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("aria-hidden", "true");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", `#icon-${iconName}`);
      icon.append(use);
      iconBox.append(icon);

      const text = document.createElement("span");
      text.className = "hardware-text";
      const name = document.createElement("span");
      name.className = "hardware-name";
      name.textContent = title;
      const summaryValue = document.createElement("span");
      summaryValue.className = "hardware-value";
      summaryValue.textContent = displayValue(value);
      text.append(name, summaryValue);

      const chevron = document.createElement("span");
      chevron.className = "hardware-chevron";
      chevron.setAttribute("aria-hidden", "true");
      chevron.textContent = "⌄";
      heading.append(iconBox, text, chevron);

      const detailBox = document.createElement("div");
      detailBox.className = "hardware-details";
      const items = Array.isArray(details) ? details : [details || {}];
      items.forEach((item, index) => {
        if (items.length > 1) {
          const deviceTitle = document.createElement("div");
          deviceTitle.className = "detail-device-title";
          deviceTitle.textContent = `Устройство ${index + 1}: ${displayValue(item.model || item.name || item.type)}`;
          detailBox.append(deviceTitle);
        }
        createDetailRows(detailBox, item);
      });

      card.append(heading, detailBox);
      summary.append(card);
    }

    function renderHardware(data) {
      summary.replaceChildren();

      const deviceGroups = [
        ["Network Hardware", "Сетевые адаптеры", "network", (item) => `${item.type || ""} ${item.name || ""}`.trim()],
        ["RAM", "Оперативная память", "ram", (item) => `${item.capacity_gb || "?"} ГБ`],
        ["Storages", "Накопители", "storage", (item) => item.model || item.type],
        ["CPU", "Процессоры", "cpu", (item) => item.model],
        ["GPUs", "Видеокарты", "gpu", (item) => item.model],
        ["Monitors", "Мониторы", "monitor", (item) => item.model],
      ];

      const board = data.Motherboard || {};
      addHardwareCard("board", "Материнская плата", board.model, board);

      for (const [key, title, icon, getLabel] of deviceGroups) {
        const items = Array.isArray(data[key]) ? data[key] : [];
        if (!items.length) continue;
        const totalMemory = key === "RAM"
          ? items.reduce((total, item) => total + (Number(item.capacity_gb) || 0), 0)
          : 0;
        const value = key === "RAM" && items.length > 1
          ? `${items.length} модуля · ${totalMemory} ГБ`
          : items.length === 1
            ? getLabel(items[0])
            : `${items.length} шт.`;
        addHardwareCard(icon, title, value, items);
      }

      const os = data.OS || {};
      addHardwareCard("os", "Операционная система", os.name, os);
      const domain = data.ActiveDirectory || {};
      addHardwareCard("user", "Пользователь и домен", domain.current_user, domain);

      const devices = data.mouse_keyboard || {};
      for (const [key, title, icon] of [
        ["keyboards", "Клавиатуры", "keyboard"],
        ["mice", "Мыши и тачпады", "mouse"],
      ]) {
        const items = Array.isArray(devices[key]) ? devices[key] : [];
        if (!items.length) continue;
        const value = items.length === 1 ? items[0].model : `${items.length} шт.`;
        addHardwareCard(icon, title, value, items);
      }

      const errors = data["Collection Errors"] || {};
      if (Object.keys(errors).length) {
        addHardwareCard("alert", "Ошибки сбора", `${Object.keys(errors).length} разделов`, errors);
      }

      const cards = summary.querySelectorAll(".hardware-card").length;
      const deviceCount = deviceGroups.reduce(
        (total, [key]) => total + (Array.isArray(data[key]) ? data[key].length : 0),
        0,
      ) + (devices.keyboards || []).length + (devices.mice || []).length;
      itemCount.textContent = `${cards} категорий · ${deviceCount} устройств`;

      if (!cards) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "Данные об оборудовании не найдены.";
        summary.append(empty);
      }
    }

    async function callApi(method, ...args) {
      if (window.pywebview?.api) {
        return window.pywebview.api[method](...args);
      }

      const routes = {
        get_inventory: { url: "/api/inventory", method: "GET" },
        send_inventory: { url: "/api/send-inventory", method: "POST" },
        check_connection: { url: "/api/check-connection", method: args.length ? "POST" : "GET" },
      };
      const route = routes[method];
      if (!route) throw new Error(`Неизвестный метод API: ${method}`);

      const options = { method: route.method };
      if (method === "send_inventory") {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify({ fio: args[0], location: args[1], additional: args[2] });
      } else if (method === "check_connection" && args.length) {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify({ server_url: args[0] });
      }

      const response = await fetch(route.url, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    }

    async function collectInventory() {
      setBusy(true);
      setStatus("Собираю сведения об оборудовании...");
      try {
        inventory = await callApi("get_inventory");
        renderHardware(inventory);
        const errorCount = Object.keys(inventory["Collection Errors"] || {}).length;
        setStatus(
          errorCount ? `Собрано с ошибками в разделах: ${errorCount}` : "Сведения об оборудовании обновлены",
          errorCount ? "error" : "success",
        );
        return inventory;
      } catch (error) {
        setStatus(`Ошибка сбора: ${error}`, "error");
        return null;
      } finally {
        setBusy(false);
      }
    }

    async function checkServer(serverUrl) {
      serverRefresh.disabled = true;
      setConnection("Проверяю сервер...");
      try {
        const connection = await callApi("check_connection", serverUrl);
        if (!connection.ok) {
          setConnection("Сервер недоступен", "error");
          setStatus(connection.error, "error");
          return false;
        }
        serverInput.value = connection.server;
        const serverName = connection.scope === "local" ? "Локальный" : "Глобальный";
        setConnection(`${serverName} сервер подключен`, "ready");
        setStatus(`Сервер: ${connection.server}`, "success");
        return true;
      } catch (error) {
        setConnection("Ошибка проверки сервера", "error");
        setStatus(String(error), "error");
        return false;
      } finally {
        serverRefresh.disabled = false;
      }
    }

    collectButton.addEventListener("click", collectInventory);
    serverControl.addEventListener("submit", (event) => {
      event.preventDefault();
      checkServer(serverInput.value.trim());
    });

    sendButton.addEventListener("click", async () => {
      setBusy(true);
      try {
        if (!inventory) {
          setStatus("Собираю сведения об оборудовании...");
          inventory = await callApi("get_inventory");
          renderHardware(inventory);
        }

        setStatus("Отправляю инвентаризацию...");
        const fio = document.getElementById("fio-input").value.trim() || null;
        const location = document.getElementById("location-input").value.trim() || null;
        const additional = document.getElementById("additional-input").value.trim() || null;
        const response = await callApi("send_inventory", fio, location, additional);
        if (!response.ok) {
          setStatus(`Ошибка отправки: ${response.error}`, "error");
          return;
        }
        const message = response.response?.message;
        setStatus(
          `Успешно отправлено (HTTP ${response.status_code})${message ? `: ${message}` : ""}`,
          "success",
        );
      } catch (error) {
        setStatus(`Ошибка отправки: ${error}`, "error");
      } finally {
        setBusy(false);
      }
    });

    async function initialize() {
      if (initialized) return;
      initialized = true;
      setConnection("Проверяю связь с сервером...");
      try {
        const connection = await callApi("check_connection");
        if (connection.ok) {
          serverInput.value = connection.server;
          const serverName = connection.scope === "local" ? "Локальный" : "Глобальный";
          setConnection(`${serverName} сервер подключен`, "ready");
          setStatus(`Сервер: ${connection.server}`);
        } else {
          setConnection("Сервер инвентаризации недоступен", "error");
          setStatus(connection.error, "error");
        }
      } catch (error) {
        setConnection(`Ошибка проверки сервера: ${error}`, "error");
      }
      await collectInventory();
    }

    window.addEventListener("pywebviewready", initialize);
    if (window.location.protocol === "http:") initialize();
