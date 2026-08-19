"""Ручной прогон мониторинга конкурентов БЕЗ Telegram — главный инструмент отладки.

Запуск:
    python -m scripts.check_competitors                 # все активные конкуренты
    python -m scripts.check_competitors --site dodo     # один сайт (подстрока url/имени)
    python -m scripts.check_competitors --no-export     # без выгрузки в Google Sheets
    python -m scripts.check_competitors --headful       # с видимым окном браузера
    python -m scripts.check_competitors --site lavka --no-llm   # только снять текст
    python -m scripts.check_competitors --no-llm --brief        # разведка: кто пускает

--no-llm — первое, чем стоит проверять новый сайт: снимает текст и печатает его,
без экстракции, без записи в базу и без трат на LLM.

--no-llm --brief — первое, что надо запустить на новом сервере: пройдёт по всем
конкурентам и покажет одной таблицей, кого пускает ЭТОТ ip. Блокировки зависят
от адреса сильнее, чем от чего-либо ещё, поэтому мерить надо до деплоя (DEPLOY.md).
"""
import argparse
import asyncio
import re
import sys

from src.competitors import fetcher, storage
from src.competitors.service import run_check

if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

PREVIEW_CHARS = 2000


async def probe(only_url: str | None, headless: bool, brief: bool = False) -> None:
    """Снять текст и показать его. Ни экстракции, ни записи в базу."""
    comps = storage.list_competitors()
    if only_url:
        needle = only_url.lower()
        comps = [c for c in comps if needle in c.url.lower() or needle in c.name.lower()]
    if not comps:
        print("Подходящих конкурентов нет.")
        return

    verdicts: list[tuple[str, str, str]] = []
    for i, comp in enumerate(comps):
        if i > 0:
            await fetcher.pause_between_sites()
        if not brief:
            print(f"\n=== {comp.name} ({comp.url}) — {comp.menu_url}")
        result = await fetcher.fetch(comp, headless)
        # Цены — главный признак, что сняли меню, а не заглушку
        prices = result.text.count("₽") + result.text.lower().count("руб")
        mark = "OK " if result.ok and prices else "НЕТ"
        verdicts.append((
            mark,
            f"{comp.name} ({comp.fetch_method})",
            f"{len(result.text)} симв., цен {prices}" if result.ok else (result.error or result.reason),
        ))
        if brief:
            print(f"  {mark}  {comp.name}")
            continue
        print(f"    ok={result.ok} reason={result.reason} текст={len(result.text)} симв., цен {prices}")
        if result.error:
            print(f"    причина: {result.error}")
        if result.text:
            print("    ---")
            print(result.text[:PREVIEW_CHARS])
            if len(result.text) > PREVIEW_CHARS:
                print(f"    … ещё {len(result.text) - PREVIEW_CHARS} симв.")

    print("\n" + "=" * 60)
    print("ИТОГ (что пускает нас с этого адреса):")
    for mark, name, detail in verdicts:
        print(f"  {mark}  {name:<34} {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка конкурентов без Telegram")
    parser.add_argument("--site", help="только один сайт (подстрока url или имени)")
    parser.add_argument("--no-export", action="store_true", help="не выгружать в Google Sheets")
    parser.add_argument("--headful", action="store_true", help="видимое окно браузера")
    parser.add_argument(
        "--no-llm", action="store_true",
        help="только снять текст и напечатать: без экстракции и без записи в базу",
    )
    parser.add_argument(
        "--brief", action="store_true",
        help="с --no-llm: только вердикт по каждому сайту, без простыней текста",
    )
    args = parser.parse_args()
    headless = not args.headful

    if args.no_llm:
        asyncio.run(probe(args.site, headless, args.brief))
        return

    summary = asyncio.run(run_check(
        bot=None,
        trigger="cli",
        only_url=args.site,
        notify=False,
        export=not args.no_export,
        headless=headless,
    ))
    # В консоль — без HTML-тегов
    print(re.sub(r"</?(b|pre|i)>", "", summary))


if __name__ == "__main__":
    main()
