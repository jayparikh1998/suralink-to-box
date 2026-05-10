from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


APP_NAME = "SuralinkBoxSync"
HOST = "localhost"
PORT = 8501
STARTUP_TIMEOUT_SECONDS = 30


def _packaging_dependency_anchor() -> None:
    # PyInstaller analyzes launcher.py, while Streamlit loads app.py at runtime.
    # These imports keep the app's runtime dependencies visible to the build.
    import box_sdk_gen  # noqa: F401
    import cryptography  # noqa: F401
    import dotenv  # noqa: F401
    import httpx  # noqa: F401
    import jwt  # noqa: F401
    import pydantic  # noqa: F401
    import pydantic_settings  # noqa: F401
    import rich  # noqa: F401
    import streamlit  # noqa: F401
    import tenacity  # noqa: F401


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent


def external_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def show_error(message: str) -> None:
    full_message = f"{APP_NAME}: {message}"
    print(full_message, file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, full_message, APP_NAME, 0x10)
        except Exception:
            pass


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    src_path = bundle_dir() / "src"
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(src_path)
        if not existing_pythonpath
        else f"{src_path}{os.pathsep}{existing_pythonpath}"
    )
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    return env


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_streamlit(process: subprocess.Popen, host: str, port: int) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if port_is_open(host, port):
            return True
        time.sleep(0.5)
    return False


def streamlit_args(app_path: Path) -> list[str]:
    return [
        "run",
        str(app_path),
        "--global.developmentMode=false",
        "--server.headless=true",
        f"--server.address={HOST}",
        f"--server.port={PORT}",
        "--browser.gatherUsageStats=false",
    ]


def run_streamlit_child() -> int:
    app_path = Path(sys.argv[2]).resolve()
    src_path = bundle_dir() / "src"
    if src_path.exists():
        sys.path.insert(0, str(src_path))
    os.chdir(external_dir())
    sys.argv = ["streamlit", *streamlit_args(app_path)]

    from streamlit.web.cli import main

    return int(main() or 0)


def start_streamlit(app_path: Path) -> subprocess.Popen:
    env = build_environment()
    cwd = external_dir()

    if is_frozen():
        command = [sys.executable, "--streamlit-child", str(app_path)]
    else:
        command = [sys.executable, "-m", "streamlit", *streamlit_args(app_path)]

    return subprocess.Popen(command, cwd=str(cwd), env=env)


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--streamlit-child":
        return run_streamlit_child()

    app_path = bundle_dir() / "app.py"
    if not app_path.exists():
        show_error(
            f"Could not find app.py at {app_path}. Rebuild the executable with app.py included."
        )
        return 1

    try:
        process = start_streamlit(app_path)
    except Exception as exc:
        show_error(f"Could not start Streamlit: {type(exc).__name__}: {exc}")
        return 1

    if not wait_for_streamlit(process, HOST, PORT):
        show_error(
            "Streamlit did not start successfully. Check that port 8501 is available "
            "and that .env and box_config.json are beside the executable."
        )
        if process.poll() is None:
            process.terminate()
        return 1

    url = f"http://{HOST}:{PORT}"
    print(f"Opening {url}")
    webbrowser.open(url)

    try:
        return int(process.wait() or 0)
    except KeyboardInterrupt:
        process.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
