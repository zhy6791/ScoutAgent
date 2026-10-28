"""全局配置：从环境变量 / .env 读取，不硬编码密钥（第 16.4 章）。"""
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 用绝对路径定位 backend/.env，避免因启动工作目录不同而读不到密钥。
# config.py 位于 backend/app/core/，向上三级即 backend/。
_BACKEND_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_ENV_FILE = os.path.join(_BACKEND_DIR, ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # LLM（智谱 GLM / BigModel OpenAI 兼容）
    zhipu_api_key: str = ""
    # 默认/杂务模型：glm-5.1（10 并发，质量高、速度快）。
    # 报告章节按 SECTION_MODEL_MAP 用 glm-5.2(核心)/glm-5.1(辅助)；
    # intake/澄清/情感分类等杂务用 zhipu_model_fast（高并发极速）。
    zhipu_model: str = "glm-5.1"
    # 核心章模型（质量最高，10 并发）
    zhipu_model_core: str = "glm-5.2"
    # 辅助章模型（质量高，10 并发）
    zhipu_model_aux: str = "glm-5.1"
    # 杂务/快速模型（30 并发，极速，用于澄清/情感分类/单条重写等轻任务）
    zhipu_model_fast: str = "glm-z1-air"
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    # 单次 LLM 调用超时（秒）与自动重试次数，避免请求卡死拖垮整个服务。
    # analyze 等重型 JSON 调用（claims+对比+定价+五力+趋势一次产出）在大 max_tokens
    # 下耗时较长，180s 给足余量；max_retries 设 1，避免超时后再叠加 2 次重试（最坏 3×timeout）。
    llm_timeout: float = 180.0
    llm_max_retries: int = 1

    # 搜索 API（博查 Bocha Web Search：https://open.bocha.cn 获取 key）
    bocha_api_key: str = ""
    bocha_base_url: str = "https://api.bocha.cn/v1"
    search_provider: str = "bocha"
    # 单次搜索超时（秒）
    search_timeout: float = 30.0
    # 兼容旧字段（已弃用，不再使用）
    serpapi_key: str = ""
    serpapi_base_url: str = "https://serpapi.com/search"
    serper_api_key: str = ""
    serper_base_url: str = "https://google.serper.dev/search"
    bing_search_key: str = ""

    # 平台采集
    douyin_cookie: str = ""
    xhs_cookie: str = ""
    bilibili_cookie: str = ""

    # 服务
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    frontend_origin: str = "http://localhost:5173"
    enable_demo_fallback: bool = True

    # 竞品订阅监控。Serverless 只承载数据模型和触发接口，不运行进程内调度器。
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
