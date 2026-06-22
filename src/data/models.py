"""Типы данных. Pydantic-модели — это как «контракт» того, что мы ожидаем
от каждой записи. Если в таблице что-то не так — модель не построится,
и мы это увидим сразу, а не через десять шагов.
"""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class Ingredient(BaseModel):
    """Одна строка из листа ING."""

    id: int
    category: str
    name: str  # колонка "Наименование ингредиента" — это наш ING-ключ
    full_name: str = ""
    pos_name: str = ""  # "Короткое для айки"
    manufacturer: str = ""
    composition: str = ""

    # КБЖУ на 100 г
    proteins_100g: Decimal | None = None
    fats_100g: Decimal | None = None
    carbs_100g: Decimal | None = None
    kcal_100g: Decimal | None = None

    # Цены
    price_per_unit: Decimal | None = None  # цена за 1 кг / шт / л
    price_per_pack: Decimal | None = None
    unit: Literal["кг", "шт", "л", "мл"] = "кг"
    weight_per_unit_g: Decimal | None = None  # для шт-позиций: масса 1 штуки в граммах

    # Потери (в долях, не в процентах: 0.05 = 5%)
    losses_total: Decimal = Decimal("0")
    losses_unpacking: Decimal = Decimal("0")
    losses_cutting: Decimal = Decimal("0")
    losses_thermal: Decimal = Decimal("0")

    status: Literal["активный", "тестируется", "архив"] = "активный"


class Packaging(BaseModel):
    """Одна строка из листа Упаковка."""

    id: int
    name: str
    full_name: str = ""
    price_per_unit: Decimal | None = None
    category: str = ""  # для какой категории блюд
    supplier: str = ""
    status: Literal["активный", "архив"] = "активный"


class Dish(BaseModel):
    """Одна строка из листа Блюда."""

    id: str  # "B001"
    name: str
    category: str
    price_menu: Decimal
    uc_actual_pos: Decimal | None = None  # из айки
    status: Literal["активное", "разработка", "архив"] = "активное"


class TTKRow(BaseModel):
    """Одна строка из листа ТТК — один ингредиент или упаковка в одном блюде."""

    dish_id: str
    ingredient_id: int | None = None
    packaging_id: int | None = None
    weight_neto_g: Decimal
    cooking_method_id: int | None = None
    row_type: Literal["Основной", "Упаковка"]


class CookingMethod(BaseModel):
    """Способ приготовления."""

    id: int
    name: str
    description: str = ""
    oil_absorption: Decimal = Decimal("0")  # в долях
    comment: str = ""


# ============================================================
# Результаты расчётов (то, что возвращает калькулятор)
# ============================================================


class DishIngredientCost(BaseModel):
    """Стоимость одного ингредиента в составе блюда."""

    name: str
    weight_g: Decimal  # нетто
    weight_brutto_g: Decimal | None = None  # брутto (с учётом потерь), для рецептуры ТТК
    unit: str = "кг"
    price_per_unit: Decimal
    weight_per_piece_g: Decimal | None = None
    cost_rub: Decimal
    row_type: str
    share_percent: Decimal | None = None


class DishUCResult(BaseModel):
    """Результат расчёта UC блюда."""

    dish_id: str
    dish_name: str
    price_menu: Decimal
    uc_rub: Decimal
    uc_percent: Decimal
    margin_rub: Decimal
    margin_percent: Decimal
    output_grams: Decimal  # выход съедобной части
    # КБЖУ на всё блюдо (по нетто-весу основных ингредиентов)
    proteins_g: Decimal = Decimal("0")
    fats_g: Decimal = Decimal("0")
    carbs_g: Decimal = Decimal("0")
    kcal: Decimal = Decimal("0")
    kbju_coverage: Decimal = Decimal("0")  # доля веса основных ингр. с заполненным КБЖУ (0..1)
    ingredients: list[DishIngredientCost]
    warnings: list[str] = Field(default_factory=list)
