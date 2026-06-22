"""Тест калькулятора — детальный вывод по каждому блюду.

Запуск:
    python -m scripts.test_calc                # сводка по всем + детально
    python -m scripts.test_calc B001           # только B001 детально
    python -m scripts.test_calc B001 B004      # несколько блюд детально
"""
import sys

from src.calc.costs import calculate_dish_uc
from src.data.sheets import get_data


def print_dish_full(data, dish_id):
    """Детальный отчёт по одному блюду."""
    result = calculate_dish_uc(data, dish_id)
    if result is None:
        print(f"\n{dish_id}: блюдо не найдено\n")
        return

    print()
    print("=" * 95)
    print(f"{result.dish_id} — {result.dish_name}")
    print(
        f"Цена меню: {result.price_menu} ₽ | "
        f"UC: {result.uc_rub} ₽ ({result.uc_percent}%) | "
        f"Маржа: {result.margin_rub} ₽ ({result.margin_percent}%) | "
        f"Выход: {result.output_grams} г"
    )
    print("=" * 95)

    main_items = [i for i in result.ingredients if i.row_type == "Основной"]
    pack_items = [i for i in result.ingredients if i.row_type == "Упаковка"]

    if main_items:
        print("\nСостав:")
        for it in main_items:
            unit_info = f"{float(it.price_per_unit):>8.2f} ₽/{it.unit}"
            if it.unit == "шт" and it.weight_per_piece_g:
                unit_info += f" ({int(it.weight_per_piece_g)} г/шт)"
            share = f"{float(it.share_percent):>5.1f}%" if it.share_percent else "   -  "
            print(
                f"  {it.name[:28]:<28} {float(it.weight_g):>6.1f} г  "
                f"{unit_info:<25} → {float(it.cost_rub):>7.2f} ₽  {share}"
            )

    if pack_items:
        print("\nУпаковка:")
        for it in pack_items:
            share = f"{float(it.share_percent):>5.1f}%" if it.share_percent else "   -  "
            print(
                f"  {it.name[:28]:<28} {float(it.weight_g):>6.1f} шт  "
                f"{float(it.price_per_unit):>8.2f} ₽/шт              "
                f"→ {float(it.cost_rub):>7.2f} ₽  {share}"
            )

    if result.warnings:
        print(f"\nЗамечания ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"  ⚠ {w}")
    print()


def print_summary(data):
    """Сводка по всем блюдам."""
    print()
    print("=" * 95)
    print(
        f"{'ID':<6} {'Название':<50} {'UC, ₽':>9} {'UC %':>6} "
        f"{'Маржа %':>8} {'Зам.':>5}"
    )
    print("=" * 95)
    for dish in sorted(data.dishes.values(), key=lambda d: d.id):
        result = calculate_dish_uc(data, dish.id)
        if result is None:
            continue
        n_warn = len(result.warnings)
        warn_mark = f"{n_warn}" if n_warn else "-"
        print(
            f"{dish.id:<6} {dish.name[:48]:<50} "
            f"{float(result.uc_rub):>9.2f} "
            f"{float(result.uc_percent):>5.1f}% "
            f"{float(result.margin_percent):>7.1f}% "
            f"{warn_mark:>5}"
        )
    print("=" * 95)


def main():
    # На Windows-консоли (cp1251) символ ₽ роняет вывод UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    print("Подключаемся к Google Sheets...")
    data = get_data()
    print(
        f"  Загружено: ингредиентов={len(data.ingredients)}, "
        f"упаковок={len(data.packagings)}, "
        f"блюд={len(data.dishes)}, "
        f"ТТК-блюд={len(data.ttk_by_dish)}"
    )

    if len(sys.argv) > 1:
        # Передали ID блюд — показываем только их подробно
        for dish_id in sys.argv[1:]:
            print_dish_full(data, dish_id)
        return

    # Иначе — сводка + детально по всем с заполненной ТТК
    print_summary(data)
    print()
    print("Детально по блюдам с непустым составом:")
    for dish_id in sorted(data.ttk_by_dish.keys()):
        print_dish_full(data, dish_id)


if __name__ == "__main__":
    main()