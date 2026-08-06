"""Тесты расчётки для коммерческого отдела. Офлайн, без сети.

Главное, что защищаем: цены, вписанные коммерсами вручную, не должны пропадать
при пересчёте — ради них вся конструкция «прочитать лист → собрать → записать».
"""
from decimal import Decimal

import pytest

from src.data.models import Dish
from src.pricing.exporter import _to_decimal, read_existing
from src.pricing.format import format_pricing_result
from src.pricing.table import (
    COL_MARGIN_PCT,
    COL_NAME,
    COL_PRICE,
    FIRST_DATA_ROW,
    HEADER,
    build_table,
    margin_drops,
    resolve_price,
)
from tests.test_calc import make_data


def _data_with_dishes(*specs):
    """База с несколькими блюдами одного состава: (id, имя, категория, цена, статус)."""
    d = make_data()
    base_rows = d.ttk_by_dish["T001"]
    d.dishes = {}
    d.ttk_by_dish = {}
    for dish_id, name, category, price, status in specs:
        d.dishes[dish_id] = Dish(
            id=dish_id, name=name, category=category,
            price_menu=price, status=status,
        )
        d.ttk_by_dish[dish_id] = [
            r.model_copy(update={"dish_id": dish_id}) for r in base_rows
        ]
    return d


# ---------- цена: три источника ----------

def test_price_from_dishes_sheet_wins():
    """Цена в «Блюда» перекрывает то, что вписал коммерс: шеф — источник истины."""
    dish = Dish(id="B1", name="Тест", category="Пицца", price_menu=Decimal("300"))
    price, from_sheet = resolve_price(dish, Decimal("250"))
    assert price == Decimal("300")
    assert from_sheet is False


def test_price_kept_from_sheet_when_dish_has_none():
    """Нет цены в «Блюда» → сохраняем вписанную в расчётку."""
    dish = Dish(id="B1", name="Тест", category="Пицца", price_menu=None)
    price, from_sheet = resolve_price(dish, Decimal("250"))
    assert price == Decimal("250")
    assert from_sheet is True


def test_price_empty_when_nowhere():
    dish = Dish(id="B1", name="Тест", category="Пицца", price_menu=None)
    assert resolve_price(dish, None) == (None, False)


def test_zero_price_in_dishes_does_not_win():
    """Ноль в «Блюда» — это «не заполнено», а не бесплатное блюдо."""
    dish = Dish(id="B1", name="Тест", category="Пицца", price_menu=Decimal("0"))
    price, from_sheet = resolve_price(dish, Decimal("250"))
    assert price == Decimal("250")
    assert from_sheet is True


def test_manual_price_survives_rebuild():
    """Сквозной сценарий: коммерс вписал цену, бот пересчитал — цена на месте."""
    d = _data_with_dishes(("B1", "Новинка", "Пицца", None, "разработка"))
    table = build_table(d, "разработка", prices_in_sheet={"новинка": Decimal("450")})
    row = table.rows[0]
    assert row.price == Decimal("450")
    assert row.price_from_sheet is True
    # Маржа посчитана ОТ ЭТОЙ цены, иначе таблица бесполезна
    assert row.margin_percent is not None
    assert row.uc_rub < Decimal("450")


# ---------- сортировка и состав колонок ----------

def test_sorted_by_category_then_name():
    d = _data_with_dishes(
        ("B1", "Яблочный", "Десерт", Decimal("100"), "разработка"),
        ("B2", "Авокадо", "Салат", Decimal("100"), "разработка"),
        ("B3", "Ананас", "Десерт", Decimal("100"), "разработка"),
    )
    names = [r.name for r in build_table(d, "разработка").rows]
    assert names == ["Ананас", "Яблочный", "Авокадо"]   # Десерт → Салат


def test_category_not_in_columns():
    """Категория — только ключ сортировки, в таблицу не выводится."""
    assert "Категория" not in HEADER
    d = _data_with_dishes(("B1", "Тест", "Пицца", Decimal("300"), "разработка"))
    cells = build_table(d, "разработка").rows[0].to_cells()
    assert "Пицца" not in cells
    assert len(cells) == len(HEADER)


def test_only_requested_status():
    d = _data_with_dishes(
        ("B1", "Новинка", "Пицца", Decimal("300"), "разработка"),
        ("B2", "Старое", "Пицца", Decimal("300"), "активное"),
    )
    assert [r.name for r in build_table(d, "разработка").rows] == ["Новинка"]
    assert [r.name for r in build_table(d, "активное").rows] == ["Старое"]


def test_dish_without_composition_skipped():
    d = _data_with_dishes(("B1", "Пустое", "Пицца", Decimal("300"), "разработка"))
    d.ttk_by_dish["B1"] = []
    table = build_table(d, "разработка")
    assert table.rows == []
    assert any("Пустое" in s for s in table.skipped)


# ---------- значения в ячейках ----------

def test_percents_written_as_fractions():
    """Проценты — долями: Sheets покажет 56,9%, но сортировка и формулы живые."""
    d = _data_with_dishes(("B1", "Тест", "Пицца", Decimal("200"), "разработка"))
    row = build_table(d, "разработка").rows[0]
    cells = row.to_cells()
    assert cells[3] == pytest.approx(float(row.uc_percent) / 100)
    assert cells[4] == pytest.approx(float(row.margin_percent) / 100)
    assert 0 < cells[3] < 1


def test_kbju_is_per_portion():
    """КБЖУ на порцию — рядом стоит «Вес продукта», так однозначно."""
    d = _data_with_dishes(("B1", "Тест", "Пицца", Decimal("200"), "разработка"))
    row = build_table(d, "разработка").rows[0]
    assert row.output_g == Decimal("160")
    assert row.kcal == Decimal("200")        # как в make_data, на всю порцию


def test_empty_price_cell_is_blank_not_zero():
    d = _data_with_dishes(("B1", "Без цены", "Пицца", None, "разработка"))
    cells = build_table(d, "разработка").rows[0].to_cells()
    assert cells[COL_PRICE] == ""
    assert cells[COL_MARGIN_PCT] == ""


# ---------- подсветка ----------

def test_loss_flag_when_uc_above_price():
    d = _data_with_dishes(("B1", "Убыток", "Пицца", Decimal("10"), "разработка"))
    row = build_table(d, "разработка").rows[0]
    assert row.is_loss is True
    assert row.is_low_margin(30) is False     # убыток красный, а не жёлтый


def test_low_margin_flag():
    d = _data_with_dishes(("B1", "Тест", "Пицца", Decimal("100"), "разработка"))
    row = build_table(d, "разработка").rows[0]
    # UC 70.57 при цене 100 → маржа ~29.4%
    assert row.is_low_margin(30) is True
    assert row.is_low_margin(20) is False
    assert row.is_loss is False


def test_no_flags_for_healthy_dish():
    d = _data_with_dishes(("B1", "Норм", "Пицца", Decimal("500"), "разработка"))
    row = build_table(d, "разработка").rows[0]
    assert row.is_loss is False
    assert row.is_low_margin(30) is False


def test_no_price_no_flags():
    """Без цены маржи нет — подсвечивать нечего."""
    d = _data_with_dishes(("B1", "Без цены", "Пицца", None, "разработка"))
    row = build_table(d, "разработка").rows[0]
    assert row.is_loss is False
    assert row.is_low_margin(30) is False


# ---------- тёзки ----------

def test_ambiguous_names_do_not_get_price():
    """Подставить цену не тому блюду хуже, чем не подставить вовсе."""
    d = _data_with_dishes(
        ("B1", "Маргарита", "Пицца", None, "разработка"),
        ("B2", "Маргарита", "Закуска", None, "разработка"),
    )
    table = build_table(d, "разработка", prices_in_sheet={"маргарита": Decimal("400")})
    assert all(r.price is None for r in table.rows)
    assert any("тёзки" in w for w in table.warnings)


# ---------- падение маржи ----------

def test_margin_drop_detected():
    d = _data_with_dishes(("B1", "Тест", "Пицца", Decimal("200"), "разработка"))
    table = build_table(d, "разработка")
    now = float(table.rows[0].margin_percent)
    drops = margin_drops(table, {"тест": Decimal(str(now + 10))}, drop_pp=5)
    assert len(drops) == 1
    assert drops[0][0] == "Тест"


def test_small_margin_change_ignored():
    d = _data_with_dishes(("B1", "Тест", "Пицца", Decimal("200"), "разработка"))
    table = build_table(d, "разработка")
    now = float(table.rows[0].margin_percent)
    assert margin_drops(table, {"тест": Decimal(str(now + 2))}, drop_pp=5) == []


def test_appearing_price_is_not_a_drop():
    """«Было пусто → стало 50%» — это появление цены, а не падение маржи."""
    d = _data_with_dishes(("B1", "Тест", "Пицца", Decimal("200"), "разработка"))
    table = build_table(d, "разработка")
    assert margin_drops(table, {}, drop_pp=5) == []


# ---------- чтение старого листа ----------

class _FakeWs:
    def __init__(self, values):
        self._values = values

    def get_all_values(self):
        return self._values


def test_read_existing_parses_prices_and_margins():
    ws = _FakeWs([
        ["", "⬇️ тут ставим цену ⬇️", "", "", "", "", "", "", "", ""],
        HEADER,
        ["Маргарита", "р.299,00", "170.01", "0.5686", "0.4314", "208.5", "", "", "", ""],
        ["Без цены", "", "50", "", "", "100", "", "", "", ""],
    ])
    prices, margins = read_existing(ws)
    assert prices == {"маргарита": Decimal("299.00")}
    assert margins["маргарита"] == pytest.approx(Decimal("43.14"))
    assert "без цены" not in prices


def test_read_existing_survives_broken_sheet():
    """Кривой лист не должен ронять пересчёт — просто не перенесём цены."""
    class _Boom:
        def get_all_values(self):
            raise RuntimeError("нет доступа")

    assert read_existing(_Boom()) == ({}, {})


def test_to_decimal_formats():
    assert _to_decimal("р.1 030,00") == Decimal("1030.00")
    assert _to_decimal("299") == Decimal("299")
    assert _to_decimal("") is None
    assert _to_decimal("мусор") is None


# ---------- сводка ----------

def test_summary_mentions_problems():
    text = format_pricing_result([{
        "sheet": "Расчётка меню", "status": "активное", "count": 3,
        "loss": ["Митболы"], "low_margin": ["Салат"],
        "drops": [("Пицца", Decimal("60"), Decimal("50"))],
        "no_price": [], "kept_prices": [], "skipped": [], "warnings": [],
        "url": "https://example.com",
    }])
    assert "Митболы" in text
    assert "Салат" in text
    assert "60.0% → 50.0%" in text
    assert "ошибка в данных" in text          # объясняем красный, а не пугаем


def test_summary_clean_when_all_good():
    text = format_pricing_result([{
        "sheet": "Расчётка новинки", "status": "разработка", "count": 2,
        "loss": [], "low_margin": [], "drops": [], "no_price": [],
        "kept_prices": [], "skipped": [], "warnings": [], "url": "https://example.com",
    }])
    assert "Всё в порядке" in text


def test_summary_reports_sheet_error():
    text = format_pricing_result([
        {"sheet": "Расчётка меню", "status": "активное", "error": "нет доступа"},
    ])
    assert "нет доступа" in text
