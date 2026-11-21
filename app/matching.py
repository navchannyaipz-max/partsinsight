"""Рівні відповідності пропозиції запиту (розділ 2): IDENTICAL / CROSS_REFERENCE / CANDIDATE.

* IDENTICAL — збігається канонічний ключ (виробник, нормалізований MPN);
  суперечність критичних атрибутів → окрема група (ціни не змішуються).
* CROSS_REFERENCE — лише з перевіреного довідника аналогів (CrossReferenceSource).
* CANDIDATE — подібність H = α·Ta + (1−α)·Tt ≥ θ за активними ознаками або входження запиту (префікс артикула / усі слова в назві).
* Інакше пропозиція відкидається (REJECTED) і не показується.
"""
from __future__ import annotations
from .normalize import norm_mpn, norm_text, trigram_sim, tokens, looks_like_mpn

ALPHA = 0.7
THETA = 0.5
IDENTICAL, CROSS, CANDIDATE = "IDENTICAL", "CROSS_REFERENCE", "CANDIDATE"
LEVEL_ORDER = {IDENTICAL: 0, CROSS: 1, CANDIDATE: 2}


def similarity(q: str, offer: dict) -> float:
    """H за активними ознаками: артикул (Ta) — якщо запит схожий на артикул, назва (Tt) — якщо є слова."""
    nq = norm_mpn(q)
    feats = []
    if looks_like_mpn(q):
        feats.append((ALPHA, trigram_sim(nq, offer["norm_mpn"])))
    words = [t for t in tokens(q) if not any(c.isdigit() for c in t)]
    if words:
        feats.append((1 - ALPHA if feats else 1.0,
                      trigram_sim(" ".join(words), norm_text(offer["title"]))))
    if not feats:
        return 0.0
    return sum(w * x for w, x in feats) / sum(w for w, _ in feats)


def contains(q: str, offer: dict) -> str | None:
    """Збіг за входженням: префікс артикула (≥3 знаків) або всі слова запиту в назві."""
    nq = norm_mpn(q)
    if looks_like_mpn(q) and len(nq) >= 3 and offer["norm_mpn"].startswith(nq):
        return "prefix"
    words = [t for t in tokens(q) if not any(c.isdigit() for c in t)]
    if words and not any(c.isdigit() for c in q):
        title = norm_text(offer["title"])
        if all(w in title for w in words):
            return "words"
    return None


def search_score(q: str, offer: dict, manufacturer_key: str | None = None) -> float:
    """S = Σ wi·xi / Σ wi по активних ознаках (2.4): N (точний/префікс), T (триграми), F (слова), M (виробник)."""
    nq = norm_mpn(q)
    feats: list[tuple[float, float]] = []
    if looks_like_mpn(q):
        m = offer["norm_mpn"]
        if m == nq:
            n = 1.0
        elif m.startswith(nq):
            n = len(nq) / len(m)
        else:
            n = 0.0
        feats += [(0.5, n), (0.2, trigram_sim(nq, m))]
    qt = [t for t in tokens(q) if not any(c.isdigit() for c in t)]
    if qt:
        title = norm_text(offer["title"])
        f = sum(1 for t in qt if t in title)
        feats.append((0.2, f / (f + 1.0) if f else 0.0))
    if manufacturer_key:
        feats.append((0.1, 1.0 if offer["brand_key"] == manufacturer_key else 0.0))
    if not feats:
        return 0.0
    return sum(w * x for w, x in feats) / sum(w for w, _ in feats)


def shared_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def classify(q: str, offer: dict, analogs: dict[tuple[str, str], dict],
             manufacturer_key: str | None = None) -> tuple[str, dict] | None:
    """Повертає (рівень, доказ) або None (REJECTED).

    manufacturer_key — виробник, заданий у запиті явно (поле «Виробник» або відомий бренд у тексті).
    Доказ містить «basis» (короткий текст для картки й деталей) і структуровані поля для деталей.
    """
    nq, nm, ob = norm_mpn(q), offer["norm_mpn"], offer["brand_key"]
    seller = offer.get("brand") or ob
    if nq and nm == nq:                                        # F1: однаковий нормалізований артикул
        if not ob:
            return CANDIDATE, {"code": "no_seller_brand", "basis": "Виробника не вказано: артикул збігається, але бренд продавця невідомий"}
        if manufacturer_key and ob != manufacturer_key:
            return CANDIDATE, {"code": "brand_conflict", "seller_brand": seller, "query_brand": manufacturer_key,
                               "basis": f"Артикул збігається, але виробники різні: у запиті {manufacturer_key}, у продавця {seller}"}
        if manufacturer_key:
            return IDENTICAL, {"code": "mpn_brand", "seller_brand": seller, "query_brand": manufacturer_key,
                               "basis": "Артикул і виробник збігаються"}
        return IDENTICAL, {"code": "mpn_only", "seller_brand": seller, "basis": "Збіг за артикулом",
                           "note": f"Виробник продавця: {seller}; у запиті виробника не задано"}
    # F2: аналог лише за однозначним записом довідника про пару деталей
    ref = analogs.get((ob, nm)) if ob else None
    ref = ref or analogs.get(("", nm))
    if not ref and not ob:
        hits = [(b, v) for (b, m), v in analogs.items() if m == nm]
        if len({b for b, _ in hits}) == 1:
            ref = hits[0][1]
        elif hits:
            return CANDIDATE, {"code": "ambiguous_analog", "basis": "Номер є в довіднику для кількох виробників, а бренд продавця не вказано — зв’язок не підтверджено"}
    if ref:
        return CROSS, {"code": "cross_ref", "basis": "Аналог за довідником", "query_mpn": ref.get("via") or q,
                       "analog_mpn": offer.get("mpn_raw") or nm, "ref_source": ref["ref_source"], "ref": ref}
    if looks_like_mpn(q) and nq:
        cp = shared_prefix(nq, nm)
        if (nm.startswith(nq) and len(nq) >= 3) or cp >= max(4, int(0.7 * min(len(nq), len(nm)) + 0.999)):
            return CANDIDATE, {"code": "prefix", "h": similarity(q, offer),
                               "basis": f"Спільний початок артикула ({cp} з {len(nm)} знаків)"}
    h = similarity(q, offer)
    if h >= THETA:
        what = "Подібний артикул" if looks_like_mpn(q) else "Подібна назва"
        return CANDIDATE, {"code": "similar", "h": h, "basis": f"{what}: подібність H = {h:.2f} ≥ θ = {THETA}"}
    if contains(q, offer) == "words":
        return CANDIDATE, {"code": "words", "h": h, "basis": "Усі слова запиту є в назві"}
    return None
