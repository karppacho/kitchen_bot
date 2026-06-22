"""Калькулятор UC — детерминированная арифметика.

ВАЖНО: LLM сюда не вмешивается. Все цифры считаем здесь, по точным формулам.
"""
from decimal import Decimal, ROUND_HALF_UP

from loguru import logger

from src.data.models import (
    Dish,
    DishIngredientCost,
    DishUCResult,
    Ingredient,
    Packaging,
    TTKRow,
)
from src.data.sheets import KitchenData


def _round_money(value: Decimal) -> Decimal:
    """Округление до 2 знаков после запятой."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round_percent(value: Decimal) -> Decimal:
    """Округление до 1 знака."""
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def kbju_coverage_status(coverage: Decimal) -> str:
    """Насколько КБЖУ блюда заполнено по весу состава: complete / partial / poor.

    poor (<50%) — цифрам нельзя доверять (большая часть ингредиентов без КБЖУ).
    """
    if coverage >= Decimal("0.999"):
        return "complete"
    if coverage >= Decimal("0.5"):
        return "partial"
    return "poor"


def calculate_brutto_g(ingredient: Ingredient, weight_neto_g: Decimal) -> Decimal:
    """Брутто из нетто с учётом потерь (перетарка + нарезка, ДО закладки).

    Если суммарные потери = 100% (остаток 0) — возвращаем нетто (вырожденный случай,
    чтобы не делить на ноль; используется только для отображения рецептуры).
    """
    perevarka = ingredient.losses_unpacking or Decimal("0")
    cutting = ingredient.losses_cutting or Decimal("0")
    remaining = (Decimal("1") - perevarka) * (Decimal("1") - cutting)
    if remaining <= 0:
        return weight_neto_g
    return weight_neto_g / remaining


def calculate_ingredient_cost_in_dish(
    ingredient: Ingredient, weight_neto_g: Decimal
) -> Decimal | None:
    """Стоимость X граммов нетто ингредиента с учётом потерь.

    Для весовых ингредиентов (кг, л):
        Цена за кг (или литр) × количество.
    Для штучных:
        В ТТК указано «X грамм этой штуки», нам нужно понять, сколько это штук.
        Считается через колонку weight_per_unit_g (вес 1 шт):
            штук = X / вес_1_шт
            стоимость = штук × цена_за_шт

    Учитываем потери перетарка + нарезка через брутто/нетто.
    """
    if ingredient.price_per_unit is None:
        return Decimal("0")

    perevarka = ingredient.losses_unpacking or Decimal("0")
    cutting = ingredient.losses_cutting or Decimal("0")
    remaining = (Decimal("1") - perevarka) * (Decimal("1") - cutting)
    if remaining == 0:
        return Decimal("0")
    weight_brutto_g = weight_neto_g / remaining

    if ingredient.unit == "шт":
        # Для штучных нужен вес 1 штуки
        if not ingredient.weight_per_unit_g or ingredient.weight_per_unit_g == 0:
            # Нет данных — не можем посчитать корректно
            return Decimal("0")
        qty_pieces = weight_brutto_g / ingredient.weight_per_unit_g
        cost = qty_pieces * ingredient.price_per_unit
    elif ingredient.unit in ("кг", "л"):
        # Цена за килограмм или литр, в ТТК граммы или миллилитры
        cost = (weight_brutto_g / Decimal("1000")) * ingredient.price_per_unit
    elif ingredient.unit == "мл":
        # Цена за миллилитр? Маловероятно, но на всякий — без деления
        cost = weight_brutto_g * ingredient.price_per_unit
    else:
        cost = Decimal("0")

    return cost


def calculate_packaging_cost(packaging: Packaging, qty: Decimal) -> Decimal:
    """Стоимость упаковки = цена_за_шт × количество. Без потерь."""
    if packaging.price_per_unit is None:
        return Decimal("0")
    return packaging.price_per_unit * qty


def _compute_uc(
    data: KitchenData,
    dish_id: str,
    dish_name: str,
    price_menu: Decimal,
    ttk_rows: list[TTKRow],
    substitutions: dict[int, int] | None = None,
) -> DishUCResult:
    """Ядро расчёта UC/КБЖУ по ЯВНОЙ рецептуре (без поиска блюда в кеше).

    Годится и для сохранённого блюда (через calculate_dish_uc), и для ещё не
    сохранённого (через calculate_uc_for_composition — превью создания блюда).

    substitutions: карта {старый_id_ингредиента: новый_id}. Если задана, для строк
    ТТК с этим ингредиентом используется новый ингредиент при ТОМ ЖЕ нетто-весе —
    нужно для симуляции замены (пересчёт цены и КБЖУ). По умолчанию None — обычный расчёт.
    """
    if not ttk_rows:
        return DishUCResult(
            dish_id=dish_id,
            dish_name=dish_name,
            price_menu=price_menu,
            uc_rub=Decimal("0"),
            uc_percent=Decimal("0"),
            margin_rub=price_menu,
            margin_percent=Decimal("100"),
            output_grams=Decimal("0"),
            ingredients=[],
            warnings=["В ТТК нет ни одной строки для этого блюда"],
        )

    items: list[DishIngredientCost] = []
    warnings: list[str] = []
    total_cost = Decimal("0")
    output_grams = Decimal("0")
    proteins = fats = carbs = kcal = Decimal("0")
    kbju_covered_g = Decimal("0")
    missing_kbju: list[str] = []

    for row in ttk_rows:
        if row.row_type == "Основной":
            if row.ingredient_id is None:
                warnings.append(
                    f"Строка ТТК без id_ингредиента (вес {row.weight_neto_g} г)"
                )
                continue
            # Подмена ингредиента при симуляции замены — вес остаётся прежним
            eff_id = row.ingredient_id
            if substitutions and eff_id in substitutions:
                eff_id = substitutions[eff_id]
            ing = data.ingredients.get(eff_id)
            if ing is None:
                warnings.append(
                    f"Ингредиент id={eff_id} не найден в ING"
                )
                continue
            if ing.price_per_unit is None:
                warnings.append(
                    f"У ингредиента «{ing.name}» нет цены — пропустил в расчёте"
                )
                continue
            # Дополнительно для штучных: нужен вес 1 шт
            if ing.unit == "шт" and (not ing.weight_per_unit_g or ing.weight_per_unit_g == 0):
                warnings.append(
                    f"У штучного ингредиента «{ing.name}» не указан вес 1 шт — пропустил"
                )
                continue

            cost = calculate_ingredient_cost_in_dish(ing, row.weight_neto_g)
            items.append(
                DishIngredientCost(
                    name=ing.name,
                    weight_g=row.weight_neto_g,
                    weight_brutto_g=_round_money(calculate_brutto_g(ing, row.weight_neto_g)),
                    unit=ing.unit,
                    price_per_unit=ing.price_per_unit,
                    weight_per_piece_g=ing.weight_per_unit_g,
                    cost_rub=_round_money(cost),
                    row_type="Основной",
                )
            )
            total_cost += cost
            # Съедобный выход — только основные ингредиенты
            output_grams += row.weight_neto_g

            # КБЖУ на нетто-вес (на 100 г → на фактический вес). Тепловые потери
            # не учитываем — как и в стоимости (см. правила проекта).
            factor = row.weight_neto_g / Decimal("100")
            if ing.kcal_100g is None:
                missing_kbju.append(ing.name)
            else:
                proteins += (ing.proteins_100g or Decimal("0")) * factor
                fats += (ing.fats_100g or Decimal("0")) * factor
                carbs += (ing.carbs_100g or Decimal("0")) * factor
                kcal += ing.kcal_100g * factor
                kbju_covered_g += row.weight_neto_g

        elif row.row_type == "Упаковка":
            if row.packaging_id is None:
                warnings.append(
                    f"Строка ТТК-упаковки без id_упаковки"
                )
                continue
            pkg = data.packagings.get(row.packaging_id)
            if pkg is None:
                warnings.append(
                    f"Упаковка id={row.packaging_id} не найдена"
                )
                continue
            if pkg.price_per_unit is None:
                warnings.append(
                    f"У упаковки «{pkg.name}» нет цены"
                )
                continue

            cost = calculate_packaging_cost(pkg, row.weight_neto_g)
            items.append(
                DishIngredientCost(
                    name=pkg.name,
                    weight_g=row.weight_neto_g,
                    unit="шт",
                    price_per_unit=pkg.price_per_unit,
                    weight_per_piece_g=None,
                    cost_rub=_round_money(cost),
                    row_type="Упаковка",
                )
            )
            total_cost += cost

    uc = _round_money(total_cost)
    uc_percent = (
        _round_percent(uc / price_menu * Decimal("100"))
        if price_menu > 0
        else Decimal("0")
    )
    margin = _round_money(price_menu - uc)
    margin_percent = (
        _round_percent(margin / price_menu * Decimal("100"))
        if price_menu > 0
        else Decimal("0")
    )

    if uc > 0:
        for item in items:
            item.share_percent = _round_percent(
                item.cost_rub / uc * Decimal("100")
            )

    kbju_coverage = (
        kbju_covered_g / output_grams if output_grams > 0 else Decimal("0")
    )
    if missing_kbju:
        base = f"КБЖУ нет у {len(missing_kbju)} ингр. ({', '.join(missing_kbju)})"
        if kbju_coverage_status(kbju_coverage) == "poor":
            pct = int(round(float(kbju_coverage) * 100))
            warnings.append(
                base + f" — заполнено лишь {pct}% состава по весу, цифрам нельзя доверять"
            )
        else:
            warnings.append(base + " — нутриенты приблизительны")

    return DishUCResult(
        dish_id=dish_id,
        dish_name=dish_name,
        price_menu=price_menu,
        uc_rub=uc,
        uc_percent=uc_percent,
        margin_rub=margin,
        margin_percent=margin_percent,
        output_grams=output_grams,
        proteins_g=_round_percent(proteins),
        fats_g=_round_percent(fats),
        carbs_g=_round_percent(carbs),
        kcal=_round_money(kcal).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        kbju_coverage=kbju_coverage,
        ingredients=items,
        warnings=warnings,
    )


def calculate_dish_uc(
    data: KitchenData,
    dish_id: str,
    substitutions: dict[int, int] | None = None,
) -> DishUCResult | None:
    """UC и КБЖУ сохранённого блюда по его ID. None — если блюдо не найдено.

    substitutions — для симуляции замены (см. _compute_uc).
    """
    dish = data.dishes.get(dish_id)
    if dish is None:
        return None
    ttk_rows = data.ttk_by_dish.get(dish_id, [])
    return _compute_uc(
        data, dish.id, dish.name, dish.price_menu, ttk_rows, substitutions
    )


def calculate_uc_for_composition(
    data: KitchenData,
    dish_id: str,
    dish_name: str,
    price_menu: Decimal,
    rows: list[TTKRow],
) -> DishUCResult:
    """UC и КБЖУ для ещё НЕ сохранённого блюда (превью создания через create_dish).

    rows — список TTKRow в памяти (с временным dish_id). Калькулятор не пишет
    в кеш и не ищет блюдо — считает ровно по переданной рецептуре.
    """
    return _compute_uc(data, dish_id, dish_name, price_menu, rows)


def find_dishes_with_ingredient(
    data: KitchenData, ingredient_id: int
) -> list[Dish]:
    """Все блюда, где используется указанный ингредиент."""
    dishes = []
    for dish_id, rows in data.ttk_by_dish.items():
        for row in rows:
            if row.ingredient_id == ingredient_id:
                dish = data.dishes.get(dish_id)
                if dish and dish not in dishes:
                    dishes.append(dish)
                break
    return dishes


def simulate_price_change(
    data: KitchenData,
    ingredient_id: int,
    new_price: Decimal | None = None,
    delta_rub: Decimal | None = None,
    multiplier: Decimal | None = None,
    dish_ids: list[str] | None = None,
) -> dict:
    """Что произойдёт с UC и маржей блюд, если цена ингредиента изменится.

    Новую цену можно задать ровно одним способом (арифметику делаем здесь, не в LLM):
      - new_price   — абсолютная новая цена за единицу;
      - delta_rub   — изменение в рублях (отрицательное = подешевение);
      - multiplier  — множитель текущей цены (2 = вдвое дороже, 0.5 = вдвое дешевле).

    dish_ids: если задан — считаем только по этим блюдам (фильтр на случай вопроса
    про конкретное блюдо). Если None — по всем блюдам, где ингредиент используется.

    Возвращает таблицу затронутых блюд: текущий UC, новый UC, дельта,
    текущая маржа, новая маржа.
    """
    ing = data.ingredients.get(ingredient_id)
    if ing is None:
        return {"error": f"Ингредиент id={ingredient_id} не найден"}

    old_price = ing.price_per_unit
    if old_price is None:
        return {"error": f"У ингредиента '{ing.name}' нет текущей цены"}

    # Резолвим новую цену из одного из способов
    provided = [x for x in (new_price, delta_rub, multiplier) if x is not None]
    if len(provided) != 1:
        return {"error": "Нужен ровно один из: new_price, delta_rub, multiplier"}

    if new_price is not None:
        new_price_per_unit = new_price
    elif delta_rub is not None:
        new_price_per_unit = old_price + delta_rub
    else:
        new_price_per_unit = old_price * multiplier

    if new_price_per_unit < 0:
        return {"error": "Новая цена получилась отрицательной — проверь параметры"}

    # Находим затронутые блюда
    affected = find_dishes_with_ingredient(data, ingredient_id)
    if dish_ids is not None:
        wanted = set(dish_ids)
        affected = [d for d in affected if d.id in wanted]
    if not affected:
        return {
            "ingredient": ing.name,
            "old_price": float(old_price),
            "new_price": float(new_price_per_unit),
            "affected_count": 0,
            "dishes": [],
        }

    # Считаем UC до и после. Цену в кеше подменяем временно — try/finally
    # гарантирует, что при любой ошибке исходная цена восстановится и кеш
    # не останется испорченным.
    results = []
    try:
        for dish in affected:
            ing.price_per_unit = old_price
            old_uc = calculate_dish_uc(data, dish.id)
            if old_uc is None:
                continue
            ing.price_per_unit = new_price_per_unit
            new_uc = calculate_dish_uc(data, dish.id)
            if new_uc is None:
                continue

            delta_uc = float(new_uc.uc_rub - old_uc.uc_rub)
            results.append({
                "dish_id": dish.id,
                "dish_name": dish.name,
                "price_menu": float(dish.price_menu),
                "old_uc": float(old_uc.uc_rub),
                "new_uc": float(new_uc.uc_rub),
                "delta_uc": round(delta_uc, 2),
                "old_margin_percent": float(old_uc.margin_percent),
                "new_margin_percent": float(new_uc.margin_percent),
                "delta_margin_percent": round(
                    float(new_uc.margin_percent - old_uc.margin_percent), 1
                ),
            })
    finally:
        # Всегда возвращаем исходную цену в кеш
        ing.price_per_unit = old_price

    return {
        "ingredient": ing.name,
        "ingredient_id": ingredient_id,
        "old_price": float(old_price),
        "new_price": float(new_price_per_unit),
        "unit": ing.unit,
        "price_delta_percent": round(
            float((new_price_per_unit - old_price) / old_price * 100), 1
        ),
        "affected_count": len(results),
        "dishes": results,
    }


def simulate_replacement(
    data: KitchenData,
    old_ingredient_id: int,
    new_ingredient_id: int,
    dish_ids: list[str] | None = None,
) -> dict:
    """Настоящая замена ингредиента: что станет с UC/маржей/КБЖУ блюд, если в них
    заменить один ингредиент другим (оба есть в ING).

    Вес из ТТК сохраняется, ингредиент подменяется через substitutions в
    calculate_dish_uc — поэтому учитываются реальные цена, потери и КБЖУ нового
    ингредиента. dish_ids ограничивает расчёт конкретными блюдами.
    """
    old = data.ingredients.get(old_ingredient_id)
    if old is None:
        return {"error": f"Ингредиент id={old_ingredient_id} не найден"}
    new = data.ingredients.get(new_ingredient_id)
    if new is None:
        return {"error": f"Ингредиент id={new_ingredient_id} не найден"}

    affected = find_dishes_with_ingredient(data, old_ingredient_id)
    if dish_ids is not None:
        wanted = set(dish_ids)
        affected = [d for d in affected if d.id in wanted]

    base = {
        "old_ingredient": old.name,
        "new_ingredient": new.name,
        "old_price": float(old.price_per_unit) if old.price_per_unit is not None else None,
        "new_price": float(new.price_per_unit) if new.price_per_unit is not None else None,
        "old_unit": old.unit,
        "new_unit": new.unit,
    }
    if not affected:
        return {**base, "affected_count": 0, "dishes": []}

    subs = {old_ingredient_id: new_ingredient_id}
    results = []
    for dish in affected:
        old_uc = calculate_dish_uc(data, dish.id)
        new_uc = calculate_dish_uc(data, dish.id, substitutions=subs)
        if old_uc is None or new_uc is None:
            continue
        results.append({
            "dish_id": dish.id,
            "dish_name": dish.name,
            "price_menu": float(dish.price_menu),
            "old_uc": float(old_uc.uc_rub),
            "new_uc": float(new_uc.uc_rub),
            "delta_uc": round(float(new_uc.uc_rub - old_uc.uc_rub), 2),
            "old_margin_percent": float(old_uc.margin_percent),
            "new_margin_percent": float(new_uc.margin_percent),
            "delta_margin_percent": round(
                float(new_uc.margin_percent - old_uc.margin_percent), 1
            ),
            "old_kcal": float(old_uc.kcal),
            "new_kcal": float(new_uc.kcal),
        })

    return {**base, "affected_count": len(results), "dishes": results}