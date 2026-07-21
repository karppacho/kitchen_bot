"""Офлайн-тесты мониторинга конкурентов: comparator, парсинг LLM-ответов,
storage (sqlite на tmp_path), html_text. Сеть и LLM не нужны.

Запуск: pytest tests/test_competitors.py
"""
from decimal import Decimal

from src.competitors import comparator, storage
from src.competitors.comparator import diff_snapshots, norm_name
from src.competitors.extractor import parse_items_json, parse_price, _split_chunks
from src.competitors.format import format_check_summary
from src.competitors.html_text import page_to_menu_text, read_uploaded_document
from src.competitors.models import CheckSiteResult, Diff, ExtractedItem
from src.config import settings

PCT = Decimal("10")
RUB = Decimal("30")


def _item(name, price=None, weight=None, category=None):
    return ExtractedItem(item=name, price_rub=price, weight=weight, category=category)


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


# ---------- html_text ----------

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
        CheckSiteResult(competitor_name="Бургер Кинг", competitor_url="burgerkingrus.ru",
                        status="skipped", error="ручной режим — пришли сохранённый HTML"),
        CheckSiteResult(competitor_name="Cofix", competitor_url="cofix.ru",
                        status="ok", items_count=80, first_snapshot=True),
    ]
    text = format_check_summary(results, datetime(2026, 7, 20))
    assert "20.07.2026" in text
    assert "&lt;Пицца&gt;" in text                     # HTML экранируется
    assert "359 → 399 ₽ (+40 ₽, +11.1%)" in text       # числа собраны Python-ом
    assert "Не смог проверить:" in text
    assert "ручной режим" in text
    assert "первый срез" in text
