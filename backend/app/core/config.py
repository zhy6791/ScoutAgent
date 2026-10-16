"""Global settings loaded from environment and .env."""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_ENV_FILE = os.path.join(_BACKEND_DIR, ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    zhipu_api_key: str = ""
    zhipu_model: str = "glm-5.1"
    zhipu_model_core: str = "glm-5.2"
    zhipu_model_aux: str = "glm-5.1"
    zhipu_model_fast: str = "glm-z1-air"
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_timeout: float = 180.0
    llm_max_retries: int = 1

    search_provider: str = "bocha"
    bocha_api_key: str = ""
    bocha_base_url: str = "https://api.bocha.cn/v1"
    serpapi_key: str = ""
    serpapi_base_url: str = "https://serpapi.com/search"
    serper_api_key: str = ""
    serper_base_url: str = "https://google.serper.dev/search"
    search_timeout: float = 30.0
    bing_search_key: str = ""

    douyin_cookie: str = ""
    xhs_cookie: str = ""
    bilibili_cookie: str = ""

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    frontend_origin: str = "http://localhost:5173"
    enable_demo_fallback: bool = True

    @property
    def llm_configured(self) -> bool:
        return bool(self.zhipu_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
