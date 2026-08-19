"""Переэкстракция меню из сохранённого сырого текста — без повторного скрапа.

Каждый прогон кладёт снятый текст в data/raw/<домен>/<timestamp>.txt (а неудачный
заход — в failed_<timestamp>.txt/.html/.png). Этот скрипт гоняет по нему только
LLM-экстракцию: удобно править промпт или проверять чанкинг, не дёргая сайт.

Запуск:
    python -m scripts.reextract --site lavka              # последний срез сайта
    python -m scripts.reextract --file data/raw/dodopizza.ru/20260806_181753.txt
    python -m scripts.reextract --site dodo --list        # что вообще сохранено

Ничего не пишет в базу — только печатает разобранные позиции.
"""
import argparse
import sys
from pathlib import Path

from src.competitors.extractor import extract_menu

if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

RAW_DIR = Path("data/raw")


def find_snapshots(site: str | None) -> list[Path]:
    """Все сохранённые .txt по сайту (или по всем), новые первыми."""
    dirs = [d for d in sorted(RAW_DIR.glob("*")) if d.is_dir()]
    if site:
        needle = site.lower()
        dirs = [d for d in dirs if needle in d.name.lower()]
    files: list[Path] = []
    for d in dirs:
        files.extend(d.glob("*.txt"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Переэкстракция из сохранённого текста")
    parser.add_argument("--site", help="домен или его часть (каталог в data/raw)")
    parser.add_argument("--file", help="конкретный файл с текстом")
    parser.add_argument("--list", action="store_true", help="показать сохранённые срезы и выйти")
    parser.add_argument("--limit", type=int, default=30, help="сколько позиций печатать")
    args = parser.parse_args()

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"Файла нет: {path}")
            return
    else:
        found = find_snapshots(args.site)
        if not found:
            print(f"В {RAW_DIR} нет сохранённых срезов" + (f" по «{args.site}»" if args.site else ""))
            return
        if args.list:
            for p in found:
                print(f"{p.stat().st_size:>8} байт  {p}")
            return
        path = found[0]

    text = path.read_text(encoding="utf-8")
    site_name = path.parent.name
    print(f"Файл: {path} ({len(text)} симв.), сайт: {site_name}\n")

    items, meta = extract_menu(text, site_name)
    print(f"Позиций: {len(items)}; токенов: {meta.get('total_tokens')}; "
          f"стоимость: {meta.get('cost_rub')} ₽\n")
    for item in items[: args.limit]:
        price = f"{item.price_rub:g} ₽" if item.price_rub is not None else "— цены нет"
        weight = f" [{item.weight}]" if item.weight else ""
        category = f"{item.category}: " if item.category else ""
        print(f"  {category}{item.item}{weight} — {price}")
    if len(items) > args.limit:
        print(f"  … и ещё {len(items) - args.limit}")


if __name__ == "__main__":
    main()
