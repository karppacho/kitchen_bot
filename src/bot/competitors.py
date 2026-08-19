"""Telegram-команды мониторинга конкурентов + ручной фолбэк (HTML-файлом).

ВАЖНО: register(dp) должен вызываться в main.py ДО объявления catch-all
хендлера @dp.message(F.text) — Dispatcher сначала прогоняет собственные
хендлеры в порядке регистрации, и include_router здесь не спас бы:
вложенные роутеры проверяются ПОСЛЕ всех хендлеров самого Dispatcher.
"""
import asyncio
import time
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

# Ждём ссылку следующим сообщением: {user_id: (действие, время)}.
#
# Формат «/add_competitor <url>» в Telegram нерабочий: команда, выбранная из
# меню, отправляется СРАЗУ, дописать к ней аргумент некуда. Целиком её наберёт
# разве что разработчик. Поэтому команда без аргумента переводит бота в режим
# ожидания ссылки, а шеф присылает её следующим сообщением.
_PENDING_TTL = 5 * 60
_pending: dict[int, tuple[str, float]] = {}


def _set_pending(user_id: int, action: str) -> None:
    _pending[user_id] = (action, time.time())


def _peek_pending(user_id: int) -> str | None:
    """Что ждём от пользователя, НЕ очищая. Протухшее выбрасывает."""
    rec = _pending.get(user_id)
    if rec is None:
        return None
    action, ts = rec
    if time.time() - ts > _PENDING_TTL:
        _pending.pop(user_id, None)
        return None
    return action


def _take_pending(user_id: int) -> str | None:
    """То же, но с очисткой."""
    action = _peek_pending(user_id)
    _pending.pop(user_id, None)
    return action


def clear_pending(user_id: int) -> None:
    """Сбросить ожидание (команда /new или отмена)."""
    _pending.pop(user_id, None)


def _is_authorized(user_id: int) -> bool:
    return user_id in settings.telegram_allowed_user_ids


def _log_command(message: Message) -> None:
    """Команды конкурентов в лог не попадали вообще: логирует только on_text.

    Из-за этого 06.08.2026 разбор жалобы «что-то не так с /add_competitor»
    начался с пустого grep — по логам нельзя было понять, вызывалась ли команда.
    """
    user = message.from_user
    logger.info(f"[{user.id} {user.username or user.full_name}] cmd >>> {message.text}")


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
    _log_command(message)
    if _parse_url_arg(message.text)[0] is None:
        # Ссылки нет — ждём её следующим сообщением (см. _pending)
        _set_pending(message.from_user.id, "add")
        await message.answer(
            "Пришли ссылку на страницу меню конкурента следующим сообщением.\n"
            "Например: dodopizza.ru/moscow\n"
            "Можно сразу с названием через пробел: dodopizza.ru/moscow Додо Пицца\n"
            "Передумал — напиши «отмена»."
        )
        return
    await _do_add(message, message.text.split(maxsplit=1)[1])


async def _do_add(message: Message, payload: str) -> None:
    """Добавить конкурента из строки «<url> [название]»."""
    parts = payload.strip().split(maxsplit=1)
    raw = parts[0].strip()
    name = parts[1].strip() if len(parts) > 1 else None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        _set_pending(message.from_user.id, "add")
        await message.answer(
            f"«{payload.strip()}» не похоже на ссылку. Пришли адрес сайта, "
            f"например dodopizza.ru/moscow. Передумал — напиши «отмена»."
        )
        return
    domain = parsed.netloc.removeprefix("www.")
    loop = asyncio.get_running_loop()
    comp = await loop.run_in_executor(
        None, lambda: storage.add_competitor(name or domain, domain, raw),
    )
    await message.answer(
        f"Добавил конкурента: {comp.name}\n"
        f"Страница меню: {comp.menu_url}\n"
        f"Проверю в ближайший еженедельный прогон, или запусти сейчас: /check_competitors"
    )


async def cmd_remove_competitor(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    _log_command(message)
    domain, _ = _parse_url_arg(message.text)
    if domain is None:
        _set_pending(message.from_user.id, "remove")
        comps = await asyncio.get_running_loop().run_in_executor(
            None, storage.list_competitors,
        )
        listed = "\n".join(f"• {c.name} — {c.url}" for c in comps) or "(список пуст)"
        await message.answer(
            f"Кого убрать? Пришли адрес следующим сообщением.\n\n{listed}\n\n"
            f"Передумал — напиши «отмена»."
        )
        return
    await _do_remove(message, domain)


async def _do_remove(message: Message, domain: str) -> None:
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
    _log_command(message)
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
    _log_command(message)
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
        # Сводка — только тому, кто нажал: ручной прогон чаще всего отладочный,
        # рассылать его второму пользователю незачем. Всем шлёт только крон.
        await service.run_check(
            message.bot, trigger="manual", notify_user_id=message.from_user.id,
        )
    except Exception as e:
        logger.exception("Ручная проверка конкурентов упала")
        await message.answer(f"Проверка конкурентов упала: {e}", parse_mode=None)


async def cmd_competitors_report(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    _log_command(message)
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


def is_awaiting_link(message: Message) -> bool:
    """Фильтр: ждём ли от этого пользователя ссылку.

    Именно фильтром, а не проверкой внутри хендлера: aiogram останавливает
    цепочку на первом СРАБОТАВШЕМ хендлере. Проверяй мы внутри — обычные
    сообщения перестали бы доходить до on_text и до LLM.
    """
    user = message.from_user
    if user is None or not _is_authorized(user.id):
        return False
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False   # команды идут своим путём
    return _peek_pending(user.id) is not None


async def on_pending_answer(message: Message):
    """Ссылка, которую ждали после команды без аргумента."""
    text = (message.text or "").strip()
    action = _take_pending(message.from_user.id)
    if action is None:
        return

    if text.lower() in ("отмена", "отменить", "не надо", "стоп"):
        await message.answer("Отменил.")
        return

    _log_command(message)
    if action == "add":
        await _do_add(message, text)
    elif action == "remove":
        domain, _ = _parse_url_arg(f"/x {text}")
        if domain is None:
            _set_pending(message.from_user.id, "remove")
            await message.answer(
                f"«{text}» не похоже на адрес. Пришли адрес из списка "
                f"или напиши «отмена»."
            )
        else:
            await _do_remove(message, domain)


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
    loop = asyncio.get_running_loop()
    caption = (message.caption or "").strip()
    if caption:
        comp = await loop.run_in_executor(None, storage.find_competitor, caption)
        if comp is None:
            await message.answer(
                f"Не нашёл конкурента по подписи «{caption}». "
                f"Список: /list_competitors, добавить: /add_competitor <url>"
            )
            return
    else:
        # Подпись нужна только чтобы понять, чей это файл. Если вручную
        # обновляется ровно один конкурент — гадать не о чем, шеф прислал его.
        comps = await loop.run_in_executor(None, storage.list_competitors)
        manual = [c for c in comps if c.fetch_method == "manual"]
        if len(manual) != 1:
            hint = (
                "например «Бургер Кинг» или burgerkingrus.ru"
                if not manual
                else "их сейчас несколько: " + ", ".join(c.name for c in manual)
            )
            await message.answer(f"Пришли файл ещё раз с подписью — название конкурента, {hint}")
            return
        comp = manual[0]

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
    # Ссылка после команды без аргумента. Фильтр is_awaiting_link срабатывает
    # только когда мы её реально ждём — остальные сообщения идут дальше, в LLM.
    dp.message.register(on_pending_answer, F.text, is_awaiting_link)
