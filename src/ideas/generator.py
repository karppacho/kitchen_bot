"""Генерация идей блюд: вызов модели → разбор → резолв по ING → расчёт UC.

Модель придумывает название, концепцию и состав. Все деньги и КБЖУ считает
калькулятор по этому составу — модель не видит цен и не печатает ни одной
денежной цифры (правило проекта №1).

`src/llm/client.py` здесь НЕ импортируется: функция обращения к модели
приходит аргументом `complete_fn`. Так нет циклического импорта, а тесты
подсовывают фейк и работают офлайн — как `competitors/extractor.py`,
который тоже живёт со своим доступом к LLM.
"""
import json
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from loguru import logger

from src.calc.costs import calculate_uc_for_composition
from src.data.models import DishUCResult, TTKRow
from src.data.sheets import KitchenData
from src.ideas.palette import build_palette

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "dish_idea.md"

MODE_FROM_BASE = "из базы"
MODE_NEW = "новое"

# Случайный «угол» на каждый вызов. Меняет вход — значит, меняет и выход:
# при одинаковом промпте модель выдаёт один и тот же ответ даже на температуре 0.9.
ANGLES = [
    "сделай упор на контраст текстур — мягкое против хрустящего",
    "оттолкнись от соуса: пусть он задаёт характер блюда",
    "возьми белок, которого мало в текущем меню",
    "сыграй на остроте и ярком вкусе",
    "собери максимально просто, из трёх-четырёх позиций",
    "сделай ставку на сытность: большая порция за понятные деньги",
    "подумай про свежесть — овощи и зелень как заметный слой",
    "сделай что-то на завтрак или перекус, а не полноценный обед",
]

# Разумные границы для граммовки: модель иногда выдаёт 0 или 5000.
MIN_GRAMS = Decimal("1")
MAX_GRAMS = Decimal("2000")


@dataclass
class IdeaIngredient:
    """Позиция состава идеи после резолва по ING."""

    name: str                    # как назвал бот (для показа шефу)
    grams: Decimal
    ingredient_id: int | None = None   # None — в ING не нашлось
    matched_name: str = ""             # реальное имя из ING
    costed: bool = False               # попал ли в UC

    @property
    def known(self) -> bool:
        return self.ingredient_id is not None


@dataclass
class DishIdea:
    """Один вариант блюда с посчитанными числами."""

    name: str
    idea: str
    category: str
    ingredients: list[IdeaIngredient] = field(default_factory=list)
    uc: DishUCResult | None = None
    notes: list[str] = field(default_factory=list)
    to_buy: list[str] = field(default_factory=list)   # чего нет в ING (режим «новое»)
    dropped: list[str] = field(default_factory=list)  # выброшенные позиции
    dropped_grams: Decimal = Decimal("0")

    @property
    def costed_count(self) -> int:
        return sum(1 for i in self.ingredients if i.costed)

    @property
    def collapsed(self) -> bool:
        """Идея развалилась: от неё осталась несущественная часть.

        Модель делает опечатки в названиях («Пельмени с говединой»), позиция
        не резолвится и выбрасывается. Если так ушла основа, остаётся «блюдо»
        из 30 г соуса — показывать такое шефу как идею нельзя.
        """
        if not self.ingredients:
            return True
        # Считаем по весу, а не по числу позиций: блюдо из двух компонентов
        # (пельмени + соус) законно, а вот потеря основы — нет.
        kept = sum(i.grams for i in self.ingredients)
        return self.dropped_grams > kept

    @property
    def full_coverage(self) -> bool:
        return bool(self.ingredients) and self.costed_count == len(self.ingredients)

    def as_create_dish_payload(self) -> list[dict]:
        """Состав в формате, который принимает create_dish.

        Только позиции, найденные в ING: остальные шеф сначала заводит вручную.
        """
        return [
            {"name": i.matched_name, "grams": float(i.grams)}
            for i in self.ingredients if i.known
        ]


def _strip_json_fences(raw: str) -> str:
    """Снять ```json-обёртки — модель ставит их, даже когда просят не ставить.

    То же самое уже делается для органолептики в ТТК (_generate_organoleptic).
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"```$", "", text, flags=re.MULTILINE)
    return text.strip()


def parse_ideas(raw: str) -> list[dict]:
    """JSON-массив идей из ответа модели. Пустой список — разобрать не удалось.

    Модель любит обрамлять ответ текстом, поэтому при неудаче ещё раз пробуем
    вырезать самый внешний массив по скобкам.
    """
    text = _strip_json_fences(raw)
    for candidate in (text, _outer_array(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    logger.warning(f"[идеи] не разобрал ответ модели: {raw[:200]}")
    return []


def _outer_array(text: str) -> str:
    start, end = text.find("["), text.rfind("]")
    return text[start:end + 1] if 0 <= start < end else ""


def _to_grams(raw) -> Decimal | None:
    try:
        grams = Decimal(str(raw).replace(",", ".").strip())
    except (InvalidOperation, AttributeError, TypeError):
        return None
    if not (MIN_GRAMS <= grams <= MAX_GRAMS):
        return None
    return grams


def _clean_name(name: str) -> str:
    """Убрать хвостовую скобку из имени, предложенного моделью.

    В палитре непосчитываемые позиции помечены «(нет цены)», и модель копирует
    пометку прямо в состав: «Сырный соус Блючиз (нет цены)». Такое имя не
    резолвится, и позиция, которая В БАЗЕ ЕСТЬ, молча выбрасывается —
    поймано на живом прогоне.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip() or name


def _resolve(data: KitchenData, name: str):
    """Ингредиент по имени — мягко: неоднозначность не ошибка.

    В отличие от `_resolve_ingredient_for_create`, который переспрашивает шефа,
    здесь выбор делает бот: идея — это черновик, и упираться в уточнения на
    каждом из трёх вариантов было бы издевательством. Архив не берём.
    """
    name = _clean_name(name)
    matches = [i for i in data.search_ingredients(name) if i.status != "архив"]
    if not matches:
        return None
    exact = [i for i in matches if i.name.lower() == name.lower().strip()]
    if exact:
        return exact[0]
    # Самое короткое имя — обычно самое общее и подходящее («Сыр» вместо
    # «Сыр плавленый Чизбургер ломтевой 45% 150гр флоу-пак»)
    return min(matches, key=lambda i: len(i.name))


def _build_idea(data: KitchenData, raw: dict, mode: str, dish_id: str) -> DishIdea | None:
    name = str(raw.get("name") or "").strip()
    if not name:
        return None

    idea = DishIdea(
        name=name,
        idea=str(raw.get("idea") or "").strip(),
        category=str(raw.get("category") or "").strip(),
    )

    rows: list[TTKRow] = []
    for item in raw.get("ingredients") or []:
        if not isinstance(item, dict):
            continue
        iname = str(item.get("name") or "").strip()
        if not iname:
            continue
        grams = _to_grams(item.get("grams"))
        if grams is None:
            idea.notes.append(f"у «{iname}» невнятный вес — позицию пропустил")
            continue

        ing = _resolve(data, iname)
        if ing is None:
            if mode == MODE_NEW:
                # Режим «совсем новое»: позиция остаётся видимой, но в UC не идёт
                idea.ingredients.append(IdeaIngredient(name=iname, grams=grams))
                idea.to_buy.append(iname)
            else:
                idea.notes.append(f"«{iname}» нет в базе — выкинул из состава")
                idea.dropped.append(iname)
                idea.dropped_grams += grams
            continue

        costed = ing.price_per_unit is not None and ing.price_per_unit != 0
        if ing.unit == "шт" and not ing.weight_per_unit_g:
            costed = False
        idea.ingredients.append(IdeaIngredient(
            name=iname, grams=grams, ingredient_id=ing.id,
            matched_name=ing.name, costed=costed,
        ))
        rows.append(TTKRow(
            dish_id=dish_id, ingredient_id=ing.id,
            weight_neto_g=grams, row_type="Основной",
        ))

    if not rows:
        idea.notes.append("ни один ингредиент не нашёлся в базе — считать нечего")
        return idea

    # Цены меню нет: идея ещё не блюдо, маржу считать не от чего.
    idea.uc = calculate_uc_for_composition(data, dish_id, name, None, rows)
    return idea


def generate_ideas(
    data: KitchenData,
    brief: str,
    complete_fn: Callable[[str, float], str],
    *,
    mode: str = MODE_FROM_BASE,
    category: str | None = None,
    target_uc: Decimal | None = None,
    count: int = 3,
    temperature: float = 0.9,
    avoid: list[str] | None = None,
    rng: random.Random | None = None,
) -> tuple[list[DishIdea], str | None]:
    """Идеи блюд: (список, ошибка). Ошибка — если модель не дала разобрать ответ.

    avoid — названия, которые бот уже показывал этому шефу (см. recent.py).
    rng   — источник случайности для палитры и «угла»; в тестах seeded.
    """
    rng = rng or random.Random()
    palette = build_palette(data, category)
    if not palette.total:
        return [], "В базе нет активных ингредиентов — придумывать не из чего."

    constraints = []
    if category:
        constraints.append(f"Категория блюда: {category}")
    if target_uc is not None:
        # Модели цифру даём как ориентир по составу, считать по ней она не будет:
        # проверку укладываемости делает бот после расчёта.
        constraints.append(
            f"Ориентир по себестоимости: не дороже {target_uc} ₽ за порцию — "
            f"это значит недорогие ингредиенты и умеренные граммовки."
        )
    new_rule = (
        "Если для идеи нужен ингредиент, которого в списке нет — можешь его "
        "добавить, бот отдельно отметит, что его надо закупить."
        if mode == MODE_NEW else
        "Ингредиентов не из списка быть не должно."
    )

    already = ""
    if avoid:
        already = (
            "\nЭТО ТЫ УЖЕ ПРЕДЛАГАЛ — не повторяй ни названия, ни ту же связку "
            "основы с белком:\n" + ", ".join(avoid)
        )

    prompt = _PROMPT_PATH.read_text(encoding="utf-8").format(
        brief=brief or "(шеф не уточнил — предложи на своё усмотрение)",
        count=count,
        constraints="\n".join(constraints),
        palette=palette.as_prompt_text(rng),
        existing=", ".join(palette.existing_dishes) or "(в этой категории пока пусто)",
        already_suggested=already,
        angle=rng.choice(ANGLES),
        new_ingredients_rule=new_rule,
        categories=", ".join(palette.categories),
    )

    raw = complete_fn(prompt, temperature)
    parsed = parse_ideas(raw)
    if not parsed:
        return [], "Модель не вернула внятный список идей. Попробуй переформулировать."

    ideas: list[DishIdea] = []
    collapsed: list[DishIdea] = []
    for n, item in enumerate(parsed[:count], start=1):
        built = _build_idea(data, item, mode, dish_id=f"IDEA{n}")
        if built is None:
            continue
        # Развалившиеся не показываем как идеи: «блюдо» из одного соуса,
        # оставшееся после выброшенной основы, только запутает шефа.
        (collapsed if built.collapsed else ideas).append(built)

    if not ideas:
        names = ", ".join(f"«{i.name}»" for i in collapsed) or "ничего"
        return [], (
            f"Идеи не собрались: модель предложила состав, которого нет в базе "
            f"({names}). Попробуй переформулировать или назвать категорию."
        )

    if collapsed:
        lost = "; ".join(
            f"«{i.name}» (не нашёл: {', '.join(i.dropped)})" for i in collapsed
        )
        ideas[0].notes.append(
            f"Ещё {len(collapsed)} вариант(а) отбросил — от них после сверки "
            f"с базой почти ничего не осталось: {lost}"
        )
    return ideas, None
