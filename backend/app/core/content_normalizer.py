"""监控页面正文归一化。

页面 Hash 基于稳定正文，而不是原始 HTML。脚本、导航、Cookie 提示和连续空白
等高频噪声会被移除，同时保留标题、价格、功能和日期等可能影响竞争判断的信息。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from bs4 import BeautifulSoup


_DROP_TAGS = (
    "script", "style", "noscript", "nav", "footer", "header", "aside", "svg", "canvas",
    "form", "dialog", "template",
)
_NOISE_HINT = re.compile(
    r"(?:^|[-_\s])(cookie|consent|gdpr|newsletter|popup|modal|breadcrumb|share|social|"
    r"advert|promo|recommend|related|nav|footer|header)(?:$|[-_\s])",
    re.IGNORECASE,
)
_NOISE_LINES = (
    re.compile(r"^(accept|reject|manage) (all )?cookies?\.?$", re.IGNORECASE),
    re.compile(r"^we use cookies\b.*$", re.IGNORECASE),
    re.compile(r"^(接受|拒绝|管理)(全部)? Cookie[s]?。?$", re.IGNORECASE),
    re.compile(r"^本网站使用 Cookie[s]?\b.*$", re.IGNORECASE),
)
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


@dataclass(frozen=True)
class NormalizedContent:
    title: str
    text: str
    content_hash: str
    content_length: int


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _ZERO_WIDTH.sub("", value)
    value = _HORIZONTAL_SPACE.sub(" ", value)

    lines = []
    previous = None
    for raw_line in value.split("\n"):
        line = raw_line.strip()
        if not line or any(pattern.match(line) for pattern in _NOISE_LINES):
            continue
        if line == previous:
            continue
        lines.append(line)
        previous = line
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def normalize_html(html: str) -> NormalizedContent:
    soup = BeautifulSoup(html or "", "html.parser")
    title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")

    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if not getattr(tag, "attrs", None):
            continue
        marker = " ".join((tag.get("id") or "", " ".join(tag.get("class") or [])))
        if marker and _NOISE_HINT.search(marker):
            tag.decompose()

    cleaned_html = str(soup)
    extracted = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            cleaned_html,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        ) or ""
    except Exception:
        extracted = ""

    if not extracted:
        root = soup.body or soup
        extracted = root.get_text("\n", strip=True)
    body_text = normalize_text(extracted)
    text = body_text
    if title and title.casefold() not in body_text[: max(200, len(title))].casefold():
        text = f"{title}\n{body_text}" if body_text else title
    return NormalizedContent(
        title=title,
        text=text,
        content_hash=hash_content(text),
        content_length=len(text),
    )
