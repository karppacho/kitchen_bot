"""Снятие текста меню с сайта конкурента. Playwright async API.

По итогам разведки (июль–август 2026):
- dodopizza.ru/moscow, vkusnoitochka.ru, msk.cofix.global — отдают меню
  headless-браузеру, сценарий не нужен;
- lavka.yandex.ru — страница открывается без всякой ботозащиты, но без
  выбранного адреса доставки показывает ДЕМО-каталог. Нужен постоянный
  профиль браузера с куками (scripts/browser_login.py);
- burgerkingrus.ru — WAF Servicepipe (заголовок x-sp-crid) режет 403 на всё,
  включая robots.txt, с любого IP. Фолбэк — fetch_method='manual'.
Публичного API меню у Додо нет (unitinfo отдаётся, меню — 403).

Что делать с конкретным сайтом, описано в src/competitors/profiles.py.
"""
import asyncio
import random
import re
from pathlib import Path

from loguru import logger

from src.competitors.html_text import MAX_TEXT_CHARS, page_to_menu_text
from src.competitors.models import Competitor, FetchResult
from src.competitors.profiles import (
    PAGE_BLOCKED,
    PAGE_NEEDS_SESSION,
    PAGE_OK,
    SiteProfile,
    classify_page,
    get_profile,
)
from src.config import settings

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Пауза между сайтами (анти-бан), сек
PAUSE_RANGE = (8.0, 20.0)

# Причины, при которых вторая попытка бессмысленна: страница отдалась,
# просто показала не то. Повтор через 5–10 с даст ровно тот же результат.
_NO_RETRY_REASONS = (PAGE_BLOCKED, PAGE_NEEDS_SESSION)

# Общие для обоих способов запуска настройки контекста
_CONTEXT_KWARGS = dict(
    user_agent=_UA,
    locale="ru-RU",
    timezone_id="Europe/Moscow",
    viewport={"width": 1366, "height": 768},
)
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# У настоящего Chrome navigator.webdriver === false, а НЕ undefined:
# Playwright ставит true, мы раньше стирали в undefined — это само по себе
# было отличием от живого браузера. Проверено 10.08.2026 сравнением
# отпечатков. Дальше этого JS-маскировка не спасает (см. CLAUDE.md).
_STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => false})"


async def pause_between_sites() -> None:
    await asyncio.sleep(random.uniform(*PAUSE_RANGE))


def profile_dir(domain: str) -> Path:
    """Каталог постоянного профиля браузера для домена."""
    return Path(settings.competitors_browser_profiles_dir) / domain


async def open_browser(pw, domain: str, profile: SiteProfile, headless: bool):
    """(context, closer) — постоянный профиль или одноразовый контекст.

    launch_persistent_context не отдаёт объект browser, поэтому закрывать надо
    сам контекст; закрывающая корутина возвращается вторым элементом, чтобы
    вызывающий код не разбирался, каким способом всё запустилось.
    """
    if profile.use_profile:
        user_data_dir = profile_dir(domain)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            str(user_data_dir), headless=headless, args=_LAUNCH_ARGS, **_CONTEXT_KWARGS,
        )
        closer = context.close
    else:
        browser = await pw.chromium.launch(headless=headless, args=_LAUNCH_ARGS)
        context = await browser.new_context(**_CONTEXT_KWARGS)
        closer = browser.close
    await context.add_init_script(_STEALTH_JS)
    return context, closer


async def _run_clicks(page, selectors) -> None:
    """Необязательные клики: cookie-баннер, закрыть модалку, «показать ещё»."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                await locator.click(timeout=5_000)
                await asyncio.sleep(random.uniform(0.5, 1.0))
        except Exception as e:
            logger.debug(f"[конкуренты] клик {selector} не прошёл: {type(e).__name__}: {e}")


async def _scroll(page, profile: SiteProfile) -> None:
    """Доскролл: ленивая подгрузка карточек меню."""
    if profile.scroll_mode == "until_stable":
        # Виртуальные списки (Лавка): крутим, пока растёт высота документа.
        last_height = 0
        for _ in range(profile.max_scrolls):
            await page.mouse.wheel(0, 1800)
            await asyncio.sleep(random.uniform(0.4, 0.9))
            height = await page.evaluate("document.body.scrollHeight")
            if height == last_height:
                break
            last_height = height
        return
    for _ in range(profile.max_scrolls):
        await page.mouse.wheel(0, 1800)
        await asyncio.sleep(random.uniform(0.4, 0.9))


async def _load_page_text(page, url: str, profile: SiteProfile, scrolls: int | None = None) -> str:
    """Открыть URL в существующей вкладке, отработать сценарий, снять чистый текст."""
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass  # SPA может тянуть аналитику вечно — работаем с тем, что отрендерилось

    await _run_clicks(page, profile.click_selectors)

    if profile.wait_selector:
        try:
            await page.wait_for_selector(
                profile.wait_selector, timeout=profile.wait_selector_timeout_ms,
            )
        except Exception:
            # Не блокер: карточки может не быть, потому что показан не тот экран.
            # Причину назовёт classify_page, а не таймаут селектора.
            logger.debug(f"[конкуренты] не дождался {profile.wait_selector} на {url}")

    if scrolls is None:
        await _scroll(page, profile)
    else:
        for _ in range(scrolls):
            await page.mouse.wheel(0, 1800)
            await asyncio.sleep(random.uniform(0.4, 0.9))
    return page_to_menu_text(await page.content())


def _join_pages(parts: list[str]) -> str:
    """Склеить страницы так, чтобы ни одна не пропала из-за общего потолка.

    Раньше склеивали целиком и резали результат по MAX_TEXT_CHARS — и если
    первая страница выбирала лимит одна (Самокат, 09.08.2026: ровно 60000
    символов, из них добрая часть — дерево навигации по всему сайту), вторая
    категория выбрасывалась молча. Теперь лимит делится между страницами.
    """
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:MAX_TEXT_CHARS]
    share = MAX_TEXT_CHARS // len(parts)
    return "\n\n".join(p[:share] for p in parts)


async def _crawl_categories(page, profile: SiteProfile, base_text: str) -> str:
    """Обход подстраниц категорий меню (site-profile) в той же браузерной сессии."""
    links = await page.eval_on_selector_all(
        profile.category_selector,
        "els => els.map(e => ({href: e.href, name: e.textContent.trim()}))",
    )
    rewrite = profile.link_rewrite
    seen: set[str] = {page.url}
    parts = [base_text]
    for link in links[: profile.max_pages]:
        href = link.get("href") or ""
        if rewrite and href:
            href = re.sub(rewrite[0], rewrite[1], href)
        if not href or href in seen:
            continue
        seen.add(href)
        await asyncio.sleep(random.uniform(0.8, 1.6))
        try:
            text = await _load_page_text(page, href, profile, scrolls=5)
        except Exception as e:
            logger.warning(f"[конкуренты] категория {href}: {type(e).__name__}: {e}")
            continue
        parts.append(f"== Категория: {link.get('name') or href} ==\n{text}")
    return _join_pages(parts)


async def _crawl_extra_urls(page, profile: SiteProfile, base_text: str) -> str:
    """Дополнительные страницы меню, заданные профилем явным списком."""
    parts = [base_text]
    for url in profile.extra_urls:
        await asyncio.sleep(random.uniform(0.8, 1.6))
        try:
            parts.append(f"== Категория: {url} ==\n{await _load_page_text(page, url, profile)}")
        except Exception as e:
            logger.warning(f"[конкуренты] доп. страница {url}: {type(e).__name__}: {e}")
    return _join_pages(parts)


async def _fetch_playwright_generic(competitor: Competitor, headless: bool) -> FetchResult:
    # Ленивый импорт: тесты и офлайн-код не требуют установленного браузера
    from playwright.async_api import async_playwright

    profile = get_profile(competitor.url)
    html, shot = "", None

    async with async_playwright() as pw:
        context, closer = await open_browser(pw, competitor.url, profile, headless)
        try:
            # У постоянного профиля уже есть открытая вкладка — берём её,
            # иначе профиль обрастает пустыми about:blank.
            page = context.pages[0] if context.pages else await context.new_page()
            text = await _load_page_text(page, competitor.menu_url, profile)
            if profile.category_selector:
                text = await _crawl_categories(page, profile, text)
            if profile.extra_urls:
                text = await _crawl_extra_urls(page, profile, text)
            # Улики снимаем ДО закрытия браузера: пригодятся, если текст плохой
            try:
                html = await page.content()
                shot = await page.screenshot(full_page=False)
            except Exception as e:
                logger.debug(f"[конкуренты] улики не снялись: {type(e).__name__}: {e}")
        finally:
            await closer()

    text = text[:MAX_TEXT_CHARS]
    reason_code, reason_text = classify_page(text, profile)
    if reason_code != PAGE_OK:
        return FetchResult(
            ok=False, text=text, error=reason_text, reason=reason_code, html=html, screenshot=shot,
        )
    return FetchResult(ok=True, text=text, html=html)


async def _fetch_cdp(competitor: Competitor) -> FetchResult:
    """Снятие меню в УЖЕ ЗАПУЩЕННОМ Chrome шефа (протокол отладки).

    Для Ozon и Самоката это единственный рабочий путь. Они детектируют любой
    автоматизированный браузер: встроенный Chromium, настоящий Chrome с ключом
    channel='chrome', headless и с видимым окном — всё блокируется одинаково.
    Пускает только обжитой профиль с историей и куками, то есть браузер, в
    котором шеф реально ходит по сайтам.

    Chrome должен быть запущен через `python -m scripts.chrome_debug`.
    """
    from playwright.async_api import async_playwright

    profile = get_profile(competitor.url)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(settings.competitors_cdp_url)
        except Exception as e:
            logger.debug(f"[конкуренты] CDP {settings.competitors_cdp_url}: {type(e).__name__}: {e}")
            return FetchResult(
                ok=False,
                error=("Chrome не запущен с отладочным портом. Запусти "
                       "`python -m scripts.chrome_debug` и оставь окно открытым"),
                reason=PAGE_NEEDS_SESSION,
            )

        # Контекст шефа — со всеми его куками. Своих не создаём: новый контекст
        # был бы чистым, а именно накопленные куки нас и пропускают.
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()   # СВОЯ вкладка: чужие не трогаем
        html, shot = "", None
        try:
            text = await _load_page_text(page, competitor.menu_url, profile)
            if profile.category_selector:
                text = await _crawl_categories(page, profile, text)
            if profile.extra_urls:
                text = await _crawl_extra_urls(page, profile, text)
            try:
                html = await page.content()
                shot = await page.screenshot(full_page=False)
            except Exception as e:
                logger.debug(f"[конкуренты] улики не снялись: {type(e).__name__}: {e}")
        finally:
            # Закрываем ТОЛЬКО свою вкладку. Браузер не наш — его не трогаем,
            # иначе проверка конкурентов закрывала бы шефу рабочие окна.
            await page.close()

    text = text[:MAX_TEXT_CHARS]
    reason_code, reason_text = classify_page(text, profile)
    if reason_code != PAGE_OK:
        return FetchResult(
            ok=False, text=text, error=reason_text, reason=reason_code, html=html, screenshot=shot,
        )
    return FetchResult(ok=True, text=text, html=html)


def _fetch_http_sync(competitor: Competitor, profile: SiteProfile) -> FetchResult:
    """Снятие меню обычным HTTP-запросом, без браузера.

    Для Лавки это не оптимизация, а единственный рабочий путь: Яндекс детектирует
    Playwright (и headless, и с видимым окном) и отдаёт SmartCaptcha, а на голый
    httpx спокойно возвращает SSR-страницу с позициями и ценами.

    trust_env=False — по той же причине, что в extractor.py: проверка гоняется
    без VPN, а HTTP(S)_PROXY в окружении смотрит на мёртвый локальный прокси.
    """
    import httpx

    headers = {
        "User-Agent": _UA,
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    parts: list[str] = []
    html = ""  # улика: HTML первой страницы, если состояние окажется плохим
    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True,
                      trust_env=False) as client:
        # Первый заход на корень: сайт заводит сессионные куки, без которых
        # страница категории отдаёт заглушку.
        root = f"https://{competitor.url}/"
        try:
            client.get(root)
        except Exception as e:
            logger.debug(f"[конкуренты] корень {root}: {type(e).__name__}: {e}")

        for i, url in enumerate((competitor.menu_url, *profile.extra_urls)):
            resp = client.get(url)
            if resp.status_code != 200:
                if i == 0:
                    return FetchResult(
                        ok=False, error=f"HTTP {resp.status_code} на {url}", reason="error",
                    )
                logger.warning(f"[конкуренты] доп. страница {url}: HTTP {resp.status_code}")
                continue
            text = page_to_menu_text(resp.text)
            parts.append(text if i == 0 else f"== Категория: {url} ==\n{text}")
            if i == 0:
                html = resp.text

    joined = _join_pages(parts)
    reason_code, reason_text = classify_page(joined, profile)
    if reason_code != PAGE_OK:
        return FetchResult(
            ok=False, text=joined, error=reason_text, reason=reason_code, html=html,
        )
    return FetchResult(ok=True, text=joined, html=html)


async def fetch(competitor: Competitor, headless: bool | None = None) -> FetchResult:
    """Текст меню конкурента. Ошибки не бросает — возвращает FetchResult(ok=False)."""
    if competitor.fetch_method == "manual":
        return FetchResult(
            ok=False,
            error="ручной режим — жду сохранённый HTML от шефа",
            reason=PAGE_BLOCKED,
        )
    if competitor.fetch_method not in ("playwright", "http", "cdp"):
        return FetchResult(
            ok=False,
            error=f"неизвестный fetch_method: {competitor.fetch_method}",
            reason="error",
        )

    if headless is None:
        headless = settings.competitors_headless

    profile = get_profile(competitor.url)
    last: FetchResult | None = None
    for attempt in (1, 2):
        try:
            if competitor.fetch_method == "http":
                result = await asyncio.get_running_loop().run_in_executor(
                    None, _fetch_http_sync, competitor, profile,
                )
            elif competitor.fetch_method == "cdp":
                result = await _fetch_cdp(competitor)
            else:
                result = await _fetch_playwright_generic(competitor, headless)
            if result.ok:
                logger.info(f"[конкуренты] {competitor.url}: снято {len(result.text)} симв.")
                return result
            last = result
            logger.warning(
                f"[конкуренты] {competitor.url}, попытка {attempt}: "
                f"{result.reason} — {result.error}"
            )
            if result.reason in _NO_RETRY_REASONS:
                return result
        except Exception as e:
            last = FetchResult(ok=False, error=f"{type(e).__name__}: {e}", reason="error")
            logger.warning(f"[конкуренты] {competitor.url}, попытка {attempt}: {last.error}")
        if attempt == 1:
            await asyncio.sleep(random.uniform(5, 10))
    return last or FetchResult(ok=False, error="?", reason="error")
