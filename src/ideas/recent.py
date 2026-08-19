"""Что бот уже предлагал этому пользователю — чтобы не повторяться.

По образцу `src/llm/history.py`: словарь в памяти процесса, протухает по TTL.
Постоянное хранилище тут не нужно — через день повтор идеи уже не раздражает,
а вот в течение одной сессии «давай ещё бургеры» обязано давать другое.

06.08.2026 шеф трижды подряд просил бургер и трижды получал «Бургер кимчи»:
промпт был одинаковый, и модель сходилась в один и тот же ответ.
"""
import time

TTL_SECONDS = 60 * 60   # час: столько шеф крутит одну тему
MAX_NAMES = 24          # хватает на 8 запросов по три варианта

# user_id -> {"names": [str], "ts": float}
_store: dict[int, dict] = {}


def remember(user_id: int | None, names: list[str]) -> None:
    """Запомнить показанные названия идей."""
    if user_id is None or not names:
        return
    rec = _store.get(user_id)
    if rec is None or time.time() - rec["ts"] > TTL_SECONDS:
        rec = {"names": [], "ts": time.time()}
        _store[user_id] = rec
    for name in names:
        clean = (name or "").strip()
        if clean and clean.lower() not in {n.lower() for n in rec["names"]}:
            rec["names"].append(clean)
    if len(rec["names"]) > MAX_NAMES:
        rec["names"] = rec["names"][-MAX_NAMES:]
    rec["ts"] = time.time()


def recent(user_id: int | None) -> list[str]:
    """Названия, показанные за последний час. Пусто, если протухло."""
    if user_id is None:
        return []
    rec = _store.get(user_id)
    if rec is None:
        return []
    if time.time() - rec["ts"] > TTL_SECONDS:
        _store.pop(user_id, None)
        return []
    return list(rec["names"])


def clear(user_id: int | None) -> None:
    """Сбросить (команда /new — начинаем с чистого листа)."""
    if user_id is not None:
        _store.pop(user_id, None)
