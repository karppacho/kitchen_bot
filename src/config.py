"""Конфигурация — все настройки из .env."""
import json
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # Google Sheets
    google_sheets_id: str
    google_service_account_json_path: str

    # Polza.ai (LLM)
    polza_api_key: str
    polza_base_url: str = "https://api.polza.ai/v1"
    llm_model: str = "gpt-4o-mini"

    # Telegram
    telegram_bot_token: str
    # NoDecode: иначе pydantic-settings требует JSON и падает на формате из README
    telegram_allowed_user_ids: Annotated[list[int], NoDecode]

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, v):
        """Список ID из .env: и "123,456" (формат README), и JSON "[123,456]"."""
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                return json.loads(s)
            return [int(x) for x in s.split(",") if x.strip()]
        return v

    # Реквизиты для официальной ТТК (.docx). Дефолты — текущая сеть «Тим Кук».
    # При масштабировании на другие сети переопределяются через .env.
    ttk_org_name: str = "ООО «Гастрономия»"
    ttk_director_position: str = "Генеральный директор"
    ttk_tr_ts_number: str = "021/2011"

    # Мониторинг конкурентов (src/competitors/)
    competitors_sheets_id: str | None = None       # None → экспорт в Sheets выключен
    competitors_db_path: str = "data/competitors.db"
    competitors_check_day: str = "mon"             # ночь вс→пн; день/время уточнить с шефом
    competitors_check_hour: int = 3
    competitors_check_minute: int = 30
    # Порог существенного изменения цены: max(pct% от старой цены, rub)
    competitors_price_threshold_pct: float = 10.0
    competitors_price_threshold_rub: float = 30.0
    competitors_llm_model: str | None = None       # None → llm_model

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_parse_none_str="None",
    )


settings = Settings()
