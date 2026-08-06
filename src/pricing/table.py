"""Сборка расчётки для коммерческого отдела — детерминированная арифметика.

Формат листа повторяет образец шефа (`Расчетка UC R&D x5 новиночки.xlsx`,
лист «Пиццы новые»): десять колонок, проценты долями, строка подсказок над шапкой.

Коммерческий отдел назначает по этой таблице розничную цену, поэтому цена может
прийти из трёх мест — см. `resolve_price`. LLM здесь не участвует: все числа
считает калькулятор (`src/calc/costs.py`).
"""
from dataclasses import dataclass, field
from decimal import Decimal

from src.calc.costs import calculate_uc_for_composition
from src.data.models import Dish
from src.data.sheets import KitchenData

# Колонки — ровно как в образце. Категория в таблицу НЕ выводится, она
# участвует только в сортировке.
HEADER = [
    "Название", "Цена продажная", "Unit cost, РУБ", "Unit cost, %", "МАРЖА %",
    "Вес продукта", "Белки", "Жиры", "Углеводы", "Каллории",
]

# Строка-подсказка над шапкой: где коммерсу можно писать, а где не трогать.
HINT_ROW = ["", "⬇️ тут ставим цену ⬇️", "тут не трогать — считает бот"] + [""] * 7

COL_NAME, COL_PRICE, COL_UC, COL_UC_PCT, COL_MARGIN_PCT = 0, 1, 2, 3, 4

# Строка данных начинается с третьей: 1 — подсказки, 2 — шапка.
FIRST_DATA_ROW = 3


@dataclass
class PricingRow:
    """Одна строка расчётки плюс служебные поля для подсветки и сводки."""

    dish_id: str
    name: str
    category: str
    price: Decimal | None
    uc_rub: Decimal
    uc_percent: Decimal | None
    margin_percent: Decimal | None
    output_g: Decimal
    proteins: Decimal
    fats: Decimal
    carbs: Decimal
    kcal: Decimal
    price_from_sheet: bool = False   # цену вписал коммерс, в «Блюда» её нет

    def to_cells(self) -> list:
        """Строка для Sheets. Проценты — долями (0.5686), Sheets покажет 56,86%."""
        return [
            self.name,
            "" if self.price is None else float(self.price),
            float(self.uc_rub),
            "" if self.uc_percent is None else float(self.uc_percent) / 100,
            "" if self.margin_percent is None else float(self.margin_percent) / 100,
            float(self.output_g),
            float(self.proteins),
            float(self.fats),
            float(self.carbs),
            float(self.kcal),
        ]

    @property
    def is_loss(self) -> bool:
        """Себестоимость съела всю цену. Почти всегда ошибка в данных."""
        return self.price is not None and self.price > 0 and self.uc_rub >= self.price

    def is_low_margin(self, threshold_percent: float) -> bool:
        if self.margin_percent is None or self.is_loss:
            return False
        return float(self.margin_percent) < threshold_percent


@dataclass
class PricingTable:
    rows: list[PricingRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # блюда без состава

    def values(self) -> list[list]:
        """Полный лист: подсказки + шапка + данные."""
        return [HINT_ROW, HEADER] + [r.to_cells() for r in self.rows]


def resolve_price(
    dish: Dish, price_in_sheet: Decimal | None
) -> tuple[Decimal | None, bool]:
    """Какая цена идёт в строку: (цена, взята_ли_из_листа).

    Приоритет:
      1. Цена в листе «Блюда» — источник истины, её ставит шеф.
      2. Цены в «Блюда» нет, но коммерс вписал в расчётку — сохраняем её.
      3. Нигде нет — пусто.

    Без пункта 2 полная перезапись листа стирала бы работу коммерческого отдела.
    """
    if dish.price_menu is not None and dish.price_menu > 0:
        return dish.price_menu, False
    if price_in_sheet is not None and price_in_sheet > 0:
        return price_in_sheet, True
    return None, False


def build_table(
    data: KitchenData,
    status: str,
    prices_in_sheet: dict[str, Decimal] | None = None,
) -> PricingTable:
    """Расчётка по блюдам указанного статуса.

    prices_in_sheet: {название блюда: цена} — то, что уже стоит в листе (вписано
    коммерсом). Ключ — название, потому что id в таблице нет: колонки фиксированы
    образцом. Поэтому тёзки обрабатываются отдельно (см. ниже).
    """
    prices_in_sheet = prices_in_sheet or {}
    table = PricingTable()

    dishes = [d for d in data.dishes.values() if d.status == status]

    # Тёзки: подставить цену не тому блюду хуже, чем не подставить вовсе.
    seen: dict[str, int] = {}
    for d in dishes:
        seen[d.name.strip().lower()] = seen.get(d.name.strip().lower(), 0) + 1
    ambiguous = {n for n, c in seen.items() if c > 1}
    if ambiguous:
        table.warnings.append(
            f"Блюда-тёзки ({len(ambiguous)}): {', '.join(sorted(ambiguous))} — "
            f"для них цену из таблицы не переношу, непонятно чья"
        )

    for dish in sorted(dishes, key=lambda d: ((d.category or "").lower(), d.name.lower())):
        if not data.ttk_by_dish.get(dish.id):
            table.skipped.append(f"{dish.id} {dish.name}")
            continue

        key = dish.name.strip().lower()
        from_sheet = None if key in ambiguous else prices_in_sheet.get(key)
        price, taken_from_sheet = resolve_price(dish, from_sheet)

        # Считаем UC по ТОЙ цене, что оказалась в строке: если её вписал коммерс,
        # маржа должна считаться от неё, иначе таблица бесполезна. Поэтому берём
        # calculate_uc_for_composition — она принимает цену явно, в отличие от
        # calculate_dish_uc, которая всегда тянет цену из листа «Блюда».
        result = calculate_uc_for_composition(
            data, dish.id, dish.name, price, data.ttk_by_dish[dish.id]
        )

        table.rows.append(PricingRow(
            dish_id=dish.id,
            name=dish.name,
            category=dish.category or "",
            price=price,
            uc_rub=result.uc_rub,
            uc_percent=result.uc_percent,
            margin_percent=result.margin_percent,
            output_g=result.output_grams,
            proteins=result.proteins_g,
            fats=result.fats_g,
            carbs=result.carbs_g,
            kcal=result.kcal,
            price_from_sheet=taken_from_sheet,
        ))

    return table


def margin_drops(
    table: PricingTable,
    previous: dict[str, Decimal],
    drop_pp: float,
) -> list[tuple[str, Decimal, Decimal]]:
    """Блюда, у которых маржа упала на drop_pp п.п. и больше: (название, было, стало).

    previous — маржа из прошлой версии листа (в процентах). Сравниваем только
    когда обе величины есть: «было пусто → стало 50%» это появление цены,
    а не падение.
    """
    out = []
    for row in table.rows:
        if row.margin_percent is None:
            continue
        was = previous.get(row.name.strip().lower())
        if was is None:
            continue
        if float(was) - float(row.margin_percent) >= drop_pp:
            out.append((row.name, was, row.margin_percent))
    return sorted(out, key=lambda t: float(t[1]) - float(t[2]), reverse=True)
