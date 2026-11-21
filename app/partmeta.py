"""Метадані товару: тип деталі (для піктограми) та евристичне вилучення артикула з назви."""
from __future__ import annotations
import re

# (ключ піктограми, ключові слова); перший збіг виграє, тому «масляний фільтр» стоїть до «масло»
KINDS = [
    ("filter", ("фільтр", "фильтр", "filter")),
    ("brake", ("колодк", "гальм", "тормоз", "brake", "суппорт", "супорт")),
    ("spark", ("свічк", "свеч", "spark", "котушк", "катушк")),
    ("shock", ("амортиз", "пружин", "shock", "стійк", "стойк")),
    ("belt", ("ремін", "ремен", "belt", "ланцюг", "грм", "timing")),
    ("bearing", ("підшипник", "подшипник", "bearing", "маточин", "ступиц")),
    ("battery", ("акумулятор", "аккумулятор", "battery")),
    ("bulb", ("лампа", "лампочк", "фара", "bulb", "headlight")),
    ("wiper", ("двірник", "дворник", "щітк", "щетк", "wiper")),
    ("tire", ("шина", "шини", "tyre", "tire", "колес", "диск колісн")),
    ("drop", ("масло", "олива", "oil", "антифриз", "охолодж", "радіатор", "помпа", "насос", "термостат")),
    ("clutch", ("зчепленн", "сцеплен", "clutch")),
    ("link", ("важіль", "рычаг", "шарнір", "шаров", "сайлент", "тяга", "наконечник", "опора", "ступиц")),
    ("exhaust", ("глушник", "глушитель", "вихлоп", "каталіз", "exhaust")),
]

_UNITS = re.compile(r"^\d+([.,]\d+)?(v|в|мм|mm|л|l|kg|кг|ml|мл|%|шт|pcs|w|вт|a|ah|ач|см|cm)$", re.I)
_SIZE = re.compile(r"^\d{2,3}[/x×]\d{2,3}([/x×r]?\d{2})?[a-z]?$", re.I)      # 205/55R16
_OIL = re.compile(r"^\d{1,2}w-?\d{2}$", re.I)                                   # 5W30


def category_of(title: str | None) -> str:
    t = (title or "").lower()
    for key, words in KINDS:
        if any(w in t for w in words):
            return key
    return "part"


def _ok_token(tok: str, exclude: set[str]) -> bool:
    n = re.sub(r"[^0-9A-Za-zА-Яа-я]", "", tok).upper()
    if not n or n in exclude or not any(c.isdigit() for c in n):
        return False
    if re.fullmatch(r"(19|20)\d{2}", n) or re.fullmatch(r"(19|20)\d{2}[-–](19|20)\d{2}", tok) or _UNITS.match(tok) or _SIZE.match(tok) or _OIL.match(tok):
        return False
    if re.fullmatch(r"\d\.\d", tok):                       # об'єм двигуна 1.6
        return False
    has_alpha = any(c.isalpha() for c in n)
    return len(n) >= 4 if has_alpha else len(n) >= 6


def guess_mpn(title: str | None, exclude: set[str] | None = None) -> str | None:
    """Артикул із назви товару, коли джерело не віддає окреме поле.

    Беремо токен з цифрою довжиною від 4 знаків (літери+цифри) чи від 6 (лише цифри); відкидаємо роки,
    розміри шин, в'язкість масла, одиниці виміру та слова з виключень (марка/модель авто).
    Артикул із пробілами («0 986 452 041») збирається з послідовних цифрових груп.
    """
    if not title:
        return None
    ex = {re.sub(r"[^0-9A-Z]", "", x.upper()) for x in (exclude or set())}
    toks = [t.strip(",;:()[]«»\"'") for t in title.split()]
    best, best_score = None, 0
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok and tok.replace("-", "").isdigit() and len(tok) <= 4:       # групи цифр: 0 986 452 041
            j, acc = i, ""
            while j < len(toks) and toks[j].isdigit() and len(toks[j]) <= 4 and len(acc) < 12:
                acc += toks[j]
                j += 1
            if j - i >= 2 and len(acc) >= 7 and not re.fullmatch(r"(19|20)\d{2}", acc):
                cand = " ".join(toks[i:j])
                if len(acc) + 2 > best_score:
                    best, best_score = cand, len(acc) + 2
                i = j
                continue
        if re.fullmatch(r"[A-Z]{1,2}", tok) and tok not in ex:            # P 85 114: літера + цифрові групи
            j, acc = i + 1, ""
            while j < len(toks) and toks[j].isdigit() and len(toks[j]) <= 4 and j - i <= 3:
                acc += toks[j]
                j += 1
            if j - i >= 3 and len(acc) >= 4 and not re.fullmatch(r"(19|20)\d{2}", acc):
                score = len(acc) + 4
                if score > best_score:
                    best, best_score = " ".join(toks[i:j]), score
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if (re.fullmatch(r"[A-Z]{1,5}", tok) and tok not in ex and nxt and nxt[0].isdigit()
                and len(re.sub(r"[^0-9A-Za-z]", "", nxt)) >= 3 and not (_UNITS.match(nxt) or _OIL.match(nxt))
                and not re.fullmatch(r"(19|20)\d{2}([-–](19|20)\d{2})?", nxt) and re.sub(r"[^0-9A-Z]", "", nxt.upper()) not in ex):
            cand = f"{tok} {nxt}"
            score = len(re.sub(r"[^0-9A-Za-z]", "", cand)) + 3
            if score > best_score:
                best, best_score = cand, score
        if _ok_token(tok, ex):
            n = re.sub(r"[^0-9A-Za-z]", "", tok)
            score = len(n) + (3 if any(c.isalpha() for c in n) else 0)
            if score > best_score:
                best, best_score = tok, score
        i += 1
    return best


# Офіційні сайти/каталоги виробників (відкриваються в новій вкладці; дані з них не завантажуються).
BRAND_SITES = {
    "TRW": "https://www.trwaftermarket.com", "MANN": "https://www.mann-filter.com", "FEBI": "https://www.febi.com",
    "BREMBO": "https://www.brembo.com", "DENSO": "https://www.denso-am.eu", "MAHLE": "https://www.mahle-aftermarket.com",
    "BOSCH": "https://www.boschaftermarket.com", "SACHS": "https://www.zf.com", "VALEO": "https://www.valeoservice.com",
    "NGK": "https://www.ngk.com", "GATES": "https://www.gates.com", "SKF": "https://www.skf.com",
    "KYB": "https://www.kyb-europe.com", "MEYLE": "https://www.meyle.com", "HELLA": "https://www.hella.com",
    "FILTRON": "https://filtron.eu", "LEMFORDER": "https://www.zf.com", "ATE": "https://www.ate-brakes.com",
}
