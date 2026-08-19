"""Палитра для генератора идей — что модель имеет право использовать.

Собирается детерминированно из ING. Без палитры модель выдумывает ингредиенты,
которых нет на складе, и идея превращается в фантазию: резолвить нечего, UC
посчитать не из чего.
"""
import random
from dataclasses import dataclass, field

from src.data.models import Ingredient
from src.data.sheets import KitchenData


@dataclass
class Palette:
    """Что доступно модели для идеи."""

    # {категория: [(имя, есть ли цена для расчёта UC)]}
    by_category: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)
    existing_dishes: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_category.values())

    @property
    def priced(self) -> int:
        return sum(1 for items in self.by_category.values() for _, ok in items if ok)

    def as_prompt_text(self, rng: random.Random | None = None) -> str:
        """Палитра в текст для промпта: по категориям, без цен.

        Цены модели НЕ показываем: она не должна ими оперировать и тем более
        печатать их в ответе. Пометка нужна только чтобы предпочитать позиции,
        по которым бот сможет честно посчитать UC.

        rng задан — порядок категорий и позиций перемешивается. Иначе промпт
        каждый раз побайтово одинаковый, и модель выдаёт один и тот же ответ:
        06.08.2026 «Бургер кимчи» пришёл первым в трёх прогонах из трёх.
        """
        categories = sorted(self.by_category)
        if rng is not None:
            rng.shuffle(categories)

        lines = []
        for category in categories:
            items = sorted(self.by_category[category], key=lambda t: t[0].lower())
            if rng is not None:
                rng.shuffle(items)
            names = ", ".join(
                name if ok else f"{name} (нет цены)" for name, ok in items
            )
            lines.append(f"{category}: {names}")
        return "\n".join(lines)


def can_be_costed(ing: Ingredient) -> bool:
    """Посчитается ли этот ингредиент в UC.

    Штучный без «Вес 1 шт» посчитать нельзя — по той же причине, по которой
    калькулятор ставит ему стоимость 0 и пишет предупреждение (src/calc/costs.py).
    """
    if ing.price_per_unit is None or ing.price_per_unit == 0:
        return False
    if ing.unit == "шт" and not ing.weight_per_unit_g:
        return False
    return True


def build_palette(data: KitchenData, category: str | None = None) -> Palette:
    """Палитра из активных ингредиентов + существующие блюда для контекста.

    Архив не берём: шеф вывел эти позиции из оборота, предлагать их в новое
    блюдо нельзя (то же правило, что в `_resolve_ingredient_for_create`).
    Ингредиенты без цены оставляем — среди них всё свежее, что шеф завёл под
    новинки (кимчи, дамплинги). Но помечаем, чтобы модель предпочитала
    посчитываемые, а бот потом честно сказал, по скольким позициям есть UC.
    """
    # Дедуп по имени: «Булочка для датского хот дога» и «Рыбные палочки» лежат
    # в ING дважды (известные дубли id). В промпте это шум и лишние токены.
    # Если одна из копий посчитываемая — оставляем её.
    by_category: dict[str, list[tuple[str, bool]]] = {}
    seen: dict[str, tuple[str, int]] = {}   # имя → (категория, индекс в списке)
    for ing in data.ingredients.values():
        if ing.status == "архив":
            continue
        cat = (ing.category or "Прочее").strip() or "Прочее"
        costed = can_be_costed(ing)
        key = ing.name.strip().lower()
        if key in seen:
            prev_cat, idx = seen[key]
            if costed and not by_category[prev_cat][idx][1]:
                by_category[prev_cat][idx] = (ing.name, True)
            continue
        by_category.setdefault(cat, []).append((ing.name, costed))
        seen[key] = (cat, len(by_category[cat]) - 1)

    # Существующие блюда — чтобы модель не предлагала то, что уже в меню.
    # При заданной категории показываем только её: весь список из 132 блюд
    # раздувает промпт и размывает задачу.
    dishes = [d for d in data.dishes.values() if d.status != "архив"]
    if category:
        needle = category.strip().lower()
        dishes = [d for d in dishes if (d.category or "").strip().lower() == needle]
    existing = sorted(d.name for d in dishes)

    return Palette(
        by_category=by_category,
        existing_dishes=existing,
        categories=[c for c, _ in data.dish_categories()],
    )
