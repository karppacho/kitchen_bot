"""Тесты записи в Google Sheets — офлайн, на фейковом worksheet.

Главное, что защищаем: адресный расчёт строки. `append_row` считает данными
любую строку, где заполнена хоть одна ячейка. В листе «Блюда» ниже последнего
блюда тянулись сорок строк с одним лишь статусом «активное» — новое блюдо
улетало на 40 строк вниз, шеф его там не находил и считал, что запись не прошла
(разбор 04.08.2026).
"""
from decimal import Decimal

import pytest

from src.data.models import Dish, TTKRow
from src.data.sheets import KitchenData


def _a1_rows(range_name: str) -> tuple[int, int]:
    """'A5:G8' → (5, 8)."""
    import re
    nums = [int(n) for n in re.findall(r"[A-Z]+(\d+)", range_name)]
    return nums[0], nums[-1]


class _FakeWorksheet:
    """Дублёр gspread.Worksheet: col_values / update / get_values / batch_clear."""

    def __init__(self, rows: list[list[str]], row_count: int = 1000, width: int = 9):
        self._rows = rows
        self.row_count = row_count
        self.width = width
        self.added = 0
        self.fail_update = False          # имитация сбоя записи
        self.swallow_update = False       # имитация «записалось молча в никуда»

    def _grow_to(self, row: int):
        while len(self._rows) < row:
            self._rows.append([""] * self.width)

    def col_values(self, col: int):
        """Как в gspread: значения до последней НЕПУСТОЙ ячейки колонки."""
        vals = [
            (r[col - 1] if len(r) >= col else "") for r in self._rows
        ]
        while vals and not str(vals[-1]).strip():
            vals.pop()
        return vals

    def add_rows(self, n: int):
        self.added += n
        self.row_count += n

    def update(self, range_name=None, values=None, **kwargs):
        if self.fail_update:
            raise RuntimeError("Sheets недоступен")
        if self.swallow_update:
            return {}                      # «ок», но ничего не записали
        first, _ = _a1_rows(range_name)
        self._grow_to(first + len(values) - 1)
        for i, row in enumerate(values):
            padded = list(row) + [""] * (self.width - len(row))
            self._rows[first - 1 + i] = padded
        return {}

    def get_values(self, range_name=None, **kwargs):
        first, last = _a1_rows(range_name)
        out = []
        for r in self._rows[first - 1:last]:
            out.append([r[0] if r else ""])
        return out

    def batch_clear(self, ranges):
        for rng in ranges:
            first, last = _a1_rows(rng)
            for i in range(first - 1, min(last, len(self._rows))):
                self._rows[i] = [""] * self.width


class _FakeSpreadsheet:
    def __init__(self, sheets: dict):
        self._sheets = sheets

    def worksheet(self, title):
        return self._sheets[title]


def _kitchen_with(dishes_ws, ttk_ws, monkeypatch) -> KitchenData:
    d = KitchenData()
    monkeypatch.setattr(
        d, "_connect", lambda: _FakeSpreadsheet({"Блюда": dishes_ws, "ТТК": ttk_ws})
    )
    monkeypatch.setattr(d, "snapshot_sheets", lambda: None)
    return d


def _new_dish() -> tuple[Dish, list[TTKRow]]:
    dish = Dish(id="B131", name="Тест", category="Бургер", price_menu=Decimal("280"))
    rows = [
        TTKRow(dish_id="B131", ingredient_id=35, weight_neto_g=Decimal("60"),
               row_type="Основной"),
        TTKRow(dish_id="B131", packaging_id=9, weight_neto_g=Decimal("1"),
               row_type="Упаковка"),
    ]
    return dish, rows


def _dish_rows(n_dishes: int, phantom_tail: int = 0) -> list[list[str]]:
    """Лист «Блюда»: заголовок + n блюд + хвост строк с одним статусом в колонке F."""
    rows = [["id", "Название", "Категория", "Цена", "", "Статус", "", "", ""]]
    for i in range(1, n_dishes + 1):
        rows.append([f"B{i:03d}", f"Блюдо {i}", "Пицца", "500", "", "активное", "", "", ""])
    for _ in range(phantom_tail):
        rows.append(["", "", "", "", "", "активное", "", "", ""])
    return rows


def test_first_free_row_ignores_phantom_rows():
    """Хвост из «активное» без id не должен сдвигать точку записи."""
    ws = _FakeWorksheet(_dish_rows(130, phantom_tail=40))
    # заголовок + 130 блюд = 131 строка с id → пишем в 132, сразу под последним
    assert KitchenData._first_free_row(ws) == 132


def test_first_free_row_without_phantoms():
    ws = _FakeWorksheet(_dish_rows(130))
    assert KitchenData._first_free_row(ws) == 132


def test_first_free_row_empty_sheet():
    """Только заголовок → первая свободная строка вторая."""
    ws = _FakeWorksheet(_dish_rows(0))
    assert KitchenData._first_free_row(ws) == 2


def test_first_free_row_never_overwrites_last_dish():
    """Расчётная строка всегда строго ниже последней заполненной в колонке A."""
    for n in (1, 5, 130):
        ws = _FakeWorksheet(_dish_rows(n, phantom_tail=7))
        last_dish_row = n + 1                     # +1 на заголовок
        assert KitchenData._first_free_row(ws) == last_dish_row + 1


def test_ensure_rows_grows_sheet_when_needed():
    ws = _FakeWorksheet(_dish_rows(10), row_count=12)
    KitchenData._ensure_rows(ws, 50)
    assert ws.row_count >= 50
    ws2 = _FakeWorksheet(_dish_rows(10), row_count=1000)
    KitchenData._ensure_rows(ws2, 50)
    assert ws2.added == 0                          # места хватало — не трогаем


def test_first_free_row_by_ttk_key_column():
    """ТТК ключуется по id_блюда в колонке A — та же логика."""
    rows = [["id_блюда", "id_ингр", "id_упак", "вес", "", "тип", ""]]
    rows += [["B001", "5", "", "100", "", "Основной", ""] for _ in range(700)]
    ws = _FakeWorksheet(rows, width=7)
    assert KitchenData._first_free_row(ws) == 702


# ---------- append_dish_and_ttk: запись, проверка, откат ----------

def _ttk_rows(n: int) -> list[list[str]]:
    rows = [["id_блюда", "id_ингр", "id_упак", "вес", "", "тип", ""]]
    rows += [["B001", "5", "", "100", "", "Основной", ""] for _ in range(n)]
    return rows


def test_write_lands_right_under_last_dish(monkeypatch):
    """Блюдо пишется в 132-ю строку, а не под хвост фантомов."""
    dishes = _FakeWorksheet(_dish_rows(130, phantom_tail=40))
    ttk = _FakeWorksheet(_ttk_rows(700), width=7)
    d = _kitchen_with(dishes, ttk, monkeypatch)
    dish, rows = _new_dish()

    d.append_dish_and_ttk(dish, rows)

    assert dishes._rows[131][0] == "B131"          # строка 132
    assert dishes._rows[131][1] == "Тест"
    assert ttk._rows[701][0] == "B131"             # строка 702
    assert ttk._rows[702][0] == "B131"
    # кеш обновлён — бот видит блюдо без /refresh
    assert d.dishes["B131"].name == "Тест"
    assert len(d.ttk_by_dish["B131"]) == 2


def test_dish_without_price_writes_empty_cell(monkeypatch):
    """str(None) записал бы в таблицу текст «None» — это был бы мусор в данных."""
    dishes = _FakeWorksheet(_dish_rows(130))
    ttk = _FakeWorksheet(_ttk_rows(700), width=7)
    d = _kitchen_with(dishes, ttk, monkeypatch)
    dish, rows = _new_dish()
    dish.price_menu = None

    d.append_dish_and_ttk(dish, rows)

    assert dishes._rows[131][3] == ""               # колонка D «Цена меню»
    assert d.dishes["B131"].price_menu is None


def test_rollback_clears_dish_row_when_ttk_fails(monkeypatch):
    """Сбой записи ТТК → строка блюда очищается, ошибка наверх."""
    dishes = _FakeWorksheet(_dish_rows(130))
    ttk = _FakeWorksheet(_ttk_rows(700), width=7)
    ttk.fail_update = True
    d = _kitchen_with(dishes, ttk, monkeypatch)
    dish, rows = _new_dish()

    with pytest.raises(Exception):
        d.append_dish_and_ttk(dish, rows)

    assert dishes._rows[131][0] == ""               # строка 132 пуста
    assert "B131" not in d.dishes                   # кеш не тронут


def test_rollback_when_ttk_write_silently_does_nothing(monkeypatch):
    """Запись «прошла» без ошибки, но строк нет — ловим проверкой, а не молчим.

    Без неё получалось блюдо без состава, о котором бот рапортовал как об успехе.
    """
    dishes = _FakeWorksheet(_dish_rows(130))
    ttk = _FakeWorksheet(_ttk_rows(700), width=7)
    ttk.swallow_update = True
    d = _kitchen_with(dishes, ttk, monkeypatch)
    dish, rows = _new_dish()

    with pytest.raises(Exception):
        d.append_dish_and_ttk(dish, rows)

    assert dishes._rows[131][0] == ""
    assert "B131" not in d.dishes
