from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "board-dev"
    version: str = "0.1.0"
    log_level: str = "info"

    database_url: str
    api_key: str
    # Дополнительные API-ключи (comma-separated, env `EXTRA_API_KEYS`).
    # Используется чтобы дать клиентам (например auto_designer) отдельные
    # токены без потери backward-compat с одним глобальным `api_key`.
    # Долгосрочная задача — централизованная система токенов
    # (см. карту _cross/feature/2026-05-30-api-tokens-admin).
    extra_api_keys: str = ""
    media_gc_token: str = ""

    @property
    def all_api_keys(self) -> set[str]:
        keys = {self.api_key} if self.api_key else set()
        if self.extra_api_keys:
            keys.update(k.strip() for k in self.extra_api_keys.split(",") if k.strip())
        return keys


settings = Settings()
