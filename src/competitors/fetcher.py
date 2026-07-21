"""Снятие текста меню с сайта конкурента. Playwright async API.

По итогам разведки (июль 2026):
- dodopizza.ru/moscow — отдаёт полное меню headless-браузеру;
- burgerkingrus.ru — ботозащита не пробивается → fetch_method='manual';
- vkusnoitochka.ru, cofix.ru — таймаут с сети разработчика (похоже на гео-блок
  не-РФ IP из-за VPN); из РФ-сети могут открыться.
Публичного API меню у Додо нет (unitinfo отдаётся, меню — 403), поэтому
адаптер один — generic Playwright. Реестр ADAPTERS — задел под site-quirks.
"""
import asyncio
import random
import re

from loguru import logger

from src.competitors.html_text import MAX_TEXT_CHARS, page_to_menu_text
from src.competitors.models import Competitor, FetchResult

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Маркеры страницы-заглушки ботозащиты (по факту разведки: у BK — «Forbidden…not a bot»)
_BLOCK_MARKERS = ("if you are not a bot", "forbidden", "access denied", "captcha")

# Меньше этого текста на странице меню не бывает — считаем, что нас не пустили
_MIN_MENU_CHARS = 500

# Пауза между сайтами (анти-бан), сек
PAUSE_RANGE = (8.0, 20.0)

# Site-quirks: у некоторых сайтов меню разбито на подстраницы категорий.
# category_selector — CSS-селектор ссылок категорий на главной странице меню;
# fetcher обходит их в одной браузерной сессии и склеивает тексты.
# link_rewrite — (regex, replacement) для href: у ВиТ плитки ведут на
# /<slug> (сервер отдаёт 404), а реальные страницы живут на /menu/<slug> —
# SPA переписывает путь на клиенте, повторяем это же преобразование.
_SITE_QUIRKS: dict[str, dict] = {
    "vkusnoitochka.ru": {
        "category_selector": "a.menu-category-item",
        "max_pages": 14,
        "link_rewrite": (r"^https://vkusnoitochka\.ru/(?!menu/)", "https://vkusnoitochka.ru/menu/"),
    },
}


async def pause_between_sites() -> None:
    await asyncio.sleep(random.uniform(*PAUSE_RANGE))


async def _load_page_text(page, url: str, scrolls: int = 10) -> str:
    """Открыть URL в существующей вкладке, доскроллить, снять чистый текст."""
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass  # SPA может тянуть аналитику вечно — работаем с тем, что отрендерилось
    # Доскролл: ленивая подгрузка карточек меню
    for _ in range(scrolls):
        await page.mouse.wheel(0, 1800)
        await asyncio.sleep(random.uniform(0.4, 0.9))
    return page_to_menu_text(await page.content())


async def _crawl_categories(page, quirk: dict, base_text: str) -> str:
    """Обход подстраниц категорий меню (site-quirk) в той же браузерной сессии."""
    links = await page.eval_on_selector_all(
        quirk["category_selector"],
        "els => els.map(e => ({href: e.href, name: e.textContent.trim()}))",
    )
    rewrite = quirk.get("link_rewrite")
    seen: set[str] = {page.url}
    parts = [base_text]
    for link in links[: quirk.get("max_pages", 14)]:
        href = link.get("href") or ""
        if rewrite and href:
            href = re.sub(rewrite[0], rewrite[1], href)
        if not href or href in seen:
            continue
        seen.add(href)
        await asyncio.sleep(random.uniform(0.8, 1.6))
        try:
            text = await _load_page_text(page, href, scrolls=5)
        except Exception as e:
            logger.warning(f"[конкуренты] категория {href}: {type(e).__name__}: {e}")
            continue
        parts.append(f"== Категория: {link.get('name') or href} ==\n{text}")
    return "\n\n".join(parts)


async def _fetch_playwright_generic(competitor: Competitor) -> FetchResult:
    # Ленивый импорт: тесты и офлайн-код не требуют установленного браузера
    from playwright.async_api import async_playwright

    quirk = _SITE_QUIRKS.get(competitor.url)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = await browser.new_context(
                user_agent=_UA,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1366, "height": 768},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            text = await _load_page_text(page, competitor.menu_url)
            if quirk and "category_selector" in quirk:
                text = await _crawl_categories(page, quirk, text)
        finally:
            await browser.close()

    text = text[:MAX_TEXT_CHARS]
    low = text[:3000].lower()
    if len(text) < _MIN_MENU_CHARS or any(m in low for m in _BLOCK_MARKERS):
        return FetchResult(
            ok=False,
            error=f"похоже на ботозащиту или пустую страницу (текст {len(text)} симв.)",
        )
    return FetchResult(ok=True, text=text)


async def fetch(competitor: Competitor) -> FetchResult:
    """Текст меню конкурента. Ошибки не бросает — возвращает FetchResult(ok=False)."""
    if competitor.fetch_method == "manual":
        return FetchResult(ok=False, error="ручной режим — жду сохранённый HTML от шефа")
    if competitor.fetch_method != "playwright":
        return FetchResult(ok=False, error=f"неизвестный fetch_method: {competitor.fetch_method}")

    last_error = ""
    for attempt in (1, 2):
        try:
            result = await _fetch_playwright_generic(competitor)
            if result.ok:
                logger.info(f"[конкуренты] {competitor.url}: снято {len(result.text)} симв.")
                return result
            last_error = result.error or "?"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[конкуренты] {competitor.url}, попытка {attempt}: {last_error}")
        if attempt == 1:
            await asyncio.sleep(random.uniform(5, 10))
    return FetchResult(ok=False, error=last_error)
