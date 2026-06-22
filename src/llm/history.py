"""Короткая память диалога по пользователю (in-memory).

Хранит последние реплики каждого пользователя, чтобы бот понимал отсылки
(«их маржа», «это блюдо», «посчитай второй»). Память живёт в процессе: при
перезапуске бота сбрасывается — осознанное упрощение MVP. Протухает по таймауту.

Храним ТОЛЬКО текст реплик (user + финальный ответ ассистента), без промежуточных
tool-вызовов: этого достаточно для разрешения отсылок, и не раздувает токены.
"""
import time

TTL_SECONDS = 30 * 60   # 30 минут без сообщений → начинаем диалог заново
MAX_MESSAGES = 16       # сколько последних реплик храним (user+assistant вперемешку)

# user_id -> {"messages": [{"role","content"}, ...], "ts": float}
_store: dict[int, dict] = {}


def get_history(user_id: int) -> list[dict]:
    """Последние реплики пользователя для подмешивания в запрос. Пусто, если протухло."""
    rec = _store.get(user_id)
    if rec is None:
        return []
    if time.time() - rec["ts"] > TTL_SECONDS:
        _store.pop(user_id, None)
        return []
    return list(rec["messages"])


def append_turn(user_id: int, user_text: str, assistant_text: str) -> None:
    """Добавить пару «вопрос/ответ» в историю пользователя."""
    rec = _store.get(user_id)
    if rec is None or time.time() - rec["ts"] > TTL_SECONDS:
        rec = {"messages": [], "ts": time.time()}
        _store[user_id] = rec
    rec["messages"].append({"role": "user", "content": user_text})
    rec["messages"].append({"role": "assistant", "content": assistant_text})
    if len(rec["messages"]) > MAX_MESSAGES:
        rec["messages"] = rec["messages"][-MAX_MESSAGES:]
    rec["ts"] = time.time()


def clear(user_id: int) -> None:
    """Сбросить историю пользователя (команда /new)."""
    _store.pop(user_id, None)
