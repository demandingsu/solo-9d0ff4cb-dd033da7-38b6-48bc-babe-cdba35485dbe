"""应用配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "sqlite:///./data/biodiversity.db"
    )
    # 启动时若数据库为空，自动写入演示数据，方便直接在 /docs 上体验
    seed_demo_on_startup: bool = os.getenv("SEED_DEMO", "1") == "1"


settings = Settings()
