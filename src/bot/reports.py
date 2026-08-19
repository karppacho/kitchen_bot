"""Очередь ночных отчётов: считаем ночью, отправляем утром.

Проверка конкурентов идёт 20-25 минут, а расчётка дёргает Google Sheets, —
поэтому оба крона стоят ночью, когда никто не мешает и лимиты свободны.
Но раньше сводка уходила сразу по готовности, и шеф получал её в 4 утра.
Теперь ночной прогон кладёт текст сюда, а отдельная задача разносит его
в 9:00 МСК.

Очередь лежит на диске, а не в памяти: между расчётом и доставкой пять
часов, за которые бот может быть перезапущен (передеплой, падение, ребут
сервера) — отчёт от этого пропадать не должен.
"""
import json
from datetime import datetime
from pathlib import Path

from loguru import logger

QUEUE_PATH = Path("data/pending_reports.json")


def _read() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"[отчёты] очередь не читается ({e}) — начинаю с пустой")
        return []


def _write(items: list[dict]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def queue_report(text: str) -> None:
    """Отложить отчёт до утренней доставки."""
    if not text or not text.strip():
        return
    items = _read()
    items.append({"queued_at": datetime.now().isoformat(timespec="seconds"), "text": text})
    _write(items)
    logger.info(f"[отчёты] отложен до утренней доставки ({len(text)} симв.)")


def pop_reports() -> list[str]:
    """Забрать всё накопленное и очистить очередь.

    Очищаем ДО отправки: если Telegram недоступен, лучше потерять один отчёт,
    чем присылать его снова и снова каждое утро.
    """
    items = _read()
    if items:
        _write([])
    return [i.get("text", "") for i in items if i.get("text")]


def pending_count() -> int:
    return len(_read())
