"""Сборка контекста и рендер ТТК в .docx по шаблону TTK_template.docx (docxtpl).

Детерминированная часть документа (рецептура брутто/нетто, выход, КБЖУ, реквизиты)
собирается здесь из калькулятора. Текстовые разделы (технологический процесс и
органолептика) генерирует LLM-слой и передаёт сюда готовыми — тут их не сочиняем.
"""
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from docxtpl import DocxTemplate

from src.calc.costs import calculate_dish_uc
from src.config import settings
from src.data.sheets import KitchenData

# Шаблон лежит в корне проекта (src/ttk/builder.py → ../../)
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "TTK_template.docx"

# Реквизиты сети берём из конфига (.env), дефолты — в src/config.py.

_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _g(x: Decimal | float | None) -> str:
    """Грамм/число без лишних нулей: 60 → '60', 47.5 → '47.5'."""
    if x is None:
        return "—"
    return f"{float(x):g}"


def _ttk_number_from_dish(dish_id: str) -> str:
    """Номер ТТК из id блюда: 'B001' → '1'. Если не распарсить — сам id.

    Это значение-заглушка: у сети может быть своя нумерация, шеф поправит в файле.
    """
    m = re.search(r"\d+", dish_id or "")
    return str(int(m.group())) if m else (dish_id or "")


def _today_ru() -> str:
    now = datetime.now()
    return f"{now.day:02d} {_MONTHS[now.month - 1]} {now.year} г."


def build_ttk_context(data: KitchenData, dish_id: str) -> tuple[dict, dict] | None:
    """Готовит контекст для шаблона (без tech_process и органолептики).

    Возвращает (context, meta) либо None, если блюдо не найдено.
      context — словарь для docxtpl (поля tech_process/organoleptic_* заполняет
                вызывающий код перед рендером);
      meta    — служебное: has_composition, kbju_complete, warnings, dish_name.
    """
    dish = data.dishes.get(dish_id)
    if dish is None:
        return None

    result = calculate_dish_uc(data, dish_id)
    if result is None:
        return None

    main_items = [i for i in result.ingredients if i.row_type == "Основной"]
    has_composition = len(main_items) > 0

    ingredients = [
        {
            "name": i.name,
            "brutto": _g(i.weight_brutto_g if i.weight_brutto_g is not None else i.weight_g),
            "netto": _g(i.weight_g),
        }
        for i in main_items
    ]

    output = result.output_grams
    if output and output > 0:
        f = Decimal("100") / output
        per100 = {
            "белки": _g(round(result.proteins_g * f, 1)),
            "жиры": _g(round(result.fats_g * f, 1)),
            "углеводы": _g(round(result.carbs_g * f, 1)),
            "ккал": _g(round(result.kcal * f, 0)),
        }
    else:
        per100 = {"белки": "—", "жиры": "—", "углеводы": "—", "ккал": "—"}

    per_portion = {
        "белки": _g(result.proteins_g),
        "жиры": _g(result.fats_g),
        "углеводы": _g(result.carbs_g),
        "ккал": _g(result.kcal),
    }

    context = {
        "org_name": settings.ttk_org_name,
        "director_position": settings.ttk_director_position,
        "approval_date": _today_ru(),
        "ttk_number": _ttk_number_from_dish(dish.id),
        "tr_ts_number": settings.ttk_tr_ts_number,
        "dish_name": dish.name,
        "ingredients": ingredients,
        "dish_output_g": _g(output),
        "kbju_per_100g": per100,
        "kbju_per_portion": per_portion,
        # Текстовые поля — заполняются LLM-слоем перед рендером:
        "tech_process": "",
        "organoleptic_appearance": "",
        "organoleptic_color": "",
        "organoleptic_taste_smell": "",
        "organoleptic_consistency": "",
    }

    kbju_complete = not any("КБЖУ нет" in w for w in result.warnings)
    meta = {
        "dish_id": dish.id,
        "dish_name": dish.name,
        "has_composition": has_composition,
        "kbju_complete": kbju_complete,
        "kbju_coverage": float(result.kbju_coverage),
        "warnings": result.warnings,
        "ingredients": ingredients,  # для подсказки LLM в техпроцессе/органолептике
        "output_g": _g(output),
    }
    return context, meta


def render_ttk(context: dict, out_path: str | Path) -> Path:
    """Рендерит шаблон с контекстом и сохраняет .docx. Возвращает путь."""
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"Не найден шаблон ТТК {TEMPLATE_PATH}. Сгенерируй его: "
            f"python build_ttk_template.py"
        )
    tpl = DocxTemplate(str(TEMPLATE_PATH))
    tpl.render(context)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tpl.save(str(out_path))
    return out_path
