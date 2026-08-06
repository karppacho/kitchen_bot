"""Детерминированное форматирование числовых ответов.

ВАЖНО: здесь собирается готовая к показу строка `display` для числовых функций
(UC блюда, сравнение маржи, симуляция цены). Смысл — НЕ давать LLM перепечатывать
числа руками (она это делает с ошибками). Калькулятор посчитал, тут красиво
оформили, LLM выводит результат дословно.

Вывод рассчитан на Telegram с parse_mode=HTML:
  - таблицы обёрнуты в <pre>...</pre> (моноширинный шрифт → колонки выравниваются);
  - любой текст, попадающий в ответ, экранируется (& < >).
"""
import html

RUB = "₽"


def _esc(s) -> str:
    """HTML-экранирование (& < >). Кавычки не трогаем — они валидны в тексте."""
    return html.escape(str(s), quote=False)


def _num(x, dec: int = 2) -> str:
    return f"{float(x):.{dec}f}"


def _num_or_dash(x, dec: int = 2) -> str:
    """Число или «—», если данных нет (например, маржа у блюда без цены меню)."""
    return "—" if x is None else _num(x, dec)


def _sign(x, dec: int = 1) -> str:
    """Число со знаком: +11.7 / -3.2."""
    return f"{float(x):+.{dec}f}"


def _table(headers: list[str], rows: list[list[str]], aligns: list[str]) -> str:
    """Моноширинная таблица в <pre>.

    Ширины и выравнивание считаются по сырым строкам, а HTML-экранирование
    применяется к собранному блоку целиком — Telegram рендерит сущности обратно,
    поэтому визуальное выравнивание сохраняется.
    """
    grid = [headers] + rows
    widths = [max(len(str(r[i])) for r in grid) for i in range(len(headers))]

    def fmt(cells: list[str]) -> str:
        out = []
        for cell, w, a in zip(cells, widths, aligns):
            s = str(cell)
            out.append(s.rjust(w) if a == "r" else s.ljust(w))
        return "  ".join(out).rstrip()

    body = "\n".join(fmt(r) for r in grid)
    return "<pre>\n" + _esc(body) + "\n</pre>"


def _with_margin_delta(dishes: list[dict]) -> list[dict]:
    """Только блюда с посчитанной дельтой маржи.

    У блюда без цены меню маржи нет (delta_margin_percent = None), и его нельзя
    подставлять в min/max — сравнение None с числом падает.
    """
    return [d for d in dishes if d.get("delta_margin_percent") is not None]


def _dish_delta_table(dishes: list[dict]) -> str:
    """Таблица «блюдо → UC было→стало, маржа было→стало (дельта пп)».

    Общая для симуляции цены и замены ингредиента.
    """
    rows = []
    for d in dishes:
        uc = f"{_num(d['old_uc'])} → {_num(d['new_uc'])}"
        if d.get("old_margin_percent") is None:
            # Цены меню нет — маржи не существует, но дельта UC осмысленна
            marg = "— (нет цены меню)"
        else:
            marg = (
                f"{_num(d['old_margin_percent'], 1)}% → {_num(d['new_margin_percent'], 1)}% "
                f"({_sign(d['delta_margin_percent'])} пп)"
            )
        rows.append([d["dish_id"], str(d["dish_name"])[:22], uc, marg])
    return _table(["ID", "Блюдо", "UC, ₽", "Маржа"], rows, ["l", "l", "r", "l"])


def format_dish_uc(r: dict) -> str:
    """display для calculate_dish_uc."""
    if r.get("price_menu_rub") is None:
        # Блюдо без цены меню (соус-топпинг, новинка): себестоимость есть,
        # маржи нет. Не пишем «0%» — шеф не должен принять это за настоящий ноль.
        head = [
            f"{_esc(r['dish_name'])} ({r['dish_id']})",
            f"Себестоимость: {_num(r['uc_rub'])} {RUB}",
            f"Выход: {_num(r['output_grams'], 1)} г",
            "Цена меню не заполнена — маржу посчитать не могу",
        ]
    else:
        head = [
            f"{_esc(r['dish_name'])} ({r['dish_id']})",
            f"UC: {_num(r['uc_rub'])} {RUB} "
            f"({_num(r['uc_percent'], 1)}% от {_num(r['price_menu_rub'], 0)} {RUB})",
            f"Маржа: {_num(r['margin_rub'])} {RUB} ({_num(r['margin_percent'], 1)}%)",
            f"Выход: {_num(r['output_grams'], 1)} г",
        ]
    if "kcal" in r:
        head.append(
            f"КБЖУ блюда: Б {_num(r['proteins_g'], 1)} / Ж {_num(r['fats_g'], 1)} / "
            f"У {_num(r['carbs_g'], 1)} / {_num(r['kcal'], 0)} ккал"
        )
    parts = ["\n".join(head)]

    ings = r.get("ingredients") or []
    if ings:
        rows = []
        for it in ings:
            if it.get("type") == "Упаковка":
                weight = f"{_num(it['weight_g'], 0)} шт"
            else:
                weight = f"{_num(it['weight_g'], 1)} г"
            share = (
                f"{_num(it['share_percent'], 1)}%"
                if it.get("share_percent") is not None else "-"
            )
            rows.append([
                str(it["name"])[:22],
                weight,
                f"{_num(it['cost_rub'])} {RUB}",
                share,
            ])
        parts.append(_table(
            ["Состав", "Вес", "Стоим.", "Доля"], rows, ["l", "r", "r", "r"]
        ))

    warns = r.get("warnings") or []
    if warns:
        parts.append("Замечания:\n" + "\n".join("- " + _esc(w) for w in warns))

    return "\n\n".join(parts)


def format_simulate(r: dict) -> str:
    """display для simulate_price_change."""
    unit = r.get("unit", "")
    head = (
        f"{_esc(r['ingredient'])}: {_num(r['old_price'])} → {_num(r['new_price'])} "
        f"{RUB}/{unit} ({_sign(r['price_delta_percent'])}%)"
    )

    dishes = r.get("dishes") or []
    if not dishes:
        return (
            head + "\n\nНи одно блюдо с этим ингредиентом не имеет состава в ТТК — "
            "пересчитывать нечего."
        )

    table = _dish_delta_table(dishes)

    # Сводку (кто сильнее всего затронут) тоже считаем здесь, не отдаём LLM.
    rated = _with_margin_delta(dishes)
    if not rated:
        summary = "Ни у одного из блюд не заполнена цена меню — маржу сравнить не с чем."
    elif float(r["new_price"]) >= float(r["old_price"]):
        worst = min(rated, key=lambda d: d["delta_margin_percent"])
        summary = (
            f"Сильнее всего пострадает: {_esc(worst['dish_name'])} "
            f"({_sign(worst['delta_margin_percent'])} п.п. маржи)."
        )
    else:
        best = max(rated, key=lambda d: d["delta_margin_percent"])
        summary = (
            f"Сильнее всего выиграет: {_esc(best['dish_name'])} "
            f"({_sign(best['delta_margin_percent'])} п.п. маржи)."
        )

    return "\n".join([head, f"Затронуто блюд: {len(dishes)}", "", table, "", summary])


def format_compare(r: dict) -> str:
    """display для compare_dishes_margin."""
    rows = []
    for d in r.get("dishes") or []:
        rows.append([
            d["id"],
            str(d["name"])[:22],
            f"{_num(d['price_menu_rub'], 0)} {RUB}",
            f"{_num(d['uc_rub'])} {RUB}",
            f"{_num(d['margin_percent'], 1)}%",
        ])
    parts = [f"Сравнение по марже. Учтено блюд: {r.get('count', len(rows))}"]
    if rows:
        parts.append(_table(
            ["ID", "Блюдо", "Цена", "UC", "Маржа"], rows, ["l", "l", "r", "r", "r"]
        ))

    skipped = r.get("skipped_dishes") or []
    if skipped:
        # Причин пропуска две (нет состава / нет цены меню) — группируем, чтобы
        # шеф видел, что именно дозаполнить, а не общее «пропущено N».
        by_reason: dict[str, list[str]] = {}
        for d in skipped:
            by_reason.setdefault(d.get("reason") or "причина не указана", []).append(
                str(d.get("name") or d.get("id"))
            )
        lines = [f"Пропущено блюд: {len(skipped)} (маржу по ним посчитать нельзя)"]
        for reason, names in by_reason.items():
            shown = ", ".join(names[:8])
            if len(names) > 8:
                shown += f" и ещё {len(names) - 8}"
            lines.append(f"- {_esc(reason)} ({len(names)}): {_esc(shown)}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _price_unit(price, unit) -> str:
    return f"{_num(price)} {RUB}/{unit}" if price is not None else "цена не указана"


def format_replacement(r: dict) -> str:
    """display для настоящей замены ингредиента (оба ингредиента есть в ING)."""
    head = (
        f"Замена: {_esc(r['old_ingredient'])} ({_price_unit(r.get('old_price'), r.get('old_unit', ''))}) "
        f"→ {_esc(r['new_ingredient'])} ({_price_unit(r.get('new_price'), r.get('new_unit', ''))})"
    )
    dishes = r.get("dishes") or []
    if not dishes:
        return (
            head + "\n\nНи одно блюдо с исходным ингредиентом не имеет состава в ТТК — "
            "пересчитывать нечего."
        )

    parts = [head, f"Затронуто блюд: {len(dishes)}", "", _dish_delta_table(dishes)]

    # Изменение калорийности (если поменялось) — отдельной строкой, без захламления таблицы
    kcal_bits = [
        f"{d['dish_id']} {_num(d['old_kcal'], 0)}→{_num(d['new_kcal'], 0)}"
        for d in dishes
        if round(d.get("old_kcal", 0)) != round(d.get("new_kcal", 0))
    ]
    if kcal_bits:
        parts += ["", "Ккал/блюдо: " + ", ".join(kcal_bits)]

    rated = _with_margin_delta(dishes)
    if rated:
        best = max(rated, key=lambda d: d["delta_margin_percent"])
        worst = min(rated, key=lambda d: d["delta_margin_percent"])
        pick = (
            worst
            if abs(worst["delta_margin_percent"]) >= abs(best["delta_margin_percent"])
            else best
        )
        parts += [
            "",
            f"Сильнее всего меняется маржа: {_esc(pick['dish_name'])} "
            f"({_sign(pick['delta_margin_percent'])} п.п.).",
        ]
    return "\n".join(parts)


def format_ttk_preview(context: dict, meta: dict) -> str:
    """display для ПРЕВЬЮ ТТК (шаг «проверь → подтверди», файл ещё не рендерим)."""
    # Реквизитов сети и даты в шапке нет: в форме шефа (август 2026) этих
    # блоков не осталось, и в контексте их больше не собирают.
    head = [
        f"ТТК № {context['ttk_number']} — {_esc(context['dish_name'])}",
        f"Выход: {context['dish_output_g']} г",
    ]
    parts = ["\n".join(head)]

    rows = [
        [str(i["name"])[:28], str(i["brutto"]), str(i["netto"])]
        for i in context.get("ingredients", [])
    ]
    if rows:
        parts.append(_table(["Ингредиент", "Брутто, г", "Нетто, г"], rows, ["l", "r", "r"]))

    k100 = context.get("kbju_per_100g", {})
    kp = context.get("kbju_per_portion", {})
    parts.append(
        "КБЖУ на 100 г: Б {} / Ж {} / У {} / {} ккал\n"
        "КБЖУ на порцию: Б {} / Ж {} / У {} / {} ккал".format(
            k100.get("белки", "—"), k100.get("жиры", "—"),
            k100.get("углеводы", "—"), k100.get("ккал", "—"),
            kp.get("белки", "—"), kp.get("жиры", "—"),
            kp.get("углеводы", "—"), kp.get("ккал", "—"),
        )
    )

    warns = meta.get("warnings") or []
    if warns:
        parts.append("Замечания:\n" + "\n".join("- " + _esc(w) for w in warns))

    parts.append(
        "Проверь рецептуру. Напиши «да» — сгенерирую .docx "
        "(техпроцесс и органолептику допишу автоматически)."
    )
    return "\n\n".join(parts)


def _dish_composition_table(ings: list[dict]) -> str:
    """Таблица состава блюда (имя/вес/стоимость/доля). Общая для превью и факта."""
    rows = []
    for it in ings:
        wpu = it.get("weight_per_piece_g")
        if it.get("type") == "Упаковка":
            weight = f"{_num(it['weight_g'], 0)} шт"
        elif it.get("unit") == "шт" and wpu:
            # Штучный ингредиент: показываем и штуки, и граммы — видно, что выбрано
            pcs = it["weight_g"] / wpu
            weight = f"{float(pcs):g} шт ({_num(it['weight_g'], 0)} г)"
        else:
            weight = f"{_num(it['weight_g'], 1)} г"
        share = (
            f"{_num(it['share_percent'], 1)}%"
            if it.get("share_percent") is not None else "-"
        )
        rows.append([
            str(it["name"])[:22], weight, f"{_num(it['cost_rub'])} {RUB}", share,
        ])
    return _table(["Состав", "Вес", "Стоим.", "Доля"], rows, ["l", "r", "r", "r"])


def _dish_head(r: dict, title: str) -> list[str]:
    head = [
        f"{title} {_esc(r['dish_name'])}",
    ]
    if r.get("price_menu_rub") is None:
        # Блюдо заводят до назначения цены — обычный R&D-сценарий:
        # сначала посчитать фудкост, потом решить, за сколько продавать.
        head += [
            f"ID: {r['dish_id']}  |  Категория: {_esc(r.get('category') or '—')}",
            f"Себестоимость: {_num(r['uc_rub'])} {RUB}",
            f"Выход: {_num(r['output_grams'], 1)} г",
            "Цена меню не заполнена — маржу посчитать не могу",
        ]
    else:
        head += [
            f"ID: {r['dish_id']}  |  Категория: {_esc(r.get('category') or '—')}  |  "
            f"Цена: {_num(r['price_menu_rub'], 0)} {RUB}",
            f"UC: {_num(r['uc_rub'])} {RUB} ({_num(r['uc_percent'], 1)}%)  |  "
            f"Маржа: {_num(r['margin_rub'])} {RUB} ({_num(r['margin_percent'], 1)}%)",
            f"Выход: {_num(r['output_grams'], 1)} г",
        ]
    return head


def format_category_prompt(
    dish_name: str, categories: list, unknown: str | None = None
) -> str:
    """display для вопроса «какая категория» при создании блюда.

    Список берётся из таблицы, а не из головы LLM: раньше модель молча
    подставляла категорию сама и одно блюдо получало то «Закуска», то «Бургер».
    """
    rows = [[_esc(name), str(count)] for name, count in categories]
    parts = []
    if unknown:
        parts.append(
            f"Категории «{_esc(unknown)}» в таблице ещё нет. "
            f"Выбери из существующих:"
        )
    else:
        parts.append(f"Какая категория у блюда «{_esc(dish_name)}»?")
    if rows:
        parts.append(_table(["Категория", "Блюд"], rows, ["l", "r"]))
    if unknown:
        parts.append(
            f"Если «{_esc(unknown)}» — действительно новая категория, "
            f"подтверди: «да, новая категория»."
        )
    return "\n\n".join(parts)


def format_ttk_batch_preview(status: str, ready: list, skipped: list) -> str:
    """display для ПРЕВЬЮ пачки ТТК: что войдёт, что пропустим."""
    title = "новинкам (статус «разработка»)" if status == "разработка" else f"блюдам «{status}»"
    parts = [f"ТТК по {title}: соберу {len(ready)} шт."]
    rows = [[d.id, str(d.name)[:34], str(d.category or "—")[:16]] for d in ready]
    if rows:
        parts.append(_table(["ID", "Блюдо", "Категория"], rows, ["l", "l", "l"]))
    if skipped:
        names = ", ".join(f"{d.id} {d.name}" for d in skipped[:8])
        if len(skipped) > 8:
            names += f" и ещё {len(skipped) - 8}"
        parts.append(f"Пропущу — не заполнен состав в ТТК ({len(skipped)}): {_esc(names)}")
    parts.append(
        "Техпроцесс и органолептику сгенерирую автоматически, это займёт "
        f"около {max(1, len(ready) * 15 // 60)}–{max(1, len(ready) * 25 // 60) + 1} мин. "
        "Делаем? Напиши «да»."
    )
    return "\n\n".join(parts)


def format_ttk_batch_result(done: list, failed: list, poor_kbju: list) -> str:
    """display после пакетной генерации: что готово, что нет, где КБЖУ пустое."""
    parts = [f"Готово карт: {len(done)}. Отправляю файлами."]
    if done:
        parts.append("\n".join(f"- {_esc(name)}" for name, _ in done))
    if failed:
        parts.append(
            "Не смог сформировать:\n"
            + "\n".join(f"- {_esc(name)}: {_esc(why)}" for name, why in failed)
        )
    if poor_kbju:
        parts.append(
            "ВНИМАНИЕ: КБЖУ почти не заполнено у "
            + ", ".join(_esc(n) for n in poor_kbju)
            + " — цифрам в этих картах доверять нельзя, заполни КБЖУ и перегенерируй."
        )
    parts.append(
        "Техпроцесс и органолептику сгенерировал автоматически — проверь перед печатью."
    )
    return "\n\n".join(parts)


def format_dish_preview(r: dict) -> str:
    """display для ПРЕВЬЮ создания блюда (запись ещё не произошла)."""
    parts = ["\n".join(_dish_head(r, "НОВОЕ БЛЮДО (превью) —"))]
    if r.get("ingredients"):
        parts.append(_dish_composition_table(r["ingredients"]))
    warns = r.get("warnings") or []
    if warns:
        parts.append("Замечания:\n" + "\n".join("- " + _esc(w) for w in warns))
    if r.get("packaging_missing"):
        # Упаковка есть у ВСЕХ блюд с составом в таблице, поэтому её отсутствие —
        # почти наверняка забывчивость, а не решение. Спрашиваем до записи.
        parts.append(
            "⚠️ Упаковка не указана — в UC её нет.\n"
            "Назови упаковку («коробка для пиццы», «контейнер для соуса») — пересчитаю. "
            "Если она правда не нужна, напиши «да, без упаковки»."
        )
    else:
        parts.append("Создать блюдо с таким составом? Напиши «да» — запишу в таблицу.")
    return "\n\n".join(parts)


def format_dish_created(r: dict) -> str:
    """display после успешной записи блюда в Sheets."""
    parts = ["\n".join(_dish_head(r, "БЛЮДО СОЗДАНО —"))]
    if r.get("ingredients"):
        parts.append(_dish_composition_table(r["ingredients"]))
    warns = r.get("warnings") or []
    if warns:
        parts.append("Замечания:\n" + "\n".join("- " + _esc(w) for w in warns))
    tail = "Записано в листы «Блюда» и «ТТК». Снимок таблицы сохранён в backups/."
    if r.get("packaging_missing"):
        tail += "\nБлюдо записано БЕЗ упаковки — её себестоимость в UC не вошла."
    if r.get("price_menu_rub") is None:
        tail += (
            "\nЦена меню не заполнена — проставь её в таблице, "
            "и я посчитаю маржу."
        )
    parts.append(tail)
    return "\n\n".join(parts)


def format_replacement_theoretical(r: dict, new_name: str) -> str:
    """display для теоретической замены: нового ингредиента нет в ING, оцениваем
    только по цене (r — результат simulate_price_change по старому ингредиенту).

    Оговорку про вес/потери/КБЖУ зашиваем здесь детерминированно — не полагаемся на LLM.
    """
    unit = r.get("unit", "")
    head = (
        f"Замена {_esc(r['ingredient'])} → {_esc(new_name)} (нет в базе) — "
        f"оценка ТОЛЬКО по цене {_num(r['new_price'])} {RUB}/{unit}."
    )
    dishes = r.get("dishes") or []
    caveat = (
        "Вес, потери и КБЖУ оставлены как у исходного ингредиента; полноценная "
        "замена возможна, когда ингредиент появится в базе."
    )
    if not dishes:
        return head + "\n\nНи одно блюдо с этим ингредиентом не имеет состава в ТТК.\n\n" + caveat

    return "\n".join([
        head,
        f"Затронуто блюд: {len(dishes)}",
        "",
        _dish_delta_table(dishes),
        "",
        caveat,
    ])
