"""Global settings loaded from environment and .env."""

import os
from functools import lru_cache
from pathlib import Path

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

    collection_max_concurrency: int = 3
    # Collect ReAct is enabled by default; failures fall back to fixed collection.
    collect_react_enabled: bool = True
    collect_react_max_rounds: int = 8
    collect_react_max_search_calls: int = 5
    collect_react_max_seconds: float = 180.0
    collect_react_no_progress_rounds: int = 2

    douyin_cookie: str = ""
    xhs_cookie: str = ""
    bilibili_cookie: str = ""

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    frontend_origin: str = "http://localhost:5173"
    enable_demo_fallback: bool = True

    # 竞品订阅监控。进程内调度器仅在长驻 backend 中启用。
    subscription_scheduler_enabled: bool = False
    monitor_tick_seconds: int = 60
    monitor_max_concurrency: int = 3
    monitor_due_batch_size: int = 20
    monitor_fetch_timeout: float = 30.0
    monitor_max_response_bytes: int = 5_000_000
    monitor_max_redirects: int = 5
    monitor_lease_seconds: int = 1800
    monitor_snapshot_retention: int = 30
    monitor_min_change_ratio: float = 0.01
    monitor_min_changed_chars: int = 40
    monitor_diff_max_chars: int = 12_000
    monitor_target_failure_threshold: int = 3
    monitor_snapshot_dir: str = ""

    @property
    def llm_configured(self) -> bool:
        return bool(self.zhipu_api_key)

    @property
    def is_serverless(self) -> bool:
        return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

    @property
    def subscription_scheduler_active(self) -> bool:
        """进程内调度器只允许在长驻后端启用。"""
        return self.subscription_scheduler_enabled and not self.is_serverless

    @property
    def monitor_snapshot_path(self) -> Path:
        if self.monitor_snapshot_dir:
            return Path(self.monitor_snapshot_dir).expanduser()
        if self.is_serverless:
            return Path("/tmp/monitor_snapshots")
        return Path(_BACKEND_DIR) / "app" / "data" / "monitor_snapshots"


@lru_cache
def get_settings() -> Settings:
    return Settings()
