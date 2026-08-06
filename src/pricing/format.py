"""Сводка по расчётке — детерминированный текст для Telegram.

Как и в src/llm/format.py: числа собирает Python, LLM выводит результат дословно.
Вывод рассчитан на parse_mode=HTML, поэтому чужой текст экранируем.
"""
import html


def _esc(s) -> str:
    return html.escape(str(s), quote=False)


def _listed(names: list[str], limit: int = 10) -> str:
    shown = ", ".join(_esc(n) for n in names[:limit])
    if len(names) > limit:
        shown += f" и ещё {len(names) - limit}"
    return shown


def format_pricing_result(results: list[dict]) -> str:
    """Сводка после пересчёта одного или обоих листов."""
    parts: list[str] = []
    url = None

    for r in results:
        if r.get("error"):
            parts.append(f"❌ «{_esc(r['sheet'])}»: {_esc(r['error'])}")
            continue
        url = r.get("url") or url
        head = f"«{_esc(r['sheet'])}» — {r['count']} блюд"
        lines = [head]

        if r["loss"]:
            lines.append(
                f"🔴 Себестоимость выше цены ({len(r['loss'])}): {_listed(r['loss'])}\n"
                f"   Почти всегда это ошибка в данных — не тот вес или единица "
                f"измерения у ингредиента."
            )
        if r["low_margin"]:
            lines.append(
                f"🟡 Маржа ниже порога ({len(r['low_margin'])}): "
                f"{_listed(r['low_margin'])}"
            )
        if r["drops"]:
            drops = ", ".join(
                f"{_esc(name)} {float(was):.1f}% → {float(now):.1f}%"
                for name, was, now in r["drops"][:10]
            )
            if len(r["drops"]) > 10:
                drops += f" и ещё {len(r['drops']) - 10}"
            lines.append(
                f"📉 Маржа просела с прошлого пересчёта ({len(r['drops'])}): {drops}\n"
                f"   Похоже на подорожание сырья — стоит проверить закупку."
            )
        if r["no_price"]:
            lines.append(
                f"Без цены ({len(r['no_price'])}): {_listed(r['no_price'])} — "
                f"их и должен заполнить коммерческий отдел."
            )
        if r["kept_prices"]:
            lines.append(
                f"Сохранил цены, вписанные в таблицу ({len(r['kept_prices'])}): "
                f"{_listed(r['kept_prices'])}"
            )
        if r["skipped"]:
            lines.append(
                f"Пропустил без состава ({len(r['skipped'])}): {_listed(r['skipped'])}"
            )
        for w in r.get("warnings") or []:
            lines.append(f"⚠️ {_esc(w)}")

        if len(lines) == 1:
            lines.append("Всё в порядке: убыточных и низкомаржинальных нет.")
        parts.append("\n".join(lines))

    if url:
        parts.append(f"Таблица: {url}")
    return "\n\n".join(parts) if parts else "Нечего пересчитывать."
