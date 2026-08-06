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
    """Замороженный слепок «Гриль ролла с говядиной» — каким он был в июне 2026.

    ВАЖНО: это синтетическая фикстура, а не текущий B001 из таблицы. В июле 2026
    шеф перестроил таблицу и id B001 достался другому блюду; слепок оставлен как
    есть намеренно — он проверяет ЛОГИКУ калькулятора (потери, штучные/весовые,
    упаковка, доли) и не должен зависеть от того, что сейчас в Google Sheets.
    Эталон живой таблицы — отдельно, в test_live_benchmark.

    Эталонные числа: UC 149.08 ₽, маржа 109.92 ₽ (42.4%), выход 190.5 г.
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


# ---------- Живая таблица ----------
# Шеф правит таблицу постоянно, поэтому «заморозить одно блюдо» — недостаточная
# защита: в июле 2026 таблицу перестроили целиком, эталонный B001 стал другим
# блюдом, и тест месяц падал вхолостую. Поэтому здесь два уровня:
# структурная целостность (переживает любые правки) + один эталон (перепривязывается).

def _live_data():
    """Живая база или None, если доступа к Sheets нет (офлайн-прогон)."""
    try:
        from src.data.sheets import get_data
        return get_data()
    except Exception:
        return None


def test_live_referential_integrity():
    """Каждая ссылка из ТТК разрешается в ING/Упаковку.

    Это и ловит настоящий дрейф id: если ссылки побились, бот считает
    себестоимость по чужим продуктам и молчит об этом.
    """
    data = _live_data()
    if data is None:
        return
    broken_ing, broken_pack = [], []
    for dish_id, rows in data.ttk_by_dish.items():
        for row in rows:
            if row.row_type == "Упаковка":
                if row.packaging_id is not None and row.packaging_id not in data.packagings:
                    broken_pack.append((dish_id, row.packaging_id))
            elif row.ingredient_id is not None and row.ingredient_id not in data.ingredients:
                broken_ing.append((dish_id, row.ingredient_id))
    assert not broken_ing, f"ТТК ссылается на несуществующие ингредиенты: {broken_ing[:10]}"
    assert not broken_pack, f"ТТК ссылается на несуществующую упаковку: {broken_pack[:10]}"


# Дубли среди АКТИВНЫХ имён, известные на 04.08.2026. Чинятся на стороне шефа
# (переименовать или заархивировать лишнюю позицию). Тест-храповик: список не
# должен РАСТИ. Когда шеф разведёт пару — просто убери имя отсюда.
# Пары «архив + активная» сюда не входят: бот их больше не путает.
KNOWN_DUPLICATE_ING_NAMES = {
    "рыбные палочки",       # id 38 и 110, обе активные
}


def test_live_no_new_duplicate_ingredient_names():
    """Имя ингредиента — ключ для человека, по нему бот ищет (правило №4).

    Считаем только активные: пара «архивная + активная» неоднозначности не даёт,
    в новое блюдо архив не предлагается. Тест следит, чтобы не появлялись НОВЫЕ
    дубли (вечно красный тест перестают читать, а этот должен что-то значить).
    """
    data = _live_data()
    if data is None:
        return
    dupes = data.duplicate_ingredient_names()
    new = {n: ids for n, ids in dupes.items() if n not in KNOWN_DUPLICATE_ING_NAMES}
    assert not new, f"новые дубли имён среди активных ингредиентов ING: {new}"


def test_duplicate_names_ignore_archived():
    """Архивная позиция не должна считаться дублем активной (офлайн)."""
    d = make_data()
    d.ingredients[9] = Ingredient(
        id=9, category="Овощи", name="Салат", unit="кг",
        price_per_unit=Decimal("400"), status="архив",
    )
    assert d.duplicate_ingredient_names() == {}
    # А две активные с одним именем — настоящий дубль
    d.ingredients[9].status = "активный"
    assert d.duplicate_ingredient_names() == {"салат": [3, 9]}


def test_live_sanity():
    """Таблица не должна внезапно «похудеть» — ловит сломанный парсинг и обрезку."""
    data = _live_data()
    if data is None:
        return
    assert len(data.ingredients) >= 100, f"ингредиентов всего {len(data.ingredients)}"
    assert len(data.dishes) >= 100, f"блюд всего {len(data.dishes)}"
    assert len(data.ttk_by_dish) >= 100, f"блюд с составом всего {len(data.ttk_by_dish)}"
    assert data.cooking_methods, "лист способов приготовления не загрузился"


def test_live_benchmark():
    """Эталон на живой таблице: B119 «Английский завтрак с драником» (июль 2026).

    Выбран потому, что UC определён полностью — 6 позиций + упаковка, у всех
    заполнены цены (единственное замечание блюда — про КБЖУ, на UC не влияет).

    Падение здесь = либо регрессия в расчёте, либо шеф поменял состав/цены B119.
    Во втором случае эталон надо ПЕРЕПРИВЯЗАТЬ, а не подгонять код: сверься с
    `python -m scripts.test_calc B119` и обнови числа тут и в CLAUDE.md.
    """
    data = _live_data()
    if data is None:
        return
    r = calculate_dish_uc(data, "B119")
    if r is None:
        return  # блюда нет в текущей таблице — разберётся test_live_sanity
    assert r.uc_rub == Decimal("151.84"), f"B119 UC поплыл: {r.uc_rub}"
    assert r.uc_percent == Decimal("31.7"), f"B119 UC% поплыл: {r.uc_percent}"
    assert r.margin_rub == Decimal("327.16"), f"B119 маржа поплыла: {r.margin_rub}"
    assert r.output_grams == Decimal("281"), f"B119 выход поплыл: {r.output_grams}"


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


# ---------- Блюдо без цены меню ----------

def test_dish_without_price_counts_uc_but_not_margin():
    """Цены меню нет (соус-топпинг) → себестоимость считаем, маржу нет.

    Ноль вместо None был бы враньём: шеф прочитал бы «маржа 0%» как настоящий ноль.
    """
    d = make_data()
    d.dishes["T001"].price_menu = None
    r = calculate_dish_uc(d, "T001")
    assert r.uc_rub == Decimal("70.57")           # себестоимость от цены не зависит
    assert r.output_grams == Decimal("160")
    assert r.price_menu is None
    assert r.uc_percent is None
    assert r.margin_rub is None
    assert r.margin_percent is None
    assert any("Цена меню не заполнена" in w for w in r.warnings)
    # Доли ингредиентов в UC считаются как обычно — они от цены меню не зависят
    assert sum(i.share_percent for i in r.ingredients) == Decimal("100.0")


def test_dish_without_price_and_without_composition():
    """Ни цены, ни состава — не должно падать на вычитании из None."""
    d = make_data()
    d.dishes["T001"].price_menu = None
    d.ttk_by_dish = {}
    r = calculate_dish_uc(d, "T001")
    assert r.uc_rub == Decimal("0")
    assert r.margin_rub is None
    assert r.margin_percent is None


def test_simulate_includes_dish_without_price():
    """В симуляции блюдо без цены остаётся: дельта UC осмысленна, маржа — None."""
    d = make_data()
    d.dishes["T001"].price_menu = None
    r = simulate_price_change(d, 3, new_price=Decimal("1000"))
    assert r["affected_count"] == 1
    row = r["dishes"][0]
    assert row["price_menu"] is None
    assert row["delta_uc"] > 0                    # подорожание видно
    assert row["old_margin_percent"] is None
    assert row["delta_margin_percent"] is None


# ---------- Неполные данные: ингредиент остаётся в составе с cost=0 ----------

def test_uc_above_price_warns():
    """UC выше цены меню — почти всегда кривые данные, а не реальный убыток.

    04.08.2026 так создалось блюдо с UC 1398 ₽ при цене 280 ₽ («шт» вместо «кг»
    у колбасок), и бот записал его без единого замечания.
    """
    d = make_data()
    d.ingredients[3].price_per_unit = Decimal("100000")   # салат по 100 000 ₽/кг
    r = calculate_dish_uc(d, "T001")
    assert r.uc_rub > r.price_menu
    assert r.margin_rub < 0
    assert any("выше цены меню" in w for w in r.warnings)


def test_normal_dish_has_no_price_warning():
    """Обычное блюдо — никаких лишних замечаний про маржу."""
    r = calculate_dish_uc(make_data(), "T001")
    assert not any("выше цены меню" in w for w in r.warnings)


def test_zero_price_ingredient_warns():
    """Цена ровно 0 — тоже «данных нет», обязано быть предупреждение.

    Раньше проверка `is None` её не ловила: ингредиент молча давал 0 ₽,
    и шеф видел заниженный UC без единого замечания.
    """
    d = make_data()
    d.ingredients[3].price_per_unit = Decimal("0")
    r = calculate_dish_uc(d, "T001")
    salad = next(i for i in r.ingredients if i.name == "Салат")
    assert salad.cost_rub == Decimal("0")
    assert any("Салат" in w and "не заполнена цена" in w for w in r.warnings)



def test_ingredient_without_price_stays_in_output():
    """Нет цены → стоимость 0, но вес входит в выход, КБЖУ и рецептуру ТТК."""
    d = make_data()
    d.ingredients[3].price_per_unit = None        # салат без цены
    r = calculate_dish_uc(d, "T001")
    assert r.output_grams == Decimal("160")       # выход не занижен
    salad = next(i for i in r.ingredients if i.name == "Салат")
    assert salad.cost_rub == Decimal("0")
    assert r.uc_rub == Decimal("20.57")           # 8.57 + 0 + 12.00
    assert r.kcal == Decimal("200")               # КБЖУ салата всё ещё учтено
    assert any("не заполнена цена" in w for w in r.warnings)


def test_piece_without_weight_stays_in_output():
    """Штучный без «Вес 1 шт» → стоимость 0, но вес в выходе и составе."""
    d = make_data()
    d.ingredients[1].weight_per_unit_g = None     # тортилья без веса 1 шт
    r = calculate_dish_uc(d, "T001")
    assert r.output_grams == Decimal("160")
    tort = next(i for i in r.ingredients if i.name == "Тортилья")
    assert tort.cost_rub == Decimal("0")
    assert r.uc_rub == Decimal("62.00")           # 0 + 50.00 + 12.00
    assert any("вес 1 шт" in w for w in r.warnings)


def test_simulate_zero_price_error():
    """Цена 0 в таблице → внятная ошибка вместо деления на ноль."""
    d = make_data()
    d.ingredients[3].price_per_unit = Decimal("0")
    r = simulate_price_change(d, 3, multiplier=Decimal("2"))
    assert "error" in r
    assert "нулевая цена" in r["error"]


# ---------- Разбиение длинных ответов под лимит Telegram ----------

def test_split_short_text_untouched():
    from src.bot.telegram_text import split_for_telegram
    assert split_for_telegram("привет") == ["привет"]


def test_split_long_text_chunks():
    from src.bot.telegram_text import split_for_telegram, TELEGRAM_MAX_LEN
    text = "\n".join(f"строка номер {i}" for i in range(1000))
    chunks = split_for_telegram(text)
    assert len(chunks) > 1
    assert all(len(c) <= TELEGRAM_MAX_LEN for c in chunks)
    # Контент не потерялся (переносы на стыках кусков съедаются)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_keeps_pre_balanced():
    from src.bot.telegram_text import split_for_telegram, TELEGRAM_MAX_LEN
    rows = "\n".join(f"позиция {i}  100 г  10.00 ₽" for i in range(500))
    text = "Шапка\n<pre>\n" + rows + "\n</pre>\nПодвал"
    chunks = split_for_telegram(text)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= TELEGRAM_MAX_LEN
        assert c.count("<pre>") == c.count("</pre>")  # каждый кусок — валидный HTML


# ---------- Конфиг: TELEGRAM_ALLOWED_USER_IDS в двух форматах ----------

def test_config_user_ids_comma_and_json():
    """Формат из README ("123,456") и JSON ("[123,456]") оба валидны."""
    from src.config import Settings
    base = dict(
        google_sheets_id="x", google_service_account_json_path="x",
        polza_api_key="x", telegram_bot_token="x",
    )
    s = Settings(telegram_allowed_user_ids="123, 456", **base)
    assert s.telegram_allowed_user_ids == [123, 456]
    s = Settings(telegram_allowed_user_ids="[123,456]", **base)
    assert s.telegram_allowed_user_ids == [123, 456]


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
