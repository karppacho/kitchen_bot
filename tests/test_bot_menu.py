"""Тесты проводки меню бота. Без сети: только регистрация хендлеров и раскладка.

Главное, что защищаем — капкан aiogram: Dispatcher проверяет собственные хендлеры
в порядке регистрации, поэтому хендлер кнопок обязан стоять ДО catch-all
@dp.message(F.text). Иначе подписи вроде «🔄 Обновить данные» уйдут в LLM как
обычный вопрос, и кнопки молча перестанут работать.
"""
import pytest

from src.bot import keyboards as kb

main = pytest.importorskip(
    "src.bot.main",
    reason="нет конфига бота (TELEGRAM_BOT_TOKEN) — тест не про это",
)


def _handler_names() -> list[str]:
    return [o.callback.__name__ for o in main.dp.message.handlers]


def test_menu_handler_registered_before_catch_all():
    names = _handler_names()
    assert "on_menu_button" in names, "хендлер кнопок не зарегистрирован"
    assert "on_text" in names
    assert names.index("on_menu_button") < names.index("on_text"), (
        "хендлер кнопок зарегистрирован ПОСЛЕ catch-all F.text — "
        "нажатия на кнопки уйдут в LLM вместо команд"
    )


def test_command_handlers_registered_before_catch_all():
    """Тот же капкан для обычных команд — регрессия на уже описанную в CLAUDE.md."""
    names = _handler_names()
    idx_text = names.index("on_text")
    for cmd in ("cmd_start", "cmd_refresh", "cmd_new", "cmd_check_competitors"):
        assert names.index(cmd) < idx_text, f"{cmd} перехватится catch-all"


def test_every_button_has_an_action():
    """Каждая кнопка клавиатуры что-то делает: либо действие, либо навигация."""
    known = set(main.BUTTON_ACTIONS) | {kb.BTN_COMPETITORS, kb.BTN_BACK}
    for markup in (kb.MAIN_KB, kb.COMPETITORS_KB):
        for row in markup.keyboard:
            for button in row:
                assert button.text in known, f"кнопка «{button.text}» ничего не делает"


def test_every_action_is_callable():
    for label, handler in main.BUTTON_ACTIONS.items():
        assert callable(handler), f"для «{label}» не корутина"


def test_bot_commands_are_valid():
    """Telegram требует имя команды без слеша, в нижнем регистре."""
    assert kb.BOT_COMMANDS, "нативное меню пустое"
    for c in kb.BOT_COMMANDS:
        assert not c.command.startswith("/"), f"{c.command}: лишний слеш"
        assert c.command == c.command.lower(), f"{c.command}: должен быть в нижнем регистре"
        assert c.description, f"{c.command}: пустое описание"


def test_bot_commands_cover_registered_commands():
    """Команда, которой нет в нативном меню, для шефа невидима."""
    listed = {c.command for c in kb.BOT_COMMANDS}
    expected = {
        "start", "help", "new", "refresh",
        "list_competitors", "add_competitor", "remove_competitor",
        "check_competitors", "competitors_report",
    }
    assert expected <= listed, f"в меню не хватает: {expected - listed}"
