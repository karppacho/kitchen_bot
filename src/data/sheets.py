"""Подключение к Google Sheets через gspread.

Загружает все нужные листы в память при старте — это в разы быстрее, чем дёргать
API на каждый запрос. Кеш живёт всё время работы бота. Если нужно перечитать —
вызвать `reload()`.

ВАЖНО про колонки в твоей таблице:
  ING (порядок колонок, 20 шт):
    A id | B Категория | C Наименование ингредиента | D Полное наименование |
    E Короткое для айки | F Изготовитель | G Состав | H Белки | I Жиры |
    J Углеводы | K Ккал | L Цена за 1 кг | M Закупочная цена за упаковку |
    N Единица измерения | O Вес 1 шт, г | P Общие потери | Q потери перетарка |
    R нарезка | S тепловая | T Статус

  Упаковка:
    A id | B Название | C Полное наименование | D Цена за 1 шт |
    E Категория блюд | F Поставщик | G Статус | H Комментарий

  Блюда:
    A id | B Название | C Категория блюд | D Цена меню | E UC фактический |
    F Статус | G Дата создания | H Дата изменения | I Комментарий

  ТТК:
    A id_блюда | B id_ингредиента | C id_упаковки | D Вес нетто г |
    E Способ_приготовления_id | F Тип строки | G Комментарий

  Способы приготовления:
    A id | B Позиция | C Способ | D Норма впитывания (доля) |
    E Масло на 100 г | F Рекомендация | G Комментарий
"""
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from loguru import logger

from src.config import settings
from src.data.models import (
    CookingMethod,
    Dish,
    Ingredient,
    Packaging,
    TTKRow,
)

# spreadsheets (rw) нужен для Этапа 6 (создание блюд). drive — только чтение
# (открыть таблицу по ключу). Service Account должен иметь доступ «Редактор».
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _to_decimal(val) -> Decimal | None:
    """Аккуратное преобразование к Decimal.

    Поддерживает форматы Google Sheets:
      р.443,00       → 443.00
      р.1 030,00     → 1030.00  (с обычным или неразрывным пробелом)
      533.99         → 533.99
      0,00%          → 0.0       (процент в долях: 5% → 0.05)
      8%             → 0.08
    Пустые значения → None.
    """
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return Decimal(str(val))

    s = str(val).strip()
    if not s:
        return None

    # Признак "это процент" — на конце %
    is_percent = s.endswith("%")
    if is_percent:
        s = s[:-1].strip()

    # Убираем префикс "р." (рубли в формате Excel) и символ ₽
    s = s.replace("р.", "").replace("₽", "")
    # Убираем все виды пробелов (обычный, неразрывный U+00A0, узкий U+202F)
    s = (
        s.replace(" ", "")
         .replace("\u00a0", "")
         .replace("\u202f", "")
    )
    # Запятая → точка
    s = s.replace(",", ".")
    if not s:
        return None

    try:
        result = Decimal(s)
        # Процент → в долю
        if is_percent:
            result = result / Decimal("100")
        return result
    except InvalidOperation:
        logger.warning(f"Не смог преобразовать '{val}' в число")
        return None


def _to_int(val) -> int | None:
    """К int. Если пусто или мусор — None."""
    if val is None or val == "":
        return None
    try:
        return int(float(str(val).replace(",", ".")))
    except (ValueError, InvalidOperation):
        return None


def _to_str(val) -> str:
    """К строке без лишних пробелов. None → ''."""
    if val is None:
        return ""
    return str(val).strip()


def _tokens(s: str) -> list[str]:
    """Слова из строки в нижнем регистре (для поиска по словам в любом порядке)."""
    return [t for t in re.split(r"\W+", s.lower()) if t]


class KitchenData:
    """Главный класс — держит все справочники в памяти."""

    def __init__(self):
        self.ingredients: dict[int, Ingredient] = {}
        self.packagings: dict[int, Packaging] = {}
        self.dishes: dict[str, Dish] = {}
        self.ttk_by_dish: dict[str, list[TTKRow]] = {}
        self.cooking_methods: dict[int, CookingMethod] = {}
        self._client: gspread.Client | None = None
        self._spreadsheet: gspread.Spreadsheet | None = None

    def _connect(self) -> gspread.Spreadsheet:
        """Один раз создаёт клиент и открывает таблицу."""
        if self._spreadsheet is not None:
            return self._spreadsheet

        creds_path = Path(settings.google_service_account_json_path)
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Не нашёл credentials по пути {creds_path}. "
                f"Скачай JSON-ключ Service Account из Google Cloud Console."
            )

        creds = Credentials.from_service_account_file(str(creds_path), scopes=SCOPES)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(settings.google_sheets_id)
        logger.info(f"Открыли таблицу: {self._spreadsheet.title}")
        return self._spreadsheet

    def load_all(self) -> None:
        """Загрузка всех листов в память."""
        sh = self._connect()

        self._load_ingredients(sh)
        self._load_packagings(sh)
        self._load_dishes(sh)
        self._load_ttk(sh)
        self._load_cooking_methods(sh)

        logger.info(
            f"Загружено: ингредиентов={len(self.ingredients)}, "
            f"упаковок={len(self.packagings)}, "
            f"блюд={len(self.dishes)}, "
            f"ТТК-блюд={len(self.ttk_by_dish)}, "
            f"способов={len(self.cooking_methods)}"
        )

    def _load_ingredients(self, sh: gspread.Spreadsheet) -> None:
        ws = sh.worksheet("ING")
        rows = ws.get_all_values()
        # Теперь 20 колонок (была 19, добавилась "Вес 1 шт, г" после "Единица измерения")
        for raw in rows[1:]:
            raw = raw + [""] * (20 - len(raw))
            id_ = _to_int(raw[0])
            name = _to_str(raw[2])
            if id_ is None or not name:
                continue
            try:
                ing = Ingredient(
                    id=id_,
                    category=_to_str(raw[1]),
                    name=name,
                    full_name=_to_str(raw[3]),
                    pos_name=_to_str(raw[4]),
                    manufacturer=_to_str(raw[5]),
                    composition=_to_str(raw[6]),
                    proteins_100g=_to_decimal(raw[7]),
                    fats_100g=_to_decimal(raw[8]),
                    carbs_100g=_to_decimal(raw[9]),
                    kcal_100g=_to_decimal(raw[10]),
                    price_per_unit=_to_decimal(raw[11]),
                    price_per_pack=_to_decimal(raw[12]),
                    unit=_to_str(raw[13]) or "кг",
                    weight_per_unit_g=_to_decimal(raw[14]),  # НОВАЯ колонка O
                    losses_total=_to_decimal(raw[15]) or Decimal("0"),
                    losses_unpacking=_to_decimal(raw[16]) or Decimal("0"),
                    losses_cutting=_to_decimal(raw[17]) or Decimal("0"),
                    losses_thermal=_to_decimal(raw[18]) or Decimal("0"),
                    status=_to_str(raw[19]) or "активный",
                )
                self.ingredients[id_] = ing
            except Exception as e:
                logger.warning(f"ING: пропустил строку с id={id_}: {e}")

    def _load_packagings(self, sh: gspread.Spreadsheet) -> None:
        ws = sh.worksheet("Упаковка")
        rows = ws.get_all_values()
        for raw in rows[1:]:
            raw = raw + [""] * (8 - len(raw))
            id_ = _to_int(raw[0])
            name = _to_str(raw[1])
            if id_ is None or not name:
                continue
            try:
                pkg = Packaging(
                    id=id_,
                    name=name,
                    full_name=_to_str(raw[2]),
                    price_per_unit=_to_decimal(raw[3]),
                    category=_to_str(raw[4]),
                    supplier=_to_str(raw[5]),
                    status=_to_str(raw[6]) or "активный",
                )
                self.packagings[id_] = pkg
            except Exception as e:
                logger.warning(f"Упаковка: пропустил строку id={id_}: {e}")

    def _load_dishes(self, sh: gspread.Spreadsheet) -> None:
        ws = sh.worksheet("Блюда")
        rows = ws.get_all_values()
        for raw in rows[1:]:
            raw = raw + [""] * (9 - len(raw))
            id_ = _to_str(raw[0])
            name = _to_str(raw[1])
            if not id_ or not name:
                continue
            try:
                price_menu = _to_decimal(raw[3])
                if price_menu is None:
                    logger.warning(f"Блюда: {id_} без цены, пропустил")
                    continue
                dish = Dish(
                    id=id_,
                    name=name,
                    category=_to_str(raw[2]),
                    price_menu=price_menu,
                    uc_actual_pos=_to_decimal(raw[4]),
                    status=_to_str(raw[5]) or "активное",
                )
                self.dishes[id_] = dish
            except Exception as e:
                logger.warning(f"Блюда: пропустил {id_}: {e}")

    def _load_ttk(self, sh: gspread.Spreadsheet) -> None:
        ws = sh.worksheet("ТТК")
        rows = ws.get_all_values()
        for raw in rows[1:]:
            raw = raw + [""] * (7 - len(raw))
            dish_id = _to_str(raw[0])
            if not dish_id:
                continue
            try:
                row = TTKRow(
                    dish_id=dish_id,
                    ingredient_id=_to_int(raw[1]),
                    packaging_id=_to_int(raw[2]),
                    weight_neto_g=_to_decimal(raw[3]) or Decimal("0"),
                    cooking_method_id=_to_int(raw[4]),
                    row_type=_to_str(raw[5]) or "Основной",
                )
                self.ttk_by_dish.setdefault(dish_id, []).append(row)
            except Exception as e:
                logger.warning(f"ТТК: пропустил строку '{raw[:4]}': {e}")

    def _load_cooking_methods(self, sh: gspread.Spreadsheet) -> None:
        try:
            ws = sh.worksheet("Способы приготовления")
        except gspread.WorksheetNotFound:
            logger.warning("Лист 'Способы приготовления' не найден")
            return
        rows = ws.get_all_values()
        for raw in rows[1:]:
            raw = raw + [""] * (7 - len(raw))
            id_ = _to_int(raw[0])
            if id_ is None:
                continue
            try:
                cm = CookingMethod(
                    id=id_,
                    name=_to_str(raw[1]),
                    description=_to_str(raw[2]),
                    oil_absorption=_to_decimal(raw[3]) or Decimal("0"),
                    comment=_to_str(raw[6]),
                )
                self.cooking_methods[id_] = cm
            except Exception as e:
                logger.warning(f"Способы: пропустил id={id_}: {e}")

    # =========================================================
    # Удобные поиски
    # =========================================================

    def find_dish_by_name(self, query: str) -> Dish | None:
        """Поиск блюда по неточному названию (без учёта регистра)."""
        q = query.lower().strip()
        # Точное совпадение
        for dish in self.dishes.values():
            if dish.name.lower() == q:
                return dish
        # Подстрока
        matches = [d for d in self.dishes.values() if q in d.name.lower()]
        if len(matches) == 1:
            return matches[0]
        # Слова в другом порядке («римская маргарита» → «Пицца МАРГАРИТА римская»)
        if not matches:
            toks = _tokens(q)
            if toks:
                tok_matches = [
                    d for d in self.dishes.values()
                    if all(t in d.name.lower() for t in toks)
                ]
                if len(tok_matches) == 1:
                    return tok_matches[0]
        return None

    def find_dishes_by_query(self, query: str) -> list[Dish]:
        """Все блюда, подходящие под запрос (подстрока, иначе — все слова в любом порядке)."""
        q = query.lower().strip()
        subs = [d for d in self.dishes.values() if q in d.name.lower()]
        if subs:
            return subs
        toks = _tokens(q)
        if not toks:
            return []
        return [
            d for d in self.dishes.values()
            if all(t in d.name.lower() for t in toks)
        ]

    def search_ingredients(self, query: str) -> list[Ingredient]:
        """Все ингредиенты, подходящие под запрос.

        Ищет по name, full_name и pos_name (без учёта регистра, по подстроке).
        Точные совпадения по name ставятся в начало списка. Дедуп по id.

        Это переиспользуемое ядро поиска для всех ингредиентных tool-ов. Резолюцию
        «ничего / один / несколько» делает вызывающий код (handler в llm-слое).
        """
        q = query.lower().strip()
        if not q:
            return []

        exact: list[Ingredient] = []
        partial: list[Ingredient] = []
        for ing in self.ingredients.values():
            name_l = ing.name.lower()
            if name_l == q:
                exact.append(ing)
            elif (
                q in name_l
                or q in ing.full_name.lower()
                or q in ing.pos_name.lower()
            ):
                partial.append(ing)

        # Дедуп по id с сохранением порядка (exact раньше partial)
        seen: set[int] = set()
        result: list[Ingredient] = []
        for ing in exact + partial:
            if ing.id not in seen:
                seen.add(ing.id)
                result.append(ing)
        return result

    def list_ingredients_by_category(self, category: str) -> list[Ingredient]:
        """Ингредиенты указанной категории (подстрока, без учёта регистра).

        Для запросов вроде «какие у нас соусы / сыры».
        """
        c = category.lower().strip()
        if not c:
            return []
        return [i for i in self.ingredients.values() if c in i.category.lower()]

    def find_ingredient_by_name(self, query: str) -> Ingredient | None:
        """Однозначный ингредиент по имени или None.

        Возвращает ингредиент только если совпадение единственное (точное по name
        либо ровно один частичный матч). При неоднозначности — None; вызывающий
        код должен сам разобрать кандидатов через search_ingredients.
        """
        q = query.lower().strip()
        for ing in self.ingredients.values():
            if ing.name.lower() == q:
                return ing
        matches = self.search_ingredients(query)
        if len(matches) == 1:
            return matches[0]
        return None

    def search_packagings(self, query: str) -> list[Packaging]:
        """Упаковки, подходящие под запрос (подстрока по name/full_name, без регистра).

        По образцу search_ingredients. Точное совпадение по name — приоритетно.
        """
        q = query.lower().strip()
        if not q:
            return []
        exact = [p for p in self.packagings.values() if p.name.lower() == q]
        if exact:
            return exact
        return [
            p for p in self.packagings.values()
            if q in p.name.lower() or q in p.full_name.lower()
        ]

    # =========================================================
    # Запись в Sheets (Этап 6). Перед записью — снимок для отката.
    # =========================================================

    def next_free_dish_id(self) -> str:
        """Следующий свободный id блюда вида B001, B002… по КЕШУ (максимум B-id + 1).

        Быстро, но может отставать от таблицы (ручное удаление/добавление видно
        только после /refresh). Для записи бери next_free_dish_id_live().
        """
        return self._next_id_from([str(d) for d in self.dishes])

    def next_free_dish_id_live(self) -> str:
        """Следующий свободный id, посчитанный по ЖИВОМУ листу «Блюда» (колонка A).

        Читает таблицу прямо сейчас — корректно даже после ручного удаления строки
        и при параллельной правке человеком. Если чтение не удалось — фолбэк на кеш.
        """
        try:
            sh = self._connect()
            col = sh.worksheet("Блюда").col_values(1)  # вся колонка id
        except Exception:
            logger.warning("Не смог прочитать id из листа «Блюда» — беру из кеша")
            return self.next_free_dish_id()
        return self._next_id_from(col)

    @staticmethod
    def _next_id_from(ids) -> str:
        max_n = 0
        for v in ids:
            m = re.fullmatch(r"[Bb](\d+)", str(v).strip())
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f"B{max_n + 1:03d}"

    def snapshot_sheets(self) -> Path:
        """Снимок листов «Блюда» и «ТТК» в backups/ перед записью — для ручного отката.

        Полагаться только на историю версий Google недостаточно (ТЗ §8), поэтому
        перед каждой записью кладём сырые значения этих листов в локальный JSON.
        """
        sh = self._connect()
        snapshot: dict[str, list] = {}
        for title in ("Блюда", "ТТК"):
            try:
                snapshot[title] = sh.worksheet(title).get_all_values()
            except gspread.WorksheetNotFound:
                snapshot[title] = []
        backups_dir = Path("backups")
        backups_dir.mkdir(parents=True, exist_ok=True)
        path = backups_dir / f"sheets_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        logger.info(f"Снимок таблицы сохранён: {path}")
        return path

    def append_dish_and_ttk(self, dish: Dish, rows: list[TTKRow]) -> None:
        """Записать новое блюдо в «Блюда» и его состав в «ТТК».

        Перед записью делает снимок. Если строки ТТК не записались после строки
        блюда — пытается удалить строку блюда (откат). При успехе обновляет кеш
        в памяти, чтобы бот сразу видел новое блюдо без /refresh.
        """
        self.snapshot_sheets()
        sh = self._connect()
        dishes_ws = sh.worksheet("Блюда")
        ttk_ws = sh.worksheet("ТТК")

        today = f"{datetime.now():%d.%m.%Y}"
        # Раскладка «Блюда»: A id|B name|C category|D price|E uc_факт|F status|G дата|H|I
        dish_row = [
            dish.id, dish.name, dish.category, str(dish.price_menu),
            "", dish.status, today, "", "",
        ]
        resp = dishes_ws.append_row(dish_row, value_input_option="USER_ENTERED")

        # Номер добавленной строки — чтобы откатить при сбое записи ТТК
        dish_row_index = None
        try:
            rng = resp["updates"]["updatedRange"]  # напр. 'Блюда!A73:I73'
            m = re.search(r"![A-Z]+(\d+)", rng)
            dish_row_index = int(m.group(1)) if m else None
        except Exception:
            pass

        try:
            # Раскладка «ТТК»: A dish_id|B ing_id|C pkg_id|D вес_нетто|E способ|F тип|G
            ttk_values = [
                [
                    r.dish_id,
                    r.ingredient_id if r.ingredient_id is not None else "",
                    r.packaging_id if r.packaging_id is not None else "",
                    str(r.weight_neto_g),
                    "",
                    r.row_type,
                    "",
                ]
                for r in rows
            ]
            if ttk_values:
                ttk_ws.append_rows(ttk_values, value_input_option="USER_ENTERED")
        except Exception:
            logger.exception("Не удалось записать строки ТТК — откатываю строку блюда")
            if dish_row_index:
                try:
                    dishes_ws.delete_rows(dish_row_index)
                except Exception:
                    logger.exception(
                        "Откат строки блюда не удался — нужен ручной разбор по снимку"
                    )
            raise

        # Обновляем кеш на месте — бот сразу видит новое блюдо
        self.dishes[dish.id] = dish
        self.ttk_by_dish[dish.id] = list(rows)
        logger.info(f"Создано блюдо {dish.id} «{dish.name}» ({len(rows)} строк ТТК)")


# Глобальный экземпляр — один на весь процесс
_data: KitchenData | None = None


def get_data() -> KitchenData:
    """Ленивая инициализация. Первый вызов — грузит из Sheets."""
    global _data
    if _data is None:
        _data = KitchenData()
        _data.load_all()
    return _data


def reload_data() -> KitchenData:
    """Принудительная перезагрузка из Sheets (например, по команде /refresh).

    Глобал подменяется только ПОСЛЕ полной загрузки: параллельные запросы всё
    время видят целый кеш (старый или новый), а при ошибке загрузки старый
    кеш остаётся рабочим.
    """
    global _data
    new_data = KitchenData()
    new_data.load_all()
    _data = new_data
    return _data
