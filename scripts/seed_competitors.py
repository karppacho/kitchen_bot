"""Первичное заполнение списка конкурентов (по итогам разведки Этапа 0, июль 2026).

Идемпотентен: add_competitor делает upsert по url — повторный запуск безопасен.
Запуск: python -m scripts.seed_competitors
"""
import sys

from src.competitors import storage

if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

# Разведка: Додо пробивается Playwright'ом; Бургер Кинг — глухая ботозащита (ручной
# режим); ВиТ и Cofix — таймаут с сети разработчика (вероятен гео-блок не-РФ IP,
# из РФ-сети могут открыться) — оставлены playwright, недельный прогон сам скажет.
SEED = [
    ("Додо Пицца", "dodopizza.ru", "https://dodopizza.ru/moscow", "playwright"),
    ("Бургер Кинг", "burgerkingrus.ru", "https://burgerkingrus.ru/menu", "manual"),
    ("Вкусно и точка", "vkusnoitochka.ru", "https://vkusnoitochka.ru/menu", "playwright"),
    ("Cofix", "cofix.ru", "https://cofix.ru/menu/", "playwright"),
]


def main() -> None:
    for name, url, menu_url, method in SEED:
        comp = storage.add_competitor(name, url, menu_url, fetch_method=method)
        print(f"OK: {comp.name} ({comp.url}) — {comp.fetch_method}")
    print(f"\nВсего активных: {len(storage.list_competitors())}")


if __name__ == "__main__":
    main()
