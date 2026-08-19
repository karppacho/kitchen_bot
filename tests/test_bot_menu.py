"""Тесты проводки меню бота. Без сети: только регистрация хендлеров и раскладка.

Главное, что защищаем — капкан aiogram: Dispatcher проверяет собственные хендлеры
в порядке регистрации, поэтому хендлер кнопок обязан стоять ДО catch-all
@dp.message(F.text). Иначе подписи вроде «🔄 Обновить данные» уйдут в LLM как
обычный вопрос, и кнопки молча перестанут работать.
"""
import time

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


# ---------- двухшаговое добавление конкурента ----------
# Формат «/add_competitor <url>» в Telegram нерабочий: команда из меню уходит
# сразу, дописать аргумент некуда. Поэтому команда переводит в режим ожидания.

class _FakeUser:
    def __init__(self, uid):
        self.id, self.username, self.full_name = uid, "chef", "chef"


class _FakeMsg:
    def __init__(self, text, uid):
        self.text, self.from_user = text, _FakeUser(uid)


def _uid() -> int:
    from src.config import settings
    return settings.telegram_allowed_user_ids[0]


def test_pending_filter_off_by_default():
    """Без ожидания обычные сообщения должны доходить до LLM, а не сюда."""
    from src.bot import competitors as cc
    cc.clear_pending(_uid())
    assert cc.is_awaiting_link(_FakeMsg("сколько стоит чизбургер", _uid())) is False


def test_pending_filter_on_after_command():
    from src.bot import competitors as cc
    cc._set_pending(_uid(), "add")
    assert cc.is_awaiting_link(_FakeMsg("dodopizza.ru", _uid())) is True
    cc.clear_pending(_uid())


def test_pending_filter_ignores_commands():
    """Пока ждём ссылку, команды обязаны работать как обычно."""
    from src.bot import competitors as cc
    cc._set_pending(_uid(), "add")
    assert cc.is_awaiting_link(_FakeMsg("/refresh", _uid())) is False
    cc.clear_pending(_uid())


def test_pending_filter_ignores_other_users():
    from src.bot import competitors as cc
    cc._set_pending(_uid(), "add")
    assert cc.is_awaiting_link(_FakeMsg("dodopizza.ru", 999999)) is False
    cc.clear_pending(_uid())


def test_pending_expires(monkeypatch):
    from src.bot import competitors as cc
    cc._set_pending(_uid(), "add")
    later = time.time() + cc._PENDING_TTL + 1
    monkeypatch.setattr(cc.time, "time", lambda: later)
    assert cc.is_awaiting_link(_FakeMsg("dodopizza.ru", _uid())) is False


def test_pending_answer_registered_before_catch_all():
    """Иначе присланная ссылка уйдёт в LLM как обычный вопрос."""
    names = _handler_names()
    assert names.index("on_pending_answer") < names.index("on_text")


def test_help_no_longer_documents_broken_format():
    """«/add_competitor <url>» через меню невозможен — не обещаем его шефу."""
    assert "/add_competitor <url>" not in main.HELP_TEXT
    assert "ссылку следующим сообщением" in main.HELP_TEXT


# ---------- справка /start ----------

def test_help_text_is_valid_telegram_html():
    """Голые & < > ломают разбор HTML — Telegram отдаст ошибку вместо справки."""
    import re
    text = main.HELP_TEXT
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|#)", text), "неэкранированный &"
    assert not re.findall(r"[<>]", re.sub(r"</?b>", "", text)), "голые угловые скобки"
    assert text.count("<b>") == text.count("</b>"), "непарные теги"


def test_help_text_chunks_fit_telegram_limit():
    """Справка длинная — куски должны влезать, а теги не рваться между ними."""
    from src.bot.telegram_text import TELEGRAM_MAX_LEN, split_for_telegram
    for chunk in split_for_telegram(main.HELP_TEXT):
        assert len(chunk) <= TELEGRAM_MAX_LEN
        assert chunk.count("<b>") == chunk.count("</b>")


def test_help_text_covers_every_command():
    """Команда, не описанная в справке, для шефа не существует."""
    for command in ("/refresh", "/new", "/pricing", "/help"):
        assert command in main.HELP_TEXT, f"{command} не описана в справке"


def test_help_text_covers_key_features():
    """Каждая крупная фича должна быть названа — иначе о ней никто не узнает."""
    for marker in ("ПОСЧИТАТЬ", "ЧТО ЕСЛИ", "ПРИДУМАТЬ", "ЗАВЕСТИ БЛЮДО",
                   "ТЕХНОЛОГИЧЕСКИЕ КАРТЫ", "РАСЧЁТКА", "КОНКУРЕНТЫ"):
        assert marker in main.HELP_TEXT, f"в справке нет раздела «{marker}»"


def test_help_text_states_trust_rules():
    """Главные обещания бота: числа из калькулятора и запись только по «да»."""
    assert "калькулятор" in main.HELP_TEXT
    assert "подтверждения" in main.HELP_TEXT


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
