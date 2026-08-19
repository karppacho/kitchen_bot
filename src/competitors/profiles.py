"""Профили сайтов: как именно снимать меню с конкретного конкурента.

Раньше это был словарь `_SITE_QUIRKS` в fetcher.py на три ключа. Разросся,
потому что двум сайтам мало «открыть и проскроллить»:

- lavka.yandex.ru — без выбранного адреса доставки отдаёт ДЕМО-каталог
  («Это демо-каталог. Укажите адрес…»), а ссылка с context_id вообще
  превращается в «Категория не существует». Адрес живёт в куках, значит
  браузеру нужен постоянный профиль на диске (см. use_profile);
- burgerkingrus.ru — WAF Servicepipe отдаёт 403 на ВСЁ, включая robots.txt.
  Тот же постоянный профиль — единственный шанс сохранить куку JS-челленджа,
  если живое окно браузера её получит.

Ключ реестра — competitor.url, то есть голый домен («dodopizza.ru»),
НЕ menu_url. Сайта нет в реестре — работает DEFAULT_PROFILE, то есть ровно
прежнее поведение: goto + слепой скролл колесом.
"""
from dataclasses import dataclass, field

# Маркеры страницы-заглушки ботозащиты.
# ⚠️ РУССКИЕ обязательны. Сначала здесь были только английские (по заглушке БК
# «Forbidden…not a bot»), и капча Самоката — «Мы хотим убедиться, что имеем дело
# именно с вами, а не с ботом. Пожалуйста, пройдите проверку» — проходила как
# нормальное меню: 1300 символов, ни одного английского слова. Дальше она уехала
# бы в LLM, вернула 0 позиций и СТАЛА БЫ базой сравнения (suspect не ловит —
# там порог 5000 символов сырого текста). Шеф получил бы стену «пропала из меню».
BLOCK_MARKERS = (
    "if you are not a bot", "forbidden", "access denied", "captcha",
    "не робот", "а не с ботом", "вы бот", "пройдите проверку",
    "доступ ограничен", "подозрительная активность", "проверка безопасности",
    "antibot", "проверяем ваш браузер",
)

# Меньше этого текста на странице меню не бывает — считаем, что нас не пустили
DEFAULT_MIN_CHARS = 500

# Столько упоминаний рубля должно быть на настоящей странице меню
DEFAULT_MIN_PRICES = 10

# По чему считаем цены
PRICE_MARKERS = ("₽", "руб")

# Коды состояния страницы (что вернул classify_page)
PAGE_OK = "ok"
PAGE_BLOCKED = "blocked"
PAGE_NEEDS_SESSION = "needs_session"
PAGE_EMPTY = "empty"


@dataclass(frozen=True)
class SiteProfile:
    """Сценарий снятия меню с одного сайта.

    Всё опционально: пустой профиль = прежнее поведение фетчера.
    """

    # Постоянный профиль браузера на диске (куки + localStorage переживают прогон).
    # Нужен там, где страница зависит от состояния: выбранный адрес, кука челленджа.
    use_profile: bool = False

    # Клики перед сбором текста: cookie-баннер, «показать ещё», закрыть модалку.
    # Каждый селектор опционален — нет на странице, значит и не надо.
    click_selectors: tuple[str, ...] = ()

    # Мягкое ожидание карточки товара: истёк таймаут — работаем с тем, что есть.
    wait_selector: str | None = None
    wait_selector_timeout_ms: int = 15_000

    # wheel — фиксированное число прокруток (хватает статичным меню);
    # until_stable — крутим, пока растёт высота документа (виртуальные списки).
    scroll_mode: str = "wheel"
    max_scrolls: int = 10

    # Маркеры «страница открылась, но показывает не меню»: (что искать, что сказать шефу).
    # Ищутся в тексте В НИЖНЕМ РЕГИСТРЕ и по ВСЕЙ странице, а не по первым 3000 символов:
    # у Лавки «Это демо-каталог» стоит ниже шапки.
    state_markers: tuple[tuple[str, str], ...] = ()

    min_chars: int = DEFAULT_MIN_CHARS
    # Страница меню пестрит ценами: у рабочих сайтов их 237–343. Если цен
    # почти нет — это не меню, каким бы длинным ни был текст.
    min_prices: int = DEFAULT_MIN_PRICES

    # Сайт, который сам себя стирает: Самокат рендерит меню, а через секунду
    # обнуляет тело страницы (проверено 19.08.2026: +1s — 20695 символов и
    # 188 цен, +2s — ноль). Обычный сценарий (networkidle → клики → скролл)
    # приходит уже к пустому. Если задано, снимаем кадры каждые 250 мс в
    # течение стольких миллисекунд и берём самый богатый ценами.
    capture_best_ms: int = 0

    # Меню разбито на подстраницы категорий: обходим их в той же сессии.
    category_selector: str | None = None
    max_pages: int = 14
    # (regex, replacement) для href: у ВиТ плитки ведут на /<slug> (сервер отдаёт
    # 404), а реальные страницы живут на /menu/<slug> — SPA переписывает путь
    # на клиенте, повторяем это же преобразование.
    link_rewrite: tuple[str, str] | None = None

    # Дополнительные страницы меню, которые надо снять помимо menu_url.
    extra_urls: tuple[str, ...] = field(default=())


DEFAULT_PROFILE = SiteProfile()

_LAVKA_BAD_CATEGORY = (
    "Лавка не нашла категорию (в демо-каталоге живут не все разделы). "
    "Проверь menu_url: рабочие пути — /catalog/technical/category/all_ready_meals "
    "и соседние, а depot-специфичные вроде hot_streetfood без выбранного адреса не резолвятся"
)

_LAVKA_CAPTCHA = (
    "Яндекс показал SmartCaptcha. С домашнего адреса Лавку снимает обычный httpx, "
    "а с датацентрового IP капча приходит и на http, и на cdp (проверено 19.08.2026 "
    "на VPS). Значит дело в репутации адреса, а не в способе съёма"
)

PROFILES: dict[str, SiteProfile] = {
    "vkusnoitochka.ru": SiteProfile(
        category_selector="a.menu-category-item",
        max_pages=14,
        link_rewrite=(r"^https://vkusnoitochka\.ru/(?!menu/)", "https://vkusnoitochka.ru/menu/"),
    ),
    # Лавка снимается БЕЗ браузера (fetch_method='http'): Playwright ловит
    # SmartCaptcha, а httpx получает SSR-страницу с позициями и ценами.
    # Демо-каталог (без выбранного адреса) отдаёт реальный ассортимент склада
    # по умолчанию — для мониторинга цен конкурента этого достаточно.
    "lavka.yandex.ru": SiteProfile(
        extra_urls=(
            "https://lavka.yandex.ru/catalog/technical/category/from_restaurants",
            "https://lavka.yandex.ru/catalog/grocery/category/healthy_ge",
            "https://lavka.yandex.ru/catalog/grocery/category/goryachii_kofe",
        ),
        state_markers=(
            ("категория не существует", _LAVKA_BAD_CATEGORY),
            ("вы не робот", _LAVKA_CAPTCHA),
            ("smartcaptcha", _LAVKA_CAPTCHA),
        ),
    ),
    # Бургер Кинг: WAF Servicepipe режет Playwright (403 Forbidden), но живой
    # браузер через cdp пускает — проверено 19.08.2026 с российского VPS.
    # Главная страница сама по себе меню: ~278 цен, как у Додо и Cofix.
    # Категории /category/NN дают ещё больше, но упираются в потолок символов
    # и стоят вчетверо дороже по LLM — при нехватке позиций включить их через
    # category_selector='a[href*="/category/"]'.
    "burgerkingrus.ru": SiteProfile(
        use_profile=True,
    ),
    # Три даркстор-конкурента (август 2026). Все трое режут не-РФ IP, поэтому
    # снимаются ТОЛЬКО с выключенным VPN — см. раздел про конкурентов в CLAUDE.md.
    # use_profile=True: у Самоката капча решаемая (повернуть картинку), у Ozon
    # челлендж; пройденное руками через scripts/browser_login живёт в профиле.
    "samokat.ru": SiteProfile(
        use_profile=True,
        # Меню живёт на странице около секунды, потом Самокат обнуляет тело.
        # Скроллить и ждать networkidle бессмысленно — берём лучший кадр.
        capture_best_ms=6000,
        extra_urls=("https://samokat.ru/category/vsya-gotovaya-eda-13",),
    ),
    "vkusvill.ru": SiteProfile(
        use_profile=True,
        scroll_mode="until_stable",
        max_scrolls=40,
    ),
    "ozon.ru": SiteProfile(
        use_profile=True,
        scroll_mode="until_stable",
        max_scrolls=40,
        # В шапке бренда «276» товаров, одна страница отдаёт ~25. Скролл их не
        # догружает — у Ozon постраничная навигация, поэтому страницы явным
        # списком. Несуществующая страница не роняет прогон: _crawl_extra_urls
        # пишет предупреждение и идёт дальше.
        extra_urls=tuple(
            f"https://www.ozon.ru/brand/hot-fresh-101879249/?page={n}" for n in range(2, 8)
        ),
        state_markers=(
            # Формулировка Ozon про VPN — ЛОЖНЫЙ след (проверено 09.08.2026:
            # та же заглушка без туннеля). На деле он детектирует автоматизацию,
            # поэтому сайт снимается методом cdp. Маркер оставлен, чтобы
            # отличать «нас не пустили» от пустой страницы.
            ("выключите vpn", "Ozon не пустил: он детектирует автоматизацию. "
                              "Запусти python -m scripts.chrome_debug и оставь окно открытым"),
            ("нет соединения", "Ozon не пустил: он детектирует автоматизацию. "
                               "Запусти python -m scripts.chrome_debug и оставь окно открытым"),
        ),
    ),
}


def get_profile(domain: str) -> SiteProfile:
    return PROFILES.get(domain, DEFAULT_PROFILE)


def classify_page(text: str, profile: SiteProfile = DEFAULT_PROFILE) -> tuple[str, str | None]:
    """Что мы на самом деле сняли: меню, блок, «нужна сессия» или пустышка.

    Возвращает (код, человеческая причина). Причина None — только для PAGE_OK.

    Порядок проверок важен. Сначала state_markers: у Лавки демо-каталог короче
    min_chars, и без этой проверки бот сказал бы «похоже на ботозащиту» —
    ровно та ошибка, из-за которой разбор 06.08.2026 ушёл не в ту сторону.
    """
    low = text.lower()
    for marker, reason in profile.state_markers:
        if marker in low:
            return PAGE_NEEDS_SESSION, reason

    # Заглушка ботозащиты. Ищем по ВСЕМУ тексту, а не по первым 3000 символов:
    # у Ozon текст челленджа стоит после шапки с меню сайта.
    if any(m in low for m in BLOCK_MARKERS):
        return (
            PAGE_BLOCKED,
            "сайт блокирует нас (ботозащита). Пришли сохранённый HTML страницы "
            "меню файлом — в подписи укажи название конкурента",
        )

    if len(text) < profile.min_chars:
        return PAGE_EMPTY, f"страница почти пустая (текст {len(text)} симв.)"

    # Порога длины мало: 19.08.2026 ВкусВилл с сервера стабильно отдавал 621
    # символ и 4 цены — формально «не пусто», а по сути огрызок страницы.
    # Такой срез прошёл бы как ok и стал базой сравнения.
    prices = sum(low.count(m) for m in PRICE_MARKERS)
    if prices < profile.min_prices:
        return PAGE_EMPTY, (
            f"на странице почти нет цен ({prices}) — это огрызок, а не меню "
            f"(текст {len(text)} симв.)"
        )

    return PAGE_OK, None
