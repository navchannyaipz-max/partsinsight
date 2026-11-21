from __future__ import annotations
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def data_dir() -> Path:
    """Папка даних поза папкою програми: оновлення програми її не чіпає. Змінити: PARTSINSIGHT_DATA."""
    d = Path(os.environ.get("PARTSINSIGHT_DATA") or (Path.home() / "PartsInsightData"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_path() -> str:
    p = data_dir() / "partsinsight.db"
    legacy = Path("partsinsight.db")                      # база зі старої версії біля програми
    if not p.exists() and legacy.exists():
        shutil.copy2(legacy, p)
    return str(p)


def stable_secret_key() -> str:
    """Ключ сесій зберігається, щоб вхід в адмінку не скидався після перезапуску."""
    f = data_dir() / "secret.key"
    if not f.exists():
        import secrets
        f.write_text(secrets.token_hex(32))
        try:
            f.chmod(0o600)
        except OSError:
            pass
    return f.read_text().strip()


def backup_db(path: str, keep: int = 7, force: bool = False) -> str | None:
    """Копія бази в <папка даних>/backups (не частіше разу на добу, зберігаємо останні `keep`)."""
    bdir = Path(path).parent / "backups"
    bdir.mkdir(exist_ok=True)
    name = f"partsinsight-{datetime.now().strftime('%Y%m%d' + ('-%H%M%S' if force else ''))}.db"
    dst = bdir / name
    if dst.exists():
        return None
    src = sqlite3.connect(path)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    for old in sorted(bdir.glob("partsinsight-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return str(dst)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN('html','json')),
  enabled INTEGER NOT NULL DEFAULT 1, search_url TEXT NOT NULL, profile TEXT NOT NULL DEFAULT '{}',
  currency TEXT NOT NULL DEFAULT 'UAH', delay_ms INTEGER NOT NULL DEFAULT 1000,
  allow_local INTEGER NOT NULL DEFAULT 0, note TEXT DEFAULT '',
  last_status TEXT, last_error TEXT, last_ms INTEGER, last_items INTEGER, last_at TEXT);
CREATE TABLE IF NOT EXISTS cache(
  source_id INTEGER NOT NULL, qkey TEXT NOT NULL, fetched_at TEXT NOT NULL, items TEXT NOT NULL,
  PRIMARY KEY(source_id,qkey));
CREATE TABLE IF NOT EXISTS cross_refs(
  id INTEGER PRIMARY KEY, mfr_a TEXT, mpn_a TEXT NOT NULL, mfr_b TEXT, mpn_b TEXT NOT NULL,
  ref_source TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS parts(
  id INTEGER PRIMARY KEY, norm_mpn TEXT NOT NULL, brand_key TEXT NOT NULL DEFAULT '', brand TEXT DEFAULT '',
  mpn TEXT NOT NULL, title TEXT, kind TEXT DEFAULT 'part', image TEXT DEFAULT '',
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
  UNIQUE(norm_mpn, brand_key));
CREATE INDEX IF NOT EXISTS ix_parts_mpn ON parts(norm_mpn);
CREATE TABLE IF NOT EXISTS vehicle_parts(
  id INTEGER PRIMARY KEY, make TEXT NOT NULL, model TEXT NOT NULL, year INTEGER NOT NULL, engine TEXT NOT NULL DEFAULT '',
  part_type TEXT NOT NULL, part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, times INTEGER NOT NULL DEFAULT 1,
  UNIQUE(make, model, year, engine, part_type, part_id));
CREATE INDEX IF NOT EXISTS ix_vp ON vehicle_parts(make, model, part_type);
CREATE TABLE IF NOT EXISTS price_snapshots(
  id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
  source TEXT NOT NULL, price REAL NOT NULL, currency TEXT NOT NULL, unit_price_uah REAL, url TEXT DEFAULT '',
  fetched_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ps ON price_snapshots(part_id, fetched_at);
CREATE TABLE IF NOT EXISTS car_models(
  id INTEGER PRIMARY KEY, make TEXT NOT NULL, model TEXT NOT NULL, year_from INTEGER NOT NULL, year_to INTEGER NOT NULL,
  UNIQUE(make, model));
CREATE TABLE IF NOT EXISTS part_types(
  id INTEGER PRIMARY KEY, category TEXT NOT NULL, name TEXT NOT NULL UNIQUE, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS brands(
  id INTEGER PRIMARY KEY, canonical TEXT NOT NULL UNIQUE, display TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS watchlist(
  id INTEGER PRIMARY KEY, part_id INTEGER NOT NULL UNIQUE REFERENCES parts(id) ON DELETE CASCADE,
  added_at TEXT NOT NULL, base_price_uah REAL);
CREATE TABLE IF NOT EXISTS xref_lookups(norm_mpn TEXT PRIMARY KEY, at TEXT NOT NULL, found INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, canonical TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS searches(
  id INTEGER PRIMARY KEY, q TEXT, at TEXT, n_offers INTEGER, n_sources INTEGER, ms INTEGER);
"""

DEFAULT_ALIASES = {
    "BOSCH": "BOSCH", "РОБЕРТБОШ": "BOSCH", "ROBERTBOSCH": "BOSCH", "БОШ": "BOSCH",
    "MANNFILTER": "MANN", "MANN": "MANN", "МАНН": "MANN",
    "KNECHT": "MAHLE", "MAHLEKNECHT": "MAHLE", "MAHLE": "MAHLE",
    "TRW": "TRW", "ТРВ": "TRW", "BREMBO": "BREMBO", "БРЕМБО": "BREMBO",
    "NGK": "NGK", "DENSO": "DENSO", "SACHS": "SACHS", "ZFSACHS": "SACHS", "VALEO": "VALEO",
}

# Розширений словник виробників автокомпонентів і марок авто (OEM): назви, під якими вони трапляються в магазинах.
_MORE_BRANDS = (
    "ATE|Lucas|LUCASTRW|Textar|Ferodo|Jurid|Pagid|Zimmermann|Brembo|Ate Ceramic|Bendix|Akebono|Nisshinbo|Hella|HellaPagid|"
    "Febi|Febi Bilstein|FebiBilstein|Lemforder|Lemförder|Meyle|Swag|Sidem|Moog|Monroe|KYB|Kayaba|Bilstein|Sachs Boge|Boge|"
    "Gates|Dayco|Contitech|Continental|INA|LuK|Schaeffler|FAG|SKF|NTN|SNR|Timken|Optibelt|SKF VKBA|"
    "Mahle|Mahle Original|Knecht|Mann Filter|Hengst|Filtron|Purflux|Sofima|UFI|Sakura|Champion|WIX|Fram|Clean Filters|"
    "Bosch|NGK|NGK NTK|NTK|Denso|Beru|Brisk|Eyquem|Valeo|Hella|Osram|Philips|Narva|Magneti Marelli|Marelli|Wahler|Behr|Nissens|Nrf|Thermotec|"
    "Luk|Ruville|Corteco|Elring|Victor Reinz|Reinz|Payen|Goetze|Glyco|Kolbenschmidt|Ajusa|Trw Lucas|"
    "Delphi|Pierburg|Vdo|Siemens|Continental VDO|Facet|Metzger|Ridex|Starline|Blue Print|Kavo Parts|Japanparts|Japko|Asam|Ashika|"
    "Patron|Quinton Hazell|Dr Motor|Sasic|Lesjofors|Tedgum|Frenkit|Ate|Jp Group|Jp Group Dk|Topran|Vaico|Vemo|Rapro|Fai|Fai AutoParts|"
    "Castrol|Mobil|Shell|Total|Motul|Liqui Moly|Elf|Ravenol|Mannol|Zic|Petronas|Lukoil|Wolf|Comma|Gt Oil|"
    "Toyota|Lexus|Honda|Nissan|Mazda|Mitsubishi|Subaru|Suzuki|Hyundai|Kia|Mobis|Ford|Opel|Vauxhall|Gm|Chevrolet|"
    "Volkswagen|Vag|Audi|Skoda|Seat|Bmw|Mini|Mercedes|Mercedes Benz|Renault|Dacia|Peugeot|Citroen|Psa|Fiat|Alfa Romeo|Lancia|"
    "Volvo|Saab|Land Rover|Jaguar|Chery|Geely|Lada|Vaz|Zaz|Daewoo|Ssangyong|Tesla"
)


def _more_aliases() -> dict:
    import re
    out = {}
    for name in _MORE_BRANDS.split("|"):
        k = re.sub(r"[^0-9A-ZА-ЯІЇЄҐ]", "", name.upper())
        if k and k not in DEFAULT_ALIASES:
            out[k] = k
    out.update({"FEBIBILSTEIN": "FEBI", "FEBI": "FEBI", "LEMFORDER": "LEMFORDER", "LEMFÖRDER": "LEMFORDER", "MANNFILTER": "MANN",
                "MAHLEORIGINAL": "MAHLE", "NGKNTK": "NGK", "NTK": "NGK", "HELLAPAGID": "HELLA", "MERCEDESBENZ": "MERCEDES",
                "MERCEDESBENZ": "MERCEDES", "VAG": "VOLKSWAGEN", "LUCASTRW": "TRW", "TRWLUCAS": "TRW", "KAYABA": "KYB",
                "SACHSBOGE": "SACHS", "JPGROUPDK": "JPGROUP", "FAIAUTOPARTS": "FAI", "CONTINENTALVDO": "VDO", "MOBIS": "HYUNDAI",
                "MAGNETIMARELLI": "MARELLI", "VAUXHALL": "OPEL", "GM": "CHEVROLET", "ATECERAMIC": "ATE", "SKFVKBA": "SKF"})
    return out

DEFAULT_CROSS = [
    ("BOSCH", "0986452041", "MANN", "W71930", "Демонстраційний довідник аналогів"),
    ("BOSCH", "0986452041", "MAHLE", "OC47", "Демонстраційний довідник аналогів"),
    ("BOSCH", "0986AB1234", "TRW", "GDB1550", "Демонстраційний довідник аналогів"),
]


def demo_on() -> int:
    """Демо-джерела вмикаються за замовчуванням; скрипти запуску задають PARTSINSIGHT_DEMO=0 для робочої установки."""
    return 0 if os.environ.get("PARTSINSIGHT_DEMO") == "0" else 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init(con: sqlite3.Connection, base_url: str) -> None:
    con.executescript(SCHEMA)
    con.executemany("INSERT OR IGNORE INTO aliases VALUES(?,?)", list(DEFAULT_ALIASES.items()) + list(_more_aliases().items()))
    if demo_on() and not con.execute("SELECT 1 FROM cross_refs LIMIT 1").fetchone():
        con.executemany(
            "INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source) VALUES(?,?,?,?,?)", DEFAULT_CROSS)
    if not con.execute("SELECT 1 FROM sources LIMIT 1").fetchone():
        seed_sources(con)
    ensure_presets(con)
    from . import vehicles
    vehicles.init(con)
    seed_catalogs(con)
    migrate_image_fields(con)
    migrate_snapshots(con)
    con.commit()


def seed_sources(con: sqlite3.Connection) -> None:
    html_a = {
        "pack_default": 1, "item": "article.item", "fields": {
            "title": ".item-title", "url": ".item-title@href", "mpn": ".sku", "brand": ".brand",
            "price": ".price", "availability": ".stock", "delivery": ".delivery"}}
    html_b = {
        "pack_default": 1, "item": "table#catalog tbody tr", "fields": {
            "title": "td.name", "url": "td.name a@href", "mpn": "td.code", "brand": "td.maker",
            "price": "td.cost", "availability": "td.qty", "delivery": "td.eta"}}
    api = {
        "pack_default": 1, "items_path": "data.items", "fields": {
            "title": "name", "url": "link", "mpn": "article", "brand": "brand", "price": "price.amount",
            "currency": "price.currency", "availability": "stock", "delivery": "delivery_days",
            "condition": "condition", "pack": "pack"}}
    tpl_html = {"item": "div.product", "fields": {
        "title": "a.title", "url": "a.title@href", "mpn": ".sku", "brand": ".brand",
        "price": ".price", "availability": ".stock", "delivery": ".delivery"}}
    tpl_json = {"items_path": "items", "headers": {"Authorization": "Bearer {api_key}"}, "api_key": "",
                "fields": {"title": "title", "url": "url", "mpn": "mpn", "brand": "brand",
                           "price": "price", "currency": "currency", "availability": "in_stock",
                           "delivery": "delivery_days"}}
    rows = [
        ("Демо-магазин А (HTML)", "html", demo_on(), "{base}/mock/shop-a/search?q={q}", html_a, "UAH", 0, 1,
         "Локальна імітація інтернет-магазину: пошук через реальні HTTP-запити."),
        ("Демо-магазин Б (HTML)", "html", demo_on(), "{base}/mock/shop-b/catalog?query={q}", html_b, "UAH", 0, 1,
         "Інша верстка (таблиця), артикули з роздільниками та кирилицею."),
        ("Демо-агрегатор (JSON API)", "json", demo_on(), "{base}/mock/api/v1/parts?search={q}", api, "UAH", 0, 1,
         "Локальна імітація відкритого JSON API; ціни в різних валютах."),
        ("Шаблон: HTML-магазин (вимкнено)", "html", 0, "https://example.com/search?q={q}", tpl_html,
         "UAH", 1500, 0, "Шаблон для реального сайту: вкажіть URL і CSS-селектори, перевірте robots.txt та умови використання."),
        ("Шаблон: JSON API (вимкнено)", "json", 0, "https://api.example.com/v1/parts?search={q}", tpl_json,
         "UAH", 1000, 0, "Шаблон для офіційного API постачальника: вкажіть ключ у профілі."),
    ]
    for name, kind, en, url, prof, cur, delay, local, note in rows:
        con.execute(
            "INSERT INTO sources(name,kind,enabled,search_url,profile,currency,delay_ms,allow_local,note)"
            " VALUES(?,?,?,?,?,?,?,?,?)", (name, kind, en, url, json.dumps(prof, ensure_ascii=False), cur, delay, local, note))


def ensure_presets(con: sqlite3.Connection) -> None:
    """Додає пресети API-джерел, якщо їх ще немає (в т.ч. у вже наявних базах)."""
    demo = {"pack_default": 1, "items_path": "itemSummaries", "mpn_from_title": True,
            "auth": {"token_url": "{base}/mock/oauth/token", "client_id": "demo", "client_secret": "demo-secret"},
            "fields": {"title": "title", "url": "itemWebUrl", "price": "price.value",
                       "currency": "price.currency", "condition": "condition"}}
    ebay = {"items_path": "itemSummaries", "mpn_from_title": True,
            "headers": {"X-EBAY-C-MARKETPLACE-ID": "EBAY_DE", "Accept": "application/json"},
            "auth": {"token_url": "https://api.ebay.com/identity/v1/oauth2/token",
                     "client_id": "$ENV:EBAY_CLIENT_ID", "client_secret": "$ENV:EBAY_CLIENT_SECRET",
                     "scope": "https://api.ebay.com/oauth/api_scope"},
            "fields": {"title": "title", "url": "itemWebUrl", "price": "price.value",
                       "currency": "price.currency", "condition": "condition", "image": "image.imageUrl"}}
    serp = {"items_path": "shopping_results", "mpn_from_title": True, "api_key": "$ENV:SERPAPI_KEY",
            "fields": {"title": "title", "url": "product_link", "price": "extracted_price", "brand": "source",
                       "image": "thumbnail"}}
    rows = [
        ("Google Shopping через SerpApi (потрібен ключ)", "json", 0,
         "https://serpapi.com/search.json?engine=google_shopping&q={q}&gl=ua&hl=uk&api_key={api_key}", serp, "UAH", 1000, 0,
         "Пошук цін у магазинах України через Google Shopping. Ключ: змінна SERPAPI_KEY. Поля відповіді потрібно звірити тестом."),
        ("Демо-API з OAuth2 (JSON)", "json", demo_on(), "{base}/mock/api/v2/parts?q={q}", demo, "EUR", 0, 1,
         "Імітація захищеного API: токен client-credentials, потім запит із Bearer."),
        ("eBay Browse API (потрібні ключі)", "json", 0,
         "https://api.ebay.com/buy/browse/v1/item_summary/search?q={q}&limit=50", ebay, "EUR", 500, 0,
         "Офіційний API eBay. Ключі з developer.ebay.com задайте змінними EBAY_CLIENT_ID та EBAY_CLIENT_SECRET. "
         "Артикул береться з назви лота."),
    ]
    for name, kind, en, url, prof, cur, delay, local, note in rows:
        if not con.execute("SELECT 1 FROM sources WHERE name=?", (name,)).fetchone():
            con.execute(
                "INSERT INTO sources(name,kind,enabled,search_url,profile,currency,delay_ms,allow_local,note)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (name, kind, en, url, json.dumps(prof, ensure_ascii=False), cur, delay, local, note))


def migrate_snapshots(con: sqlite3.Connection) -> None:
    """Старі бази: додає атрибути знімків цін. Для старих записів вони лишаються NULL і показуються як «невідомо»."""
    have = {r[1] for r in con.execute("PRAGMA table_info(price_snapshots)")}
    for col, typ in (("pack", "INTEGER"), ("condition", "TEXT"), ("origin", "TEXT"), ("unit_reason", "TEXT DEFAULT ''"),
                     ("shipping_checked", "INTEGER"), ("shipping_cost", "REAL"), ("shipping_currency", "TEXT"),
                     ("shipping_free", "INTEGER"), ("total_uah", "REAL")):
        if col not in have:
            con.execute(f"ALTER TABLE price_snapshots ADD COLUMN {col} {typ}")


def migrate_image_fields(con: sqlite3.Connection) -> None:
    """У вже створених базах додає поле image до пресетів eBay/SerpApi, не чіпаючи інших налаштувань."""
    for name, field in (("eBay Browse API (потрібні ключі)", "image.imageUrl"),
                        ("Google Shopping через SerpApi (потрібен ключ)", "thumbnail")):
        row = con.execute("SELECT id, profile FROM sources WHERE name=?", (name,)).fetchone()
        if row:
            prof = json.loads(row["profile"] or "{}")
            if "image" not in prof.get("fields", {}):
                prof.setdefault("fields", {})["image"] = field
                con.execute("UPDATE sources SET profile=? WHERE id=?", (json.dumps(prof, ensure_ascii=False), row["id"]))


def seed_catalogs(con: sqlite3.Connection) -> None:
    """Початкові довідники: каталог авто, типи деталей, словник брендів (далі поповнюються під час роботи)."""
    from . import vehicles
    if True:  # INSERT OR IGNORE: наявні БД отримують нові моделі, записи адміністратора зберігаються
        con.executemany("INSERT OR IGNORE INTO car_models(make,model,year_from,year_to) VALUES(?,?,?,?)",
                        [(mk, m, y0, y1) for mk, lst in vehicles.VEHICLES.items() for m, y0, y1 in lst])
    if not con.execute("SELECT 1 FROM part_types LIMIT 1").fetchone():
        n = 0
        for cat, names in vehicles.PART_TYPES.items():
            for nm in names:
                n += 1
                con.execute("INSERT OR IGNORE INTO part_types(category,name,sort) VALUES(?,?,?)", (cat, nm, n))
    caps = {"BMW", "NGK", "TRW", "ZF", "ATE", "INA", "SKF", "KYB", "FAG", "NTN", "SNR", "UFI", "WIX", "VDO", "GM", "PSA", "VAG", "LUK", "NRF", "FAI", "JP"}
    for canon in sorted(set(DEFAULT_ALIASES.values()) | set(_more_aliases().values())):
        con.execute("INSERT OR IGNORE INTO brands(canonical,display,verified,source,first_seen) VALUES(?,?,1,?,?)",
                    (canon, canon if canon in caps else canon.title(), "початковий словник", now_iso()))
