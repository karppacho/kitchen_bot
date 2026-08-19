"""Оркестратор проверки конкурентов: fetch → extract → store → diff → notify → export.

Один прогон за раз (asyncio.Lock): крон и /check_competitors не пересекаются.
Ошибка одного сайта не роняет прогон — уходит строкой «не смог проверить» в сводку.
Сводка отправляется ВСЕГДА (политика ошибок из ТЗ: алертим, не молчим).
"""
import asyncio
import random
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from loguru import logger

from src.competitors import comparator, extractor, fetcher, storage
from src.competitors.format import format_check_summary
from src.competitors.models import CheckSiteResult, Competitor, ExtractedItem
from src.config import settings

_check_lock = asyncio.Lock()

# Меньше позиций при большом тексте страницы — экстракция «подозрительная»
_SUSPECT_MIN_ITEMS = 5
_SUSPECT_MIN_CHARS = 5000

# Признаки того, что на странице вообще есть цены. Страница меню без единого
# упоминания рубля — это не меню, а заглушка: так ловятся антибот-страницы,
# формулировок которых ещё нет в BLOCK_MARKERS (порог _SUSPECT_MIN_CHARS их
# пропускает, они короткие).
_PRICE_MARKERS = ("₽", "руб")

_RAW_DIR = Path("data/raw")

ALREADY_RUNNING_MSG = "⏳ Проверка конкурентов уже идёт — дождись сводки."


def is_check_running() -> bool:
    return _check_lock.locked()


def _raw_dir(comp: Competitor) -> Path:
    domain = comp.url.replace("https://", "").replace("http://", "").strip("/").replace("/", "_")
    path = _RAW_DIR / domain
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_raw(comp: Competitor, text: str) -> str:
    """Сырой текст страницы — на диск: реэкстракция без повторного скрапа."""
    path = _raw_dir(comp) / f"{datetime.now():%Y%m%d_%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _save_failure(comp: Competitor, fr) -> str | None:
    """Улики неудачного захода: текст, HTML, скриншот.

    Раньше при fetch_failed не сохранялось НИЧЕГО — разбор Лавки 06.08.2026
    начался с пустого места (в снимке raw_chars=NULL). Возвращает путь к .txt
    для колонки snapshots.raw_path, чтобы из сводки можно было дойти до улик.
    """
    stem = _raw_dir(comp) / f"failed_{datetime.now():%Y%m%d_%H%M%S}"
    txt_path: str | None = None
    try:
        if fr.text:
            (stem.with_suffix(".txt")).write_text(fr.text, encoding="utf-8")
            txt_path = str(stem.with_suffix(".txt"))
        if fr.html:
            (stem.with_suffix(".html")).write_text(fr.html, encoding="utf-8")
        if fr.screenshot:
            (stem.with_suffix(".png")).write_bytes(fr.screenshot)
    except Exception as e:
        logger.warning(f"[конкуренты] не сохранил улики {comp.url}: {type(e).__name__}: {e}")
    if txt_path or fr.html or fr.screenshot:
        logger.info(f"[конкуренты] улики неудачи {comp.url}: {stem}.*")
    return txt_path


def _filter_llm_flaps(diffs, new_raw_text: str, old_raw_path: str | None):
    """Отсев ложных «пропала»/«новинка», рождённых нестабильностью LLM-экстракции.

    Детерминированная проверка по сырому тексту страниц (не LLM):
    - «пропала из меню», но имя ЕСТЬ в новом сыром тексте → LLM потеряла позицию;
    - «новинка», но имя БЫЛО в старом сыром тексте → LLM теряла её в прошлый раз.
    """
    new_norm = comparator.norm_name(new_raw_text)
    old_norm = None
    if old_raw_path and Path(old_raw_path).exists():
        try:
            old_norm = comparator.norm_name(Path(old_raw_path).read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[конкуренты] не прочитал старый raw {old_raw_path}: {e}")

    kept, dropped = [], 0
    for d in diffs:
        name = comparator.norm_name(d.item)
        if d.change_type == "item_removed" and name and name in new_norm:
            dropped += 1
            continue
        if d.change_type == "item_added" and old_norm is not None and name and name in old_norm:
            dropped += 1
            continue
        kept.append(d)
    if dropped:
        logger.info(f"[конкуренты] отфильтровано {dropped} ложных диффов (нестабильность LLM)")
    return kept


def _process_snapshot_sync(
    comp: Competitor,
    items: list[ExtractedItem],
    raw_text: str,
    raw_path: str | None,
    source: str,
) -> CheckSiteResult:
    """Синхронная часть: снимок → сравнение с прошлым ok-срезом → запись диффов."""
    result = CheckSiteResult(
        competitor_name=comp.name, competitor_url=comp.url,
        status="ok", items_count=len(items),
    )
    few_items = len(items) < _SUSPECT_MIN_ITEMS
    no_prices = not any(m in raw_text.lower() for m in _PRICE_MARKERS)
    if few_items and (len(raw_text) > _SUSPECT_MIN_CHARS or no_prices):
        result.status = "suspect"

    prev = storage.latest_ok_snapshot(comp.id)
    snapshot_id = storage.save_snapshot(
        comp.id, items, source=source, status=result.status,
        raw_chars=len(raw_text), raw_path=raw_path,
    )
    if result.status == "suspect":
        # Плохую экстракцию с прошлым срезом не сравниваем — иначе стена ложных «пропала»
        result.first_snapshot = prev is None
        return result
    if prev is None:
        result.first_snapshot = True
        return result

    old_id, _, old_items, old_raw_path = prev
    diffs = comparator.diff_snapshots(
        old_items, items,
        threshold_pct=Decimal(str(settings.competitors_price_threshold_pct)),
        threshold_rub=Decimal(str(settings.competitors_price_threshold_rub)),
    )
    result.diffs = _filter_llm_flaps(diffs, raw_text, old_raw_path)
    storage.save_changes(comp.id, result.diffs, old_id, snapshot_id)
    return result


async def _check_one(comp: Competitor, headless: bool | None = None) -> CheckSiteResult:
    loop = asyncio.get_running_loop()
    failed = CheckSiteResult(competitor_name=comp.name, competitor_url=comp.url, status="ok")

    if comp.fetch_method == "manual":
        failed.status = "skipped"
        failed.error = "ручной режим — пришли сохранённый HTML страницы меню"
        failed.stale_days = await loop.run_in_executor(
            None, storage.days_since_ok_snapshot, comp.id,
        )
        return failed

    fr = await fetcher.fetch(comp, headless)
    if not fr.ok:
        failed.status = "fetch_failed"
        failed.error = fr.error
        evidence = await loop.run_in_executor(None, _save_failure, comp, fr)
        await loop.run_in_executor(
            None,
            lambda: storage.save_snapshot(
                comp.id, [], status="fetch_failed", raw_chars=len(fr.text),
                raw_path=evidence, error=fr.error,
            ),
        )
        return failed

    raw_path = await loop.run_in_executor(None, _save_raw, comp, fr.text)
    try:
        items, _meta = await loop.run_in_executor(None, extractor.extract_menu, fr.text, comp.name)
    except Exception as e:
        logger.exception(f"[конкуренты] экстракция {comp.url} упала")
        failed.status = "extract_failed"
        failed.error = f"экстракция упала: {e}"
        await loop.run_in_executor(
            None,
            lambda: storage.save_snapshot(
                comp.id, [], status="extract_failed", raw_chars=len(fr.text),
                raw_path=raw_path, error=str(e),
            ),
        )
        return failed

    return await loop.run_in_executor(
        None, _process_snapshot_sync, comp, items, fr.text, raw_path, "auto",
    )


async def run_check(
    bot=None,
    *,
    trigger: str = "manual",
    only_url: str | None = None,
    notify: bool = True,
    export: bool = True,
    notify_user_id: int | None = None,
    headless: bool | None = None,
) -> str:
    """Полный прогон по активным конкурентам. Возвращает текст сводки.

    notify_user_id: кому слать сводку. Задан — только этому пользователю (ручной
    запуск: результат нужен тому, кто попросил). None — всем разрешённым
    (еженедельный крон: это рассылка, её ждут все).
    """
    if _check_lock.locked():
        return ALREADY_RUNNING_MSG

    async with _check_lock:
        loop = asyncio.get_running_loop()
        started = datetime.now()
        logger.info(f"[конкуренты] старт проверки (trigger={trigger}, only={only_url or 'все'})")

        comps = await loop.run_in_executor(None, storage.list_competitors)
        if only_url:
            needle = only_url.lower()
            comps = [c for c in comps if needle in c.url.lower() or needle in c.name.lower()]
        if not comps:
            return "Список конкурентов пуст. Добавь сайт: /add_competitor <url>"

        results: list[CheckSiteResult] = []
        for i, comp in enumerate(comps):
            if i > 0:
                await fetcher.pause_between_sites()
            results.append(await _check_one(comp, headless))

        summary = format_check_summary(results, started)

        if export and settings.competitors_sheets_id and any(r.status == "ok" for r in results):
            from src.competitors import exporter  # ленивый импорт: не тянуть gspread в тесты
            try:
                await loop.run_in_executor(None, exporter.export_run, results)
            except Exception as e:
                logger.exception("[конкуренты] экспорт в Google Sheets упал")
                summary += f"\n\n⚠️ Экспорт в Google Sheets не удался: {e}"

        logger.info(
            f"[конкуренты] прогон закончен за {(datetime.now() - started).seconds} с; "
            f"статусы: {[r.status for r in results]}"
        )

        if notify and bot is not None:
            await _send_summary(bot, summary, notify_user_id)
        return summary


async def _send_summary(bot, summary: str, only_user_id: int | None = None) -> None:
    """Сводка в личку (chat_id == user_id).

    only_user_id задан — шлём одному ему (ручной запуск). Иначе всем разрешённым
    пользователям — это еженедельная рассылка.
    """
    from aiogram.exceptions import TelegramBadRequest
    from src.bot.telegram_text import split_for_telegram

    recipients = (
        [only_user_id] if only_user_id is not None
        else settings.telegram_allowed_user_ids
    )
    for user_id in recipients:
        for chunk in split_for_telegram(summary):
            try:
                await bot.send_message(user_id, chunk)
            except TelegramBadRequest:
                await bot.send_message(user_id, chunk, parse_mode=None)
            except Exception as e:
                logger.warning(f"[конкуренты] не отправилась сводка {user_id}: {e}")
                break


async def ingest_manual_html(comp: Competitor, file_path: str) -> CheckSiteResult:
    """Ручной фолбэк: присланный шефом HTML → тот же пайплайн, source='manual_html'."""
    from src.competitors.html_text import read_uploaded_document

    loop = asyncio.get_running_loop()
    result = CheckSiteResult(competitor_name=comp.name, competitor_url=comp.url, status="ok")
    try:
        text = await loop.run_in_executor(None, read_uploaded_document, file_path)
    except Exception as e:
        result.status = "fetch_failed"
        result.error = f"не разобрал файл: {e}"
        return result
    try:
        items, _meta = await loop.run_in_executor(None, extractor.extract_menu, text, comp.name)
    except Exception as e:
        logger.exception(f"[конкуренты] экстракция ручного HTML {comp.url} упала")
        result.status = "extract_failed"
        result.error = f"экстракция упала: {e}"
        return result
    return await loop.run_in_executor(
        None, _process_snapshot_sync, comp, items, text, file_path, "manual_html",
    )
