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
from openai import APIConnectionError, APITimeoutError

from src.config import settings
from src.llm.client import chat
from src.llm.history import clear as clear_history
from src.bot import competitors as competitors_cmds
from src.bot.competitors import register as register_competitors
from src.bot.keyboards import (
    BOT_COMMANDS,
    BTN_BACK,
    BTN_PRICING,
    BTN_COMP_ADD,
    BTN_COMP_CHECK,
    BTN_COMP_LIST,
    BTN_COMP_REMOVE,
    BTN_COMP_REPORT,
    BTN_COMPETITORS,
    BTN_HELP,
    BTN_NEW,
    BTN_REFRESH,
    COMPETITORS_KB,
    MAIN_KB,
)
from src.bot.telegram_text import split_for_telegram
from src.competitors.service import run_check as run_competitors_check
from src.data.sheets import get_data, reload_data
from src.pricing import service as pricing_service
from src.pricing.format import format_pricing_result

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
    # Список команд не дублируем: он в кнопке «Меню» и на клавиатуре ниже.
    # Здесь — только то, что кнопкой не выразишь: как со мной разговаривать.
    await message.answer(
        "Привет. Я ассистент R&D кухни. Спрашивай обычными словами:\n\n"
        "• «сколько стоит чизбургер» — UC, маржа, разбивка по составу\n"
        "• «где используется моцарелла» — в каких блюдах ингредиент\n"
        "• «у какого ролла лучшая маржа» — сравнение\n"
        "• «какие у нас шаурмы» — список блюд\n"
        "• «что если говядина подорожает на 15%» — пересчёт всех блюд\n"
        "• «заменим айсберг на пекинскую капусту» — что будет с UC и КБЖУ\n"
        "• «сделай ТТК на гриль ролл» — покажу превью, по «да» пришлю .docx\n"
        "• «новое блюдо: тортилья 1шт, курица 80, цена 220» — посчитаю и, "
        "после твоего «да», запишу в таблицу\n\n"
        "Кнопки внизу — для команд. Все команды также в меню слева от поля ввода.\n\n"
        "Если сайт конкурента не пускает бота — сохрани страницу меню (Ctrl+S) "
        "и пришли файл с подписью-названием конкурента.",
        reply_markup=MAIN_KB,
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


@dp.message(Command("pricing"))
async def cmd_pricing(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    if pricing_service.is_running():
        await message.answer(pricing_service.ALREADY_RUNNING_MSG)
        return
    await message.answer("Пересчитываю расчётку для коммерческого отдела...")
    try:
        results = await pricing_service.rebuild()
    except Exception as e:
        logger.exception("Пересчёт расчётки упал")
        await message.answer(f"Не получилось пересчитать: {e}", parse_mode=None)
        return
    await _send_long(message, format_pricing_result(results))


# Команды мониторинга конкурентов. СТРОГО до on_text: Dispatcher проверяет свои
# хендлеры в порядке регистрации, catch-all F.text иначе перехватит команды.
register_competitors(dp)


# Подпись кнопки → тот же обработчик, что и у команды. Аргументов у кнопки нет,
# и это ровно то, что нужно: cmd_add_competitor без URL сам ответит подсказкой
# формата, то есть кнопка работает как приглашение прислать ссылку.
BUTTON_ACTIONS = {
    BTN_REFRESH: cmd_refresh,
    BTN_NEW: cmd_new,
    BTN_HELP: cmd_start,
    BTN_PRICING: cmd_pricing,
    BTN_COMP_LIST: competitors_cmds.cmd_list_competitors,
    BTN_COMP_CHECK: competitors_cmds.cmd_check_competitors,
    BTN_COMP_REPORT: competitors_cmds.cmd_competitors_report,
    BTN_COMP_ADD: competitors_cmds.cmd_add_competitor,
    BTN_COMP_REMOVE: competitors_cmds.cmd_remove_competitor,
}

# Ловим ТОЧНОЕ совпадение с подписью — обычные вопросы шефа сюда не попадут
# и уйдут дальше, в on_text → LLM. Регистрация до @dp.message(F.text) обязательна.
_MENU_TEXTS = set(BUTTON_ACTIONS) | {BTN_COMPETITORS, BTN_BACK}


@dp.message(F.text.in_(_MENU_TEXTS))
async def on_menu_button(message: Message):
    if not _is_authorized(message.from_user.id):
        return
    text = message.text
    if text == BTN_COMPETITORS:
        await message.answer(
            "Мониторинг конкурентов. Раз в неделю проверяю сам, "
            "сводку пришлю при заметных изменениях цен.",
            reply_markup=COMPETITORS_KB,
        )
        return
    if text == BTN_BACK:
        await message.answer("Главное меню.", reply_markup=MAIN_KB)
        return
    await BUTTON_ACTIONS[text](message)


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
    except (APITimeoutError, APIConnectionError):
        # Сеть, а не логика. «Request timed out» шефу ничего не говорит —
        # он должен понимать, что это не его вопрос сломал бота.
        logger.exception("Не достучался до polza.ai")
        reply = (
            "Не смог связаться с polza.ai — похоже, проблема со связью, "
            "а не с твоим вопросом. Повтори через минуту, всё остальное работает."
        )
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

    # Еженедельная проверка конкурентов — в этом же процессе, рядом с polling
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    async def _weekly_competitors_check():
        try:
            await run_competitors_check(bot, trigger="cron")
        except Exception:
            logger.exception("Еженедельная проверка конкурентов упала")

    async def _weekly_pricing_rebuild():
        """Пересчёт расчётки. Сводка — всем разрешённым: это рассылка, как у
        конкурентов. Смысл в том, чтобы не проглядеть подорожание сырья."""
        try:
            results = await pricing_service.rebuild()
        except Exception:
            logger.exception("Еженедельный пересчёт расчётки упал")
            return
        text = format_pricing_result(results)
        for user_id in settings.telegram_allowed_user_ids:
            for chunk in split_for_telegram(text):
                try:
                    await bot.send_message(user_id, chunk)
                except Exception as e:
                    logger.warning(f"[расчётка] сводка не ушла {user_id}: {e}")
                    break

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        _weekly_competitors_check,
        CronTrigger(
            day_of_week=settings.competitors_check_day,
            hour=settings.competitors_check_hour,
            minute=settings.competitors_check_minute,
        ),
    )
    scheduler.add_job(
        _weekly_pricing_rebuild,
        CronTrigger(
            day_of_week=settings.pricing_check_day,
            hour=settings.pricing_check_hour,
            minute=settings.pricing_check_minute,
        ),
    )
    scheduler.start()
    logger.info(
        f"Мониторинг конкурентов: {settings.competitors_check_day} "
        f"{settings.competitors_check_hour:02d}:{settings.competitors_check_minute:02d} МСК; "
        f"расчётка: {settings.pricing_check_day} "
        f"{settings.pricing_check_hour:02d}:{settings.pricing_check_minute:02d} МСК"
    )

    # Нативное меню Telegram (кнопка «Меню» слева от поля ввода). Не критично
    # для работы: если Bot API не ответил — стартуем без него, кнопки на месте.
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning(f"Не смог зарегистрировать меню команд: {e}")

    logger.info(f"Бот стартует. Модель: {settings.llm_model}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
