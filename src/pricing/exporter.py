"""Запись расчётки в РАБОЧУЮ таблицу шефа — на свои листы.

Отличие от `src/competitors/exporter.py`: та таблица целиком принадлежит боту,
а здесь мы пишем рядом с данными, которые шеф правит руками. Поэтому строже:

  - перед перезаписью снимок листа в backups/ (там могут лежать цены коммерсов);
  - читаем старый лист и переносим введённые цены — иначе перезапись стёрла бы
    работу коммерческого отдела;
  - трогаем ТОЛЬКО свой лист, соседние не задеваем ни при каких условиях.
"""
from decimal import Decimal, InvalidOperation

import gspread
from loguru import logger

from src.config import settings
from src.data.sheets import KitchenData
from src.pricing.table import (
    COL_MARGIN_PCT,
    COL_NAME,
    COL_PRICE,
    FIRST_DATA_ROW,
    HEADER,
    PricingTable,
    build_table,
    margin_drops,
)

# Листы бота в рабочей таблице. Создаются при первом запуске.
SHEET_NEW = "Расчётка новинки"
SHEET_MENU = "Расчётка меню"

STATUS_SHEETS = {
    "разработка": SHEET_NEW,
    "активное": SHEET_MENU,
}

_RED = {"red": 0.96, "green": 0.80, "blue": 0.80}
_YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.75}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_HEADER_GREY = {"red": 0.85, "green": 0.85, "blue": 0.85}


def _to_decimal(raw) -> Decimal | None:
    """Число из ячейки листа. Понимает «299», «299,5», «р.299,00», пустоту."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = (
        s.replace("р.", "").replace("₽", "").replace("%", "")
        .replace(" ", "").replace(" ", "").replace(" ", "")
        .replace(",", ".")
    )
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def read_existing(ws: gspread.Worksheet) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Что уже стоит в листе: ({название: цена}, {название: маржа %}).

    Цены нужны, чтобы не потерять работу коммерсов. Маржа — чтобы после
    пересчёта сказать, у каких блюд она просела: лист сам себе хранилище
    предыдущего состояния, отдельная база для этого не нужна.
    """
    try:
        values = ws.get_all_values()
    except Exception:
        logger.exception("Не смог прочитать расчётку — цены не перенесу")
        return {}, {}

    prices: dict[str, Decimal] = {}
    margins: dict[str, Decimal] = {}
    for row in values[FIRST_DATA_ROW - 1:]:
        if not row or not str(row[COL_NAME]).strip():
            continue
        key = str(row[COL_NAME]).strip().lower()
        price = _to_decimal(row[COL_PRICE]) if len(row) > COL_PRICE else None
        if price is not None:
            prices[key] = price
        raw_margin = row[COL_MARGIN_PCT] if len(row) > COL_MARGIN_PCT else ""
        margin = _to_decimal(raw_margin)
        if margin is not None:
            # В листе маржа лежит долей (0.6832), в отчётах оперируем процентами
            margins[key] = margin * 100 if abs(margin) <= 1 else margin
    return prices, margins


def _get_or_create_ws(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        logger.info(f"[расчётка] создаю лист «{title}»")
        return sh.add_worksheet(title=title, rows=400, cols=len(HEADER) + 2)


def _highlight_ranges(table: PricingTable) -> tuple[list[str], list[str]]:
    """Диапазоны ячеек «МАРЖА %» под красную и жёлтую заливку."""
    col = chr(ord("A") + COL_MARGIN_PCT)
    red, yellow = [], []
    threshold = settings.pricing_margin_warn_percent
    for i, row in enumerate(table.rows):
        cell = f"{col}{FIRST_DATA_ROW + i}"
        if row.is_loss:
            red.append(cell)
        elif row.is_low_margin(threshold):
            yellow.append(cell)
    return red, yellow


def export_table(data: KitchenData, status: str) -> dict:
    """Пересобрать лист расчётки по статусу блюд. Возвращает сводку."""
    title = STATUS_SHEETS.get(status)
    if title is None:
        raise ValueError(f"Нет листа для статуса «{status}»")

    sh = data._connect()
    ws = _get_or_create_ws(sh, title)

    prices, previous_margins = read_existing(ws)
    # Снимок ПОСЛЕ чтения, но ДО записи: страхуем цены, вписанные руками
    data.snapshot_sheets(titles=(title,))

    table = build_table(data, status, prices_in_sheet=prices)
    drops = margin_drops(table, previous_margins, settings.pricing_margin_drop_pp)

    values = table.values()
    ws.clear()
    ws.update(values=values, range_name="A1", value_input_option="USER_ENTERED")

    last_row = FIRST_DATA_ROW + len(table.rows) - 1
    fmt: list[dict] = [
        {"range": f"A2:{chr(ord('A') + len(HEADER) - 1)}2",
         "format": {"textFormat": {"bold": True},
                    "backgroundColor": _HEADER_GREY}},
    ]
    if table.rows:
        fmt += [
            {"range": f"B{FIRST_DATA_ROW}:C{last_row}",
             "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00 ₽"}}},
            {"range": f"D{FIRST_DATA_ROW}:E{last_row}",
             "format": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
            # Вес и КБЖУ — автоформат («General»). Паттерн вида «0.##» рисует у
            # целых хвост разделителя («180,» вместо «180»), проверено на живом
            # листе. Задаём явно, а не пропускаем: clear() значения стирает, а
            # формат ячейки оставляет, и прилипший с прошлого прогона всплывёт.
            {"range": f"F{FIRST_DATA_ROW}:J{last_row}",
             "format": {"numberFormat": {"type": "NUMBER", "pattern": "General"}}},
            # Сбрасываем заливку по всей колонке маржи, иначе подсветка от
            # прошлого прогона останется на блюде, у которого всё уже хорошо.
            {"range": f"E{FIRST_DATA_ROW}:E{last_row}",
             "format": {"backgroundColor": _WHITE}},
        ]
    red, yellow = _highlight_ranges(table)
    for cell in red:
        fmt.append({"range": cell, "format": {"backgroundColor": _RED}})
    for cell in yellow:
        fmt.append({"range": cell, "format": {"backgroundColor": _YELLOW}})

    try:
        ws.batch_format(fmt)
    except Exception:
        # Числа важнее оформления: потерять форматирование не страшно,
        # уронить из-за него пересчёт — страшно.
        logger.exception("[расчётка] не смог применить форматирование")

    try:
        ws.freeze(rows=2)
    except Exception:
        logger.exception("[расчётка] не смог закрепить шапку")

    logger.info(
        f"[расчётка] лист «{title}»: {len(table.rows)} блюд, "
        f"красных {len(red)}, жёлтых {len(yellow)}, просевших {len(drops)}"
    )
    return {
        "sheet": title,
        "status": status,
        "count": len(table.rows),
        "loss": [r.name for r in table.rows if r.is_loss],
        "low_margin": [
            r.name for r in table.rows
            if r.is_low_margin(settings.pricing_margin_warn_percent)
        ],
        "drops": drops,
        "no_price": [r.name for r in table.rows if r.price is None],
        "kept_prices": [r.name for r in table.rows if r.price_from_sheet],
        "skipped": table.skipped,
        "warnings": table.warnings,
        "url": f"https://docs.google.com/spreadsheets/d/{settings.google_sheets_id}",
    }
