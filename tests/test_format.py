"""Тесты детерминированного вывода (src/llm/format.py) — без сети и LLM.

Главное, что защищаем: строки `display` собирает Python, и они не должны падать
или врать, когда у блюда не заполнена цена меню (маржи в этом случае не существует).
"""
from src.llm.format import (
    format_category_prompt,
    format_compare,
    format_dish_created,
    format_dish_preview,
    format_dish_uc,
    format_replacement,
    format_simulate,
    format_ttk_preview,
)

_CATEGORIES = [("Закуска", 43), ("Блюдо", 24), ("Пицца", 15), ("Бургер", 10)]


def _uc_result(**over) -> dict:
    """Типовой результат calculate_dish_uc в виде словаря (как его отдаёт tool)."""
    base = {
        "dish_id": "T001",
        "dish_name": "Тест-ролл",
        "price_menu_rub": 200.0,
        "uc_rub": 70.57,
        "uc_percent": 35.3,
        "margin_rub": 129.43,
        "margin_percent": 64.7,
        "output_grams": 160.0,
        "ingredients": [
            {"name": "Тортилья", "weight_g": 60.0, "cost_rub": 8.57, "share_percent": 12.1},
            {"name": "Салат", "weight_g": 100.0, "cost_rub": 50.0, "share_percent": 70.9},
        ],
        "warnings": [],
    }
    base.update(over)
    return base


def _delta_row(**over) -> dict:
    base = {
        "dish_id": "T001",
        "dish_name": "Тест-ролл",
        "old_uc": 70.57,
        "new_uc": 120.57,
        "delta_uc": 50.0,
        "old_margin_percent": 64.7,
        "new_margin_percent": 39.7,
        "delta_margin_percent": -25.0,
        "old_kcal": 200.0,
        "new_kcal": 200.0,
    }
    base.update(over)
    return base


_NO_MARGIN = {
    "old_margin_percent": None,
    "new_margin_percent": None,
    "delta_margin_percent": None,
}


# ---------- calculate_dish_uc ----------

def test_dish_uc_with_price():
    out = format_dish_uc(_uc_result())
    assert "UC: 70.57" in out
    assert "Маржа: 129.43" in out
    assert "Цена меню не заполнена" not in out


def test_dish_uc_without_price():
    """Без цены — себестоимость есть, маржи нет и никаких «0%»."""
    out = format_dish_uc(_uc_result(
        price_menu_rub=None, uc_percent=None, margin_rub=None, margin_percent=None,
        warnings=["Цена меню не заполнена — считаю только себестоимость, маржу не могу"],
    ))
    assert "Себестоимость: 70.57" in out
    assert "Цена меню не заполнена" in out
    assert "Маржа:" not in out
    assert "0.0%" not in out          # ноль вместо маржи шеф принял бы за настоящий
    assert "Выход: 160.0 г" in out
    assert "Тортилья" in out          # состав на месте


# ---------- симуляции ----------

def test_simulate_mixed_priced_and_unpriced():
    """Блюдо без цены не должно ронять min/max по дельте маржи."""
    r = {
        "ingredient": "Салат", "old_price": 500.0, "new_price": 1000.0,
        "unit": "кг", "price_delta_percent": 100.0,
        "dishes": [
            _delta_row(),
            _delta_row(dish_id="T002", dish_name="Соус сырный", **_NO_MARGIN),
        ],
    }
    out = format_simulate(r)
    assert "Затронуто блюд: 2" in out
    assert "нет цены меню" in out                 # во второй строке таблицы
    assert "Сильнее всего пострадает: Тест-ролл" in out


def test_simulate_all_dishes_without_price():
    """Если маржи нет ни у кого — честная фраза вместо падения."""
    r = {
        "ingredient": "Салат", "old_price": 500.0, "new_price": 1000.0,
        "unit": "кг", "price_delta_percent": 100.0,
        "dishes": [_delta_row(**_NO_MARGIN)],
    }
    out = format_simulate(r)
    assert "маржу сравнить не с чем" in out


def test_replacement_mixed_priced_and_unpriced():
    r = {
        "old_ingredient": "Салат", "new_ingredient": "Капуста",
        "old_price": 500.0, "new_price": 50.0,
        "old_unit": "кг", "new_unit": "кг",
        "affected_count": 2,
        "dishes": [
            _delta_row(),
            _delta_row(dish_id="T002", dish_name="Соус сырный", **_NO_MARGIN),
        ],
    }
    out = format_replacement(r)
    assert "Затронуто блюд: 2" in out
    assert "Сильнее всего меняется маржа: Тест-ролл" in out


def test_replacement_all_without_price():
    """Нет ни одной маржи — блок сводки просто не печатается, ошибки нет."""
    r = {
        "old_ingredient": "Салат", "new_ingredient": "Капуста",
        "old_price": 500.0, "new_price": 50.0,
        "old_unit": "кг", "new_unit": "кг",
        "affected_count": 1,
        "dishes": [_delta_row(**_NO_MARGIN)],
    }
    out = format_replacement(r)
    assert "Сильнее всего меняется маржа" not in out
    assert "Затронуто блюд: 1" in out


# ---------- превью ТТК ----------

def test_ttk_preview_without_legal_fields():
    """Превью собирается на контексте БЕЗ реквизитов — их больше не существует."""
    context = {
        "ttk_number": "119",
        "dish_name": "Английский завтрак с драником",
        "dish_output_g": "281",
        "ingredients": [
            {"name": "Драники картофельные", "brutto": "100", "netto": "100"},
        ],
        "kbju_per_100g": {"белки": "7.9", "жиры": "16.1",
                          "углеводы": "0.3", "ккал": "175"},
        "kbju_per_portion": {"белки": "22.2", "жиры": "45.2",
                             "углеводы": "0.8", "ккал": "492"},
    }
    out = format_ttk_preview(context, {"warnings": []})
    assert "ТТК № 119" in out
    assert "Английский завтрак с драником" in out
    assert "Выход: 281 г" in out
    assert "Гастрономия" not in out          # реквизитов сети в шапке больше нет


# ---------- создание блюда: категория ----------

def test_category_prompt_lists_existing():
    """Шефу показывают реальные категории из таблицы, а не выдумку LLM."""
    out = format_category_prompt("Круассан с чоризо", _CATEGORIES)
    assert "Круассан с чоризо" in out
    for name, count in _CATEGORIES:
        assert name in out
        assert str(count) in out
    assert "новая категория" not in out


def test_category_prompt_for_unknown_category():
    """Незнакомую категорию не блокируем, но переспрашиваем — вдруг опечатка."""
    out = format_category_prompt("Тест", _CATEGORIES, unknown="Бургеры")
    assert "Бургеры" in out
    assert "ещё нет" in out
    assert "да, новая категория" in out
    assert "Закуска" in out                       # список всё равно показан


# ---------- создание блюда: забытая упаковка ----------

def _new_dish(**over) -> dict:
    base = {
        "dish_id": "B131", "dish_name": "Круассан с чоризо", "category": "Бургер",
        "price_menu_rub": 280.0, "uc_rub": 142.16, "uc_percent": 50.8,
        "margin_rub": 137.84, "margin_percent": 49.2, "output_grams": 320.0,
        "ingredients": [
            {"name": "Круассан", "weight_g": 200.0, "cost_rub": 49.5, "share_percent": 34.8},
        ],
        "warnings": [],
        "packaging_missing": False,
    }
    base.update(over)
    return base


def test_preview_without_price_shows_cost_only():
    """Блюдо заводят до назначения цены — превью не должно падать и врать про маржу."""
    out = format_dish_preview(_new_dish(
        price_menu_rub=None, uc_percent=None, margin_rub=None, margin_percent=None,
    ))
    assert "Себестоимость: 142.16" in out
    assert "Цена меню не заполнена" in out
    assert "Маржа:" not in out
    assert "Создать блюдо с таким составом?" in out    # создать всё равно можно


def test_created_without_price_suggests_filling_it():
    out = format_dish_created(_new_dish(
        price_menu_rub=None, uc_percent=None, margin_rub=None, margin_percent=None,
    ))
    assert "Цена меню не заполнена" in out
    assert "посчитаю маржу" in out


def test_preview_asks_about_missing_packaging():
    """Упаковку забыли — превью обязано спросить, а не звать подтверждать запись."""
    out = format_dish_preview(_new_dish(packaging_missing=True))
    assert "Упаковка не указана" in out
    assert "без упаковки" in out                  # шефу дан явный способ отказаться
    assert "Напиши «да» — запишу в таблицу" not in out


def test_preview_normal_when_packaging_given():
    out = format_dish_preview(_new_dish())
    assert "Упаковка не указана" not in out
    assert "Напиши «да» — запишу в таблицу" in out


def test_created_notes_dish_written_without_packaging():
    """Если всё-таки записали без упаковки — сказать об этом, а не умолчать."""
    out = format_dish_created(_new_dish(packaging_missing=True))
    assert "БЕЗ упаковки" in out
    assert "backups/" in out


def test_created_silent_when_packaging_present():
    out = format_dish_created(_new_dish())
    assert "БЕЗ упаковки" not in out


# ---------- сравнение маржи ----------

def test_compare_lists_skipped_dishes():
    """Блюда без цены уходят в skipped — шеф должен видеть, что их не сравнивали."""
    r = {
        "count": 1,
        "dishes": [{
            "id": "T001", "name": "Тест-ролл", "category": "Ролл",
            "price_menu_rub": 200.0, "uc_rub": 70.57,
            "uc_percent": 35.3, "margin_rub": 129.43, "margin_percent": 64.7,
        }],
        "skipped_dishes": [
            {"id": "T002", "name": "Соус сырный", "reason": "цена меню не заполнена"},
        ],
    }
    out = format_compare(r)
    assert "Тест-ролл" in out
    assert "Соус сырный" in out
    assert "цена меню не заполнена" in out
