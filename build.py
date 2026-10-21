import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


PROJECT_DIR = Path(__file__).resolve().parent
SENDER_DIR = PROJECT_DIR / "sender"
STAGING_DIR = SENDER_DIR / "dist_build"
APPLICATION_NAME = "Inventarizatsiya"
OUTPUT_EXE = PROJECT_DIR / f"{APPLICATION_NAME}.exe"
BUILD_DIR = SENDER_DIR / "build"
SOURCE_CONFIG = SENDER_DIR / ".env"
ICON_FILE = SENDER_DIR / "inventory.ico"
VERSION_FILE = SENDER_DIR / "version_info.txt"
EMBEDDED_CONFIG = BUILD_DIR / "server_config.json"


def load_build_config() -> dict[str, str]:
    values = {}
    for line_number, line in enumerate(
        SOURCE_CONFIG.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if "=" not in stripped_line:
            raise ValueError(
                f"Invalid configuration at {SOURCE_CONFIG}:{line_number}; "
                "expected KEY=VALUE."
            )
        key, value = stripped_line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")

    config = {
        "INVENTORY_LOCAL_URL": values.get("INVENTORY_LOCAL_URL", "").strip().rstrip("/"),
        "INVENTORY_GLOBAL_URL": values.get("INVENTORY_GLOBAL_URL", "").strip().rstrip("/"),
    }
    if not config["INVENTORY_LOCAL_URL"]:
        raise ValueError(
            f"Set INVENTORY_LOCAL_URL in {SOURCE_CONFIG} before building."
        )

    for key, url in config.items():
        if not url:
            continue
        try:
            parsed_url = urlsplit(url)
        except ValueError as error:
            raise ValueError(f"{key} is not a valid URL: {error}") from error
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise ValueError(f"{key} must be a full http:// or https:// URL.")
        if parsed_url.username or parsed_url.password:
            raise ValueError(f"Do not put credentials in {key}.")

    return config


def main() -> int:
    source_files = (
        SENDER_DIR / "sender.py",
        SENDER_DIR / "sysinfo.py",
        SENDER_DIR / "web" / "index.html",
        SENDER_DIR / "web" / "styles.css",
        SENDER_DIR / "web" / "styles_ledger.css",
        SENDER_DIR / "web" / "styles_neon.css",
        SENDER_DIR / "web" / "styles_harbor.css",
        SENDER_DIR / "web" / "app.js",
        SENDER_DIR / "rootCA.pem",
        SOURCE_CONFIG,
        ICON_FILE,
        VERSION_FILE,
    )
    missing_files = [str(path) for path in source_files if not path.is_file()]
    if missing_files:
        print("Cannot build sender; required files are missing:")
        print("\n".join(missing_files))
        return 1

    try:
        config = load_build_config()
    except (OSError, ValueError) as error:
        print(f"Cannot read build configuration: {error}")
        return 1

    try:
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        EMBEDDED_CONFIG.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        print(f"Cannot prepare embedded server configuration: {error}")
        return 1

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onefile",
        "--name",
        APPLICATION_NAME,
        "--icon",
        str(ICON_FILE),
        "--version-file",
        str(VERSION_FILE),
        "--distpath",
        str(STAGING_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--specpath",
        str(BUILD_DIR),
        "--add-data",
        f"{SENDER_DIR / 'web'};web",
        "--add-data",
        f"{SENDER_DIR / 'rootCA.pem'};.",
        "--add-data",
        f"{EMBEDDED_CONFIG};.",
        "--collect-all",
        "webview",
        "--hidden-import",
        "sysinfo",
        "--hidden-import",
        "pythoncom",
        "--hidden-import",
        "pywintypes",
        str(SENDER_DIR / "sender.py"),
    ]

    try:
        subprocess.run(command, cwd=PROJECT_DIR, check=True)
    except FileNotFoundError as error:
        print(f"Cannot start the build command: {error}")
        return 1
    except subprocess.CalledProcessError as error:
        print(f"PyInstaller failed with exit code {error.returncode}.")
        return error.returncode or 1
    finally:
        EMBEDDED_CONFIG.unlink(missing_ok=True)

    staged_exe = STAGING_DIR / f"{APPLICATION_NAME}.exe"
    if not staged_exe.is_file():
        print(f"Build did not produce the expected executable: {staged_exe}")
        return 1

    backup_exe = PROJECT_DIR / f"{APPLICATION_NAME}.previous.exe"
    try:
        if backup_exe.exists():
            backup_exe.unlink()
        if OUTPUT_EXE.exists():
            OUTPUT_EXE.rename(backup_exe)
        try:
            staged_exe.rename(OUTPUT_EXE)
        except OSError:
            if backup_exe.exists() and not OUTPUT_EXE.exists():
                backup_exe.rename(OUTPUT_EXE)
            raise
        if backup_exe.exists():
            backup_exe.unlink()
    except OSError as error:
        print(f"Could not install the new build: {error}")
        return 1
    finally:
        if STAGING_DIR.exists():
            shutil.rmtree(STAGING_DIR)

    for generated_dir in (SENDER_DIR / "dist", BUILD_DIR):
        if generated_dir.exists():
            try:
                shutil.rmtree(generated_dir)
            except OSError as error:
                print(f"Build completed, but could not remove {generated_dir}: {error}")
                return 1

    print(f"Build complete: {OUTPUT_EXE}")
    print("The executable includes the Inventory icon and Windows product details.")
    print("Server URLs are embedded in the application; no .env is needed beside the EXE.")
    print("Temporary dist and build cache folders have been removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
