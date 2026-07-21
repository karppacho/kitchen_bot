"""Этап 0 — разведка сайтов конкурентов.

Для каждого сайта проверяет:
1. Отдаёт ли что-то прямой HTTP-запрос (httpx) — или сразу 403/challenge.
2. Что видит Playwright (headless chromium): длина текста, есть ли цены (₽).
   Текст сохраняется в data/recon/<site>.txt — глазами проверить, есть ли там меню.
3. Для Додо — пробует публичное API (без скрапинга вообще).

Запуск: python -m scripts.recon_competitors [--site <кусок-имени>]
"""
import asyncio
import random
import re
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# Windows-консоль по умолчанию cp1251 — падает на ₽ и эмодзи
if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

RECON_DIR = Path("data/recon")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Кандидаты страниц меню. Точные URL зафиксируем по итогам разведки.
SITES: list[dict] = [
    {
        "name": "burgerking",
        "base": "https://burgerkingrus.ru",
        "menu_candidates": ["https://burgerkingrus.ru/menu", "https://burgerkingrus.ru"],
    },
    {
        "name": "dodopizza",
        "base": "https://dodopizza.ru",
        "menu_candidates": ["https://dodopizza.ru/moscow"],
    },
    {
        "name": "vkusnoitochka",
        "base": "https://vkusnoitochka.ru",
        "menu_candidates": ["https://vkusnoitochka.ru/menu", "https://vkusnoitochka.ru"],
    },
    {
        # cofix.global — корпоративный сайт без меню; российское меню — на cofix.ru
        "name": "cofix",
        "base": "https://cofix.ru",
        "menu_candidates": [
            "https://cofix.ru/menu/",
            "https://cofix.ru/menu",
            "https://cofix.ru",
        ],
    },
]

# Известные кандидаты публичного API Додо (инженерная открытость — publicapi.dodois.io)
DODO_API_CANDIDATES = [
    "https://publicapi.dodois.io/ru/api/v1/unitinfo/all",
    "https://dodopizza.ru/api/v2/menu?cityUrl=moscow",
    "https://publicapi.dodois.io/ru/api/v1/menu/moscow",
]

CHALLENGE_MARKERS = ("cf-browser-verification", "challenge-platform", "captcha", "ddos", "qrator")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


async def probe_plain(url: str) -> str:
    """Прямой запрос без браузера: фиксируем, пускают ли вообще."""
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            follow_redirects=True,
            timeout=20,
        ) as client:
            r = await client.get(url)
        body = r.text[:20000].lower()
        challenge = any(m in body for m in CHALLENGE_MARKERS)
        return (
            f"HTTP {r.status_code}, {len(r.content)} байт, server={r.headers.get('server', '?')}"
            + (", ПОХОЖЕ НА CHALLENGE" if challenge else "")
        )
    except Exception as e:
        return f"ошибка: {type(e).__name__}: {e}"


async def probe_playwright(pw, site: dict) -> None:
    """Рендер кандидатов меню; сохраняем текст лучшего в data/recon/."""
    browser = await pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        context = await browser.new_context(
            user_agent=UA,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1366, "height": 768},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        best_text = ""
        best_url = ""
        for url in site["menu_candidates"]:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass  # SPA может грузить что-то вечно — работаем с тем, что есть
                # Доскролл: ленивая подгрузка карточек меню
                for _ in range(8):
                    await page.mouse.wheel(0, 1800)
                    await asyncio.sleep(random.uniform(0.4, 0.9))
                text = html_to_text(await page.content())
                rub = text.count("₽") + len(re.findall(r"\d+\s*(?:руб|р\.)", text))
                print(f"    {url} -> текст {len(text)} симв., цен (₽): {rub}, title={await page.title()!r}")
                if len(text) > len(best_text):
                    best_text, best_url = text, url
            except Exception as e:
                print(f"    {url} -> ошибка: {type(e).__name__}: {e}")
            await asyncio.sleep(random.uniform(2, 4))

        if best_text:
            out = RECON_DIR / f"{site['name']}.txt"
            out.write_text(f"# {best_url}\n\n{best_text}", encoding="utf-8")
            print(f"    сохранено: {out} ({len(best_text)} симв.)")
    finally:
        await browser.close()


async def probe_dodo_api() -> None:
    print("\n=== Dodo Pizza — публичное API ===")
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept": "application/json"},
        follow_redirects=True,
        timeout=20,
    ) as client:
        for url in DODO_API_CANDIDATES:
            try:
                r = await client.get(url)
                ct = r.headers.get("content-type", "?")
                preview = r.text[:200].replace("\n", " ")
                print(f"  {url}\n    -> HTTP {r.status_code}, {ct}, {len(r.content)} байт: {preview}")
            except Exception as e:
                print(f"  {url}\n    -> ошибка: {type(e).__name__}: {e}")


async def main() -> None:
    only = None
    if "--site" in sys.argv:
        only = sys.argv[sys.argv.index("--site") + 1].lower()

    RECON_DIR.mkdir(parents=True, exist_ok=True)
    sites = [s for s in SITES if only is None or only in s["name"]]

    print("=== Прямые запросы (httpx) ===")
    for site in sites:
        print(f"  {site['base']}: {await probe_plain(site['base'])}")

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        for site in sites:
            print(f"\n=== Playwright: {site['name']} ===")
            await probe_playwright(pw, site)
            await asyncio.sleep(random.uniform(5, 10))

    if only is None or "dodo" in only:
        await probe_dodo_api()

    print("\nГотово. Тексты — в data/recon/*.txt, проверь глазами наличие меню и цен.")


if __name__ == "__main__":
    asyncio.run(main())
