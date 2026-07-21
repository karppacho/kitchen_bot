"""Хранилище мониторинга конкурентов — локальный SQLite (data/competitors.db).

Все функции СИНХРОННЫЕ: объёмы крошечные (сотни строк раз в неделю),
из async-кода вызываются через run_in_executor — тот же паттерн, что gspread.
Соединения короткоживущие (with sqlite3.connect), поэтому вопросов
check_same_thread не возникает.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from src.competitors.comparator import norm_name, norm_weight
from src.competitors.models import Competitor, Diff, ExtractedItem
from src.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS competitors (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    url          TEXT NOT NULL UNIQUE,
    menu_url     TEXT NOT NULL,
    city         TEXT NOT NULL DEFAULT 'Москва',
    fetch_method TEXT NOT NULL DEFAULT 'playwright',
    active       INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    taken_at      TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'auto',
    status        TEXT NOT NULL,
    items_count   INTEGER,
    raw_chars     INTEGER,
    raw_path      TEXT,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS snapshot_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    category    TEXT,
    item        TEXT NOT NULL,
    norm_name   TEXT NOT NULL,
    weight      TEXT,
    price_rub   REAL,
    composition TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_snapshot ON snapshot_items(snapshot_id);

CREATE TABLE IF NOT EXISTS price_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id   INTEGER NOT NULL REFERENCES competitors(id),
    detected_at     TEXT NOT NULL,
    change_type     TEXT NOT NULL,
    item            TEXT NOT NULL,
    weight          TEXT,
    old_price       REAL,
    new_price       REAL,
    delta_rub       REAL,
    delta_percent   REAL,
    old_snapshot_id INTEGER,
    new_snapshot_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_changes_comp ON price_changes(competitor_id, detected_at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    path = Path(settings.competitors_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.execute("PRAGMA user_version = 1")
    return conn


def _row_to_competitor(row: sqlite3.Row) -> Competitor:
    return Competitor(
        id=row["id"], name=row["name"], url=row["url"], menu_url=row["menu_url"],
        city=row["city"], fetch_method=row["fetch_method"],
        active=bool(row["active"]), added_at=row["added_at"],
    )


def add_competitor(
    name: str,
    url: str,
    menu_url: str,
    city: str = "Москва",
    fetch_method: str = "playwright",
) -> Competitor:
    """Добавить конкурента. Если url уже есть (в т.ч. деактивированный) — обновить и включить."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO competitors (name, url, menu_url, city, fetch_method, active, added_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(url) DO UPDATE SET
                name = excluded.name,
                menu_url = excluded.menu_url,
                city = excluded.city,
                fetch_method = excluded.fetch_method,
                active = 1
            """,
            (name, url, menu_url, city, fetch_method, _now()),
        )
        row = conn.execute("SELECT * FROM competitors WHERE url = ?", (url,)).fetchone()
    return _row_to_competitor(row)


def deactivate_competitor(url: str) -> Competitor | None:
    """Убрать конкурента из проверок (soft delete). None — если такого url нет."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM competitors WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE competitors SET active = 0 WHERE id = ?", (row["id"],))
    comp = _row_to_competitor(row)
    comp.active = False
    return comp


def list_competitors(active_only: bool = True) -> list[Competitor]:
    q = "SELECT * FROM competitors"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY id"
    with _connect() as conn:
        return [_row_to_competitor(r) for r in conn.execute(q).fetchall()]


def find_competitor(query: str) -> Competitor | None:
    """Найти активного конкурента по подстроке имени или url (для ручного фолбэка)."""
    needle = query.strip().lower()
    if not needle:
        return None
    for comp in list_competitors(active_only=True):
        if needle in comp.name.lower() or needle in comp.url.lower() or comp.url.lower() in needle:
            return comp
    return None


def save_snapshot(
    competitor_id: int,
    items: list[ExtractedItem],
    *,
    source: str = "auto",
    status: str = "ok",
    raw_chars: int | None = None,
    raw_path: str | None = None,
    error: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO snapshots (competitor_id, taken_at, source, status,
                                   items_count, raw_chars, raw_path, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (competitor_id, _now(), source, status, len(items), raw_chars, raw_path, error),
        )
        snapshot_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO snapshot_items (snapshot_id, category, item, norm_name,
                                        weight, price_rub, composition)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (snapshot_id, it.category, it.item, f"{norm_name(it.item)}|{norm_weight(it.weight)}",
                 it.weight, it.price_rub, it.composition)
                for it in items
            ],
        )
    return snapshot_id


def _load_items(conn: sqlite3.Connection, snapshot_id: int) -> list[ExtractedItem]:
    rows = conn.execute(
        "SELECT category, item, weight, price_rub, composition "
        "FROM snapshot_items WHERE snapshot_id = ? ORDER BY id",
        (snapshot_id,),
    ).fetchall()
    return [
        ExtractedItem(category=r["category"], item=r["item"], weight=r["weight"],
                      price_rub=r["price_rub"], composition=r["composition"])
        for r in rows
    ]


def latest_ok_snapshot(
    competitor_id: int,
) -> tuple[int, str, list[ExtractedItem], str | None] | None:
    """Последний успешный срез (status='ok'): (snapshot_id, taken_at, items, raw_path).

    'suspect' и упавшие срезы намеренно не берутся в базу сравнения —
    иначе один плохой прогон породил бы стену ложных «позиция пропала».
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, taken_at, raw_path FROM snapshots "
            "WHERE competitor_id = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
            (competitor_id,),
        ).fetchone()
        if row is None:
            return None
        return row["id"], row["taken_at"], _load_items(conn, row["id"]), row["raw_path"]


def last_check_info(competitor_id: int) -> tuple[str, str] | None:
    """(taken_at, status) последней попытки проверки — для /list_competitors."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT taken_at, status FROM snapshots "
            "WHERE competitor_id = ? ORDER BY id DESC LIMIT 1",
            (competitor_id,),
        ).fetchone()
    return (row["taken_at"], row["status"]) if row else None


def save_changes(
    competitor_id: int,
    diffs: list[Diff],
    old_snapshot_id: int | None,
    new_snapshot_id: int,
) -> None:
    if not diffs:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO price_changes (competitor_id, detected_at, change_type, item, weight,
                                       old_price, new_price, delta_rub, delta_percent,
                                       old_snapshot_id, new_snapshot_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (competitor_id, _now(), d.change_type, d.item, d.weight,
                 d.old_price, d.new_price, d.delta_rub, d.delta_percent,
                 old_snapshot_id, new_snapshot_id)
                for d in diffs
            ],
        )


def latest_items_for_export() -> list[tuple[Competitor, str, list[ExtractedItem]]]:
    """Последний ok-срез каждого активного конкурента — для листа «Конкуренты»."""
    result = []
    for comp in list_competitors(active_only=True):
        snap = latest_ok_snapshot(comp.id)
        if snap is not None:
            _snapshot_id, taken_at, items, _raw_path = snap
            result.append((comp, taken_at, items))
    return result
