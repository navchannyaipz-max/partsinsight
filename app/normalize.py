"""Нормалізація артикулів, виробників і тексту (розділ 2, підрозділ нормалізації).

Правила: верхній регістр; контрольована таблиця кириличних гомогліфів;
роздільники видаляються; O/0 та I/1 автоматично НЕ замінюються;
сирі значення завжди зберігаються окремо.
"""
from __future__ import annotations
import re

_HOMO = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")
_SEP = re.compile(r"[\s\-_./\\,;:()]+")
_NON_ALNUM = re.compile(r"[^0-9A-Z]")


def norm_mpn(raw: str | None) -> str:
    if not raw:
        return ""
    s = raw.upper().translate(_HOMO)
    s = _SEP.sub("", s)
    return _NON_ALNUM.sub("", s)


def norm_text(raw: str | None) -> str:
    if not raw:
        return ""
    s = raw.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def tokens(raw: str | None) -> list[str]:
    return [t for t in norm_text(raw).split() if len(t) > 1]


def brand_key(name: str | None, aliases: dict[str, str]) -> str:
    """Канонічний ключ виробника: лише через словник аліасів (без нечіткого злиття)."""
    if not name:
        return ""
    k = re.sub(r"[^0-9A-ZА-ЯІЇЄҐ]", "", name.upper())
    return aliases.get(k, k)


def alias_key(name: str) -> str:
    return re.sub(r"[^0-9A-ZА-ЯІЇЄҐ]", "", name.upper())


def trigrams(s: str) -> set[str]:
    out: set[str] = set()
    for w in s.split():
        p = f"  {w} "
        out.update(p[i:i + 3] for i in range(len(p) - 2))
    return out


def trigram_sim(a: str, b: str) -> float:
    """Подібність за триграмами (як pg_trgm): |A∩B| / |A∪B|."""
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def looks_like_mpn(q: str) -> bool:
    n = norm_mpn(q)
    return len(n) >= 4 and any(c.isdigit() for c in n) and len(q.split()) <= 4
