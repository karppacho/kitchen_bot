"""Telegram-команды мониторинга конкурентов + ручной фолбэк (HTML-файлом).

ВАЖНО: register(dp) должен вызываться в main.py ДО объявления catch-all
хендлера @dp.message(F.text) — Dispatcher сначала прогоняет собственные
хендлеры в порядке регистрации, и include_router здесь не спас бы:
вложенные роутеры проверяются ПОСЛЕ всех хендлеров самого Dispatcher.
"""
import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from src.competitors import service, storage
from src.competitors.format import format_manual_ingest
from src.config import settings

_UPLOADS_DIR = Path("data/manual_uploads")
_ALLOWED_UPLOAD_EXT = (".html", ".htm", ".mhtml", ".mht")
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # лимит Bot API на скачивание файла


def _is_authorized(user_id: int) -> bool:
    return user_id in settings.telegram_allowed_user_ids


def _parse_url_arg(text: str) -> tuple[str | None, str | None]:
    """'/cmd <url> [название]' → (домен-ключ, название или None)."""
    parts = (text or "").split(maxsplit=2)
    if len(parts) < 2:
        return None, None
    raw = parts[1].strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        return None, None
    domain = parsed.netloc.removeprefix("www.")
    name = parts[2].strip() if len(parts) > 2 else None
    return domain, name


async def cmd_add_competitor(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    domain, name = _parse_url_arg(message.text)
    if domain is None:
        await message.answer(
            "Формат: /add_competitor <url> [название]\n"
            "Например: /add_competitor dodopizza.ru/moscow Додо Пицца\n"
            "Лучше давать ссылку сразу на страницу меню нужного города."
        )
        return
    # Ключ конкурента — домен; menu_url — ссылка как прислал шеф (со схемой)
    raw = message.text.split(maxsplit=2)[1].strip()
    menu_url = raw if raw.startswith(("http://", "https://")) else "https://" + raw
    loop = asyncio.get_running_loop()
    comp = await loop.run_in_executor(
        None, lambda: storage.add_competitor(name or domain, domain, menu_url),
    )
    await message.answer(
        f"Добавил конкурента: {comp.name}\n"
        f"Страница меню: {comp.menu_url}\n"
        f"Проверю в ближайший еженедельный прогон, или запусти сейчас: /check_competitors"
    )


async def cmd_remove_competitor(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    domain, _ = _parse_url_arg(message.text)
    if domain is None:
        await message.answer("Формат: /remove_competitor <url>")
        return
    loop = asyncio.get_running_loop()
    comp = await loop.run_in_executor(None, storage.deactivate_competitor, domain)
    if comp is None:
        await message.answer(
            f"Не нашёл конкурента с адресом {domain}. Список: /list_competitors"
        )
        return
    await message.answer(f"Убрал из проверок: {comp.name} ({comp.url}). История срезов сохранена.")


async def cmd_list_competitors(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    loop = asyncio.get_running_loop()
    comps = await loop.run_in_executor(None, storage.list_competitors)
    if not comps:
        await message.answer("Список пуст. Добавь сайт: /add_competitor <url>")
        return
    status_ru = {
        "ok": "успешно", "suspect": "подозрительный срез",
        "fetch_failed": "сайт не открылся", "extract_failed": "не разобрал меню",
    }
    lines = ["Отслеживаемые конкуренты:"]
    for comp in comps:
        info = await loop.run_in_executor(None, storage.last_check_info, comp.id)
        if info is None:
            check = "ещё не проверялся"
        else:
            taken_at, status = info
            check = f"{taken_at[:10]}, {status_ru.get(status, status)}"
        mode = " (ручной режим)" if comp.fetch_method == "manual" else ""
        lines.append(f"• {comp.name} — {comp.url}{mode}\n  последняя проверка: {check}")
    await message.answer("\n".join(lines))


async def cmd_check_competitors(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    if service.is_check_running():
        await message.answer(service.ALREADY_RUNNING_MSG)
        return
    await message.answer(
        "Запустил проверку конкурентов — обычно 3–8 минут.\n"
        "Пришлю сводку по готовности, бот при этом отвечает как обычно."
    )
    asyncio.create_task(_run_check_safe(message))


async def _run_check_safe(message: Message):
    try:
        await service.run_check(message.bot, trigger="manual")
    except Exception as e:
        logger.exception("Ручная проверка конкурентов упала")
        await message.answer(f"Проверка конкурентов упала: {e}", parse_mode=None)


async def cmd_competitors_report(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    if not settings.competitors_sheets_id:
        await message.answer(
            "Экспорт в Google Sheets не настроен: задай COMPETITORS_SHEETS_ID в .env "
            "(отдельная таблица, расшаренная на сервисный аккаунт)."
        )
        return
    await message.answer("Собираю отчёт...")
    loop = asyncio.get_running_loop()
    try:
        from src.competitors import exporter
        url = await loop.run_in_executor(None, exporter.export_snapshot)
        await message.answer(f"Готово, текущий срез в таблице:\n{url}")
    except Exception as e:
        logger.exception("Экспорт отчёта по конкурентам упал")
        await message.answer(f"Не получилось выгрузить отчёт: {e}", parse_mode=None)


async def on_manual_document(message: Message):
    """Ручной фолбэк: шеф сохраняет страницу меню (Ctrl+S) и шлёт файл с подписью."""
    if not _is_authorized(message.from_user.id):
        return
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(_ALLOWED_UPLOAD_EXT):
        return  # чужие документы (PDF и т.п.) — не наша история, молчим
    if doc.file_size and doc.file_size > _MAX_UPLOAD_BYTES:
        await message.answer("Файл больше 20 МБ — Telegram не даст его скачать. Сохрани страницу без медиа (только HTML).")
        return
    caption = (message.caption or "").strip()
    if not caption:
        await message.answer(
            "Пришли файл ещё раз с подписью — название или сайт конкурента, "
            "например «Бургер Кинг» или burgerkingrus.ru"
        )
        return
    loop = asyncio.get_running_loop()
    comp = await loop.run_in_executor(None, storage.find_competitor, caption)
    if comp is None:
        await message.answer(
            f"Не нашёл конкурента по подписи «{caption}». "
            f"Список: /list_competitors, добавить: /add_competitor <url>"
        )
        return

    await message.answer(f"Принял файл для «{comp.name}», разбираю меню...")
    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(name).suffix
    path = _UPLOADS_DIR / f"{comp.url.replace('/', '_')}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
    try:
        await message.bot.download(doc, destination=path)
        result = await service.ingest_manual_html(comp, str(path))
        await message.answer(format_manual_ingest(result))
    except Exception as e:
        logger.exception("Ручная загрузка HTML упала")
        await message.answer(f"Не получилось обработать файл: {e}", parse_mode=None)


def register(dp: Dispatcher) -> None:
    """Вызывать в main.py ДО объявления @dp.message(F.text) — см. докстринг модуля."""
    dp.message.register(cmd_add_competitor, Command("add_competitor"))
    dp.message.register(cmd_remove_competitor, Command("remove_competitor"))
    dp.message.register(cmd_list_competitors, Command("list_competitors"))
    dp.message.register(cmd_check_competitors, Command("check_competitors"))
    dp.message.register(cmd_competitors_report, Command("competitors_report"))
    dp.message.register(on_manual_document, F.document)
