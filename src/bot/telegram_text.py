"""Разбиение длинных ответов под лимит Telegram (4096 символов на сообщение).

Чистая функция без зависимостей от aiogram/настроек — тестируется офлайн.
Ответ длиннее лимита раньше вообще не доходил до шефа (TelegramBadRequest).
"""

TELEGRAM_MAX_LEN = 4096
# Запас под закрытие/переоткрытие <pre> при разрезе таблицы между кусками
_CHUNK_LEN = 4000


def _pre_open(s: str) -> bool:
    """Есть ли в куске незакрытый <pre> (таблица разрезана посередине)."""
    return s.count("<pre>") > s.count("</pre>")


def split_for_telegram(text: str) -> list[str]:
    """Режет текст на куски ≤4096 символов по границам строк.

    Таблицы в <pre>...</pre> не ломаются: незакрытый <pre> в куске закрывается,
    следующий кусок открывается заново — каждый кусок остаётся валидным HTML.
    """
    if len(text) <= TELEGRAM_MAX_LEN:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # Сверхдлинную строку без переносов режем жёстко
        while len(line) > _CHUNK_LEN:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:_CHUNK_LEN])
            line = line[_CHUNK_LEN:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > _CHUNK_LEN and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    # Балансируем <pre> на стыках кусков
    balanced: list[str] = []
    reopen = False
    for chunk in chunks:
        if reopen:
            chunk = "<pre>\n" + chunk
        reopen = _pre_open(chunk)
        if reopen:
            chunk = chunk + "\n</pre>"
        balanced.append(chunk)
    return balanced
