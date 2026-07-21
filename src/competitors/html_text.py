"""HTML → чистый текст меню. Плюс разбор .mhtml для ручного фолбэка."""
import email
import email.policy
from pathlib import Path

from bs4 import BeautifulSoup

# Контроль токенов: страница меню больше этого — почти наверняка мусор в конце
MAX_TEXT_CHARS = 60_000

_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template",
              "header", "footer", "nav")


def page_to_menu_text(html: str) -> str:
    """Основной текстовый контент страницы: без скриптов, навигации и пустых строк."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.split("\n")]
    cleaned = "\n".join(ln for ln in lines if ln)
    return cleaned[:MAX_TEXT_CHARS]


def _mhtml_to_html(raw: bytes) -> str | None:
    """Достать HTML-часть из .mhtml (MIME multipart, «Ctrl+S — одним файлом»)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return None


def read_uploaded_document(path: str | Path) -> str:
    """Текст меню из присланного шефом файла (.html/.htm/.mhtml/.mht).

    Бросает ValueError, если HTML извлечь не удалось.
    """
    p = Path(path)
    raw = p.read_bytes()
    suffix = p.suffix.lower()
    if suffix in (".mhtml", ".mht"):
        html = _mhtml_to_html(raw)
        if html is None:
            raise ValueError("в .mhtml не нашлось HTML-части")
    else:
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("cp1251", errors="replace")
    text = page_to_menu_text(html)
    if not text.strip():
        raise ValueError("после очистки HTML текст пустой")
    return text
