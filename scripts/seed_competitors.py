"""Первичное заполнение списка конкурентов (по итогам разведки Этапа 0, июль 2026).

Идемпотентен: add_competitor делает upsert по url — повторный запуск безопасен.
Запуск: python -m scripts.seed_competitors
"""
import sys

from src.competitors import storage

if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

# Разведка: Додо пробивается Playwright'ом; Бургер Кинг — WAF Servicepipe, 403 на
# всё с любого IP (ручной режим); ВиТ и Cofix — таймаут с сети разработчика
# (вероятен гео-блок не-РФ IP, из РФ-сети могут открыться) — оставлены playwright.
# Лавка — только 'http': Playwright она детектирует и отдаёт SmartCaptcha,
# а httpx получает SSR-страницу с ценами (см. profiles.py).
SEED = [
    ("Додо Пицца", "dodopizza.ru", "https://dodopizza.ru/moscow", "playwright"),
    ("Бургер Кинг", "burgerkingrus.ru", "https://burgerkingrus.ru/", "cdp"),
    ("Вкусно и точка", "vkusnoitochka.ru", "https://vkusnoitochka.ru/menu", "playwright"),
    # Именно cofix.global: на cofix.ru меню не отдаётся, запись по нему заархивирована
    ("Cofix", "cofix.global", "https://msk.cofix.global/", "playwright"),
    ("Яндекс Лавка", "lavka.yandex.ru",
     "https://lavka.yandex.ru/catalog/technical/category/all_ready_meals", "cdp"),
    # Даркстор-конкуренты (август 2026).
    # ВкусВилл: обычный Playwright, но ТОЛЬКО с выключенным VPN — с туннелем
    # рвёт соединение (ERR_CONNECTION_CLOSED).
    # Самокат и Ozon: 'cdp' — детектируют любой автоматизированный браузер,
    # пускает только обжитой профиль (python -m scripts.chrome_debug).
    # Вторая категория Самоката — в extra_urls его профиля, не отдельной записью.
    ("ВкусВилл", "vkusvill.ru", "https://vkusvill.ru/goods/gotovaya-eda/", "cdp"),
    ("Самокат", "samokat.ru", "https://samokat.ru/category/vsyo-goryachee-1", "cdp"),
    ("Ozon Hot&Fresh", "ozon.ru", "https://www.ozon.ru/brand/hot-fresh-101879249/", "manual"),
]


def main() -> None:
    for name, url, menu_url, method in SEED:
        comp = storage.add_competitor(name, url, menu_url, fetch_method=method)
        print(f"OK: {comp.name} ({comp.url}) — {comp.fetch_method}")
    print(f"\nВсего активных: {len(storage.list_competitors())}")


if __name__ == "__main__":
    main()
