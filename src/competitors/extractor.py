"""LLM-экстракция: сырой текст меню → список позиций.

LLM здесь ТОЛЬКО структурирует текст (переписывает цены как есть в JSON).
Всю арифметику (диффы, проценты, пороги) делает comparator — правило проекта.

Свой OpenAI-клиент поверх polza.ai (не импортируем src/llm/client.py —
там tools, история и промпты ТТК, слои не смешиваем).
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from loguru import logger
from openai import OpenAI

from src.competitors.comparator import norm_name, norm_weight
from src.competitors.models import ExtractedItem
from src.config import settings

# Таймаут больше, чем в чат-клиенте: экстракция кусков меню дольше обычного ответа.
# trust_env=False: НЕ ходим через HTTP(S)_PROXY из окружения — это локальный прокси
# VPN-клиента (127.0.0.1:...), который мёртв при выключенном VPN, а проверка
# конкурентов как раз гоняется без VPN (иначе РФ-сайты гео-блочат). Polza.ai
# доступна из РФ напрямую, прокси ей не нужен ни в одном из режимов.
_client = OpenAI(
    base_url=settings.polza_base_url,
    api_key=settings.polza_api_key,
    timeout=120.0,
    http_client=httpx.Client(trust_env=False, timeout=120.0),
)

EXTRACT_PROMPT = (Path(__file__).parent / "prompts" / "extract_menu.md").read_text(encoding="utf-8")

# Размер чанка текста на один вызов LLM (символы)
CHUNK_CHARS = 12_000

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class ExtractMeta:
    chunks: int = 0
    cost_rub: float = 0.0
    total_tokens: int = 0
    warnings: list[str] = field(default_factory=list)


def _split_chunks(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Режем текст на чанки, предпочитая границы категорий.

    Блок одной позиции (имя/вес/цена на соседних строках) нельзя разрезать
    между чанками — LLM потеряет позицию в обоих. Поэтому на маркере
    «== Категория:» (его ставит fetcher при обходе подстраниц) чанк закрывается
    досрочно, как только набралась заметная часть лимита.
    """
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        at_category = line.startswith("== Категория:")
        if current and (size + len(line) + 1 > max_chars
                        or (at_category and size > max_chars * 0.6)):
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def parse_price(value) -> float | None:
    """Цена из ответа LLM: 289 | "289" | "289 ₽" | "от 289" | "1 030,00" → float | None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if not s:
        return None
    s = (s.replace("от", "").replace("₽", "").replace("руб.", "").replace("руб", "")
         .replace(" ", "").replace(" ", "").replace(" ", ""))
    s = s.replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_items_json(raw: str) -> list[ExtractedItem]:
    """Ответ LLM → валидные позиции. Мусор отбрасывается с warning, не роняет прогон."""
    cleaned = _JSON_FENCE_RE.sub("", raw.strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(f"[конкуренты] LLM вернула не-JSON ({e}); ответ: {cleaned[:200]!r}")
        return []
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        logger.warning(f"[конкуренты] неожиданная структура JSON: {type(data).__name__}")
        return []

    items: list[ExtractedItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("item") or "").strip()
        if not name:
            continue
        items.append(ExtractedItem(
            category=(str(entry["category"]).strip() or None) if entry.get("category") else None,
            item=name,
            weight=(str(entry["weight"]).strip() or None) if entry.get("weight") else None,
            price_rub=parse_price(entry.get("price_rub")),
            composition=(str(entry["composition"]).strip() or None) if entry.get("composition") else None,
        ))
    return items


def _call_llm(system: str, chunk: str, meta: ExtractMeta) -> str:
    model = settings.competitors_llm_model or settings.llm_model
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": chunk},
    ]
    try:
        resp = _client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            seed=7,  # стабильность: один и тот же текст → один и тот же разбор
            response_format={"type": "json_object"},
        )
    except Exception as e:
        # polza.ai/модель может не поддерживать response_format/seed — пробуем без них
        logger.debug(f"[конкуренты] response_format не прошёл ({type(e).__name__}), фолбэк")
        resp = _client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
        )
    if resp.usage:
        usage = resp.usage.model_dump()
        meta.total_tokens += usage.get("total_tokens") or 0
        cost = usage.get("cost_rub")
        if cost:
            meta.cost_rub += float(cost)
    return resp.choices[0].message.content or ""


def extract_menu(text: str, site_name: str) -> tuple[list[ExtractedItem], ExtractMeta]:
    """Текст меню → позиции. Чанки обрабатываются отдельно, результат сливается.

    Дедуп по (норм. имя, норм. вес): позиция, попавшая в два чанка, не задвоится.
    """
    meta = ExtractMeta()
    system = EXTRACT_PROMPT.format(site_name=site_name)
    merged: dict[tuple[str, str], ExtractedItem] = {}

    for chunk in _split_chunks(text):
        meta.chunks += 1
        raw = _call_llm(system, chunk, meta)
        for item in parse_items_json(raw):
            key = (norm_name(item.item), norm_weight(item.weight))
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
            elif existing.category is None and item.category is not None:
                merged[key] = item  # из чанка без заголовка категория теряется — берём полную

    items = list(merged.values())
    logger.info(
        f"[конкуренты] экстракция «{site_name}»: {len(items)} позиций, "
        f"{meta.chunks} чанков, {meta.total_tokens} токенов, {meta.cost_rub:.2f} ₽"
    )
    return items, meta
