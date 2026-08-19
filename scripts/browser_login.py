"""Разовый вход на сайт конкурента руками — сессия остаётся в профиле браузера.

Зачем: некоторые сайты пускают только после действия человека — пройденной
капчи, принятого баннера, выбранного адреса. Всё это живёт в куках, а фетчер
на каждый прогон поднимал чистый контекст и терял их. Профиль на диске
(SiteProfile.use_profile) эту проблему закрывает: разово прошёл руками —
дальше еженедельные headless-прогоны используют ту же сессию.

Запуск:
    python -m scripts.browser_login burgerkingrus.ru
    python -m scripts.browser_login burger --url https://burgerkingrus.ru/menu

Откроется НАСТОЯЩЕЕ окно браузера. Сделай, что нужно, вернись в консоль и
нажми Enter. Куки и localStorage останутся в data/browser_profiles/<домен>/.

Лавке этот скрипт НЕ нужен: Яндекс детектирует Playwright и отдаёт
SmartCaptcha при любом режиме окна, поэтому она снимается обычным HTTP
(fetch_method='http'), вообще без браузера.
"""
import argparse
import asyncio
import sys

from src.competitors import storage
from src.competitors.fetcher import open_browser, profile_dir
from src.competitors.profiles import classify_page, get_profile
from src.competitors.html_text import page_to_menu_text

if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


async def login(site: str, url_override: str | None) -> None:
    from playwright.async_api import async_playwright

    comp = storage.find_competitor(site)
    if comp is None and url_override is None:
        print(f"Конкурент «{site}» не найден в базе. Добавь его или укажи --url явно.")
        return

    domain = comp.url if comp else site
    menu_url = url_override or comp.menu_url
    profile = get_profile(domain)
    if not profile.use_profile:
        print(
            f"⚠️  У {domain} в profiles.py не включён use_profile — сессия сохранится "
            f"в {profile_dir(domain)}, но рабочий прогон её НЕ прочитает."
        )

    print(f"Открываю {menu_url}")
    print(f"Профиль: {profile_dir(domain)}")

    async with async_playwright() as pw:
        context, closer = await open_browser(pw, domain, profile, headless=False)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(menu_url, wait_until="domcontentloaded", timeout=90_000)

            print()
            print("Окно браузера открыто. Сделай, что нужно (выбери адрес, пройди проверку),")
            print("а потом вернись сюда и нажми Enter.")
            await asyncio.get_running_loop().run_in_executor(None, input)

            # Проверяем то же, что проверит рабочий прогон — чтобы не уйти с ложной уверенностью
            text = page_to_menu_text(await page.content())
            code, reason = classify_page(text, profile)
            print()
            print(f"Снято {len(text)} симв., состояние страницы: {code}")
            if reason:
                print(f"  {reason}")
                print("  Сессия сохранена, но прогон в таком виде не пройдёт.")
            else:
                print("  Похоже на меню — можно запускать проверку:")
                print(f"  python -m scripts.check_competitors --site {domain} --no-llm")
        finally:
            await closer()
    print(f"\nПрофиль сохранён: {profile_dir(domain)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Разовый вход на сайт конкурента")
    parser.add_argument("site", help="домен или часть имени конкурента (как в базе)")
    parser.add_argument("--url", help="открыть другой URL вместо menu_url из базы")
    args = parser.parse_args()
    asyncio.run(login(args.site, args.url))


if __name__ == "__main__":
    main()
