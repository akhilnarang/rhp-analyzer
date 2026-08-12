from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    rhp_database_path: Path = Path("data/rhp-cache.sqlite3")
    rhp_default_model: str = "gpt-5.6-terra"
    rhp_default_retries: int = 2
    rhp_max_pdf_bytes: int = 50_000_000
    rhp_section_concurrency: int = Field(default=4, ge=1, le=10)
    rhp_job_concurrency: int = Field(default=1, ge=1, le=10)
    rhp_allowed_pdf_hosts: str = "www.bseindia.com,bseindia.com"

    def allowed_pdf_hosts(self) -> set[str]:
        return {
            host.strip().lower().rstrip(".")
            for host in self.rhp_allowed_pdf_hosts.split(",")
            if host.strip()
        }

    def require_openai_api_key(self) -> str:
        if self.openai_api_key is None or not self.openai_api_key.get_secret_value():
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env. Then set the key."
            )
        return self.openai_api_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
