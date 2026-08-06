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
from google.auth.transport.requests import AuthorizedSession
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

# (connect, read) для вызовов Sheets API и отдельно на обновление OAuth-токена.
# Без них любой сетевой затык вешает поток бота навсегда — у gspread и google-auth
# таймаутов по умолчанию нет. Read щедрый: get_all_values по большому листу небыстр.
GOOGLE_TIMEOUT = (10, 60)
GOOGLE_REFRESH_TIMEOUT = 15


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
        # Строки ING с именем, но без id: (номер строки листа, имя). Бот их не
        # загружает, но помнит — чтобы на «не найдено» ответить по делу.
        self.ingredients_without_id: list[tuple[int, str]] = []
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
        # Свой AuthorizedSession с таймаутом на ОБНОВЛЕНИЕ токена: по умолчанию
        # его нет вообще, и зависшее TLS-рукопожатие к oauth2.googleapis.com
        # держит поток бесконечно (не 3 минуты, как у polza.ai, а навсегда).
        session = AuthorizedSession(creds, refresh_timeout=GOOGLE_REFRESH_TIMEOUT)
        self._client = gspread.authorize(creds, session=session)
        # (connect, read) на сами вызовы Sheets API — по той же причине
        self._client.set_timeout(GOOGLE_TIMEOUT)
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
        self.ingredients_without_id.clear()
        # Теперь 20 колонок (была 19, добавилась "Вес 1 шт, г" после "Единица измерения")
        # Нумерация строк листа: rows[0] — заголовок, значит data-строка i это строка i+2
        for line_no, raw in enumerate(rows[1:], start=2):
            raw = raw + [""] * (20 - len(raw))
            id_ = _to_int(raw[0])
            name = _to_str(raw[2])
            if id_ is None or not name:
                # Имя есть, id нет — это живой ингредиент, которого бот не увидит.
                # Копим и сообщаем одной сводкой (иначе хвост пустых строк листа
                # засорит лог сотнями сообщений).
                if name:
                    self.ingredients_without_id.append((line_no, name))
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

        if self.ingredients_without_id:
            listed = ", ".join(f"стр.{n} «{nm}»" for n, nm in self.ingredients_without_id)
            logger.warning(
                f"ING: {len(self.ingredients_without_id)} строк без id — бот их НЕ видит "
                f"({listed}). Проставь id в колонке A и вызови /refresh."
            )

        # Имя ингредиента — ключ для человека, дубли ломают поиск (правило №4).
        # Архив не считаем: пара «архивная + активная» — не конфликт, выбирать
        # там не из чего, и такие пары только зашумляли предупреждение.
        dupes = self.duplicate_ingredient_names()
        if dupes:
            listed = ", ".join(f"«{n}» → id {ids}" for n, ids in dupes.items())
            logger.warning(f"ING: дубли имён среди активных ({len(dupes)}): {listed}")

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
        no_price: list[str] = []
        for raw in rows[1:]:
            raw = raw + [""] * (9 - len(raw))
            id_ = _to_str(raw[0])
            name = _to_str(raw[1])
            if not id_ or not name:
                continue
            try:
                # Цены нет — блюдо всё равно грузим: себестоимость от цены продажи
                # не зависит, а у соусов-топпингов цены меню и не бывает.
                # Калькулятор в этом случае не считает маржу и пишет замечание.
                price_menu = _to_decimal(raw[3])
                if price_menu is None:
                    no_price.append(id_)
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

        if no_price:
            logger.info(
                f"Блюда без цены меню ({len(no_price)}): {', '.join(no_price)} — "
                f"загружены, себестоимость считаю, маржу нет"
            )

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

    # Лист со способами приготовления шеф переименовал (июль 2026), колонки те же.
    # Держим оба имени: старые копии таблицы тоже должны читаться.
    COOKING_METHODS_SHEETS = ("Способы приготовления", "Впитывание масла")

    def _load_cooking_methods(self, sh: gspread.Spreadsheet) -> None:
        ws = None
        for title in self.COOKING_METHODS_SHEETS:
            try:
                ws = sh.worksheet(title)
                break
            except gspread.WorksheetNotFound:
                continue
        if ws is None:
            logger.warning(
                f"Лист со способами приготовления не найден "
                f"(искал: {', '.join(self.COOKING_METHODS_SHEETS)})"
            )
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

    def duplicate_ingredient_names(self) -> dict[str, list[int]]:
        """Дубли имён среди АКТИВНЫХ ингредиентов: {имя в нижнем регистре: [id]}.

        Имя — ключ для человека (правило №4), дубли делают поиск неоднозначным.
        Архивные позиции не считаем: «архивная + активная» — не конфликт, бот
        всё равно предложит только активную.
        """
        by_name: dict[str, list[int]] = {}
        for ing in self.ingredients.values():
            if ing.status == "архив":
                continue
            by_name.setdefault(ing.name.lower(), []).append(ing.id)
        return {n: sorted(ids) for n, ids in by_name.items() if len(ids) > 1}

    def dish_categories(self) -> list[tuple[str, int]]:
        """Категории блюд, которые РЕАЛЬНО есть в таблице: (название, сколько блюд).

        По убыванию частоты. Нужны, чтобы при создании блюда предлагать шефу выбор
        из существующих, а не позволять LLM сочинять категорию на ходу.
        """
        counts: dict[str, int] = {}
        for d in self.dishes.values():
            c = (d.category or "").strip()
            if c:
                counts[c] = counts.get(c, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

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

    def snapshot_sheets(self, titles: tuple[str, ...] = ("Блюда", "ТТК")) -> Path:
        """Снимок листов в backups/ перед записью — для ручного отката.

        Полагаться только на историю версий Google недостаточно (ТЗ §8), поэтому
        перед каждой записью кладём сырые значения этих листов в локальный JSON.

        titles задаётся явно: расчётка для коммерческого отдела живёт на своих
        листах, и там лежат цены, вписанные руками, — их тоже надо страховать.
        """
        sh = self._connect()
        snapshot: dict[str, list] = {}
        for title in titles:
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

    @staticmethod
    def _first_free_row(ws, key_col: int = 1) -> int:
        """Номер первой свободной строки — по КЛЮЧЕВОЙ колонке, а не по append.

        `append_row` ориентируется на «таблицу» в понимании Google: любая строка,
        где заполнена хоть одна ячейка, считается данными. В листе «Блюда» ниже
        последнего блюда тянется сорок строк с одним лишь статусом «активное» —
        из-за них новое блюдо улетало на 40 строк вниз, в мёртвую зону, и шеф
        его там просто не находил (разбор 04.08.2026).

        col_values отдаёт значения до последней непустой ячейки колонки, поэтому
        len+1 — это строка сразу после последнего РЕАЛЬНОГО блюда. Реальные
        строки при этом никогда не перезаписываются.
        """
        return len(ws.col_values(key_col)) + 1

    @staticmethod
    def _ensure_rows(ws, last_row: int) -> None:
        """Дорастить лист, если пишем ниже его текущей высоты."""
        if last_row > ws.row_count:
            ws.add_rows(last_row - ws.row_count + 10)

    @staticmethod
    def _clear_rows(ws, first_row: int, last_row: int, last_col: str) -> None:
        """Очистить диапазон строк.

        Именно очистка, а не delete_rows: удаление сдвигает всё, что ниже, и
        ломает раскладку листа, с которой шеф работает руками.
        """
        ws.batch_clear([f"A{first_row}:{last_col}{last_row}"])

    def append_dish_and_ttk(self, dish: Dish, rows: list[TTKRow]) -> None:
        """Записать новое блюдо в «Блюда» и его состав в «ТТК».

        Перед записью делает снимок. Пишет адресно, в первую свободную строку
        каждого листа. После записи ПРОВЕРЯЕТ, что строки ТТК действительно легли:
        иначе блюдо осталось бы без состава, а бот отрапортовал бы успех. Если
        не сошлось — строка блюда очищается (откат) и поднимается исключение.
        При успехе обновляет кеш в памяти, чтобы бот сразу видел новое блюдо.
        """
        self.snapshot_sheets()
        sh = self._connect()
        dishes_ws = sh.worksheet("Блюда")
        ttk_ws = sh.worksheet("ТТК")

        today = f"{datetime.now():%d.%m.%Y}"
        # Раскладка «Блюда»: A id|B name|C category|D price|E uc_факт|F status|G дата|H|I
        # Цены может не быть — тогда ячейка пустая. str(None) записал бы в
        # таблицу текст «None», и при следующей загрузке это стало бы мусором.
        price_cell = "" if dish.price_menu is None else str(dish.price_menu)
        dish_row = [
            dish.id, dish.name, dish.category, price_cell,
            "", dish.status, today, "", "",
        ]
        dish_row_index = self._first_free_row(dishes_ws)
        self._ensure_rows(dishes_ws, dish_row_index)
        dishes_ws.update(
            range_name=f"A{dish_row_index}:I{dish_row_index}",
            values=[dish_row],
            value_input_option="USER_ENTERED",
        )

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
                first = self._first_free_row(ttk_ws)
                last = first + len(ttk_values) - 1
                self._ensure_rows(ttk_ws, last)
                ttk_ws.update(
                    range_name=f"A{first}:G{last}",
                    values=ttk_values,
                    value_input_option="USER_ENTERED",
                )
                # Проверяем факт записи: без этого «блюдо без состава» проходит молча
                written = ttk_ws.get_values(f"A{first}:A{last}")
                got = sum(1 for r in written if (r and r[0].strip() == dish.id))
                if got != len(ttk_values):
                    raise RuntimeError(
                        f"в ТТК легло {got} строк из {len(ttk_values)} "
                        f"(диапазон A{first}:A{last})"
                    )
        except Exception:
            logger.exception("Не удалось записать строки ТТК — откатываю строку блюда")
            try:
                self._clear_rows(dishes_ws, dish_row_index, dish_row_index, "I")
            except Exception:
                logger.exception(
                    "Откат строки блюда не удался — нужен ручной разбор по снимку"
                )
            raise

        # Обновляем кеш на месте — бот сразу видит новое блюдо
        self.dishes[dish.id] = dish
        self.ttk_by_dish[dish.id] = list(rows)
        logger.info(
            f"Создано блюдо {dish.id} «{dish.name}»: строка {dish_row_index} "
            f"в «Блюда», {len(rows)} строк в ТТК"
        )


# Глобальный экземпляр — один на весь процесс
_data: KitchenData | None = None


def get_data() -> KitchenData:
    """Ленивая инициализация. Первый вызов — грузит из Sheets.

    Глобал подменяется только ПОСЛЕ успешной загрузки (как в reload_data): иначе
    сорвавшийся первый запрос оставлял в кеше пустой объект навсегда, и бот до
    перезапуска молча отвечал «не найдено» на любой вопрос.
    """
    global _data
    if _data is None:
        new_data = KitchenData()
        new_data.load_all()
        _data = new_data
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
