"""Очередь ночных отчётов: считаем ночью, отправляем утром.

Причина существования очереди: 19.08.2026 сводка мониторинга пришла шефу
в 4 утра — прогон стартует в 03:30 и отправлял результат сразу по готовности.
"""
import json

from src.bot import reports


def _use_tmp_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "QUEUE_PATH", tmp_path / "pending.json")


def test_queue_and_pop_roundtrip(tmp_path, monkeypatch):
    _use_tmp_queue(tmp_path, monkeypatch)
    assert reports.pop_reports() == []          # пустая очередь никого не будит

    reports.queue_report("Мониторинг конкурентов — 8 сайтов")
    reports.queue_report("Расчётка пересобрана")
    assert reports.pending_count() == 2

    got = reports.pop_reports()
    assert got == ["Мониторинг конкурентов — 8 сайтов", "Расчётка пересобрана"]
    # очередь очищена: повторной доставки завтра быть не должно
    assert reports.pop_reports() == []


def test_queue_survives_restart(tmp_path, monkeypatch):
    """Между расчётом и доставкой пять часов — бот может быть перезапущен."""
    _use_tmp_queue(tmp_path, monkeypatch)
    reports.queue_report("ночной отчёт")
    # имитируем перезапуск: состояние только на диске, ничего в памяти
    assert (tmp_path / "pending.json").exists()
    assert reports.pop_reports() == ["ночной отчёт"]


def test_empty_text_not_queued(tmp_path, monkeypatch):
    _use_tmp_queue(tmp_path, monkeypatch)
    reports.queue_report("")
    reports.queue_report("   ")
    assert reports.pending_count() == 0


def test_broken_queue_file_does_not_crash(tmp_path, monkeypatch):
    """Битый файл не должен ронять утреннюю доставку."""
    _use_tmp_queue(tmp_path, monkeypatch)
    (tmp_path / "pending.json").write_text("{не json", encoding="utf-8")
    assert reports.pop_reports() == []
    reports.queue_report("после сбоя")
    assert reports.pop_reports() == ["после сбоя"]


def test_queue_file_is_readable_json(tmp_path, monkeypatch):
    _use_tmp_queue(tmp_path, monkeypatch)
    reports.queue_report("отчёт с ₽ и кириллицей")
    data = json.loads((tmp_path / "pending.json").read_text(encoding="utf-8"))
    assert data[0]["text"] == "отчёт с ₽ и кириллицей"
    assert "queued_at" in data[0]
