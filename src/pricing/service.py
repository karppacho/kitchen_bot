"""Оркестратор пересчёта расчёток + человекочитаемая сводка.

Сводку собирает Python, не LLM: это числа, а числа в проекте модель не печатает.
"""
import asyncio

from loguru import logger

from src.data.sheets import get_data
from src.pricing.exporter import STATUS_SHEETS, export_table

_lock = asyncio.Lock()

ALREADY_RUNNING_MSG = "Пересчёт расчётки уже идёт — дождись, пожалуйста, окончания."


def rebuild_sync(statuses: tuple[str, ...] = ("разработка", "активное")) -> list[dict]:
    """Синхронный пересчёт листов. Ошибка одного листа не роняет остальные."""
    data = get_data()
    out = []
    for status in statuses:
        if status not in STATUS_SHEETS:
            continue
        try:
            out.append(export_table(data, status))
        except Exception as e:
            logger.exception(f"[расчётка] лист для статуса «{status}» не собрался")
            out.append({
                "sheet": STATUS_SHEETS[status], "status": status, "error": str(e),
            })
    return out


async def rebuild(statuses: tuple[str, ...] = ("разработка", "активное")) -> list[dict]:
    """То же, но не блокируя event loop бота. Single-flight через Lock."""
    if _lock.locked():
        raise RuntimeError(ALREADY_RUNNING_MSG)
    async with _lock:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, rebuild_sync, statuses)


def is_running() -> bool:
    return _lock.locked()
