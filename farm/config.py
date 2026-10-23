import ipaddress
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def local_ollama_url(value: str) -> str:
    """Only a local Ollama service may receive repository evidence."""
    if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
        raise ValueError("FARM_OLLAMA_URL must be a loopback HTTP or HTTPS URL.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if host != "localhost" and (
            not host or "%" in host or not ipaddress.ip_address(host).is_loopback
        ):
            raise ValueError
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "FARM_OLLAMA_URL must point to localhost or a loopback IP, without "
            "credentials, a path, query, or fragment."
        ) from None
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("FARM_DATA_DIR", str(ROOT / "data"))
        ).resolve()
    )
    model_name: str = "BAAI/bge-small-en-v1.5"
    max_files: int = 24
    max_file_bytes: int = 80_000
    max_total_bytes: int = 900_000
    chunk_words: int = 280
    max_chunks: int = 100
    ollama_url: str = field(
        default_factory=lambda: os.getenv("FARM_OLLAMA_URL", "http://127.0.0.1:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: os.getenv("FARM_OLLAMA_MODEL", "llama3.2:3b")
    )
    ollama_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("FARM_OLLAMA_TIMEOUT_SECONDS", "60"))
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "ollama_url", local_ollama_url(self.ollama_url))
        if (
            not isinstance(self.ollama_model, str)
            or not self.ollama_model.strip()
            or len(self.ollama_model) > 200
            or any(ord(char) <= 32 for char in self.ollama_model)
        ):
            raise ValueError("FARM_OLLAMA_MODEL must be a nonempty model name.")
        timeout = self.ollama_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 1 <= timeout <= 180
        ):
            raise ValueError("FARM_OLLAMA_TIMEOUT_SECONDS must be between 1 and 180.")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "catalog.sqlite3"


settings = Settings()
