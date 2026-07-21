"""Ручной прогон мониторинга конкурентов БЕЗ Telegram — главный инструмент отладки.

Запуск:
    python -m scripts.check_competitors                 # все активные конкуренты
    python -m scripts.check_competitors --site dodo     # один сайт (подстрока url/имени)
    python -m scripts.check_competitors --no-export     # без выгрузки в Google Sheets
"""
import argparse
import asyncio
import re
import sys

from src.competitors.service import run_check

if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка конкурентов без Telegram")
    parser.add_argument("--site", help="только один сайт (подстрока url или имени)")
    parser.add_argument("--no-export", action="store_true", help="не выгружать в Google Sheets")
    args = parser.parse_args()

    summary = asyncio.run(run_check(
        bot=None,
        trigger="cli",
        only_url=args.site,
        notify=False,
        export=not args.no_export,
    ))
    # В консоль — без HTML-тегов
    print(re.sub(r"</?(b|pre|i)>", "", summary))


if __name__ == "__main__":
    main()
