from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    telegram_api_id: int
    telegram_api_hash: SecretStr
    telegram_local_api_url: str = "http://127.0.0.1:8081"
    telegram_local_file_url: str | None = None
    admin_ids: str = ""
    drive_mount_point: Path = Path("/content/drive")
    drive_root: Path = Path("/content/drive/MyDrive")
    drive_shared_drive_roots: str = ""
    index_filename: str = "_drive_telegram_index.csv"
    temp_dir: Path = Path("/content/dtm-tmp")
    max_concurrent_jobs: int = Field(default=2, ge=1, le=10)
    max_files_per_batch: int = Field(default=20, ge=1, le=200)
    transfer_chunk_size_mb: int = Field(default=512, ge=1, le=1900)
    progress_update_interval: int = Field(default=5, ge=1, le=60)
    retry_attempts: int = Field(default=3, ge=0, le=10)
    max_operation_gb: int = Field(default=100, ge=1)
    log_level: str = "INFO"

    @field_validator("telegram_local_api_url")
    @classmethod
    def local_only(cls, value: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("TELEGRAM_LOCAL_API_URL must be a valid HTTP(S) URL")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Local Bot API must bind to loopback; remote addresses are rejected")
        return value.rstrip("/")

    @property
    def admin_id_set(self) -> set[int]:
        result: set[int] = set()
        for item in self.admin_ids.split(","):
            if item.strip():
                result.add(int(item.strip()))
        return result

    @property
    def allowed_roots(self) -> list[Path]:
        roots = [self.drive_root]
        roots.extend(Path(p.strip()) for p in self.drive_shared_drive_roots.split(",") if p.strip())
        return roots


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
