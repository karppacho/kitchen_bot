"""LLM-клиент: обёртка над OpenAI SDK для работы с polza.ai.

Главная функция — `chat()`. Принимает текст от пользователя, ведёт диалог
с моделью (включая tool calling), возвращает финальный текстовый ответ.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from decimal import Decimal

import httpx
from loguru import logger
from openai import OpenAI

from src.config import settings
from src.data.sheets import get_data, reload_data
from src.data.models import Dish, TTKRow
from src.llm.tools import TOOLS
from src.llm.format import (
    format_dish_uc,
    format_simulate,
    format_compare,
    format_replacement,
    format_replacement_theoretical,
    format_ttk_preview,
    format_ttk_batch_preview,
    format_ttk_batch_result,
    format_category_prompt,
    format_dish_preview,
    format_dish_created,
)
from src.calc.costs import (
    opt_float,
    calculate_dish_uc,
    calculate_uc_for_composition,
    find_dishes_with_ingredient,
    simulate_price_change,
    simulate_replacement,
    kbju_coverage_status,
)
from src.ttk.builder import build_ttk_context, render_ttk
from src.llm.history import get_history, append_turn

# Один клиент на весь процесс.
#
# Таймауты раздельные. Плоский timeout=60 означал 60 секунд на КАЖДУЮ фазу, и
# при недоступной сети SDK успевал сделать три попытки по 60 с: 04.08.2026 шеф
# ждал ответа 3 минуты 1 секунду, всё это время не видя ничего (индикатор
# «печатает…» в Telegram живёт ~5 секунд). TLS-рукопожатие, не сложившееся за
# 10 секунд, не сложится и за 60 — поэтому connect отделён от чтения. Долгий
# read оставляем: LLM думает медленно, и это нормально.
client = OpenAI(
    base_url=settings.polza_base_url,
    api_key=settings.polza_api_key,
    timeout=httpx.Timeout(60.0, connect=10.0),
    max_retries=2,
)

# Промпты — читаем из файлов, не из кода
_PROMPTS_DIR = Path(__file__).parent / "prompts"
SYSTEM_PROMPT = (_PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
TTK_TECH_PROMPT = (_PROMPTS_DIR / "ttk_tech.md").read_text(encoding="utf-8")
TTK_ORG_PROMPT = (_PROMPTS_DIR / "ttk_organoleptic.md").read_text(encoding="utf-8")

# Куда складываем сгенерированные .docx перед отправкой в Telegram
GENERATED_TTK_DIR = Path("generated_ttk")

# Лимит шагов в цикле tool calling, чтобы избежать бесконечной петли
MAX_TOOL_LOOPS = 8

# Сколько ТТК готовы сделать за один раз. На каждую карту два запроса к LLM,
# поэтому «все активные» (126 блюд) — это ~250 запросов и десятки минут.
MAX_TTK_BATCH = 15


@dataclass
class ChatResult:
    """Результат диалога: текст ответа + пути к файлам (например, сгенерированная ТТК)."""
    text: str
    files: list[str] = field(default_factory=list)


# ================================================================
# Реализация функций, которые LLM может вызывать
# ================================================================


def _not_found_error(data, query: str) -> dict:
    """«Ингредиент не найден» — но сначала проверим строки ING без id.

    Такие строки бот не загружает (id — ключ, на него ссылается ТТК), и шефу
    важно понимать разницу между «нет в базе» и «есть, но не подхватилось».
    """
    q = query.lower().strip()
    for line_no, name in getattr(data, "ingredients_without_id", []):
        if q in name.lower() or name.lower() in q:
            return {
                "error": (
                    f"«{name}» есть в ING (строка {line_no}), но у неё не проставлен "
                    f"id в колонке A — поэтому бот её не видит. Проставь id и вызови "
                    f"/refresh, тогда смогу с ней работать."
                )
            }
    return {"error": f"Ингредиент '{query}' не найден"}


def _resolve_ingredient(data, query: str):
    """Разбор поиска ингредиента: (ingredient, error_dict).

    Возвращает (ing, None) при однозначном совпадении, либо (None, error_dict)
    с сообщением «не найдено» или «несколько, уточни» со списком кандидатов.

    Используется в tool-ах, которые работают по блюдам (find_dishes_with_ingredient,
    simulate_price_change), поэтому при неоднозначности учитываем, в скольких блюдах
    ингредиент реально используется (ТТК). Если среди совпадений ровно один задействован
    в блюдах — выбираем его автоматически: запрос всё равно про блюда, а кандидаты с
    нулевым использованием для такого вопроса бессмысленны. Это убирает перебор
    кандидатов по очереди. Кандидаты возвращаются с полем used_in_dishes и сортируются
    по убыванию использования, чтобы при настоящей неоднозначности LLM выбрал за один шаг.
    """
    matches = data.search_ingredients(query)
    if not matches:
        return None, _not_found_error(data, query)
    if len(matches) == 1:
        return matches[0], None
    # Точное совпадение по имени делает выбор однозначным — но только если оно
    # ОДНО. Двух активных тёзок (сахар id 12 и 123) точное имя не различает,
    # и брать первого попавшегося нельзя: цены у них разные.
    q = query.lower().strip()
    exact = [i for i in matches if i.name.lower() == q]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        matches = exact

    usage = {i.id: len(find_dishes_with_ingredient(data, i.id)) for i in matches}
    used = [i for i in matches if usage[i.id] > 0]
    if len(used) == 1:
        return used[0], None

    matches_sorted = sorted(matches, key=lambda i: usage[i.id], reverse=True)
    return None, {
        "error": "Найдено несколько ингредиентов, уточни название",
        "candidates": [
            {
                "id": i.id,
                "name": i.name,
                "category": i.category,
                "unit": i.unit,
                "price_per_unit_rub": (
                    float(i.price_per_unit) if i.price_per_unit is not None else None
                ),
                "used_in_dishes": usage[i.id],
            }
            for i in matches_sorted
        ],
    }


def _resolve_dish_filter(data, dish_arg: str, ing):
    """Резолв опционального фильтра по блюду: (dish_ids | None, error | None).

    Пусто → (None, None) — считаем по всем блюдам. Иначе резолвит блюдо по id/имени
    и проверяет, что оно содержит ингредиент ing. Используется в симуляции цены и замене.
    """
    dish_arg = (dish_arg or "").strip()
    if not dish_arg:
        return None, None
    dish = data.dishes.get(dish_arg) or data.dishes.get(dish_arg.upper())
    if dish is None:
        dish = data.find_dish_by_name(dish_arg)
    if dish is None:
        matches = data.find_dishes_by_query(dish_arg)
        if matches:
            return None, {
                "error": "Найдено несколько подходящих блюд, уточни",
                "candidates": [
                    {"id": d.id, "name": d.name, "category": d.category}
                    for d in matches
                ],
            }
        return None, {"error": f"Блюдо '{dish_arg}' не найдено"}
    used_ids = {d.id for d in find_dishes_with_ingredient(data, ing.id)}
    if dish.id not in used_ids:
        return None, {
            "error": (
                f"Блюдо '{dish.name}' не содержит ингредиент '{ing.name}' — "
                f"изменение по нему не влияет"
            )
        }
    return [dish.id], None


def _tool_calculate_dish_uc(args: dict) -> dict:
    """Вызов calculate_dish_uc → красивый JSON-результат."""
    data = get_data()
    query = (args.get("dish_name_or_id") or "").strip()
    if not query:
        return {"error": "Не указано название или ID блюда"}

    # Сначала пробуем как ID
    dish = data.dishes.get(query) or data.dishes.get(query.upper())
    if dish is None:
        # Иначе ищем по названию
        found = data.find_dish_by_name(query)
        if found is None:
            # Несколько подходящих?
            matches = data.find_dishes_by_query(query)
            if matches:
                return {
                    "error": "Найдено несколько подходящих блюд, уточни",
                    "candidates": [
                        {"id": d.id, "name": d.name, "category": d.category}
                        for d in matches
                    ],
                }
            return {"error": f"Блюдо '{query}' не найдено"}
        dish = found

    result = calculate_dish_uc(data, dish.id)
    if result is None:
        return {"error": "Не удалось посчитать UC"}

    out = {
        "dish_id": result.dish_id,
        "dish_name": result.dish_name,
        "price_menu_rub": opt_float(result.price_menu),
        "uc_rub": float(result.uc_rub),
        "uc_percent": opt_float(result.uc_percent),
        "margin_rub": opt_float(result.margin_rub),
        "margin_percent": opt_float(result.margin_percent),
        "output_grams": float(result.output_grams),
        "proteins_g": float(result.proteins_g),
        "fats_g": float(result.fats_g),
        "carbs_g": float(result.carbs_g),
        "kcal": float(result.kcal),
        "ingredients": [
            {
                "name": ing.name,
                "weight_g": float(ing.weight_g),
                "unit": ing.unit,
                "price_per_unit_rub": (
                    float(ing.price_per_unit)
                    if ing.price_per_unit is not None else None
                ),
                "weight_per_piece_g": (
                    float(ing.weight_per_piece_g)
                    if ing.weight_per_piece_g is not None else None
                ),
                "cost_rub": float(ing.cost_rub),
                "share_percent": (
                    float(ing.share_percent)
                    if ing.share_percent is not None else None
                ),
                "type": ing.row_type,
            }
            for ing in result.ingredients
        ],
        "warnings": result.warnings,
    }
    out["display"] = format_dish_uc(out)
    return out


def _tool_list_dishes(args: dict) -> dict:
    data = get_data()
    category = (args.get("category") or "").strip().lower()
    only_with_composition = bool(args.get("only_with_composition"))

    dishes = list(data.dishes.values())
    if category:
        dishes = [d for d in dishes if category in d.category.lower()]

    def _has_comp(dish_id: str) -> bool:
        return bool(data.ttk_by_dish.get(dish_id))

    if only_with_composition:
        dishes = [d for d in dishes if _has_comp(d.id)]

    return {
        "count": len(dishes),
        "with_composition_count": sum(1 for d in dishes if _has_comp(d.id)),
        "dishes": [
            {
                "id": d.id,
                "name": d.name,
                "category": d.category,
                "price_menu_rub": opt_float(d.price_menu),
                "status": d.status,
                "has_composition": _has_comp(d.id),
            }
            for d in dishes
        ],
    }


def _tool_find_dishes_with_ingredient(args: dict) -> dict:
    data = get_data()
    query = (args.get("ingredient_name") or "").strip()
    if not query:
        return {"error": "Не указано название ингредиента"}

    ing, err = _resolve_ingredient(data, query)
    if err is not None:
        return err

    dishes = find_dishes_with_ingredient(data, ing.id)
    return {
        "ingredient": {"id": ing.id, "name": ing.name},
        "count": len(dishes),
        "dishes": [
            {"id": d.id, "name": d.name, "category": d.category}
            for d in dishes
        ],
    }


def _tool_compare_dishes_margin(args: dict) -> dict:
    data = get_data()
    category = (args.get("category") or "").strip().lower()
    sort_by = args.get("sort_by") or "margin_percent"
    order = args.get("order") or "desc"

    dishes_to_compare = list(data.dishes.values())
    if category:
        dishes_to_compare = [
            d for d in dishes_to_compare if category in d.category.lower()
        ]

    rows = []
    skipped = []
    for d in dishes_to_compare:
        uc_result = calculate_dish_uc(data, d.id)
        if uc_result is None:
            continue
        # Пропускаем блюда без состава — у них UC=0 и маржа выходит фиктивно 100%
        if uc_result.uc_rub == 0:
            skipped.append({
                "id": d.id,
                "name": d.name,
                "reason": "состав не заполнен в ТТК",
            })
            continue
        # Без цены меню маржи не существует — сравнивать нечего
        if uc_result.margin_percent is None:
            skipped.append({
                "id": d.id,
                "name": d.name,
                "reason": "цена меню не заполнена",
            })
            continue
        rows.append({
            "id": d.id,
            "name": d.name,
            "category": d.category,
            "price_menu_rub": float(uc_result.price_menu),
            "uc_rub": float(uc_result.uc_rub),
            "uc_percent": float(uc_result.uc_percent),
            "margin_rub": float(uc_result.margin_rub),
            "margin_percent": float(uc_result.margin_percent),
        })

    rows.sort(key=lambda x: x.get(sort_by, 0), reverse=(order == "desc"))
    out = {
        "count": len(rows),
        "dishes": rows,
        "skipped_dishes": skipped,  # бот видит и может упомянуть
    }
    out["display"] = format_compare(out)
    return out


def _tool_reload_database(args: dict) -> dict:
    reload_data()
    data = get_data()
    return {
        "status": "ok",
        "ingredients": len(data.ingredients),
        "packagings": len(data.packagings),
        "dishes": len(data.dishes),
        "ttk_dishes": len(data.ttk_by_dish),
    }


def _tool_simulate_price_change(args: dict) -> dict:
    data = get_data()
    query = (args.get("ingredient_name") or "").strip()
    if not query:
        return {"error": "Не указано название ингредиента"}

    # Ровно один способ задать новую цену
    new_price_raw = args.get("new_price")
    delta_raw = args.get("delta_rub")
    mult_raw = args.get("multiplier")
    provided = [x for x in (new_price_raw, delta_raw, mult_raw) if x is not None]
    if len(provided) == 0:
        return {"error": "Нужен один из: new_price, delta_rub или multiplier"}
    if len(provided) > 1:
        return {"error": "Укажи только один из: new_price, delta_rub или multiplier"}

    ing, err = _resolve_ingredient(data, query)
    if err is not None:
        return err

    # Опциональный фильтр по конкретному блюду («что с маржой ЭТОГО блюда»)
    dish_ids, err = _resolve_dish_filter(data, args.get("dish_name_or_id", ""), ing)
    if err is not None:
        return err

    def _dec(v):
        return Decimal(str(v))

    try:
        if new_price_raw is not None:
            result = simulate_price_change(
                data, ing.id, new_price=_dec(new_price_raw), dish_ids=dish_ids
            )
        elif delta_raw is not None:
            result = simulate_price_change(
                data, ing.id, delta_rub=_dec(delta_raw), dish_ids=dish_ids
            )
        else:
            result = simulate_price_change(
                data, ing.id, multiplier=_dec(mult_raw), dish_ids=dish_ids
            )
    except Exception as e:
        return {"error": f"Не смог посчитать новую цену: {e}"}

    if "error" not in result:
        result["display"] = format_simulate(result)
    return result

def _tool_simulate_replacement(args: dict) -> dict:
    data = get_data()
    old_q = (args.get("old_ingredient_name") or "").strip()
    if not old_q:
        return {"error": "Не указан исходный ингредиент (old_ingredient_name)"}
    new_q = (args.get("new_ingredient_name") or "").strip()
    new_price_raw = args.get("new_price")
    if not new_q and new_price_raw is None:
        return {
            "error": "Укажи новый ингредиент (new_ingredient_name) или его цену (new_price)"
        }

    old_ing, err = _resolve_ingredient(data, old_q)
    if err is not None:
        return err

    dish_ids, derr = _resolve_dish_filter(data, args.get("dish_name_or_id", ""), old_ing)
    if derr is not None:
        return derr

    # Резолвим новый ингредиент в ING. ВАЖНО: не используем usage-авторезолв
    # (_resolve_ingredient) — для ингредиента-заменителя «используется в блюдах»
    # нерелевантно и может выбрать не то.
    new_matches = data.search_ingredients(new_q) if new_q else []
    exact = [i for i in new_matches if i.name.lower() == new_q.lower()]
    new_ing = None
    if exact:
        new_ing = exact[0]
    elif len(new_matches) == 1:
        new_ing = new_matches[0]
    elif len(new_matches) > 1:
        return {
            "error": "Несколько ингредиентов на замену, уточни какой",
            "candidates": [
                {"id": i.id, "name": i.name, "category": i.category, "unit": i.unit}
                for i in new_matches
            ],
        }

    if new_ing is not None:
        # Настоящая замена — оба ингредиента в базе
        result = simulate_replacement(data, old_ing.id, new_ing.id, dish_ids=dish_ids)
        if "error" not in result:
            result["display"] = format_replacement(result)
        return result

    # Нового ингредиента нет в базе → теоретическая оценка по цене
    if new_price_raw is None:
        return {
            "error": (
                f"Ингредиент '{new_q}' не найден в базе. Назови его цену за "
                f"{old_ing.unit} — посчитаю оценку по цене (вес, потери и КБЖУ "
                f"останутся как у '{old_ing.name}')."
            )
        }
    try:
        result = simulate_price_change(
            data, old_ing.id, new_price=Decimal(str(new_price_raw)), dish_ids=dish_ids
        )
    except Exception as e:
        return {"error": f"Не смог посчитать оценку: {e}"}
    if "error" not in result:
        result["display"] = format_replacement_theoretical(
            result, new_q or "новый ингредиент"
        )
    return result


def _tool_list_ingredients(args: dict) -> dict:
    """Список ингредиентов по категории или по части названия.

    Для запросов «какие у нас соусы», «покажи сыры», «что есть из моцареллы».
    """
    data = get_data()
    category = (args.get("category") or "").strip()
    query = (args.get("query") or "").strip()

    if category:
        ings = data.list_ingredients_by_category(category)
    elif query:
        ings = data.search_ingredients(query)
    else:
        ings = list(data.ingredients.values())

    return {
        "count": len(ings),
        "ingredients": [
            {
                "id": i.id,
                "name": i.name,
                "category": i.category,
                "unit": i.unit,
                "price_per_unit_rub": (
                    float(i.price_per_unit) if i.price_per_unit is not None else None
                ),
                "status": i.status,
            }
            for i in ings
        ],
    }


def _llm_complete(prompt_text: str, temperature: float = 0.4) -> str:
    """Разовый вызов модели без tools — для генерации текста ТТК."""
    resp = client.chat.completions.create(
        model=settings.llm_model,
        messages=[{"role": "user", "content": prompt_text}],
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()


def _generate_tech_process(dish_name: str, ingredients_str: str, tech_hint: str) -> str:
    prompt = TTK_TECH_PROMPT.format(
        dish_name=dish_name, ingredients=ingredients_str,
        tech_hint=tech_hint or "(не указана)",
    )
    return _llm_complete(prompt)


def _generate_organoleptic(dish_name: str, ingredients_str: str, tech_hint: str) -> dict:
    prompt = TTK_ORG_PROMPT.format(
        dish_name=dish_name, ingredients=ingredients_str,
        tech_hint=tech_hint or "(не указана)",
    )
    raw = _llm_complete(prompt)
    # Снимаем возможные ```json ... ``` обёртки
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    return {
        "appearance": str(data.get("appearance", "")).strip(),
        "color": str(data.get("color", "")).strip(),
        "taste_smell": str(data.get("taste_smell", "")).strip(),
        "consistency": str(data.get("consistency", "")).strip(),
    }


def _fill_and_render_ttk(context: dict, meta: dict, tech_hint: str = "") -> Path:
    """Догенерить тексты LLM и отрендерить .docx. Возвращает путь к файлу.

    Общая часть одиночной и пакетной генерации. Бросает исключение при неудаче —
    в пакете это ловится по каждому блюду отдельно, чтобы одна сбойная карта
    не роняла всю пачку.
    """
    ingredients_str = ", ".join(f"{i['name']} {i['netto']} г" for i in meta["ingredients"])
    context["tech_process"] = _generate_tech_process(
        meta["dish_name"], ingredients_str, tech_hint
    )
    org = _generate_organoleptic(meta["dish_name"], ingredients_str, tech_hint)
    context["organoleptic_appearance"] = org["appearance"]
    context["organoleptic_color"] = org["color"]
    context["organoleptic_taste_smell"] = org["taste_smell"]
    context["organoleptic_consistency"] = org["consistency"]

    safe = re.sub(r"[^\w\-]+", "_", meta["dish_name"]).strip("_")[:40]
    out_path = GENERATED_TTK_DIR / f"TTK_{meta['dish_id']}_{safe}.docx"
    render_ttk(context, out_path)
    return out_path


def _tool_generate_ttk_document(args: dict) -> dict:
    data = get_data()
    query = (args.get("dish_name_or_id") or "").strip()
    if not query:
        return {"error": "Не указано блюдо для ТТК"}

    dish = data.dishes.get(query) or data.dishes.get(query.upper())
    if dish is None:
        dish = data.find_dish_by_name(query)
    if dish is None:
        matches = data.find_dishes_by_query(query)
        if matches:
            return {
                "error": "Найдено несколько подходящих блюд, уточни",
                "candidates": [
                    {"id": d.id, "name": d.name, "category": d.category}
                    for d in matches
                ],
            }
        return {"error": f"Блюдо '{query}' не найдено"}

    built = build_ttk_context(data, dish.id)
    if built is None:
        return {"error": "Не удалось собрать данные блюда"}
    context, meta = built
    if not meta["has_composition"]:
        return {
            "error": f"У блюда «{meta['dish_name']}» не заполнен состав в ТТК — "
            f"ТТК не из чего собрать."
        }

    # Шаг 1 — превью (confirm не передан/False): показываем рецептуру и КБЖУ,
    # файл НЕ рендерим и LLM-тексты НЕ генерим (дёшево). Шеф подтверждает «да» →
    # модель вызывает функцию снова с confirm=true.
    if not bool(args.get("confirm")):
        return {"display": format_ttk_preview(context, meta)}

    tech_hint = (args.get("tech_process_hint") or "").strip()
    try:
        out_path = _fill_and_render_ttk(context, meta, tech_hint)
    except Exception as e:
        logger.exception("Ошибка формирования ТТК")
        return {"error": f"Не смог сформировать ТТК: {e}"}

    display = (
        f"ТТК готова: {meta['dish_name']} (№ {context['ttk_number']}). Отправляю файлом.\n"
        f"Техпроцесс и органолептику сгенерировал автоматически — проверь и при "
        f"необходимости попроси перегенерировать."
    )
    status = kbju_coverage_status(Decimal(str(meta["kbju_coverage"])))
    if status == "poor":
        pct = int(round(meta["kbju_coverage"] * 100))
        display += (
            f"\nВНИМАНИЕ: КБЖУ почти не заполнено (данные лишь у {pct}% состава по весу) — "
            f"цифрам в карте нельзя доверять, НЕ вноси их в документ. Заполни КБЖУ "
            f"ингредиентов в таблице и перегенерируй."
        )
    elif status == "partial":
        display += (
            "\nКБЖУ неполное: у части ингредиентов нет данных — значения приблизительны."
        )
    return {"display": display, "file_path": str(out_path)}


def _tool_manage_competitors(args: dict) -> dict:
    """Список конкурентов, добавить, убрать.

    Шеф пишет живым языком («добавь конкурента <ссылка>»), а не командой
    /add_competitor — 06.08.2026 бот на такую фразу ответил «это не входит
    в мои функции», хотя умеет. Проверку прогоном сюда не тащим: она идёт
    3–8 минут в фоне, для неё есть команда и кнопка.
    """
    from urllib.parse import urlparse

    from src.competitors import storage

    action = (args.get("action") or "list").strip().lower()

    if action in ("list", "список"):
        comps = storage.list_competitors()
        return {
            "count": len(comps),
            "competitors": [
                {"name": c.name, "url": c.url, "fetch_method": c.fetch_method}
                for c in comps
            ],
        }

    raw = (args.get("url") or "").strip()
    if not raw:
        return {"error": "Не указана ссылка на сайт конкурента"}
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        return {"error": f"«{args.get('url')}» не похоже на адрес сайта"}
    domain = parsed.netloc.removeprefix("www.")

    if action in ("remove", "delete", "убрать", "удалить"):
        comp = storage.deactivate_competitor(domain)
        if comp is None:
            return {"error": f"Конкурента с адресом {domain} не отслеживаю"}
        return {"removed": comp.name, "url": comp.url}

    if action not in ("add", "добавить"):
        return {"error": f"Не понял действие «{action}». Бывает: add, remove, list."}

    name = (args.get("name") or "").strip() or domain
    comp = storage.add_competitor(name, domain, raw)
    logger.info(f"[конкуренты] добавлен {comp.name} ({comp.url}) через LLM")
    return {
        "added": comp.name,
        "url": comp.url,
        "menu_url": comp.menu_url,
        "note": (
            "Проверю в ближайший еженедельный прогон. Запустить сейчас — "
            "команда /check_competitors или кнопка «Проверить сейчас»."
        ),
    }


def _tool_suggest_dishes(args: dict, user_id: int | None = None) -> dict:
    """Идеи новых блюд. Модель придумывает состав, числа считает калькулятор.

    user_id нужен, чтобы не предлагать одно и то же на «давай ещё» (см. recent.py).
    """
    from src.ideas import recent
    from src.ideas.generator import MODE_FROM_BASE, MODE_NEW, generate_ideas
    from src.llm.format import format_dish_ideas

    data = get_data()
    brief = (args.get("brief") or "").strip()

    raw_mode = (args.get("mode") or "").strip().lower()
    mode = MODE_NEW if raw_mode in ("новое", "новые", "с закупкой") else MODE_FROM_BASE

    category = (args.get("category") or "").strip() or None
    target_raw = args.get("target_uc")
    target_uc = None
    if target_raw not in (None, ""):
        try:
            target_uc = Decimal(str(target_raw))
        except Exception:
            target_uc = None

    try:
        count = int(args.get("count") or settings.ideas_count)
    except (TypeError, ValueError):
        count = settings.ideas_count
    count = max(1, min(count, 5))

    ideas, error = generate_ideas(
        data, brief, _llm_complete,
        mode=mode, category=category, target_uc=target_uc,
        count=count, temperature=settings.ideas_temperature,
        avoid=recent.recent(user_id),
    )
    if error:
        return {"error": error}

    recent.remember(user_id, [i.name for i in ideas])

    return {
        "mode": mode,
        "count": len(ideas),
        # Машиночитаемая часть: по ней модель вызовет create_dish, когда шеф
        # выберет вариант. Числа отсюда в ответ не идут — только из display.
        "variants": [
            {
                "n": n,
                "name": idea.name,
                "category": idea.category,
                "ingredients": idea.as_create_dish_payload(),
            }
            for n, idea in enumerate(ideas, start=1)
        ],
        "display": format_dish_ideas(ideas, mode),
    }


def _tool_build_pricing_table(args: dict) -> dict:
    """Пересборка расчётки для коммерческого отдела."""
    from src.pricing.format import format_pricing_result
    from src.pricing.service import rebuild_sync

    raw = (args.get("status") or "").strip().lower()
    if raw in ("разработка", "новинки", "новинка", "новые"):
        statuses = ("разработка",)
    elif raw in ("активное", "активные", "меню"):
        statuses = ("активное",)
    else:
        statuses = ("разработка", "активное")

    results = rebuild_sync(statuses)
    return {
        "sheets": [r.get("sheet") for r in results],
        "display": format_pricing_result(results),
    }


def _tool_generate_ttk_batch(args: dict) -> dict:
    """Пачка ТТК по статусу блюд: confirm=false → список, confirm=true → файлы."""
    data = get_data()
    status = (args.get("status") or "разработка").strip().lower()
    if status in ("новинки", "новинка", "разработка"):
        status = "разработка"
    elif status in ("активное", "активные", "меню"):
        status = "активное"
    else:
        return {"error": (
            f"Не понял статус «{args.get('status')}». Бывает «разработка» (новинки) "
            f"или «активное»."
        )}

    dishes = sorted(
        (d for d in data.dishes.values() if d.status == status),
        key=lambda d: (d.category or "", d.name),
    )
    if not dishes:
        return {"error": f"Нет блюд со статусом «{status}»."}

    ready = [d for d in dishes if data.ttk_by_dish.get(d.id)]
    skipped = [d for d in dishes if not data.ttk_by_dish.get(d.id)]
    if not ready:
        return {"error": (
            f"У всех блюд со статусом «{status}» ({len(dishes)}) не заполнен состав "
            f"в ТТК — собирать карты не из чего."
        )}

    # На каждую карту два запроса к LLM. 126 активных блюд — это ~250 запросов,
    # десятки минут и заметные деньги. Лучше отказать, чем спалить бюджет молча.
    if len(ready) > MAX_TTK_BATCH:
        return {"error": (
            f"Блюд со статусом «{status}» слишком много: {len(ready)}. За раз делаю "
            f"не больше {MAX_TTK_BATCH} — на каждую карту уходит два запроса к модели. "
            f"Попроси по категории или по конкретным блюдам."
        )}

    if not bool(args.get("confirm")):
        return {
            "status_filter": status,
            "count": len(ready),
            "display": format_ttk_batch_preview(status, ready, skipped),
        }

    tech_hint = (args.get("tech_process_hint") or "").strip()
    done: list[tuple[str, str]] = []      # (название, путь)
    failed: list[tuple[str, str]] = []    # (название, причина)
    poor_kbju: list[str] = []

    for dish in ready:
        try:
            built = build_ttk_context(data, dish.id)
            if built is None:
                raise ValueError("не удалось собрать данные блюда")
            context, meta = built
            out_path = _fill_and_render_ttk(context, meta, tech_hint)
            done.append((dish.name, str(out_path)))
            if kbju_coverage_status(Decimal(str(meta["kbju_coverage"]))) == "poor":
                poor_kbju.append(dish.name)
        except Exception as e:
            # Одна сбойная карта не должна ронять пачку
            logger.exception(f"ТТК для {dish.id} не собралась")
            failed.append((dish.name, str(e)))

    return {
        "status_filter": status,
        "made": len(done),
        "failed": len(failed),
        "file_paths": [p for _, p in done],
        "display": format_ttk_batch_result(done, failed, poor_kbju),
    }


def _resolve_ingredient_for_create(data, name: str):
    """Резолв ингредиента при создании блюда: (ingredient, error_dict).

    В отличие от _resolve_ingredient — БЕЗ авто-выбора по использованию в блюдах
    (для нового блюда это нерелевантно). Точное/единственное совпадение → берём;
    несколько → candidates; ноль → ошибка с просьбой завести ингредиент в ING.

    Архивные позиции в НОВОЕ блюдо не предлагаем: шеф их вывел из оборота. Это
    заодно снимает половину мнимых «дублей имён» — четыре из семи пар в ING это
    архивная позиция против активной, и выбирать там на самом деле не из чего.
    Из кеша архив при этом НЕ убираем: id 40 и 98 всё ещё стоят в составе
    15 существующих блюд, их UC должен считаться по-прежнему.
    """
    matches = data.search_ingredients(name)
    if not matches:
        err = _not_found_error(data, name)
        # Строка в ING есть, просто без id — подсказка уже точная, не перетираем
        if "не проставлен" not in err["error"]:
            err = {
                "error": (
                    f"Ингредиент «{name}» не найден в ING. Сначала заведи его в "
                    f"справочнике ING, потом создадим блюдо."
                )
            }
        return None, err

    active = [i for i in matches if i.status != "архив"]
    if not active:
        names = ", ".join(f"«{i.name}» (id {i.id})" for i in matches)
        return None, {
            "error": (
                f"Под «{name}» нашёлся только архив: {names}. В новое блюдо "
                f"архивные позиции не ставлю — верни статус «активный» в ING "
                f"или назови другой ингредиент."
            )
        }
    matches = active

    # Точное совпадение снимает неоднозначность, только если оно ОДНО. При двух
    # активных строках с одинаковым именем (сахар: id 12 по 100 ₽/кг и id 123
    # по 0 ₽/шт) взять первую попавшуюся значит молча посчитать не тот UC.
    q = name.lower().strip()
    exact = [i for i in matches if i.name.lower() == q]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        matches = exact
    elif len(matches) == 1:
        return matches[0], None
    return None, {
        "error": f"Несколько ингредиентов под «{name}» — уточни, какой именно",
        "candidates": [
            {
                "id": i.id, "name": i.name, "category": i.category,
                "unit": i.unit,
                "price_per_unit_rub": (
                    float(i.price_per_unit) if i.price_per_unit is not None else None
                ),
            }
            for i in matches
        ],
    }


def _resolve_packaging_for_create(data, name: str):
    """Резолв упаковки при создании блюда: (packaging, error_dict).

    Архив в новое блюдо не предлагаем — как и с ингредиентами. Сейчас все
    28 упаковок активны, но правило должно работать и когда шеф что-то выведет.
    """
    matches = data.search_packagings(name)
    if not matches:
        return None, {
            "error": (
                f"Упаковка «{name}» не найдена в листе «Упаковка». Заведи её, "
                f"потом добавим в блюдо."
            )
        }
    active = [p for p in matches if p.status != "архив"]
    if not active:
        return None, {
            "error": (
                f"Упаковка «{name}» в архиве — в новое блюдо не ставлю. "
                f"Верни статус «активный» или назови другую."
            )
        }
    matches = active
    q = name.lower().strip()
    for p in matches:
        if p.name.lower() == q:
            return p, None
    if len(matches) == 1:
        return matches[0], None
    return None, {
        "error": f"Несколько упаковок под «{name}» — уточни, какую",
        "candidates": [{"id": p.id, "name": p.name} for p in matches],
    }


def _split_packaging_from_ingredients(data, ingredients: list, packaging: list):
    """Вынуть из состава то, что на самом деле упаковка: (ingredients, packaging, notes).

    LLM регулярно кладёт упаковку и в `packaging`, и в `ingredients` — тогда её
    ищут в справочнике ING и диалог падает с «не найден в ING» или «укажи граммы»
    (разбор лога 04.08.2026, два отказа подряд на ровном месте). Правило чинит
    это в коде: решение детерминированное, промпту тут доверять нельзя.
    """
    known = {(p.get("name") or "").strip().lower() for p in packaging}
    keep, extra, notes = [], [], []

    for item in ingredients:
        iname = (item.get("name") or "").strip()
        if not iname:
            keep.append(item)
            continue

        # Уже назвали упаковкой — просто выкидываем дубль, без шума
        if iname.lower() in known:
            continue

        has_weight = item.get("grams") is not None or item.get("pieces") is not None
        # Признак упаковки: нет веса, но есть qty (ключ из схемы упаковки),
        # либо позиции нет в ING, зато она есть в листе «Упаковка».
        looks_like_pkg = (not has_weight and item.get("qty") is not None) or (
            not data.search_ingredients(iname) and data.search_packagings(iname)
        )
        if looks_like_pkg and data.search_packagings(iname):
            qty = item.get("qty") or item.get("pieces") or 1
            extra.append({"name": iname, "qty": qty})
            known.add(iname.lower())
            notes.append(f"«{iname}» учтён как упаковка, а не ингредиент")
            continue

        keep.append(item)

    return keep, list(packaging) + extra, notes


def _tool_create_dish(args: dict) -> dict:
    """Создание блюда: confirm=false → превью с UC/маржой; confirm=true → запись в Sheets."""
    data = get_data()
    name = (args.get("name") or "").strip()
    category = (args.get("category") or "").strip()
    price_raw = args.get("price_menu")
    ingredients = args.get("ingredients") or []
    packaging = args.get("packaging") or []
    confirm = bool(args.get("confirm"))

    if not name:
        return {"error": "Не указано название блюда (name)"}
    if not ingredients:
        return {"error": "Не указан состав (ingredients) — нужны пары ингредиент+граммы"}

    # Цена меню НЕ обязательна. Шеф сначала считает фудкост и только потом
    # назначает цену — требовать её заранее означало загонять его в тупик
    # (04.08.2026: три отказа подряд на «мне надо знать FC чтобы посчитать»).
    # Без цены считаем себестоимость, маржу не считаем — как и для 14 блюд,
    # которые уже живут в таблице без цены.
    if price_raw is None or str(price_raw).strip() == "":
        price_menu = None
    else:
        try:
            price_menu = Decimal(str(price_raw))
        except Exception:
            return {"error": f"Цена «{price_raw}» не похожа на число"}
        if price_menu < 0:
            return {"error": "Цена меню не может быть отрицательной"}
        if price_menu == 0:
            price_menu = None

    # Категорию не даём выдумывать LLM: в логе одно блюдо получило сначала
    # «Закуска», потом «Бургер», хотя шеф её не называл. Спрашиваем списком.
    known_categories = data.dish_categories()
    known_lower = {c.lower(): c for c, _ in known_categories}
    if not category:
        out = {"needs_category": True, "categories": known_categories}
        out["display"] = format_category_prompt(name, known_categories)
        return out
    if category.lower() not in known_lower:
        if not args.get("new_category"):
            out = {
                "needs_category": True,
                "unknown_category": category,
                "categories": known_categories,
            }
            out["display"] = format_category_prompt(name, known_categories, category)
            return out
    else:
        # Приводим к написанию, которое уже есть в таблице («бургер» → «Бургер»)
        category = known_lower[category.lower()]

    ingredients, packaging, pkg_notes = _split_packaging_from_ingredients(
        data, ingredients, packaging
    )

    # id считаем по ЖИВОМУ листу — корректно после ручного удаления/правки
    new_id = data.next_free_dish_id_live()
    rows: list[TTKRow] = []

    for item in ingredients:
        iname = (item.get("name") or "").strip()
        grams_raw = item.get("grams")
        pieces_raw = item.get("pieces")
        if not iname:
            return {"error": "У каждого ингредиента нужно имя (name)"}
        if grams_raw is None and pieces_raw is None:
            return {"error": f"Для «{iname}» укажи вес в граммах (grams) или штуки (pieces)"}

        ing, err = _resolve_ingredient_for_create(data, iname)
        if err is not None:
            return err

        # В ТТК вес всегда хранится в граммах нетто. Если шеф задал штуки —
        # переводим шт→граммы по «вес 1 шт» (арифметику делает код, не LLM).
        if pieces_raw is not None:
            try:
                pieces = Decimal(str(pieces_raw))
            except Exception:
                return {"error": f"Количество штук «{pieces_raw}» для «{iname}» не похоже на число"}
            if pieces <= 0:
                return {"error": f"Штук для «{iname}» должно быть больше нуля"}
            if ing.unit != "шт":
                return {"error": (
                    f"«{ing.name}» измеряется в «{ing.unit}», а не в штуках — "
                    f"укажи вес в граммах (grams)."
                )}
            if not ing.weight_per_unit_g or ing.weight_per_unit_g == 0:
                return {"error": (
                    f"У штучного «{ing.name}» не заполнен «Вес 1 шт, г» в ING — "
                    f"не могу перевести штуки в граммы. Заполни вес 1 шт или задай граммы."
                )}
            grams = pieces * ing.weight_per_unit_g
        else:
            try:
                grams = Decimal(str(grams_raw))
            except Exception:
                return {"error": f"Вес «{grams_raw}» для «{iname}» не похож на число"}
            if grams <= 0:
                return {"error": f"Вес для «{iname}» должен быть больше нуля"}

        rows.append(TTKRow(
            dish_id=new_id, ingredient_id=ing.id,
            weight_neto_g=grams, row_type="Основной",
        ))

    for item in packaging:
        pname = (item.get("name") or "").strip()
        if not pname:
            continue
        qty_raw = item.get("qty", 1)
        try:
            qty = Decimal(str(qty_raw))
        except Exception:
            return {"error": f"Количество «{qty_raw}» для упаковки «{pname}» не число"}
        pkg, err = _resolve_packaging_for_create(data, pname)
        if err is not None:
            return err
        rows.append(TTKRow(
            dish_id=new_id, packaging_id=pkg.id,
            weight_neto_g=qty, row_type="Упаковка",
        ))

    uc = calculate_uc_for_composition(data, new_id, name, price_menu, rows)

    # Дубль по названию не блокируем (решает шеф), но честно предупреждаем в превью
    warnings = list(uc.warnings) + pkg_notes
    dup = next(
        (d for d in data.dishes.values() if d.name.lower() == name.lower()), None
    )
    if dup is not None:
        warnings.append(
            f"Блюдо с таким названием уже есть в базе ({dup.id}) — "
            f"получится дубль по имени"
        )

    result = {
        "dish_id": new_id,
        "dish_name": name,
        "category": category,
        "price_menu_rub": opt_float(price_menu),
        "uc_rub": float(uc.uc_rub),
        "uc_percent": opt_float(uc.uc_percent),
        "margin_rub": opt_float(uc.margin_rub),
        "margin_percent": opt_float(uc.margin_percent),
        "output_grams": float(uc.output_grams),
        "ingredients": [
            {
                "name": i.name,
                "weight_g": float(i.weight_g),
                "unit": i.unit,
                "weight_per_piece_g": (
                    float(i.weight_per_piece_g)
                    if i.weight_per_piece_g is not None else None
                ),
                "cost_rub": float(i.cost_rub),
                "share_percent": (
                    float(i.share_percent) if i.share_percent is not None else None
                ),
                "type": i.row_type,
            }
            for i in uc.ingredients
        ],
        "warnings": warnings,
        # Упаковка есть у всех 126 блюд с составом — если её не назвали, это
        # почти наверняка забыли. Превью спросит об этом до записи (format.py).
        "packaging_missing": not packaging,
    }

    if not confirm:
        result["display"] = format_dish_preview(result)
        return result

    # confirm=true → пишем в Sheets (внутри — снимок и откат при сбое)
    dish = Dish(
        id=new_id, name=name, category=category,
        price_menu=price_menu, status="разработка",
    )
    try:
        data.append_dish_and_ttk(dish, rows)
    except Exception as e:
        logger.exception("Ошибка записи блюда")
        return {"error": f"Не смог записать блюдо в таблицу: {e}"}
    result["display"] = format_dish_created(result)
    return result


# Инструменты, которым нужен user_id: вызываются как handler(args, user_id).
USER_AWARE_TOOLS = {"suggest_dishes"}

TOOL_HANDLERS = {
    "calculate_dish_uc": _tool_calculate_dish_uc,
    "list_dishes": _tool_list_dishes,
    "list_ingredients": _tool_list_ingredients,
    "find_dishes_with_ingredient": _tool_find_dishes_with_ingredient,
    "compare_dishes_margin": _tool_compare_dishes_margin,
    "simulate_price_change": _tool_simulate_price_change,
    "simulate_replacement": _tool_simulate_replacement,
    "generate_ttk_document": _tool_generate_ttk_document,
    "generate_ttk_batch": _tool_generate_ttk_batch,
    "build_pricing_table": _tool_build_pricing_table,
    "suggest_dishes": _tool_suggest_dishes,
    "manage_competitors": _tool_manage_competitors,
    "create_dish": _tool_create_dish,
    "reload_database": _tool_reload_database,
}


# ================================================================
# Главная функция диалога
# ================================================================


def chat(user_message: str, user_id: int | None = None) -> ChatResult:
    """Один цикл диалога: пользовательский запрос → ответ (текст + файлы).

    Внутри ведём цикл с tool calling. Возвращаем ChatResult: текст для пользователя
    и пути к сгенерированным файлам (например, .docx ТТК), которые бот отправит.

    Если передан user_id — подмешиваем короткую историю диалога (для отсылок вроде
    «их маржа», «посчитай второй») и в конце сохраняем пару вопрос/ответ.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if user_id is not None:
        messages.extend(get_history(user_id))
    messages.append({"role": "user", "content": user_message})

    total_cost = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    files: list[str] = []

    for step in range(MAX_TOOL_LOOPS):
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.3,
        )

        # Учёт стоимости (polza.ai кладёт cost_rub в usage)
        usage = response.usage
        if usage:
            total_tokens_in += usage.prompt_tokens or 0
            total_tokens_out += usage.completion_tokens or 0
            usage_dict = usage.model_dump() if hasattr(usage, "model_dump") else {}
            total_cost += usage_dict.get("cost_rub", 0) or 0

        choice = response.choices[0]
        msg = choice.message

        # Если модель не вызывает функции — это финальный ответ
        if not msg.tool_calls:
            logger.info(
                f"LLM done: steps={step+1}, "
                f"tokens in/out={total_tokens_in}/{total_tokens_out}, "
                f"cost={total_cost:.4f}₽"
            )
            final_text = msg.content or "(пустой ответ от модели)"
            if user_id is not None:
                append_turn(user_id, user_message, final_text)
            return ChatResult(text=final_text, files=files)

        # Иначе обрабатываем все вызовы функций
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            handler = TOOL_HANDLERS.get(name)
            if handler is None:
                result = {"error": f"Неизвестная функция: {name}"}
            else:
                try:
                    # Явный список, а не скрытый ключ в args: видно, кто зависит
                    # от пользователя. Пока это только идеи — им нужна память,
                    # чтобы не предлагать одно и то же на «давай ещё».
                    if name in USER_AWARE_TOOLS:
                        result = handler(args, user_id)
                    else:
                        result = handler(args)
                except Exception as e:
                    logger.exception(f"Ошибка в tool {name}: {e}")
                    result = {"error": f"Ошибка выполнения: {e}"}

            if isinstance(result, dict):
                if result.get("file_path"):
                    files.append(result["file_path"])
                # Пакетная генерация возвращает список путей
                files.extend(result.get("file_paths") or [])

            logger.info(f"Tool {name}({args}) → {str(result)[:200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    logger.warning(f"Превышен лимит {MAX_TOOL_LOOPS} итераций tool calling")
    return ChatResult(
        text="Не удалось завершить обработку запроса (слишком много вызовов функций). "
        "Попробуй переформулировать.",
        files=files,
    )
