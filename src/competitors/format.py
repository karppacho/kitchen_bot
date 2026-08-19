"""Детерминированная сводка мониторинга конкурентов для Telegram.

Как и в src/llm/format.py: числа собирает Python, строка показывается дословно.
Вывод под parse_mode=HTML, весь пользовательский текст экранируется.
"""
import html
from datetime import datetime

from src.competitors.models import CheckSiteResult, Diff

# Больше диффов на сайт в Telegram не показываем — полный список уходит в Sheets
MAX_DIFFS_SHOWN = 15

# Ручной срез старше этого — данные пора обновить (Бургер Кинг меняет меню чаще)
STALE_AFTER_DAYS = 14


def _waiting_line(r: CheckSiteResult) -> str:
    """Строка про конкурента в ручном режиме — с возрастом последних данных."""
    name = f"  • <b>{_esc(r.competitor_name)}</b>"
    if r.stale_days is None:
        return f"{name} — данных ещё нет, пришли сохранённый HTML страницы меню"
    if r.stale_days == 0:
        return f"{name} — данные свежие (сегодня)"
    days = r.stale_days
    mark = " ⚠️ пора обновить" if days >= STALE_AFTER_DAYS else ""
    return f"{name} — последние данные {days} дн. назад{mark}"


def _esc(s) -> str:
    return html.escape(str(s), quote=False)


def _price(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:g}"


def _diff_line(d: Diff) -> str:
    name = d.item + (f" {d.weight}" if d.weight else "")
    if d.change_type in ("price_up", "price_down"):
        arrow = "↑" if d.change_type == "price_up" else "↓"
        pct = f", {d.delta_percent:+.1f}%" if d.delta_percent is not None else ""
        return (f"  {arrow} {_esc(name)}: {_price(d.old_price)} → {_price(d.new_price)} ₽ "
                f"({d.delta_rub:+g} ₽{pct})")
    if d.change_type == "item_added":
        price = f" — {_price(d.new_price)} ₽" if d.new_price is not None else ""
        return f"  + новинка: {_esc(name)}{price}"
    return f"  − пропала из меню: {_esc(name)}"


def format_check_summary(results: list[CheckSiteResult], when: datetime) -> str:
    """Сводка прогона: по сайтам — изменения; отдельным блоком — кто не проверился."""
    lines: list[str] = [f"<b>Мониторинг конкурентов — {when.strftime('%d.%m.%Y')}</b>", ""]
    failed: list[str] = []
    waiting: list[str] = []

    for r in results:
        # Ручной режим — это не сбой, а ожидание файла от шефа. В одной куче
        # с настоящими поломками он выглядел как вечная ошибка, и его перестали замечать.
        if r.status == "skipped":
            waiting.append(_waiting_line(r))
            continue
        if r.status in ("fetch_failed", "extract_failed"):
            failed.append(f"  • {_esc(r.competitor_name)} ({_esc(r.competitor_url)}) — {_esc(r.error or r.status)}")
            continue

        suspect = " ⚠️ подозрительно мало позиций — проверь вручную" if r.status == "suspect" else ""
        if r.first_snapshot:
            lines.append(
                f"<b>{_esc(r.competitor_name)}</b>: первый срез, {r.items_count} позиций "
                f"— сравнивать пока не с чем{suspect}"
            )
            lines.append("")
            continue

        n = len(r.diffs)
        changes = "без существенных изменений" if n == 0 else f"изменений: {n}"
        lines.append(f"<b>{_esc(r.competitor_name)}</b>: {r.items_count} позиций, {changes}{suspect}")
        # Сначала цены (важнее), потом новинки/пропажи
        order = {"price_up": 0, "price_down": 1, "item_added": 2, "item_removed": 3}
        shown = sorted(r.diffs, key=lambda d: order.get(d.change_type, 9))[:MAX_DIFFS_SHOWN]
        lines.extend(_diff_line(d) for d in shown)
        if n > MAX_DIFFS_SHOWN:
            lines.append(f"  … и ещё {n - MAX_DIFFS_SHOWN} изменений (полный список — в таблице)")
        lines.append("")

    if waiting:
        lines.append("Обновляются вручную (сайт нас не пускает):")
        lines.extend(waiting)
        lines.append("Сохрани страницу меню (Ctrl+S) и пришли файл сюда.")
        lines.append("")

    if failed:
        lines.append("Не смог проверить:")
        lines.extend(failed)
        lines.append("")

    return "\n".join(lines).strip()


def format_manual_ingest(result: CheckSiteResult) -> str:
    """Мини-сводка после ручной загрузки HTML одного конкурента."""
    if result.status in ("fetch_failed", "extract_failed"):
        return f"Не получилось разобрать файл: {_esc(result.error or result.status)}"
    header = (f"Принял срез «{_esc(result.competitor_name)}» из файла: "
              f"{result.items_count} позиций.")
    if result.status == "suspect":
        header += " ⚠️ Подозрительно мало — проверь, тот ли файл."
    if result.first_snapshot:
        return header + " Это первый срез — сравнивать пока не с чем."
    if not result.diffs:
        return header + " Существенных изменений нет."
    lines = [header, f"Изменений: {len(result.diffs)}"]
    lines.extend(_diff_line(d) for d in result.diffs[:MAX_DIFFS_SHOWN])
    if len(result.diffs) > MAX_DIFFS_SHOWN:
        lines.append(f"  … и ещё {len(result.diffs) - MAX_DIFFS_SHOWN}")
    return "\n".join(lines)
