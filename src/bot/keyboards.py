"""Меню бота: нативный список команд + reply-клавиатура под полем ввода.

Почему reply, а не inline: по нажатию reply-кнопки в чат уходит обычное сообщение,
то есть кнопка работает как «за шефа набрали команду». Inline-кнопки шлют скрытый
callback и в переписке ничего не оставляют.

Подписи кнопок человеческие, поэтому фильтр Command на них не сработает — сопоставление
«подпись → действие» живёт в main.py (BUTTON_ACTIONS), а здесь только раскладка.
"""
from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup

# ---------- Нативное меню (синяя кнопка «Меню» слева от поля ввода) ----------

BOT_COMMANDS = [
    BotCommand(command="start", description="Что я умею"),
    BotCommand(command="refresh", description="Перечитать таблицу после правок"),
    BotCommand(command="new", description="Новый диалог, сбросить контекст"),
    BotCommand(command="pricing", description="Пересчитать расчётку для коммерции"),
    BotCommand(command="list_competitors", description="Конкуренты: кого отслеживаем"),
    BotCommand(command="check_competitors", description="Конкуренты: проверить сейчас"),
    BotCommand(command="competitors_report", description="Конкуренты: отчёт в Google Sheets"),
    BotCommand(command="add_competitor", description="Конкуренты: добавить сайт"),
    BotCommand(command="remove_competitor", description="Конкуренты: убрать сайт"),
    BotCommand(command="help", description="Помощь"),
]

# ---------- Подписи кнопок ----------
# Вынесены в константы: на них завязано сопоставление с действиями, опечатка
# в одной из двух копий строки молча сломала бы кнопку.

BTN_REFRESH = "🔄 Обновить данные"
BTN_NEW = "🆕 Новый диалог"
BTN_HELP = "❓ Что я умею"
BTN_COMPETITORS = "📈 Конкуренты"
BTN_PRICING = "💰 Расчётка"

BTN_COMP_LIST = "📋 Кого отслеживаем"
BTN_COMP_CHECK = "🔍 Проверить сейчас"
BTN_COMP_REPORT = "📊 Отчёт в таблицу"
BTN_COMP_ADD = "➕ Добавить сайт"
BTN_COMP_REMOVE = "➖ Убрать сайт"
BTN_BACK = "⬅️ Назад"


def _kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    """resize_keyboard: без него кнопки занимают пол-экрана на телефоне."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
    )


MAIN_KB = _kb([
    [BTN_REFRESH, BTN_NEW],
    [BTN_PRICING, BTN_COMPETITORS],
    [BTN_HELP],
])

COMPETITORS_KB = _kb([
    [BTN_COMP_LIST, BTN_COMP_CHECK],
    [BTN_COMP_REPORT],
    [BTN_COMP_ADD, BTN_COMP_REMOVE],
    [BTN_BACK],
])
