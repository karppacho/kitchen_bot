"""Офлайн-тесты мониторинга конкурентов: comparator, парсинг LLM-ответов,
storage (sqlite на tmp_path), html_text. Сеть и LLM не нужны.

Запуск: pytest tests/test_competitors.py
"""
import asyncio
import sys
from decimal import Decimal

from src.competitors import comparator, fetcher, profiles, storage
from src.competitors.comparator import diff_snapshots, norm_name
from src.competitors.extractor import parse_items_json, parse_price, _split_chunks
from src.competitors.format import format_check_summary
from src.competitors.html_text import page_to_menu_text, read_uploaded_document
from src.competitors.models import CheckSiteResult, Competitor, Diff, ExtractedItem, FetchResult
from src.config import settings

PCT = Decimal("10")
RUB = Decimal("30")


def _item(name, price=None, weight=None, category=None):
    return ExtractedItem(item=name, price_rub=price, weight=weight, category=category)


async def _no_sleep(_seconds):
    """Заглушка asyncio.sleep: пауза между попытками не нужна тесту."""
    return None


# ---------- comparator ----------

def test_norm_name():
    assert norm_name("Воппер!") == norm_name("воппер")
    assert norm_name("Пельмени с ёлкой") == norm_name("пельмени  с елкой")
    assert norm_name(None) == ""


def test_threshold_is_max_of_pct_and_rub():
    # old=299: порог = max(29.9, 30) = 30 → дельта +30 значима, +29 нет
    old = [_item("Воппер", 299)]
    assert diff_snapshots(old, [_item("Воппер", 329)], PCT, RUB)[0].change_type == "price_up"
    assert diff_snapshots(old, [_item("Воппер", 328)], PCT, RUB) == []
    # old=400: порог = max(40, 30) = 40 → дельта +35 не значима, +40 значима
    old = [_item("Латте", 400)]
    assert diff_snapshots(old, [_item("Латте", 435)], PCT, RUB) == []
    d = diff_snapshots(old, [_item("Латте", 360)], PCT, RUB)[0]
    assert d.change_type == "price_down"
    assert d.delta_rub == -40.0
    assert d.delta_percent == -10.0


def test_weight_makes_items_distinct():
    old = [_item("Капучино", 199, "0,2 л"), _item("Капучино", 249, "0,4 л")]
    new = [_item("Капучино", 199, "0,2 л"), _item("Капучино", 299, "0,4 л")]
    diffs = diff_snapshots(old, new, PCT, RUB)
    assert len(diffs) == 1
    assert diffs[0].weight == "0,4 л"
    assert diffs[0].delta_rub == 50.0


def test_weight_flap_matches_by_name():
    # LLM в разных прогонах пишет вес по-разному («Стандартный» ↔ пусто) —
    # позиция не должна превращаться в пару «пропала + новинка»
    old = [_item("Биг Хит Комбо", 376, "Стандартный")]
    assert diff_snapshots(old, [_item("Биг Хит Комбо", 376, None)], PCT, RUB) == []
    d = diff_snapshots(old, [_item("Биг Хит Комбо", 426, None)], PCT, RUB)
    assert len(d) == 1 and d[0].change_type == "price_up"


def test_rename_is_removed_plus_added():
    diffs = diff_snapshots([_item("Старый бургер", 100)], [_item("Новый бургер", 100)], PCT, RUB)
    types = sorted(d.change_type for d in diffs)
    assert types == ["item_added", "item_removed"]


def test_missing_price_not_compared():
    assert diff_snapshots([_item("Кофе", None)], [_item("Кофе", 500)], PCT, RUB) == []
    assert diff_snapshots([_item("Кофе", 500)], [_item("Кофе", None)], PCT, RUB) == []


# ---------- extractor: парсинг ----------

def test_parse_price():
    assert parse_price(289) == 289.0
    assert parse_price("289") == 289.0
    assert parse_price("от 289 ₽") == 289.0
    assert parse_price("1 030,00") == 1030.0
    assert parse_price(None) is None
    assert parse_price("") is None
    assert parse_price("цена по запросу") is None


def test_parse_items_plain_json():
    raw = '{"items": [{"category": "Пиццы", "item": "Пепперони", "weight": "30 см", "price_rub": 359}]}'
    items = parse_items_json(raw)
    assert len(items) == 1
    assert items[0].item == "Пепперони"
    assert items[0].price_rub == 359.0


def test_parse_items_fenced_json_and_bare_list():
    fenced = '```json\n{"items": [{"item": "Латте", "price_rub": "199"}]}\n```'
    assert parse_items_json(fenced)[0].price_rub == 199.0
    bare = '[{"item": "Латте", "price_rub": 199}]'
    assert parse_items_json(bare)[0].item == "Латте"


def test_parse_items_garbage_survives():
    assert parse_items_json("не могу разобрать") == []
    raw = '{"items": [{"item": ""}, {"нет": "имени"}, {"item": "Ролл", "price_rub": null}, "мусор"]}'
    items = parse_items_json(raw)
    assert len(items) == 1
    assert items[0].item == "Ролл"
    assert items[0].price_rub is None


def test_split_chunks_by_lines():
    text = "\n".join(f"строка {i}" for i in range(100))
    chunks = _split_chunks(text, max_chars=200)
    assert len(chunks) > 1
    assert "\n".join(chunks) == text  # ничего не потеряли


# ---------- storage (sqlite на tmp_path) ----------

def test_storage_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "competitors_db_path", str(tmp_path / "test.db"))

    comp = storage.add_competitor("Додо", "dodopizza.ru", "https://dodopizza.ru/moscow")
    assert comp.id > 0
    # upsert по url: повторное добавление обновляет, не плодит дубли
    comp2 = storage.add_competitor("Додо Пицца", "dodopizza.ru", "https://dodopizza.ru/spb")
    assert comp2.id == comp.id
    assert comp2.menu_url == "https://dodopizza.ru/spb"
    assert len(storage.list_competitors()) == 1

    # снимок и чтение обратно
    items = [_item("Пепперони", 359, "30 см", "Пиццы")]
    snap_id = storage.save_snapshot(comp.id, items, raw_chars=8000)
    got = storage.latest_ok_snapshot(comp.id)
    assert got is not None
    assert got[0] == snap_id
    assert got[2][0].item == "Пепперони"
    assert got[2][0].price_rub == 359.0

    # suspect-срез не становится базой сравнения
    storage.save_snapshot(comp.id, [], status="suspect", raw_chars=8000)
    assert storage.latest_ok_snapshot(comp.id)[0] == snap_id
    # но в last_check_info он виден
    assert storage.last_check_info(comp.id)[1] == "suspect"

    # диффы персистятся
    storage.save_changes(comp.id, [Diff(change_type="price_up", item="Пепперони",
                                        old_price=359, new_price=399, delta_rub=40,
                                        delta_percent=11.1)], snap_id, snap_id + 1)

    # soft delete
    gone = storage.deactivate_competitor("dodopizza.ru")
    assert gone is not None
    assert storage.list_competitors() == []
    assert len(storage.list_competitors(active_only=False)) == 1
    assert storage.deactivate_competitor("neizvesten.ru") is None


def test_find_competitor(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "competitors_db_path", str(tmp_path / "test.db"))
    storage.add_competitor("Бургер Кинг", "burgerkingrus.ru", "https://burgerkingrus.ru/menu")
    assert storage.find_competitor("бургер").url == "burgerkingrus.ru"
    assert storage.find_competitor("burgerkingrus.ru").name == "Бургер Кинг"
    assert storage.find_competitor("додо") is None


def test_days_since_ok_snapshot(tmp_path, monkeypatch):
    """Возраст считается по УДАЧНЫМ срезам: неудачная попытка не «освежает» данные."""
    import sqlite3
    from datetime import datetime, timedelta

    monkeypatch.setattr(settings, "competitors_db_path", str(tmp_path / "test.db"))
    comp = storage.add_competitor("Бургер Кинг", "burgerkingrus.ru",
                                  "https://burgerkingrus.ru/menu", fetch_method="manual")
    assert storage.days_since_ok_snapshot(comp.id) is None  # срезов ещё не было

    snap_id = storage.save_snapshot(comp.id, [_item("Воппер", 359)], source="manual_html")
    assert storage.days_since_ok_snapshot(comp.id) == 0

    # состарим срез на 20 дней
    old = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute("UPDATE snapshots SET taken_at = ? WHERE id = ?", (old, snap_id))
    assert storage.days_since_ok_snapshot(comp.id) == 20

    # свежая НЕудачная попытка возраст не сбрасывает
    storage.save_snapshot(comp.id, [], status="fetch_failed", error="403")
    assert storage.days_since_ok_snapshot(comp.id) == 20


def test_upsert_keeps_fetch_method(tmp_path, monkeypatch):
    """Повторное /add_competitor не должно сбрасывать ручной режим в playwright.

    Бот и tool manage_competitors зовут add_competitor без fetch_method —
    раньше upsert подставлял дефолт, и Бургер Кинг молча терял 'manual'.
    """
    monkeypatch.setattr(settings, "competitors_db_path", str(tmp_path / "test.db"))
    storage.add_competitor(
        "Бургер Кинг", "burgerkingrus.ru", "https://burgerkingrus.ru/menu",
        city="Питер", fetch_method="manual",
    )
    again = storage.add_competitor(
        "Бургер Кинг", "burgerkingrus.ru", "https://burgerkingrus.ru/menu",
    )
    assert again.fetch_method == "manual"
    assert again.city == "Питер"
    # явная передача по-прежнему работает
    changed = storage.add_competitor(
        "Бургер Кинг", "burgerkingrus.ru", "https://burgerkingrus.ru/menu",
        fetch_method="playwright",
    )
    assert changed.fetch_method == "playwright"
    # у нового конкурента — прежние дефолты
    fresh = storage.add_competitor("Додо", "dodopizza.ru", "https://dodopizza.ru/moscow")
    assert (fresh.fetch_method, fresh.city) == ("playwright", "Москва")


# ---------- profiles: классификация того, что сняли со страницы ----------

# Живой текст lavka.yandex.ru от 06.08.2026 без выбранного адреса (291 симв.).
LAVKA_DEMO_TEXT = (
    "Ещё больше скидок и новых функций — в приложении Лавки\n"
    "Перейти\n"
    "Категория не существует\n"
    "На главный экран\n"
    "Это демо-каталог. Укажите адрес, чтобы посмотреть настоящий\n"
    "5–10 мин, 0 ₽\n"
    "Доставка бесплатно, а это всегда приятно\n"
    "В корзине пока ничего нет.\n"
)

# Живая заглушка burgerkingrus.ru (Servicepipe), data/recon/burgerking.txt
BK_BLOCK_TEXT = (
    "Forbidden\nDatetime: 2026-08-06 16:02:04 +0000\nIP: 79.139.160.8\nID: 42TiU56Q44Y1\n"
    "Origin: https://burgerkingrus.ru\n"
    "If you are not a bot, please copy the report and send it to our support team.\nCopy"
)


def test_classify_lavka_bad_category():
    """«Категория не существует» — это НЕ ботозащита, а неверный menu_url.

    Регресс на разбор 06.08.2026: бот на всё короткое отвечал «похоже на
    ботозащиту» и уводил в неверную сторону.
    """
    code, reason = profiles.classify_page(LAVKA_DEMO_TEXT, profiles.get_profile("lavka.yandex.ru"))
    assert code == profiles.PAGE_NEEDS_SESSION
    assert "menu_url" in reason


def test_classify_lavka_captcha():
    """SmartCaptcha ловится маркером профиля и объясняет, что делать."""
    text = "Вы не робот?\nПодтвердите, что запросы отправляли вы, а не робот\nYandex SmartCaptcha"
    code, reason = profiles.classify_page(text, profiles.get_profile("lavka.yandex.ru"))
    assert code == profiles.PAGE_NEEDS_SESSION
    assert "http" in reason


def test_classify_blocked():
    code, reason = profiles.classify_page(BK_BLOCK_TEXT, profiles.get_profile("burgerkingrus.ru"))
    assert code == profiles.PAGE_BLOCKED
    assert "HTML" in reason


# Живая капча samokat.ru от 08.08.2026 (Playwright, headless)
SAMOKAT_CAPTCHA_TEXT = (
    "Мы хотим убедиться, что имеем дело именно с вами, а не с ботом.\n"
    "Пожалуйста, пройдите проверку, чтобы получить доступ к сайту.\n"
    "2026-08-08 16:38:13 +0000\nВаш IP:\n79.139.175.107\nID запроса:\nDcVFFP10OiE1\n"
    "Разверните картинку горизонтально\nЗачем потребовалась эта проверка?\n"
    "Что-то в поведении вашего браузера привлекло наше внимание.\n"
) * 4

# Живой челлендж ozon.ru от 08.08.2026
OZON_BLOCK_TEXT = (
    "Antibot Challenge Page\nfab_chlg_20260808163642_01KZH3QQT3QVZ4NBBJS0AMX9Z1\n"
    "Попробуйте:\nобновить страницу\nотключить расширения и вновь обновить страницу\n"
)


def test_classify_russian_block_pages():
    """Русские заглушки обязаны ловиться: английских слов в них нет вовсе.

    Капча Самоката — 1300 символов связного русского текста. С одними
    английскими маркерами она проходила как нормальное меню, уезжала в LLM,
    возвращала 0 позиций и становилась базой сравнения — а дальше шеф получал
    стену «пропала из меню» по всему ассортименту.
    """
    assert len(SAMOKAT_CAPTCHA_TEXT) > profiles.DEFAULT_MIN_CHARS  # порог длины не спасёт
    assert profiles.classify_page(SAMOKAT_CAPTCHA_TEXT)[0] == profiles.PAGE_BLOCKED
    assert profiles.classify_page(OZON_BLOCK_TEXT)[0] == profiles.PAGE_BLOCKED


def test_classify_finds_marker_after_header():
    """Маркер ищется по всему тексту: у Ozon челлендж стоит ПОСЛЕ шапки сайта."""
    page = "Каталог\nДоставка\nОплата\n" + "меню " * 1200 + "\nAntibot Challenge Page"
    assert profiles.classify_page(page)[0] == profiles.PAGE_BLOCKED


def test_classify_empty_and_ok():
    code, reason = profiles.classify_page("Меню\nПепперони 359 ₽")
    assert code == profiles.PAGE_EMPTY
    assert "почти пустая" in reason

    code, reason = profiles.classify_page("Пепперони 359 ₽\n" * 100)
    assert (code, reason) == (profiles.PAGE_OK, None)


def test_classify_state_markers_win_over_length():
    """Заглушка Лавки короче min_chars — маркер обязан сработать первым."""
    assert len(LAVKA_DEMO_TEXT) < profiles.DEFAULT_MIN_CHARS
    code, _ = profiles.classify_page(LAVKA_DEMO_TEXT, profiles.get_profile("lavka.yandex.ru"))
    assert code == profiles.PAGE_NEEDS_SESSION
    # без профиля тот же текст — просто пустая страница
    assert profiles.classify_page(LAVKA_DEMO_TEXT)[0] == profiles.PAGE_EMPTY


def test_default_profile_for_unknown_site():
    """Сайты вне реестра работают ровно как раньше."""
    assert profiles.get_profile("dodopizza.ru") is profiles.DEFAULT_PROFILE


# ---------- fetcher: диспетчер fetch_method (браузер не нужен) ----------

def _competitor(fetch_method: str) -> Competitor:
    return Competitor(id=1, name="Тест", url="test.ru", menu_url="https://test.ru/menu",
                      fetch_method=fetch_method)


def test_fetch_manual_does_not_launch_browser():
    result = asyncio.run(fetcher.fetch(_competitor("manual")))
    assert result.ok is False
    assert result.reason == profiles.PAGE_BLOCKED
    assert "ручной режим" in result.error


def test_fetch_unknown_method():
    result = asyncio.run(fetcher.fetch(_competitor("dodo_api")))
    assert result.ok is False
    assert result.reason == "error"
    assert "dodo_api" in result.error


def test_fetch_http_method_skips_browser(monkeypatch):
    """fetch_method='http' обязан идти мимо Playwright.

    Яндекс детектирует Playwright и отдаёт SmartCaptcha даже с видимым окном,
    а голому httpx — нормальную страницу. Если диспетчер перепутает методы,
    Лавка молча перестанет сниматься.
    """
    def boom(*a, **kw):
        raise AssertionError("http-метод не должен запускать браузер")

    monkeypatch.setattr(fetcher, "_fetch_playwright_generic", boom)
    monkeypatch.setattr(
        fetcher, "_fetch_http_sync",
        lambda comp, profile: FetchResult(ok=True, text="Грудка куриная 342 ₽"),
    )
    result = asyncio.run(fetcher.fetch(_competitor("http")))
    assert result.ok is True


def test_fetch_cdp_without_chrome_gives_actionable_error(monkeypatch):
    """Chrome не запущен → внятная подсказка, а не трейс. И без ретрая.

    Вторая попытка через 5–10 с ничего не изменит: порт как не слушал,
    так и не слушает.
    """
    monkeypatch.setattr(settings, "competitors_cdp_url", "http://localhost:59999")
    result = asyncio.run(fetcher.fetch(_competitor("cdp")))
    assert result.ok is False
    assert result.reason == profiles.PAGE_NEEDS_SESSION
    assert "chrome_debug" in result.error


def test_fetch_cdp_does_not_close_foreign_browser(monkeypatch):
    """Браузер шефа закрывать нельзя — только свою вкладку.

    Иначе еженедельная проверка захлопывала бы ему рабочие окна.
    """
    closed = {"browser": False, "page": False}

    class _Page:
        url = "https://test.ru/menu"
        async def goto(self, *a, **kw): pass
        async def wait_for_load_state(self, *a, **kw): pass
        async def mouse_wheel(self, *a): pass
        async def content(self): return "<div>Борщ 227 ₽</div>" * 60
        async def screenshot(self, **kw): return b""
        async def close(self): closed["page"] = True
        @property
        def mouse(self):
            class _M:
                async def wheel(self, *a): pass
            return _M()

    class _Ctx:
        async def new_page(self): return _Page()

    class _Browser:
        contexts = [_Ctx()]
        async def close(self): closed["browser"] = True

    class _Chromium:
        async def connect_over_cdp(self, url): return _Browser()

    class _PW:
        chromium = _Chromium()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(fetcher, "async_playwright", lambda: _PW(), raising=False)
    monkeypatch.setitem(sys.modules, "playwright.async_api",
                        type(sys)("playwright.async_api"))
    sys.modules["playwright.async_api"].async_playwright = lambda: _PW()

    result = asyncio.run(fetcher.fetch(_competitor("cdp")))
    assert result.ok is True
    assert closed["page"] is True, "свою вкладку закрыть обязаны"
    assert closed["browser"] is False, "чужой браузер закрывать нельзя"


def test_join_pages_keeps_every_page():
    """Лимит символов делится между страницами — ни одна не пропадает молча.

    Регресс: Самокат 09.08.2026 отдал ровно 60000 символов (потолок
    MAX_TEXT_CHARS) одной первой категорией, и вторая, склеенная следом,
    срезалась целиком — в срез не попало полменю.
    """
    from src.competitors.html_text import MAX_TEXT_CHARS

    huge, small = "а" * MAX_TEXT_CHARS, "Борщ 227 ₽"
    joined = fetcher._join_pages([huge, small])
    assert len(joined) <= MAX_TEXT_CHARS
    assert small in joined, "вторая страница обязана уцелеть"

    # одна страница режется по общему потолку, как раньше
    assert len(fetcher._join_pages([huge])) == MAX_TEXT_CHARS
    assert fetcher._join_pages([]) == ""

    # короткие страницы не режутся вовсе
    pages = ["Пицца 359 ₽", "Кофе 199 ₽", "Суп 227 ₽"]
    assert all(p in fetcher._join_pages(pages) for p in pages)


def test_lavka_is_http_not_playwright():
    """Профиль Лавки не должен звать браузер: у неё extra_urls, а не use_profile."""
    lavka = profiles.get_profile("lavka.yandex.ru")
    assert lavka.use_profile is False
    assert lavka.extra_urls


def test_fetch_does_not_retry_needs_session(monkeypatch):
    """Вторая попытка через 5–10 с даст ровно тот же демо-каталог — не ходим дважды."""
    calls = []

    async def fake_generic(comp, headless):
        calls.append(headless)
        return FetchResult(ok=False, error="нужен адрес", reason=profiles.PAGE_NEEDS_SESSION)

    monkeypatch.setattr(fetcher, "_fetch_playwright_generic", fake_generic)
    result = asyncio.run(fetcher.fetch(_competitor("playwright"), headless=True))
    assert result.reason == profiles.PAGE_NEEDS_SESSION
    assert len(calls) == 1


def test_fetch_retries_transient_error(monkeypatch):
    calls = []

    async def fake_generic(comp, headless):
        calls.append(headless)
        if len(calls) == 1:
            raise TimeoutError("сеть моргнула")
        return FetchResult(ok=True, text="Пепперони 359 ₽")

    monkeypatch.setattr(fetcher, "_fetch_playwright_generic", fake_generic)
    monkeypatch.setattr(fetcher.asyncio, "sleep", _no_sleep)
    result = asyncio.run(fetcher.fetch(_competitor("playwright"), headless=True))
    assert result.ok is True
    assert len(calls) == 2


# ---------- service: срез без цен не становится базой сравнения ----------

def test_snapshot_without_prices_is_suspect(tmp_path, monkeypatch):
    """Страница без единого упоминания рубля — не меню, что бы ни вернула LLM.

    Страховка от заглушек, чьих формулировок ещё нет в BLOCK_MARKERS: они
    короткие, и порог _SUSPECT_MIN_CHARS (5000) их пропускает. Такой срез
    обязан получить статус suspect и НЕ стать базой сравнения — иначе
    следующий прогон выдаст стену ложных «пропала из меню».
    """
    from src.competitors import service

    monkeypatch.setattr(settings, "competitors_db_path", str(tmp_path / "test.db"))
    comp = storage.add_competitor("Самокат", "samokat.ru", "https://samokat.ru/category/x")

    # нормальный срез — база сравнения
    good = [_item(f"Блюдо {i}", 100 + i) for i in range(20)]
    service._process_snapshot_sync(comp, good, "Блюдо 1 — 359 ₽" * 40, None, "auto")
    base_id = storage.latest_ok_snapshot(comp.id)[0]

    # заглушка: мало позиций, текст короткий, ни одного «₽»/«руб»
    res = service._process_snapshot_sync(
        comp, [], "Пожалуйста, пройдите проверку, чтобы получить доступ", None, "auto",
    )
    assert res.status == "suspect"
    assert storage.latest_ok_snapshot(comp.id)[0] == base_id  # база не сменилась
    assert res.diffs == []                                    # и диффов не наплодили

    # короткий, но настоящий срез с ценами suspect-ом НЕ становится
    res = service._process_snapshot_sync(
        comp, [_item("Борщ", 227)], "Борщ 227 ₽", None, "auto",
    )
    assert res.status == "ok"


# ---------- html_text ----------

def test_page_to_menu_text_strips_lavka_typography():
    """Мягкие переносы и <notr> Лавки не должны попадать в название позиции.

    Лавка расставляет U+00AD внутри каждого слова («тво\xadрож\xadный») и
    оставляет служебный маркер «не переводить». Без чистки LLM тащит это
    прямо в item, и одна позиция перестаёт матчиться сама с собой.
    """
    html = (
        "<div>Мусс тво­рож­ный 125 г</div>"
        "<div>Грудка куриная с пюре &lt;notr&gt;Из Лавки&lt;/notr&gt; 340 г</div>"
    )
    text = page_to_menu_text(html)
    assert "Мусс творожный 125 г" in text
    assert "Грудка куриная с пюре Из Лавки 340 г" in text
    assert "notr" not in text
    assert "­" not in text


def test_page_to_menu_text_drops_chrome():
    html = """
    <html><head><script>var x=1;</script><style>.a{}</style></head>
    <body><nav>Главная Меню Контакты</nav>
    <h2>Пиццы</h2><div>Пепперони</div><div>359 ₽</div>
    <footer>© 2026</footer></body></html>
    """
    text = page_to_menu_text(html)
    assert "Пепперони" in text and "359 ₽" in text
    assert "var x" not in text and "Контакты" not in text and "© 2026" not in text


def test_read_uploaded_mhtml(tmp_path):
    mhtml = (
        "From: <Saved by Blink>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; boundary="----=_Part_0"\r\n'
        "\r\n"
        "------=_Part_0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "\r\n"
        "<html><body><h2>=D0=9F=D0=B8=D1=86=D1=86=D1=8B</h2><p>359</p></body></html>\r\n"
        "------=_Part_0--\r\n"
    )
    path = tmp_path / "menu.mhtml"
    path.write_bytes(mhtml.encode("utf-8"))
    text = read_uploaded_document(path)
    assert "Пиццы" in text and "359" in text


# ---------- format ----------

def test_format_summary_escapes_and_reports_failures():
    from datetime import datetime
    results = [
        CheckSiteResult(competitor_name="Додо <Пицца>", competitor_url="dodopizza.ru",
                        status="ok", items_count=150,
                        diffs=[Diff(change_type="price_up", item="Пепперони", old_price=359,
                                    new_price=399, delta_rub=40, delta_percent=11.1)]),
        CheckSiteResult(competitor_name="Вкусно и точка", competitor_url="vkusnoitochka.ru",
                        status="fetch_failed", error="таймаут"),
        CheckSiteResult(competitor_name="Cofix", competitor_url="cofix.ru",
                        status="ok", items_count=80, first_snapshot=True),
    ]
    text = format_check_summary(results, datetime(2026, 7, 20))
    assert "20.07.2026" in text
    assert "&lt;Пицца&gt;" in text                     # HTML экранируется
    assert "359 → 399 ₽ (+40 ₽, +11.1%)" in text       # числа собраны Python-ом
    assert "Не смог проверить:" in text
    assert "таймаут" in text
    assert "первый срез" in text


def test_manual_competitor_is_not_a_failure():
    """Ручной режим — ожидание файла, а не поломка: отдельный блок и возраст данных.

    В общей куче «Не смог проверить» Бургер Кинг выглядел вечной ошибкой,
    и его переставали замечать — вместе с тем, что данные протухли.
    """
    from datetime import datetime
    fresh = CheckSiteResult(competitor_name="Бургер Кинг", competitor_url="burgerkingrus.ru",
                            status="skipped", error="ручной режим", stale_days=3)
    text = format_check_summary([fresh], datetime(2026, 7, 20))
    assert "Обновляются вручную" in text
    assert "Не смог проверить" not in text
    assert "3 дн. назад" in text
    assert "пора обновить" not in text

    stale = fresh.model_copy(update={"stale_days": 40})
    assert "пора обновить" in format_check_summary([stale], datetime(2026, 7, 20))

    never = fresh.model_copy(update={"stale_days": None})
    assert "данных ещё нет" in format_check_summary([never], datetime(2026, 7, 20))


# ---------- кому уходит сводка ----------

class _FakeBot:
    """Ловит вызовы send_message, чтобы проверить список получателей."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def _run_send(only_user_id):
    import asyncio
    from src.competitors.service import _send_summary
    bot = _FakeBot()
    asyncio.run(_send_summary(bot, "Сводка по конкурентам", only_user_id))
    return [uid for uid, _ in bot.sent]


def test_manual_check_notifies_only_requester():
    """Ручной прогон — ответ только тому, кто попросил.

    Раньше сводка уходила всем разрешённым: разработчик проверял бота, а сообщение
    прилетало и шефу.
    """
    allowed = settings.telegram_allowed_user_ids
    assert len(allowed) >= 2, "тест имеет смысл только при нескольких пользователях"
    me = allowed[0]
    assert _run_send(me) == [me]


def test_cron_check_notifies_everyone():
    """Еженедельный прогон — это рассылка, её ждут все."""
    assert _run_send(None) == list(settings.telegram_allowed_user_ids)
