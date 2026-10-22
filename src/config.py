"""Environment-backed runtime configuration for field recording."""
import os
from pathlib import Path
from typing import Dict


def load_dotenv(path: Path | None = None) -> Dict[str, str]:
    """Load simple KEY=VALUE settings without requiring a third-party package."""
    env_path = path or Path.cwd() / ".env"
    if path is None and not env_path.is_file():
        env_path = Path(__file__).resolve().parents[1] / ".env"
    values: Dict[str, str] = {}
    if not env_path.is_file():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
            os.environ.setdefault(key, value)
    return values


def set_dotenv_value(name: str, value: str, path: Path | None = None) -> None:
    """Persist one setting while preserving the rest of the dotenv file."""
    env_path = path or Path.cwd() / ".env"
    if path is None and not env_path.is_file():
        env_path = Path(__file__).resolve().parents[1] / ".env"

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    replacement = f"{name}={value}"
    found = False
    updated = []
    for line in lines:
        if line.strip().startswith(f"{name}="):
            updated.append(replacement)
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(replacement)
    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.environ[name] = value


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
