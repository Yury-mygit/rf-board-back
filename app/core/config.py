from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "board-dev"
    version: str = "0.1.0"
    log_level: str = "info"

    database_url: str
    api_key: str


settings = Settings()
