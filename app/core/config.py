"""
Application configuration via Pydantic BaseSettings.
All values can be overridden via environment variables or a .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    app_name: str = "Geospatial File Measurement API"
    app_version: str = "1.0.0"
    debug: bool = True

    # Database
    database_url: str = "sqlite+aiosqlite:///./geo_api.db"

    # File storage
    upload_dir: str = "uploads"
    max_upload_size_mb: int = 100
    delete_file_on_failure: bool = False

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


# Singleton settings instance used throughout the app
settings = Settings()
