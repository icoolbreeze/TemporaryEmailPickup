"""Deploy the loopback Outlook pickup service to ssh host ``beike-server``."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICE_DIR = Path(__file__).resolve().parent
REMOTE_DIR = "/home/ubuntu/outlook-pickup"
DEFAULT_SSH_HOST = "beike-server"
SETTINGS_NAME = "TemporaryEmailPickup"
SETTINGS_FILE = "settings.json"


def _ssh_host() -> str:
    return os.environ.get("OUTLOOK_PICKUP_SSH", "").strip() or DEFAULT_SSH_HOST


def _settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / SETTINGS_NAME
    return root / SETTINGS_FILE


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


def _ssh(host: str, remote: str) -> str:
    result = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", host, remote])
    return result.stdout


def _scp(local: Path, remote_spec: str) -> None:
    _run(
        [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=12",
            str(local),
            remote_spec,
        ]
    )


def _load_or_create_api_key() -> str:
    env_key = os.environ.get("OUTLOOK_PICKUP_KEY", "").strip()
    if env_key:
        return env_key

    sys.path.insert(0, str(REPO))
    from secret_store import SecretStoreError, protect_secret, unprotect_secret

    path = _settings_path()
    settings: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, ValueError, TypeError):
            settings = {}

    protected = str(settings.get("outlook_pickup_key") or "")
    if protected:
        try:
            existing = unprotect_secret(protected).strip()
            if existing:
                return existing
        except SecretStoreError:
            pass

    key = secrets.token_urlsafe(32)
    settings["outlook_pickup_key"] = protect_secret(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return key


def _write_remote_env(host: str, api_key: str) -> None:
    body = (
        "PICKUP_BIND=127.0.0.1\n"
        "PICKUP_PORT=18793\n"
        f"PICKUP_API_KEY={api_key}\n"
    )
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        suffix=".env",
        delete=False,
    ) as handle:
        handle.write(body)
        temp_path = Path(handle.name)
    try:
        _scp(temp_path, f"{host}:{REMOTE_DIR}/.env")
        _ssh(host, f"chmod 600 {REMOTE_DIR}/.env")
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def main() -> int:
    host = _ssh_host()
    imap_source = REPO / "outlook_imap.py"
    server_source = SERVICE_DIR / "server.py"
    unit_source = SERVICE_DIR / "outlook-pickup.service"
    for required in (imap_source, server_source, unit_source):
        if not required.is_file():
            print(f"缺少文件：{required}", file=sys.stderr)
            return 2

    try:
        api_key = _load_or_create_api_key()
    except Exception as exc:
        print(f"无法准备 API key：{exc}", file=sys.stderr)
        return 2

    try:
        _ssh(host, f"mkdir -p {REMOTE_DIR}")
        _scp(imap_source, f"{host}:{REMOTE_DIR}/outlook_imap.py")
        _scp(server_source, f"{host}:{REMOTE_DIR}/server.py")
        _write_remote_env(host, api_key)
        _scp(unit_source, f"{host}:/tmp/outlook-pickup.service")
        _ssh(
            host,
            "sudo cp /tmp/outlook-pickup.service /etc/systemd/system/outlook-pickup.service"
            " && sudo systemctl daemon-reload"
            " && sudo systemctl enable --now outlook-pickup"
            " && sudo systemctl restart outlook-pickup",
        )
        health = _ssh(host, "curl -sf http://127.0.0.1:18793/health")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"部署失败：{detail or exc}", file=sys.stderr)
        return 1

    if '"ok"' not in health and "'ok'" not in health:
        print(f"健康检查失败：{health!r}", file=sys.stderr)
        return 1

    print(f"outlook-pickup 已部署到 {host}:{REMOTE_DIR}")
    print(f"API key fingerprint: {api_key[:6]}…")
    print(health.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
