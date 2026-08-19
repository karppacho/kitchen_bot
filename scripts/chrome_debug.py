"""Запуск Chrome с отладочным портом — для сайтов с fetch_method='cdp'.

Зачем: Ozon и Самокат детектируют ЛЮБОЙ автоматизированный браузер. Проверено
09.08.2026: встроенный Chromium, настоящий Chrome (channel='chrome'), headless
и с видимым окном — блокируются одинаково. Пускает только обжитой профиль с
историей и куками. Поэтому бот не поднимает свой браузер, а подключается к
этому — уже запущенному.

Запуск:
    python -m scripts.chrome_debug          # запустить и оставить окно открытым
    python -m scripts.chrome_debug --print  # только показать команду

Первый раз профиль надо «обжить»: открыть в этом окне ozon.ru и samokat.ru,
у Самоката пройти капчу (повернуть картинку). Дальше куки живут в профиле,
и еженедельная проверка ходит через это же окно.

⚠️ --user-data-dir обязателен. С Chrome 136 отладочный порт на профиле по
умолчанию игнорируется из соображений безопасности, так что профиль отдельный.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from src.config import settings

if sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

_CHROME_PATHS = (
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # Linux (VPS): ставится как google-chrome-stable или chromium
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def find_chrome() -> str | None:
    return next((p for p in _CHROME_PATHS if Path(p).exists()), None)


def _headless_env() -> bool:
    """Linux без графической сессии: окно показать некуда."""
    return sys.platform.startswith("linux") and not os.environ.get("DISPLAY")


def profile_dir() -> Path:
    return Path(settings.competitors_browser_profiles_dir).resolve() / "chrome_cdp"


def port() -> int:
    return urlparse(settings.competitors_cdp_url).port or 9222


def already_running() -> bool:
    try:
        with urlopen(f"{settings.competitors_cdp_url}/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Chrome с отладочным портом для CDP-проверок")
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="только показать команду, не запускать")
    args = parser.parse_args()

    chrome = find_chrome()
    if chrome is None:
        print("Не нашёл chrome.exe. Проверял:")
        for p in _CHROME_PATHS:
            print("   ", p)
        return

    user_data = profile_dir()
    cmd = [chrome, f"--remote-debugging-port={port()}", f"--user-data-dir={user_data}"]
    if _headless_env():
        # На VPS без графической сессии окна нет. Браузер всё равно запускается
        # НЕ через Playwright, то есть без флагов автоматизации — ради этого всё
        # и затевалось. Капчу в таком режиме пройти некому: если сайт её покажет,
        # нужен Xvfb + VNC либо ручной режим (см. DEPLOY.md).
        cmd += ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"]

    if args.print_only:
        print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        return

    if already_running():
        print(f"Chrome с отладочным портом уже слушает {settings.competitors_cdp_url} — всё готово.")
        return

    user_data.mkdir(parents=True, exist_ok=True)
    print(f"Запускаю Chrome, профиль: {user_data}")
    subprocess.Popen(cmd)
    print(f"Отладочный порт: {settings.competitors_cdp_url}")
    print()
    print("Что сделать в открывшемся окне (первый раз):")
    print("  1. Зайти на https://www.ozon.ru/brand/hot-fresh-101879249/")
    print("  2. Зайти на https://samokat.ru/category/vsyo-goryachee-1 и пройти капчу")
    print("  3. Окно НЕ закрывать — через него работает проверка конкурентов")
    print()
    print("Дальше: python -m scripts.check_competitors --site samokat --no-llm")


if __name__ == "__main__":
    main()
