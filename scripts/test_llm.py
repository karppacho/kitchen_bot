"""REPL для тестирования LLM-слоя без Telegram.

Запуск:
    python -m scripts.test_llm

Дальше пишешь запросы прямо в консоль, видишь ответы. Удобно для отладки промптов
и описаний tools — быстрее, чем через Telegram.

Выход: пустая строка или Ctrl+C.
"""
import sys

from src.llm.client import chat


def main():
    # На Windows-консоли (cp1251) символ ₽ роняет вывод UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    print("LLM REPL. Пиши запросы. Пустая строка — выход.\n")
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПока.")
            break
        if not text:
            print("Пока.")
            break

        try:
            # Фиксированный user_id, чтобы REPL помнил контекст между репликами
            result = chat(text, user_id=0)
            reply = result.text
            if result.files:
                reply += "\n[файлы: " + ", ".join(result.files) + "]"
        except Exception as e:
            reply = f"[Ошибка] {e}"

        print(f"\n{reply}\n")


if __name__ == "__main__":
    main()
