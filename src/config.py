"""Конфигурация — все настройки из .env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    telegram_allowed_user_ids: list[int]

    # Реквизиты для официальной ТТК (.docx). Дефолты — текущая сеть «Тим Кук».
    # При масштабировании на другие сети переопределяются через .env.
    ttk_org_name: str = "ООО «Гастрономия»"
    ttk_director_position: str = "Генеральный директор"
    ttk_tr_ts_number: str = "021/2011"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Список читаем из строки "123,456" → [123, 456]
        env_parse_none_str="None",
    )


settings = Settings()
