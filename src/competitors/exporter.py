"""Экспорт мониторинга в ОТДЕЛЬНУЮ Google-таблицу (не рабочую таблицу шефа!).

Лист «Конкуренты» — текущий срез (полная перезапись при каждом экспорте).
Лист «История изменений» — только дозапись диффов прогона.
Таблица целиком принадлежит боту, поэтому clear()+update здесь допустимы —
правило «snapshot + rollback» защищает именно рабочую таблицу шефа.
"""
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from loguru import logger

from src.competitors import storage
from src.competitors.models import CheckSiteResult
from src.config import settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

SNAPSHOT_SHEET = "Конкуренты"
HISTORY_SHEET = "История изменений"

_SNAPSHOT_HEADER = ["Сайт", "Категория", "Позиция", "Вес", "Цена", "Состав", "Дата среза"]
_HISTORY_HEADER = ["Сайт", "Позиция", "Вес", "Было", "Стало",
                   "Изменение ₽", "Изменение %", "Тип", "Дата обнаружения"]

_CHANGE_TYPE_RU = {
    "price_up": "подорожание",
    "price_down": "подешевение",
    "item_added": "новинка",
    "item_removed": "пропала из меню",
}


def _open_spreadsheet() -> gspread.Spreadsheet:
    if not settings.competitors_sheets_id:
        raise RuntimeError("COMPETITORS_SHEETS_ID не задан в .env")
    creds = Credentials.from_service_account_file(
        settings.google_service_account_json_path, scopes=SCOPES,
    )
    return gspread.authorize(creds).open_by_key(settings.competitors_sheets_id)


def _get_or_create_ws(sh: gspread.Spreadsheet, title: str, header: list[str]) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=len(header) + 1)
        ws.append_row(header, value_input_option="USER_ENTERED")
        return ws


def export_snapshot(sh: gspread.Spreadsheet | None = None) -> str:
    """Полная перезапись листа «Конкуренты» последними ok-срезами. Возвращает URL."""
    sh = sh or _open_spreadsheet()
    ws = _get_or_create_ws(sh, SNAPSHOT_SHEET, _SNAPSHOT_HEADER)

    rows: list[list] = [_SNAPSHOT_HEADER]
    for comp, taken_at, items in storage.latest_items_for_export():
        date = taken_at[:10]
        for it in items:
            rows.append([
                comp.url, it.category or "", it.item, it.weight or "",
                it.price_rub if it.price_rub is not None else "",
                it.composition or "", date,
            ])

    ws.clear()
    ws.update(values=rows, range_name="A1", value_input_option="USER_ENTERED")
    logger.info(f"[конкуренты] лист «{SNAPSHOT_SHEET}»: {len(rows) - 1} строк")
    return f"https://docs.google.com/spreadsheets/d/{settings.competitors_sheets_id}"


def append_changes(results: list[CheckSiteResult], sh: gspread.Spreadsheet | None = None) -> None:
    """Дозапись диффов прогона в «Историю изменений»."""
    rows: list[list] = []
    today = datetime.now().strftime("%Y-%m-%d")
    for r in results:
        for d in r.diffs:
            rows.append([
                r.competitor_url, d.item, d.weight or "",
                d.old_price if d.old_price is not None else "",
                d.new_price if d.new_price is not None else "",
                d.delta_rub if d.delta_rub is not None else "",
                d.delta_percent if d.delta_percent is not None else "",
                _CHANGE_TYPE_RU.get(d.change_type, d.change_type), today,
            ])
    if not rows:
        return
    sh = sh or _open_spreadsheet()
    ws = _get_or_create_ws(sh, HISTORY_SHEET, _HISTORY_HEADER)
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info(f"[конкуренты] лист «{HISTORY_SHEET}»: +{len(rows)} строк")


def export_run(results: list[CheckSiteResult]) -> str:
    """Полный экспорт после прогона: срез перезаписать, диффы дозаписать."""
    sh = _open_spreadsheet()
    url = export_snapshot(sh)
    append_changes(results, sh)
    return url
