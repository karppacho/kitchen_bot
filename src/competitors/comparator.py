"""Сравнение срезов меню. Чистый Python, никакой LLM.

Позиции матчатся по ключу (norm_name, norm_weight) внутри одного сайта:
«Капучино 0,2 л» и «Капучино 0,4 л» — разные позиции.
Переименования не угадываем: консервативно фиксируем removed + added.
"""
import re
from decimal import Decimal

from src.competitors.models import Diff, ExtractedItem

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().replace("ё", "е")
    s = _PUNCT_RE.sub(" ", s)
    return _SPACES_RE.sub(" ", s).strip()


def norm_weight(s: str | None) -> str:
    # Вес сравниваем по цифрам и буквам: «250 г» == «250г», но != «300 г»
    return norm_name(s)


def _key(item: ExtractedItem) -> tuple[str, str]:
    return (norm_name(item.item), norm_weight(item.weight))


def _index(items: list[ExtractedItem]) -> dict[tuple[str, str], ExtractedItem]:
    idx: dict[tuple[str, str], ExtractedItem] = {}
    for it in items:
        idx.setdefault(_key(it), it)  # дубли: берём первое вхождение
    return idx


def _price_diff(old: ExtractedItem, new: ExtractedItem,
                threshold_pct: Decimal, threshold_rub: Decimal) -> Diff | None:
    if old.price_rub is None or new.price_rub is None:
        return None
    old_p = Decimal(str(old.price_rub))
    new_p = Decimal(str(new.price_rub))
    delta = new_p - old_p
    if delta == 0:
        return None
    threshold = max(old_p * threshold_pct / Decimal(100), threshold_rub)
    if abs(delta) < threshold:
        return None
    pct = (delta / old_p * Decimal(100)).quantize(Decimal("0.1")) if old_p else None
    return Diff(
        change_type="price_up" if delta > 0 else "price_down",
        item=new.item,
        weight=new.weight,
        old_price=old.price_rub,
        new_price=new.price_rub,
        delta_rub=float(delta),
        delta_percent=float(pct) if pct is not None else None,
    )


def diff_snapshots(
    old_items: list[ExtractedItem],
    new_items: list[ExtractedItem],
    threshold_pct: Decimal,
    threshold_rub: Decimal,
) -> list[Diff]:
    """Диффы нового среза относительно старого.

    Изменение цены значимо, если |дельта| >= max(старая_цена * pct / 100, rub).
    Позиции без цены в одном из срезов в ценовое сравнение не попадают.

    Матчинг в два прохода:
    1) точный ключ (имя, вес);
    2) непарные — по одному имени, если оно уникально с обеих сторон.
       LLM от прогона к прогону нестабильно заполняет вес («Стандартный» ↔ пусто),
       без второго прохода это давало ложные пары «пропала + новинка».
    """
    old_idx = _index(old_items)
    new_idx = _index(new_items)
    diffs: list[Diff] = []

    unmatched_old: list[ExtractedItem] = []
    for key, old in old_idx.items():
        new = new_idx.get(key)
        if new is None:
            unmatched_old.append(old)
            continue
        d = _price_diff(old, new, threshold_pct, threshold_rub)
        if d is not None:
            diffs.append(d)
    unmatched_new = [new for key, new in new_idx.items() if key not in old_idx]

    # Второй проход: только по имени. Берём имена, встречающиеся РОВНО один раз
    # среди непарных с каждой стороны, — «Капучино 0,2» и «0,4» так не склеятся.
    def _by_unique_name(items: list[ExtractedItem]) -> dict[str, ExtractedItem]:
        counts: dict[str, int] = {}
        for it in items:
            counts[norm_name(it.item)] = counts.get(norm_name(it.item), 0) + 1
        return {norm_name(it.item): it for it in items if counts[norm_name(it.item)] == 1}

    old_by_name = _by_unique_name(unmatched_old)
    new_by_name = _by_unique_name(unmatched_new)
    matched_names = set(old_by_name) & set(new_by_name)
    for name in matched_names:
        d = _price_diff(old_by_name[name], new_by_name[name], threshold_pct, threshold_rub)
        if d is not None:
            diffs.append(d)

    for old in unmatched_old:
        if norm_name(old.item) not in matched_names:
            diffs.append(Diff(change_type="item_removed", item=old.item, weight=old.weight,
                              old_price=old.price_rub))
    for new in unmatched_new:
        if norm_name(new.item) not in matched_names:
            diffs.append(Diff(change_type="item_added", item=new.item, weight=new.weight,
                              new_price=new.price_rub))

    return diffs
