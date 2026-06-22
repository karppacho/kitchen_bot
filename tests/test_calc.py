"""Регрессионные тесты калькулятора и парсинга — на синтетических данных, без сети.

Запуск (любой вариант):
    python -m tests.test_calc        # без pytest, печатает OK/FAIL
    pytest tests/test_calc.py        # если pytest установлен

Защищает trust-ядро: формулы UC/маржи/КБЖУ, парсинг чисел из Sheets, симуляции.
"""
from decimal import Decimal

from src.data.models import Dish, Ingredient, Packaging, TTKRow
from src.data.sheets import KitchenData, _to_decimal
from src.calc.costs import (
    calculate_dish_uc,
    calculate_ingredient_cost_in_dish,
    simulate_price_change,
    simulate_replacement,
    kbju_coverage_status,
)


def _q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"))


def make_data() -> KitchenData:
    """Маленькая детерминированная база: ролл из тортильи + салата + упаковки."""
    d = KitchenData()
    d.ingredients = {
        1: Ingredient(
            id=1, category="Основа", name="Тортилья", unit="шт",
            price_per_unit=Decimal("10"), weight_per_unit_g=Decimal("70"),
            proteins_100g=Decimal("10"), fats_100g=Decimal("5"),
            carbs_100g=Decimal("50"), kcal_100g=Decimal("300"),
        ),
        3: Ingredient(
            id=3, category="Овощи", name="Салат", unit="кг",
            price_per_unit=Decimal("500"),
            proteins_100g=Decimal("2"), fats_100g=Decimal("0"),
            carbs_100g=Decimal("3"), kcal_100g=Decimal("20"),
        ),
        4: Ingredient(
            id=4, category="Овощи", name="Капуста", unit="кг",
            price_per_unit=Decimal("50"),
        ),
    }
    d.packagings = {1: Packaging(id=1, name="Коробка", price_per_unit=Decimal("12"))}
    d.dishes = {
        "T001": Dish(id="T001", name="Тест-ролл", category="Ролл", price_menu=Decimal("200"))
    }
    d.ttk_by_dish = {
        "T001": [
            TTKRow(dish_id="T001", ingredient_id=1, weight_neto_g=Decimal("60"), row_type="Основной"),
            TTKRow(dish_id="T001", ingredient_id=3, weight_neto_g=Decimal("100"), row_type="Основной"),
            TTKRow(dish_id="T001", packaging_id=1, weight_neto_g=Decimal("1"), row_type="Упаковка"),
        ]
    }
    return d


def make_b001_data() -> KitchenData:
    """Замороженный слепок B001 (Гриль ролл с говядиной) на июнь 2026.

    Точные входные данные из живой таблицы — чтобы детерминированно, без сети,
    воспроизводить эталон: UC 149.08 ₽, маржа 109.92 ₽ (42.4%), выход 190.5 г.
    Защищает ЛОГИКУ калькулятора (потери, штучные/весовые, упаковка, доли).
    """
    d = KitchenData()
    d.ingredients = {
        113: Ingredient(
            id=113, category="Соус", name="Соус табаско", unit="шт",
            price_per_unit=Decimal("10.42"), weight_per_unit_g=Decimal("70"),
            proteins_100g=Decimal("1.4"), fats_100g=Decimal("1.4"),
            carbs_100g=Decimal("7.2"), kcal_100g=Decimal("47"),
        ),
        101: Ingredient(
            id=101, category="Мясо", name="Колбаски говяжьи", unit="шт",
            price_per_unit=Decimal("120.00"), weight_per_unit_g=Decimal("95"),
            losses_unpacking=Decimal("0.0022"),
            proteins_100g=Decimal("15"), fats_100g=Decimal("23"), kcal_100g=Decimal("270"),
        ),
        39: Ingredient(
            id=39, category="Овощи", name="Салат айсберг", unit="кг",
            price_per_unit=Decimal("495.00"), losses_cutting=Decimal("0.2791"),
        ),
        8: Ingredient(
            id=8, category="Сыр", name="Сыр плавленный", unit="кг",
            price_per_unit=Decimal("1253.33"),
        ),
        25: Ingredient(
            id=25, category="Соус", name="Соус гриль", unit="кг",
            price_per_unit=Decimal("740.00"),
        ),
        19: Ingredient(
            id=19, category="Овощи", name="Томаты", unit="кг",
            price_per_unit=Decimal("580.00"), losses_cutting=Decimal("0.1116"),
        ),
    }
    d.packagings = {
        9: Packaging(id=9, name="Упаковка/коробка бумажная", price_per_unit=Decimal("12.20")),
    }
    d.dishes = {
        "B001": Dish(
            id="B001", name="Гриль ролл с говядиной и соусом гриль-чиз",
            category="Ролл", price_menu=Decimal("259"),
        )
    }
    d.ttk_by_dish = {
        "B001": [
            TTKRow(dish_id="B001", ingredient_id=113, weight_neto_g=Decimal("60"), row_type="Основной"),
            TTKRow(dish_id="B001", ingredient_id=101, weight_neto_g=Decimal("47.5"), row_type="Основной"),
            TTKRow(dish_id="B001", ingredient_id=39, weight_neto_g=Decimal("14"), row_type="Основной"),
            TTKRow(dish_id="B001", ingredient_id=8, weight_neto_g=Decimal("19"), row_type="Основной"),
            TTKRow(dish_id="B001", ingredient_id=25, weight_neto_g=Decimal("20"), row_type="Основной"),
            TTKRow(dish_id="B001", ingredient_id=19, weight_neto_g=Decimal("30"), row_type="Основной"),
            TTKRow(dish_id="B001", packaging_id=9, weight_neto_g=Decimal("1"), row_type="Упаковка"),
        ]
    }
    return d


# ---------- Парсинг чисел из Sheets ----------

def test_to_decimal_ruble_prefix():
    assert _to_decimal("р.443,00") == Decimal("443.00")


def test_to_decimal_thousands_nbsp():
    assert _to_decimal("р.1 030,00") == Decimal("1030.00")
    assert _to_decimal("р.1 030,00") == Decimal("1030.00")


def test_to_decimal_percent():
    assert _to_decimal("0,00%") == Decimal("0")
    assert _to_decimal("8%") == Decimal("0.08")


def test_to_decimal_empty():
    assert _to_decimal("") is None
    assert _to_decimal(None) is None


# ---------- Стоимость ингредиента ----------

def test_cost_weight():
    # 100 г салата по 500 ₽/кг → 50 ₽
    ing = make_data().ingredients[3]
    assert _q2(calculate_ingredient_cost_in_dish(ing, Decimal("100"))) == Decimal("50.00")


def test_cost_piece():
    # 60 г тортильи (70 г/шт) по 10 ₽/шт → 60/70*10 = 8.57
    ing = make_data().ingredients[1]
    assert _q2(calculate_ingredient_cost_in_dish(ing, Decimal("60"))) == Decimal("8.57")


def test_cost_with_losses():
    # потери перетарка 10% → брутто 100/0.9 = 111.11 г → *500/1000 = 55.56
    ing = Ingredient(
        id=9, category="x", name="С потерями", unit="кг",
        price_per_unit=Decimal("500"), losses_unpacking=Decimal("0.1"),
    )
    assert _q2(calculate_ingredient_cost_in_dish(ing, Decimal("100"))) == Decimal("55.56")


# ---------- UC блюда ----------

def test_dish_uc_totals():
    r = calculate_dish_uc(make_data(), "T001")
    assert r.uc_rub == Decimal("70.57")          # 8.57 + 50.00 + 12.00
    assert r.output_grams == Decimal("160")       # упаковка не в выход
    assert r.margin_rub == Decimal("129.43")
    assert r.margin_percent == Decimal("64.7")


def test_dish_nutrition():
    r = calculate_dish_uc(make_data(), "T001")
    # тортилья 60г (factor .6): 180 ккал; салат 100г: 20 ккал → 200
    assert r.kcal == Decimal("200")
    assert r.proteins_g == Decimal("8.0")         # 10*.6 + 2*1
    assert r.carbs_g == Decimal("33.0")           # 50*.6 + 3*1
    # У обоих основных есть КБЖУ → покрытие полное
    assert r.kbju_coverage == Decimal("1")
    assert kbju_coverage_status(r.kbju_coverage) == "complete"


# ---------- Эталон B001 (регрессия калькулятора) ----------

def test_b001_benchmark():
    """Замороженный B001: точные числа эталона из ТЗ §1.3."""
    r = calculate_dish_uc(make_b001_data(), "B001")
    assert r.uc_rub == Decimal("149.08")
    assert r.uc_percent == Decimal("57.6")
    assert r.margin_rub == Decimal("109.92")
    assert r.margin_percent == Decimal("42.4")
    assert r.output_grams == Decimal("190.5")
    # Самая дорогая позиция — колбаски говяжьи, 40.3% от UC
    top = max(r.ingredients, key=lambda i: i.cost_rub)
    assert top.name == "Колбаски говяжьи"
    assert top.share_percent == Decimal("40.3")


def test_b001_benchmark_components():
    """Постатейная себестоимость B001 — ловит регрессию в потерях/штучных/весовых."""
    r = calculate_dish_uc(make_b001_data(), "B001")
    costs = {i.name: i.cost_rub for i in r.ingredients}
    assert costs["Соус табаско"] == Decimal("8.93")        # штучный, без потерь
    assert costs["Колбаски говяжьи"] == Decimal("60.13")   # штучный + перетарка 0.22%
    assert costs["Салат айсберг"] == Decimal("9.61")       # весовой + нарезка 27.91%
    assert costs["Сыр плавленный"] == Decimal("23.81")     # весовой, без потерь
    assert costs["Соус гриль"] == Decimal("14.80")         # весовой
    assert costs["Томаты"] == Decimal("19.59")             # весовой + нарезка 11.16%
    assert costs["Упаковка/коробка бумажная"] == Decimal("12.20")  # упаковка, без потерь


def test_b001_live_benchmark():
    """Живой регресс на реальной таблице — защищает ДАННЫЕ от дрейфа.

    Без доступа к Sheets (нет creds/сети) — тихо пропускаем, чтобы тест-набор
    оставался офлайн-дружелюбным. Падение здесь при наличии доступа = данные B001
    в таблице изменились, нужно разобраться.
    """
    try:
        from src.data.sheets import get_data
        data = get_data()
    except Exception:
        return  # нет доступа — пропускаем
    r = calculate_dish_uc(data, "B001")
    if r is None:
        return  # блюда нет в текущей таблице
    assert r.uc_rub == Decimal("149.08"), f"B001 UC поплыл: {r.uc_rub}"
    assert r.margin_rub == Decimal("109.92"), f"B001 маржа поплыла: {r.margin_rub}"
    assert r.output_grams == Decimal("190.5"), f"B001 выход поплыл: {r.output_grams}"


def test_kbju_coverage_poor():
    # Уберём КБЖУ у салата (100 г из 160 г) → покрыто только 60/160 = 37.5% → poor
    d = make_data()
    d.ingredients[3].kcal_100g = None
    r = calculate_dish_uc(d, "T001")
    assert r.kbju_coverage < Decimal("0.5")
    assert kbju_coverage_status(r.kbju_coverage) == "poor"
    assert any("цифрам нельзя доверять" in w for w in r.warnings)


# ---------- Симуляция цены ----------

def test_simulate_multiplier():
    r = simulate_price_change(make_data(), 3, multiplier=Decimal("2"))
    dish = r["dishes"][0]
    assert dish["new_uc"] == 120.57              # 8.57 + 100.00 + 12.00


def test_simulate_delta_negative():
    r = simulate_price_change(make_data(), 3, delta_rub=Decimal("-100"))
    # салат 400 ₽/кг → 100г = 40 ₽ → uc = 8.57+40+12 = 60.57
    assert r["dishes"][0]["new_uc"] == 60.57


def test_simulate_dish_filter():
    r = simulate_price_change(make_data(), 3, multiplier=Decimal("2"), dish_ids=["T001"])
    assert r["affected_count"] == 1
    r2 = simulate_price_change(make_data(), 3, multiplier=Decimal("2"), dish_ids=["NOPE"])
    assert r2["affected_count"] == 0


def test_simulate_cache_restored():
    d = make_data()
    simulate_price_change(d, 3, multiplier=Decimal("5"))
    assert d.ingredients[3].price_per_unit == Decimal("500")  # цена в кеше не испорчена


# ---------- Замена ингредиента ----------

def test_replacement_real():
    r = simulate_replacement(make_data(), 3, 4)   # салат(500) → капуста(50)
    assert r["affected_count"] == 1
    dish = r["dishes"][0]
    assert dish["old_uc"] == 70.57
    assert dish["new_uc"] == 25.57                # 8.57 + 5.00 + 12.00


# ---------- Запуск без pytest ----------

def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e!r}")
        except Exception as e:
            failed += 1
            print(f"  ERR  {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} прошло")
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_all() else 0)
