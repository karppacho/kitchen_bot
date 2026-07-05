"""Telegram-бот. Минимальная версия: текст → LLM → текст."""
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from loguru import logger

from src.config import settings
from src.llm.client import chat
from src.llm.history import clear as clear_history
from src.bot.telegram_text import split_for_telegram
from src.data.sheets import get_data, reload_data

# parse_mode=HTML: числовые ответы приходят с таблицей в <pre> (моноширинный шрифт,
# колонки выравниваются). Текст ответов экранируется в src/llm/format.py.
bot = Bot(
    token=settings.telegram_bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


def _is_authorized(user_id: int) -> bool:
    return user_id in settings.telegram_allowed_user_ids


async def _send_long(message: Message, text: str) -> None:
    """Отправка ответа с учётом лимита Telegram (4096 символов на сообщение).

    Длинный текст режется по границам строк (таблицы <pre> не ломаются).
    Если Telegram не принял HTML — кусок уходит плоским текстом, чтобы шеф
    хоть что-то получил вместо молчания.
    """
    for chunk in split_for_telegram(text):
        try:
            await message.answer(chunk)
        except TelegramBadRequest:
            logger.warning("HTML parse не прошёл, отправляю плоским текстом")
            await message.answer(chunk, parse_mode=None)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not _is_authorized(message.from_user.id):
        await message.answer("Доступ закрыт.")
        return
    await message.answer(
        "Привет. Я ассистент R&D кухни.\n\n"
        "Умею:\n"
        "• Считать UC и маржу блюд («сколько стоит чизбургер»)\n"
        "• Искать блюда с ингредиентом («где используется моцарелла»)\n"
        "• Сравнивать маржу («у какого ролла лучшая маржа»)\n"
        "• Показывать список блюд («какие у нас шаурмы»)\n"
        "• Симулировать изменение цены и замену ингредиента\n"
        "• Генерировать ТТК (.docx) — сначала покажу превью, по «да» пришлю файл\n"
        "• Создавать новые блюда («новое блюдо: тортилья 60, курица 80, цена 220») — "
        "покажу состав и UC, запишу в таблицу после твоего «да»\n\n"
        "Команды:\n"
        "/refresh — перечитать таблицу после изменений\n"
        "/new — начать новый диалог (сбросить контекст)\n"
        "/help — это сообщение"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await cmd_start(message)


@dp.message(Command("new"))
async def cmd_new(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    clear_history(message.from_user.id)
    await message.answer("Начал новый диалог — предыдущий контекст сброшен.")


@dp.message(Command("refresh"))
async def cmd_refresh(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    await message.answer("Перечитываю таблицу...")
    try:
        # В поток: синхронный gspread иначе блокирует event loop на всё время чтения
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, reload_data)
        data = get_data()
        await message.answer(
            f"Обновлено. Сейчас в базе:\n"
            f"• {len(data.ingredients)} ингредиентов\n"
            f"• {len(data.dishes)} блюд\n"
            f"• {len(data.ttk_by_dish)} блюд с составом\n"
            f"• {len(data.packagings)} упаковок"
        )
    except Exception as e:
        logger.exception("Ошибка перезагрузки данных")
        await message.answer(f"Ошибка при чтении таблицы: {e}")


@dp.message(F.text)
async def on_text(message: Message):
    user_id = message.from_user.id
    user_name = message.from_user.username or message.from_user.full_name
    if not _is_authorized(user_id):
        return

    text = message.text or ""
    logger.info(f"[{user_id} {user_name}] >>> {text}")

    # Покажем «печатает», пока ждём LLM
    await bot.send_chat_action(message.chat.id, "typing")

    files: list[str] = []
    try:
        # Запускаем LLM в потоке (синхронный SDK блокирует event loop иначе)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, chat, text, user_id)
        reply = result.text
        files = result.files
    except Exception as e:
        logger.exception("Ошибка обработки сообщения")
        reply = f"Что-то пошло не так: {e}"

    logger.info(f"[{user_id} {user_name}] <<< {reply[:300]}")
    await _send_long(message, reply)

    # Сгенерированные файлы (например, .docx ТТК) — отправляем документами
    for path in files:
        try:
            await message.answer_document(FSInputFile(path))
        except Exception as e:
            logger.exception(f"Не смог отправить файл {path}: {e}")
            await message.answer(f"Файл сформирован, но не отправился: {e}", parse_mode=None)


async def main():
    # Логирование в файл с ротацией по дням
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
    )

    # Прогреем кеш данных при старте
    logger.info("Загружаем данные из Google Sheets...")
    try:
        data = get_data()
        logger.info(
            f"Готово. Блюд с составом: {len(data.ttk_by_dish)}, "
            f"ингредиентов: {len(data.ingredients)}"
        )
    except Exception as e:
        logger.exception(f"Не удалось загрузить данные: {e}")
        raise

    logger.info(f"Бот стартует. Модель: {settings.llm_model}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
