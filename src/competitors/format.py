"""Детерминированная сводка мониторинга конкурентов для Telegram.

Как и в src/llm/format.py: числа собирает Python, строка показывается дословно.
Вывод под parse_mode=HTML, весь пользовательский текст экранируется.
"""
import html
from datetime import datetime

from src.competitors.models import CheckSiteResult, Diff

# Больше диффов на сайт в Telegram не показываем — полный список уходит в Sheets
MAX_DIFFS_SHOWN = 15


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

    for r in results:
        if r.status in ("fetch_failed", "extract_failed", "skipped"):
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
