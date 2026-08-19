"""Модели данных мониторинга конкурентов."""
from pydantic import BaseModel, Field


class Competitor(BaseModel):
    id: int
    name: str                       # «Додо Пицца»
    url: str                        # базовый домен — ключ для /add|/remove
    menu_url: str                   # конкретная страница меню (городская)
    city: str = "Москва"
    fetch_method: str = "playwright"  # playwright | http (без браузера) | manual
    active: bool = True
    added_at: str = ""


class ExtractedItem(BaseModel):
    """Одна позиция меню, как её вернула LLM-экстракция (после валидации)."""
    category: str | None = None
    item: str = Field(min_length=1)
    weight: str | None = None       # как на сайте: «250 г», «0,4 л»
    price_rub: float | None = None  # None = цена не распознана
    composition: str | None = None


class FetchResult(BaseModel):
    ok: bool
    text: str = ""
    error: str | None = None
    # Машинный код причины: ok | blocked | needs_session | empty | error.
    # blocked/needs_session не ретраятся — вторая попытка даст то же самое.
    reason: str = "ok"
    # Улики на случай неудачи: разбор Лавки 06.08.2026 начался с пустого места,
    # потому что при fetch_failed не сохранялось вообще ничего.
    html: str = ""
    screenshot: bytes | None = None


class Diff(BaseModel):
    change_type: str                # price_up | price_down | item_added | item_removed
    item: str
    weight: str | None = None
    old_price: float | None = None
    new_price: float | None = None
    delta_rub: float | None = None
    delta_percent: float | None = None


class CheckSiteResult(BaseModel):
    """Итог проверки одного конкурента — сырьё для сводки в Telegram."""
    competitor_name: str
    competitor_url: str
    status: str                     # ok | suspect | fetch_failed | extract_failed | skipped
    items_count: int = 0
    diffs: list[Diff] = Field(default_factory=list)
    error: str | None = None
    first_snapshot: bool = False    # первый срез — сравнивать не с чем
    # Возраст последнего удачного среза в днях (ручной режим): None — срезов не было
    stale_days: int | None = None
