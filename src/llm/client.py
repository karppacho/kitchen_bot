"""LLM-клиент: обёртка над OpenAI SDK для работы с polza.ai.

Главная функция — `chat()`. Принимает текст от пользователя, ведёт диалог
с моделью (включая tool calling), возвращает финальный текстовый ответ.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from decimal import Decimal
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
    format_dish_preview,
    format_dish_created,
)
from src.calc.costs import (
    calculate_dish_uc,
    calculate_uc_for_composition,
    find_dishes_with_ingredient,
    simulate_price_change,
    simulate_replacement,
    kbju_coverage_status,
)
from src.ttk.builder import build_ttk_context, render_ttk
from src.llm.history import get_history, append_turn

# Один клиент на весь процесс. timeout: иначе зависший запрос к polza.ai
# держит поток (и «печатает…» у шефа) до 10 минут — дефолта SDK.
client = OpenAI(
    base_url=settings.polza_base_url,
    api_key=settings.polza_api_key,
    timeout=60.0,
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


@dataclass
class ChatResult:
    """Результат диалога: текст ответа + пути к файлам (например, сгенерированная ТТК)."""
    text: str
    files: list[str] = field(default_factory=list)


# ================================================================
# Реализация функций, которые LLM может вызывать
# ================================================================


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
        return None, {"error": f"Ингредиент '{query}' не найден"}
    if len(matches) == 1:
        return matches[0], None
    # Точное совпадение по имени делает выбор однозначным
    q = query.lower().strip()
    for ing in matches:
        if ing.name.lower() == q:
            return ing, None

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
        "price_menu_rub": float(result.price_menu),
        "uc_rub": float(result.uc_rub),
        "uc_percent": float(result.uc_percent),
        "margin_rub": float(result.margin_rub),
        "margin_percent": float(result.margin_percent),
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
                "price_menu_rub": float(d.price_menu),
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
    ingredients_str = ", ".join(f"{i['name']} {i['netto']} г" for i in meta["ingredients"])
    try:
        context["tech_process"] = _generate_tech_process(
            meta["dish_name"], ingredients_str, tech_hint
        )
        org = _generate_organoleptic(meta["dish_name"], ingredients_str, tech_hint)
    except Exception as e:
        logger.exception("Ошибка генерации текста ТТК")
        return {"error": f"Не смог сгенерировать текст ТТК: {e}"}

    context["organoleptic_appearance"] = org["appearance"]
    context["organoleptic_color"] = org["color"]
    context["organoleptic_taste_smell"] = org["taste_smell"]
    context["organoleptic_consistency"] = org["consistency"]

    safe = re.sub(r"[^\w\-]+", "_", meta["dish_name"]).strip("_")[:40]
    out_path = GENERATED_TTK_DIR / f"TTK_{meta['dish_id']}_{safe}.docx"
    try:
        render_ttk(context, out_path)
    except Exception as e:
        logger.exception("Ошибка рендера ТТК")
        return {"error": f"Не смог сформировать .docx: {e}"}

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


def _resolve_ingredient_for_create(data, name: str):
    """Резолв ингредиента при создании блюда: (ingredient, error_dict).

    В отличие от _resolve_ingredient — БЕЗ авто-выбора по использованию в блюдах
    (для нового блюда это нерелевантно). Точное/единственное совпадение → берём;
    несколько → candidates; ноль → ошибка с просьбой завести ингредиент в ING.
    """
    matches = data.search_ingredients(name)
    if not matches:
        return None, {
            "error": (
                f"Ингредиент «{name}» не найден в ING. Сначала заведи его в "
                f"справочнике ING, потом создадим блюдо."
            )
        }
    q = name.lower().strip()
    for ing in matches:
        if ing.name.lower() == q:
            return ing, None
    if len(matches) == 1:
        return matches[0], None
    return None, {
        "error": f"Несколько ингредиентов под «{name}» — уточни, какой именно",
        "candidates": [
            {"id": i.id, "name": i.name, "category": i.category, "unit": i.unit}
            for i in matches
        ],
    }


def _resolve_packaging_for_create(data, name: str):
    """Резолв упаковки при создании блюда: (packaging, error_dict)."""
    matches = data.search_packagings(name)
    if not matches:
        return None, {
            "error": (
                f"Упаковка «{name}» не найдена в листе «Упаковка». Заведи её, "
                f"потом добавим в блюдо."
            )
        }
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
    if price_raw is None:
        return {"error": "Не указана цена меню (price_menu)"}
    if not ingredients:
        return {"error": "Не указан состав (ingredients) — нужны пары ингредиент+граммы"}
    try:
        price_menu = Decimal(str(price_raw))
    except Exception:
        return {"error": f"Цена «{price_raw}» не похожа на число"}
    if price_menu <= 0:
        return {"error": "Цена меню должна быть больше нуля"}

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
    warnings = list(uc.warnings)
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
        "price_menu_rub": float(price_menu),
        "uc_rub": float(uc.uc_rub),
        "uc_percent": float(uc.uc_percent),
        "margin_rub": float(uc.margin_rub),
        "margin_percent": float(uc.margin_percent),
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


TOOL_HANDLERS = {
    "calculate_dish_uc": _tool_calculate_dish_uc,
    "list_dishes": _tool_list_dishes,
    "list_ingredients": _tool_list_ingredients,
    "find_dishes_with_ingredient": _tool_find_dishes_with_ingredient,
    "compare_dishes_margin": _tool_compare_dishes_margin,
    "simulate_price_change": _tool_simulate_price_change,
    "simulate_replacement": _tool_simulate_replacement,
    "generate_ttk_document": _tool_generate_ttk_document,
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
                    result = handler(args)
                except Exception as e:
                    logger.exception(f"Ошибка в tool {name}: {e}")
                    result = {"error": f"Ошибка выполнения: {e}"}

            if isinstance(result, dict) and result.get("file_path"):
                files.append(result["file_path"])

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
