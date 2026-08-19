"""Тесты генератора идей. Офлайн: модель подменяется фейковой complete_fn.

Главное, что защищаем: модель придумывает только состав, а все деньги и КБЖУ
считает калькулятор. Если сюда просочится цифра от модели — доверие к боту
рухнет целиком, а не только к идеям.
"""
import json
import random
import re
import time
from decimal import Decimal

import pytest

from src.data.models import Dish, Ingredient
from src.ideas.generator import (
    MODE_FROM_BASE,
    MODE_NEW,
    generate_ideas,
    parse_ideas,
)
from src.ideas.palette import build_palette, can_be_costed
from src.llm.format import format_dish_ideas
from tests.test_calc import make_data


def _data():
    """База: тортилья (шт с весом), салат (кг), капуста без цены, архивный соус."""
    d = make_data()
    d.ingredients[7] = Ingredient(
        id=7, category="Овощи", name="Капуста кимчи", unit="шт",
        price_per_unit=Decimal("0"), status="активный",
    )
    d.ingredients[8] = Ingredient(
        id=8, category="Соусы", name="Соус архивный", unit="кг",
        price_per_unit=Decimal("300"), status="архив",
    )
    d.dishes["T001"] = Dish(
        id="T001", name="Тест-ролл", category="Ролл",
        price_menu=Decimal("200"), status="активное",
    )
    return d


def _fake(payload) -> "callable":
    """complete_fn, отдающая заранее заданный ответ модели."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    def _complete(prompt: str, temperature: float = 0.9) -> str:
        _complete.prompt = prompt
        _complete.temperature = temperature
        return text

    return _complete


def _idea(name="Новинка", ingredients=None, category="Ролл"):
    return {
        "name": name,
        "idea": "Что-то новое.",
        "category": category,
        "ingredients": ingredients or [
            {"name": "Тортилья", "grams": 60},
            {"name": "Салат", "grams": 40},
        ],
    }


# ---------- палитра ----------

def test_palette_excludes_archive():
    p = build_palette(_data())
    names = [n for items in p.by_category.values() for n, _ in items]
    assert "Соус архивный" not in names
    assert "Тортилья" in names


def test_palette_marks_uncostable():
    """Позиции без цены остаются, но помечены: из них шеф делает новинки."""
    p = build_palette(_data())
    flags = {n: ok for items in p.by_category.values() for n, ok in items}
    assert flags["Тортилья"] is True
    assert flags["Капуста кимчи"] is False
    assert "Капуста кимчи (нет цены)" in p.as_prompt_text()
    assert p.priced < p.total


def test_can_be_costed_rules():
    piece_no_weight = Ingredient(
        id=1, category="х", name="х", unit="шт", price_per_unit=Decimal("10"),
    )
    assert can_be_costed(piece_no_weight) is False
    piece_no_weight.weight_per_unit_g = Decimal("70")
    assert can_be_costed(piece_no_weight) is True
    piece_no_weight.price_per_unit = Decimal("0")
    assert can_be_costed(piece_no_weight) is False


def test_palette_filters_existing_dishes_by_category():
    d = _data()
    d.dishes["T002"] = Dish(id="T002", name="Пицца тест", category="Пицца")
    assert build_palette(d, "Ролл").existing_dishes == ["Тест-ролл"]
    assert build_palette(d, "Пицца").existing_dishes == ["Пицца тест"]


def test_palette_dedupes_names():
    """Дубли ING («Булочка для датского хот дога» 34/121) не должны идти дважды."""
    d = _data()
    d.ingredients[9] = Ingredient(
        id=9, category="Основа", name="Тортилья", unit="кг",
        price_per_unit=Decimal("500"), status="активный",
    )
    p = build_palette(d)
    names = [n for items in p.by_category.values() for n, _ in items]
    assert names.count("Тортилья") == 1


def test_palette_dedup_prefers_costable_copy():
    """Из двух копий оставляем ту, по которой можно посчитать UC."""
    d = _data()
    d.ingredients[3].price_per_unit = None          # «Салат» без цены
    d.ingredients[9] = Ingredient(
        id=9, category="Овощи", name="Салат", unit="кг",
        price_per_unit=Decimal("500"), status="активный",
    )
    flags = {n: ok for items in build_palette(d).by_category.values() for n, ok in items}
    assert flags["Салат"] is True


# ---------- разнообразие ----------

def test_palette_shuffle_changes_order():
    p = build_palette(_data())
    plain = p.as_prompt_text()
    shuffled = [p.as_prompt_text(random.Random(seed)) for seed in range(8)]
    assert any(s != plain for s in shuffled), "перемешивание не меняет промпт"


def test_palette_shuffle_is_reproducible_with_seed():
    p = build_palette(_data())
    assert p.as_prompt_text(random.Random(42)) == p.as_prompt_text(random.Random(42))


def test_palette_shuffle_keeps_all_items():
    """Перемешали — но ничего не потеряли и не добавили."""
    p = build_palette(_data())
    plain = sorted(re.findall(r"[\w\"' ]+", p.as_prompt_text()))
    mixed = sorted(re.findall(r"[\w\"' ]+", p.as_prompt_text(random.Random(1))))
    assert plain == mixed


def test_angle_appears_in_prompt_and_varies():
    """Случайный «угол» — ещё один способ сдвинуть вывод модели."""
    from src.ideas.generator import ANGLES
    seen = set()
    for seed in range(12):
        fake = _fake([_idea()])
        generate_ideas(_data(), "придумай", fake, rng=random.Random(seed))
        angle = next(a for a in ANGLES if a in fake.prompt)
        seen.add(angle)
    assert len(seen) > 1, "угол не меняется между вызовами"


def test_avoid_list_goes_into_prompt():
    fake = _fake([_idea()])
    generate_ideas(_data(), "придумай", fake, avoid=["Бургер кимчи", "Бургер с форелью"])
    assert "уже предлагал" in fake.prompt.lower()
    assert "Бургер кимчи" in fake.prompt


def test_no_avoid_block_when_nothing_suggested_yet():
    fake = _fake([_idea()])
    generate_ideas(_data(), "придумай", fake)
    assert "уже предлагал" not in fake.prompt.lower()


def test_prompt_limits_unpriced_positions():
    """Правило против бургера за 15 рублей должно быть в промпте."""
    fake = _fake([_idea()])
    generate_ideas(_data(), "придумай", fake)
    assert "НЕ БОЛЬШЕ ОДНОЙ" in fake.prompt


# ---------- память показанных идей ----------

def test_recent_remembers_and_expires(monkeypatch):
    from src.ideas import recent
    recent.clear(1)
    recent.remember(1, ["Бургер кимчи", "Бургер с форелью"])
    assert recent.recent(1) == ["Бургер кимчи", "Бургер с форелью"]

    # протухание по TTL. Момент фиксируем ДО подмены: иначе лямбда вызовет
    # саму себя через уже подменённый time.time.
    later = time.time() + recent.TTL_SECONDS + 1
    monkeypatch.setattr(recent.time, "time", lambda: later)
    assert recent.recent(1) == []


def test_recent_no_duplicates_and_capped():
    from src.ideas import recent
    recent.clear(2)
    recent.remember(2, ["Бургер"])
    recent.remember(2, ["бургер", "Ролл"])          # регистр не плодит дубли
    assert recent.recent(2) == ["Бургер", "Ролл"]

    recent.remember(2, [f"Блюдо {i}" for i in range(recent.MAX_NAMES + 5)])
    assert len(recent.recent(2)) <= recent.MAX_NAMES


def test_recent_is_per_user():
    from src.ideas import recent
    recent.clear(3), recent.clear(4)
    recent.remember(3, ["Только третьего"])
    assert recent.recent(4) == []


def test_recent_ignores_missing_user():
    from src.ideas import recent
    recent.remember(None, ["Что-то"])
    assert recent.recent(None) == []


def test_competitors_tool_exists_and_is_described():
    """Шеф пишет «добавь конкурента <ссылка>» текстом, а не командой.

    06.08.2026 бот на такую фразу ответил «это не входит в мои функции»,
    хотя умеет: управление конкурентами было только в Telegram-командах.
    """
    import src.llm.client as client
    from src.llm.tools import TOOLS

    assert "manage_competitors" in client.TOOL_HANDLERS
    tool = next(t for t in TOOLS if t["function"]["name"] == "manage_competitors")
    desc = tool["function"]["description"]
    assert "НИКОГДА не отвечай" in desc           # прямой запрет на «не умею»
    assert "check_competitors" in desc            # долгую проверку сюда не тащим


def test_only_suggest_dishes_is_user_aware():
    """Явный список: остальные tool-ы вызываются как раньше, одним аргументом."""
    import inspect
    import src.llm.client as client

    assert client.USER_AWARE_TOOLS == {"suggest_dishes"}
    for name in client.USER_AWARE_TOOLS:
        params = inspect.signature(client.TOOL_HANDLERS[name]).parameters
        assert len(params) == 2, f"{name} должен принимать (args, user_id)"
    for name, handler in client.TOOL_HANDLERS.items():
        if name not in client.USER_AWARE_TOOLS:
            params = inspect.signature(handler).parameters
            assert len(params) == 1, f"{name} не должен требовать user_id"


def test_suggest_dishes_remembers_what_it_showed(monkeypatch):
    """Показанные идеи попадают в память — на «давай ещё» они уже в avoid."""
    from src.ideas import recent
    import src.llm.client as client

    recent.clear(777)
    monkeypatch.setattr(client, "get_data", lambda: _data())
    monkeypatch.setattr(client, "_llm_complete", _fake([_idea(name="Уникальный")]))

    client._tool_suggest_dishes({"brief": "придумай"}, 777)
    assert recent.recent(777) == ["Уникальный"]


# ---------- разбор ответа модели ----------

def test_parse_plain_json():
    assert len(parse_ideas('[{"name": "A"}, {"name": "B"}]')) == 2


def test_parse_json_in_fences():
    raw = '```json\n[{"name": "A"}]\n```'
    assert parse_ideas(raw) == [{"name": "A"}]


def test_parse_json_with_text_around():
    """Модель любит писать «Вот идеи:» перед JSON — вырезаем массив."""
    raw = 'Вот три идеи:\n[{"name": "A"}]\nНадеюсь, подойдёт!'
    assert parse_ideas(raw) == [{"name": "A"}]


def test_parse_single_object():
    assert parse_ideas('{"name": "A"}') == [{"name": "A"}]


def test_parse_broken_returns_empty():
    assert parse_ideas("это не json вообще") == []
    assert parse_ideas("") == []


# ---------- режим «из базы» ----------

def test_numbers_come_from_calculator_not_model():
    """Ключевой тест: себестоимость считает калькулятор по составу."""
    ideas, err = generate_ideas(_data(), "придумай", _fake([_idea()]))
    assert err is None
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.uc is not None
    # 60 г тортильи (10 ₽/шт при 70 г/шт) + 40 г салата (500 ₽/кг)
    assert idea.uc.uc_rub == Decimal("28.57")
    assert idea.uc.output_grams == Decimal("100")
    assert idea.full_coverage is True


def test_unknown_ingredient_dropped_in_base_mode():
    ideas, err = generate_ideas(
        _data(), "придумай",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Трюфель чёрный", "grams": 10},
        ])]),
    )
    assert err is None
    idea = ideas[0]
    assert [i.matched_name for i in idea.ingredients] == ["Тортилья"]
    assert any("Трюфель" in n and "нет в базе" in n for n in idea.notes)
    assert idea.to_buy == []


def test_archived_ingredient_not_used():
    ideas, _ = generate_ideas(
        _data(), "придумай",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Соус архивный", "grams": 20},
        ])]),
    )
    assert [i.matched_name for i in ideas[0].ingredients] == ["Тортилья"]


def test_partial_coverage_reported():
    """Ингредиент без цены в составе → покрытие неполное, это видно."""
    ideas, _ = generate_ideas(
        _data(), "азиатское",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Капуста кимчи", "grams": 30},
        ])]),
    )
    idea = ideas[0]
    assert len(idea.ingredients) == 2
    assert idea.costed_count == 1
    assert idea.full_coverage is False


# ---------- режим «новое» ----------

def test_new_mode_keeps_unknown_and_lists_to_buy():
    ideas, _ = generate_ideas(
        _data(), "что-то совсем новое",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Трюфель чёрный", "grams": 10},
        ])]),
        mode=MODE_NEW,
    )
    idea = ideas[0]
    assert len(idea.ingredients) == 2
    assert idea.to_buy == ["Трюфель чёрный"]
    unknown = next(i for i in idea.ingredients if not i.known)
    assert unknown.costed is False
    # В UC вошла только тортилья
    assert idea.uc.uc_rub == Decimal("8.57")


# ---------- граммовки ----------

@pytest.mark.parametrize("bad", [0, -5, 99999, "много", None])
def test_absurd_grams_rejected(bad):
    ideas, _ = generate_ideas(
        _data(), "придумай",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Салат", "grams": bad},
        ])]),
    )
    idea = ideas[0]
    assert [i.matched_name for i in idea.ingredients] == ["Тортилья"]
    assert any("вес" in n for n in idea.notes)


# ---------- передача в create_dish ----------

def test_payload_matches_create_dish_format():
    ideas, _ = generate_ideas(
        _data(), "придумай",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Трюфель чёрный", "grams": 10},
        ])]),
        mode=MODE_NEW,
    )
    payload = ideas[0].as_create_dish_payload()
    assert payload == [{"name": "Тортилья", "grams": 60.0}]   # без ненайденного
    assert all(set(p) == {"name", "grams"} for p in payload)


# ---------- ошибки ----------

def test_palette_marker_copied_into_name_is_stripped():
    """Модель тащит пометку «(нет цены)» в состав — имя всё равно должно найтись.

    Всплыло на живом прогоне: «Сырный соус Блючиз (нет цены)» не резолвился,
    и позиция, которая В БАЗЕ ЕСТЬ, молча выбрасывалась.
    """
    ideas, _ = generate_ideas(
        _data(), "придумай",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Капуста кимчи (нет цены)", "grams": 30},
        ])]),
    )
    idea = ideas[0]
    assert [i.matched_name for i in idea.ingredients] == ["Тортилья", "Капуста кимчи"]
    assert idea.dropped == []


def test_clean_name_keeps_meaningful_parentheses_only_at_tail():
    from src.ideas.generator import _clean_name
    assert _clean_name("Соус (нет цены)") == "Соус"
    assert _clean_name("Соус томатный( для пиццы)") == "Соус томатный"
    assert _clean_name("Тортилья") == "Тортилья"
    assert _clean_name("(нет цены)") == "(нет цены)"      # пустой остаток не годится


def test_collapsed_idea_is_dropped_not_shown():
    """Развалившийся вариант не показываем: «блюдо» из одного соуса — не идея.

    Всплыло на живом прогоне: модель написала «Пельмени с говединой» (опечатка),
    позиция не нашлась, и от варианта остались 30 г соуса.
    """
    ideas, err = generate_ideas(
        _data(), "придумай",
        _fake([
            _idea(name="Нормальная"),
            _idea(name="Пельмени", ingredients=[
                {"name": "Пельмени с говединой", "grams": 200},
                {"name": "Салат", "grams": 20},
            ]),
        ]),
    )
    assert err is None
    assert [i.name for i in ideas] == ["Нормальная"]
    assert any("Пельмени" in n and "говединой" in n for n in ideas[0].notes)


def test_all_collapsed_gives_error_with_names():
    ideas, err = generate_ideas(
        _data(), "придумай",
        _fake([_idea(name="Фуагра-бургер", ingredients=[
            {"name": "Фуагра", "grams": 100},
        ])]),
    )
    assert ideas == []
    assert "Фуагра-бургер" in err


def test_idea_survives_when_only_minor_position_dropped():
    """Выпала мелочь, основа на месте — вариант остаётся."""
    ideas, _ = generate_ideas(
        _data(), "придумай",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Салат", "grams": 40},
            {"name": "Трюфель", "grams": 5},
        ])]),
    )
    assert len(ideas) == 1
    assert len(ideas[0].ingredients) == 2


def test_unparsable_model_answer_gives_error():
    ideas, err = generate_ideas(_data(), "придумай", _fake("мусор"))
    assert ideas == []
    assert "не вернула" in err


def test_idea_with_all_ingredients_unknown_is_not_shown():
    """Если в базе нет ничего из состава — показывать нечего, это не идея."""
    ideas, err = generate_ideas(
        _data(), "придумай",
        _fake([_idea(ingredients=[{"name": "Фуагра", "grams": 50}])]),
    )
    assert ideas == []
    assert "Фуагра" not in err or "не собрались" in err


def test_count_limits_variants():
    ideas, _ = generate_ideas(
        _data(), "придумай",
        _fake([_idea(name=f"Идея {i}") for i in range(5)]),
        count=2,
    )
    assert len(ideas) == 2


def test_temperature_passed_through():
    fake = _fake([_idea()])
    generate_ideas(_data(), "придумай", fake, temperature=0.75)
    assert fake.temperature == 0.75


def test_prompt_contains_palette_and_brief():
    fake = _fake([_idea()])
    generate_ideas(_data(), "что-нибудь азиатское", fake)
    assert "что-нибудь азиатское" in fake.prompt
    assert "Тортилья" in fake.prompt
    assert "Тест-ролл" in fake.prompt          # существующие блюда — в контекст
    assert "{" not in fake.prompt.split("Верни СТРОГО")[0].replace("{{", "")


# ---------- вывод ----------

def test_display_has_numbers_and_no_raw_json():
    ideas, _ = generate_ideas(_data(), "придумай", _fake([_idea()]))
    out = format_dish_ideas(ideas, MODE_FROM_BASE)
    assert "Новинка" in out
    assert "Себестоимость: 28.57" in out
    assert "Выход: 100 г" in out
    assert "создай первый" in out
    assert "Граммовки предварительные" in out
    assert '"ingredients"' not in out          # сырой JSON не просочился


def test_display_warns_about_partial_coverage():
    ideas, _ = generate_ideas(
        _data(), "азиатское",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Капуста кимчи", "grams": 30},
        ])]),
    )
    out = format_dish_ideas(ideas, MODE_FROM_BASE)
    assert "по 1 из 2 позиций" in out
    assert "реальный UC будет выше" in out


def test_display_hides_zero_cost_when_nothing_costed():
    """Ноль вместо себестоимости — бесполезное число, шеф прочитает его как настоящее.

    Всплыло на живом прогоне: запрос «азиатское» вытянул только позиции без цены,
    и бот показал «Себестоимость: 0.00 ₽».
    """
    ideas, _ = generate_ideas(
        _data(), "азиатское",
        _fake([_idea(ingredients=[{"name": "Капуста кимчи", "grams": 30}])]),
    )
    out = format_dish_ideas(ideas, MODE_FROM_BASE)
    assert "0.00" not in out
    assert "Себестоимость посчитать не могу" in out
    assert "Выход:" in out                     # выход и КБЖУ всё равно полезны
    assert "ккал" in out


def test_display_lists_what_to_buy_in_new_mode():
    ideas, _ = generate_ideas(
        _data(), "новое",
        _fake([_idea(ingredients=[
            {"name": "Тортилья", "grams": 60},
            {"name": "Трюфель чёрный", "grams": 10},
        ])]),
        mode=MODE_NEW,
    )
    out = format_dish_ideas(ideas, MODE_NEW)
    assert "Нужно закупить" in out
    assert "Трюфель чёрный" in out
    assert "нет в базе" in out
