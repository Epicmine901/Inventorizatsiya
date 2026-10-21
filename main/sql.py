from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Generator
from urllib.parse import parse_qs, unquote, urlsplit


ENV_PATH = Path(__file__).resolve().parent / ".env"
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "inventory.sqlite3"


class DatabaseError(RuntimeError):
    """Raised when database settings or database operations are invalid."""


def read_database_settings(env_path: Path = ENV_PATH) -> dict[str, str]:
    settings: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []

    for line_number, line in enumerate(lines, start=1):
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if "=" not in stripped_line:
            raise DatabaseError(
                f"Некорректная строка {line_number} в {env_path.name}."
            )
        key, value = stripped_line.split("=", 1)
        settings[key.strip()] = value.strip().strip("\"'")

    return settings


class InventoryDatabase:
    def __init__(self, env_path: Path = ENV_PATH) -> None:
        settings = read_database_settings(env_path)
        self.base_dir = env_path.resolve().parent
        self.mode = settings.get("db_mode", "local").strip().lower()
        self.db_path = settings.get("db_path", "").strip()
        self.db_url = settings.get("db_url", "").strip()
        self.db_admin = settings.get("db_admin", "").strip()
        self.db_pass = settings.get("db_pass", "")
        self._database_errors: tuple[type[BaseException], ...] = (sqlite3.Error,)
        self._postgresql_details: dict[str, str | int] | None = None

        if self.mode == "local":
            if not self.db_path:
                self.db_path = str(self.base_dir / DEFAULT_DB_PATH.name)
        elif self.mode == "server":
            self._load_postgresql_settings()
        else:
            raise DatabaseError("db_mode должен быть local или server.")

    def _load_postgresql_settings(self) -> None:
        if not self.db_url:
            raise DatabaseError("Для режима server укажите db_url.")

        try:
            parsed_url = urlsplit(self.db_url)
        except ValueError as error:
            raise DatabaseError(f"db_url указан неверно: {error}") from error
        if parsed_url.scheme not in ("postgres", "postgresql"):
            raise DatabaseError("db_url должен быть адресом PostgreSQL.")
        if not parsed_url.hostname or not parsed_url.path.strip("/"):
            raise DatabaseError("db_url должен содержать адрес сервера и имя базы.")

        try:
            port = parsed_url.port or 5432
        except ValueError as error:
            raise DatabaseError("Порт в db_url указан неверно.") from error
        if not 1 <= port <= 65535:
            raise DatabaseError("Порт PostgreSQL должен быть от 1 до 65535.")

        query_values = parse_qs(parsed_url.query)
        self._postgresql_details = {
            "host": parsed_url.hostname,
            "port": port,
            "dbname": unquote(parsed_url.path.strip("/")),
            "user": self.db_admin or unquote(parsed_url.username or ""),
            "password": self.db_pass or unquote(parsed_url.password or ""),
        }
        ssl_modes = query_values.get("sslmode")
        if ssl_modes:
            self._postgresql_details["sslmode"] = ssl_modes[-1]
        self._database_errors = (sqlite3.Error,)

    def _connect(self):
        if self.mode == "local":
            database_path = Path(self.db_path).expanduser()
            if not database_path.is_absolute():
                database_path = self.base_dir / database_path
            try:
                database_path.parent.mkdir(parents=True, exist_ok=True)
                return sqlite3.connect(database_path, timeout=30)
            except OSError as error:
                raise DatabaseError(f"Не удалось открыть SQLite-файл: {error}") from error

        try:
            import psycopg
        except ImportError as error:
            raise DatabaseError(
                "Для PostgreSQL установите пакет psycopg[binary]."
            ) from error

        self._database_errors = (sqlite3.Error, psycopg.Error)
        try:
            return psycopg.connect(**(self._postgresql_details or {}))
        except psycopg.Error as error:
            raise DatabaseError(f"Не удалось подключиться к PostgreSQL: {error}") from error

    @contextmanager
    def _connection(self) -> Generator[object, None, None]:
        connection = None
        try:
            connection = self._connect()
            yield connection
            connection.commit()
        except self._database_errors as error:
            if connection is not None:
                connection.rollback()
            raise DatabaseError(f"Ошибка базы данных: {error}") from error
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        if self.mode == "local":
            create_table = """
                CREATE TABLE IF NOT EXISTS inventory_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    name TEXT NOT NULL,
                    fio TEXT,
                    location TEXT,
                    additional TEXT,
                    in_stock INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )
            """
        else:
            create_table = """
                CREATE TABLE IF NOT EXISTS inventory_records (
                    id BIGSERIAL PRIMARY KEY,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    name TEXT NOT NULL,
                    fio TEXT,
                    location TEXT,
                    additional TEXT,
                    in_stock BOOLEAN NOT NULL,
                    payload TEXT NOT NULL
                )
            """

        try:
            with self._connection() as connection:
                connection.cursor().execute(create_table)
        except DatabaseError:
            raise
        except OSError as error:
            raise DatabaseError(f"Не удалось создать таблицу инвентаризации: {error}") from error

    def save_inventory(self, data: dict[str, object]) -> int:
        name = data.get("name", "Неизвестно")
        if not isinstance(name, str) or not name.strip():
            name = "Неизвестно"
        if self.mode == "local":
            placeholder = "?"
        else:
            placeholder = "%s"
        insert_record = (
            "INSERT INTO inventory_records "
            "(name, fio, location, additional, in_stock, payload) "
            f"VALUES ({placeholder}, {placeholder}, {placeholder}, "
            f"{placeholder}, {placeholder}, {placeholder})"
        )
        values = (
            name,
            self._optional_text(data.get("fio")),
            self._optional_text(data.get("location")),
            self._optional_text(data.get("additional")),
            bool(data.get("in_stock", True)),
            json.dumps(data, ensure_ascii=False),
        )

        try:
            with self._connection() as connection:
                cursor = connection.cursor()
                if self.mode == "server":
                    cursor.execute(insert_record + " RETURNING id", values)
                    row = cursor.fetchone()
                    if row is None:
                        raise DatabaseError("PostgreSQL не вернул ID записи.")
                    return int(row[0])
                cursor.execute(insert_record, values)
                return int(cursor.lastrowid)
        except DatabaseError:
            raise
        except OSError as error:
            raise DatabaseError(f"Не удалось сохранить инвентаризацию: {error}") from error

    def list_inventory(self, limit: int = 500) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise ValueError("Количество записей должно быть от 1 до 500.")
        placeholder = "?" if self.mode == "local" else "%s"
        select_records = (
            "SELECT id, received_at, name, fio, location, additional, in_stock, payload "
            "FROM inventory_records ORDER BY id DESC "
            f"LIMIT {placeholder}"
        )

        try:
            with self._connection() as connection:
                rows = connection.cursor().execute(select_records, (limit,)).fetchall()
        except DatabaseError:
            raise
        except OSError as error:
            raise DatabaseError(f"Не удалось получить инвентаризацию: {error}") from error

        return [
            {
                "id": int(row[0]),
                "received_at": str(row[1]),
                "name": str(row[2]),
                "fio": row[3] if isinstance(row[3], str) else "",
                "location": row[4] if isinstance(row[4], str) else "",
                "additional": row[5] if isinstance(row[5], str) else "",
                "in_stock": bool(row[6]),
                "payload": str(row[7]),
            }
            for row in rows
        ]

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) else None


def initialize_database(env_path: Path = ENV_PATH) -> InventoryDatabase:
    database = InventoryDatabase(env_path)
    database.initialize()
    return database
