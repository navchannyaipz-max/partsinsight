"""Підбір запчастин за автомобілем: марка → модель → рік → (двигун) → тип деталі.

Довідник марок/моделей слугує для побудови запиту; список деталей знаходиться живим пошуком в Інтернеті
(service.discover), а ціни та аналоги завантажуються лише після вибору конкретної деталі.
Таблиця fitment залишена для підключення API сумісності (import_fitment) з позначкою confirmed.
"""
from __future__ import annotations
import sqlite3

from .normalize import norm_mpn

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles(
  id INTEGER PRIMARY KEY, make TEXT NOT NULL, model TEXT NOT NULL, year_from INTEGER NOT NULL,
  year_to INTEGER NOT NULL, engine TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_veh ON vehicles(make, model);
CREATE TABLE IF NOT EXISTS fitment(
  id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
  category TEXT NOT NULL, part_name TEXT NOT NULL, mfr TEXT NOT NULL, mpn TEXT NOT NULL,
  is_oem INTEGER NOT NULL DEFAULT 0, ref_source TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
  UNIQUE(vehicle_id, mfr, mpn));
CREATE INDEX IF NOT EXISTS ix_fit ON fitment(vehicle_id, category);
"""

EXTRA_CROSS = [
    ("TRW", "GDB1550", "BREMBO", "P85114"),
    ("NGK", "BKR6E", "DENSO", "K20PRU"),
    ("BOSCH", "F026400238", "MANN", "C25114/1"),
]

# Марки та покоління, поширені в Україні (роки випуску орієнтовні, за європейським ринком).
# Довідник слугує лише для формування пошукового запиту; сумісність деталей він НЕ підтверджує.
Y = 2026
VEHICLES = {
    "Audi": [("A3 8P", 2003, 2012), ("A3 8V", 2012, 2020), ("A4 B7", 2004, 2008), ("A4 B8", 2007, 2015), ("A4 B9", 2015, Y),
             ("A6 C6", 2004, 2011), ("A6 C7", 2011, 2018), ("Q3 8U", 2011, 2018), ("Q5 8R", 2008, 2017), ("Q7 4L", 2005, 2015)],
    "BMW": [("3 E46", 1998, 2006), ("3 E90", 2005, 2012), ("3 F30", 2011, 2019), ("5 E60", 2003, 2010), ("5 F10", 2010, 2017),
            ("X3 F25", 2010, 2017), ("X5 E70", 2006, 2013), ("X5 F15", 2013, 2018)],
    "Chevrolet": [("Aveo T250", 2005, 2011), ("Aveo T300", 2011, 2015), ("Cruze J300", 2009, 2016), ("Lacetti J200", 2002, 2013), ("Lanos", 1997, 2009)],
    "Citroen": [("Berlingo II", 2008, 2018), ("C-Elysee", 2012, Y), ("C4 II", 2010, 2018), ("Jumper", 2006, Y)],
    "Dacia": [("Duster I", 2010, 2017), ("Duster II", 2017, 2023), ("Logan I", 2004, 2012), ("Logan II", 2012, 2020), ("Sandero II", 2012, 2020)],
    "Daewoo": [("Lanos", 1997, 2009), ("Matiz", 1998, 2015), ("Nexia", 1994, 2016), ("Sens", 2002, 2017)],
    "Fiat": [("Doblo II", 2010, Y), ("Ducato", 2006, Y), ("Panda II", 2003, 2012), ("Punto Evo", 2009, 2012), ("Tipo", 2015, Y)],
    "Ford": [("Fiesta VI", 2008, 2017), ("Focus II", 2004, 2011), ("Focus III", 2011, 2018), ("Focus IV", 2018, 2025), ("Fusion", 2002, 2012),
             ("Kuga I", 2008, 2012), ("Kuga II", 2012, 2019), ("Mondeo IV", 2007, 2014), ("Transit VII", 2006, 2013), ("Transit Custom", 2012, Y)],
    "Honda": [("Accord VIII", 2008, 2015), ("Civic VIII", 2005, 2011), ("Civic IX", 2011, 2017), ("CR-V III", 2006, 2012), ("CR-V IV", 2012, 2018)],
    "Hyundai": [("Accent", 2010, 2017), ("Elantra AD", 2015, 2020), ("Elantra MD", 2010, 2015), ("i30 GD", 2012, 2017), ("ix35", 2010, 2015),
                ("Santa Fe III", 2012, 2018), ("Solaris", 2010, 2017), ("Tucson TL", 2015, 2020)],
    "Kia": [("Ceed JD", 2012, 2018), ("Ceed CD", 2018, 2024), ("Rio III", 2011, 2017), ("Rio IV", 2017, 2023), ("Sorento II", 2009, 2014),
            ("Sportage III", 2010, 2015), ("Sportage IV", 2015, 2021)],
    "Lexus": [("RX 350 III", 2008, 2015), ("RX IV", 2015, 2022), ("ES VI", 2012, 2018)],
    "Mazda": [("3 BL", 2009, 2013), ("3 BM", 2013, 2019), ("6 GH", 2007, 2012), ("6 GJ", 2012, 2023), ("CX-5 KE", 2012, 2017), ("CX-5 KF", 2017, Y)],
    "Mercedes-Benz": [("C-Class W204", 2007, 2014), ("C-Class W205", 2014, 2021), ("E-Class W211", 2002, 2009), ("E-Class W212", 2009, 2016),
                      ("Sprinter 906", 2006, 2018), ("Vito W639", 2003, 2014), ("Vito W447", 2014, Y), ("GLC X253", 2015, 2022)],
    "Mitsubishi": [("ASX", 2010, 2023), ("Lancer X", 2007, 2017), ("L200 V", 2015, 2023), ("Outlander III", 2012, 2021), ("Pajero Sport II", 2008, 2015)],
    "Nissan": [("Juke F15", 2010, 2019), ("Leaf ZE0", 2010, 2017), ("Qashqai J10", 2006, 2013), ("Qashqai J11", 2013, 2021),
               ("Tiida C11", 2004, 2012), ("X-Trail T31", 2007, 2013), ("X-Trail T32", 2013, 2021)],
    "Opel": [("Astra H", 2004, 2014), ("Astra J", 2009, 2015), ("Astra K", 2015, 2021), ("Corsa D", 2006, 2014), ("Insignia A", 2008, 2017),
             ("Vectra C", 2002, 2008), ("Vivaro A", 2001, 2014), ("Zafira B", 2005, 2014)],
    "Peugeot": [("206", 1998, 2012), ("301", 2012, 2020), ("308 I", 2007, 2013), ("308 II", 2013, 2021), ("407", 2004, 2011), ("Partner II", 2008, 2018)],
    "Renault": [("Clio III", 2005, 2012), ("Duster I", 2010, 2017), ("Kangoo II", 2008, 2021), ("Laguna III", 2007, 2015), ("Logan II", 2012, 2020),
                ("Megane III", 2008, 2016), ("Megane IV", 2015, 2023), ("Trafic III", 2014, Y)],
    "Seat": [("Ibiza IV", 2008, 2017), ("Leon II", 2005, 2012), ("Leon III", 2012, 2020), ("Octavia", 1996, 2010)],
    "Skoda": [("Fabia II", 2007, 2014), ("Fabia III", 2014, 2021), ("Octavia A5", 2004, 2013), ("Octavia A7", 2013, 2020), ("Octavia A8", 2019, Y),
              ("Rapid", 2012, 2019), ("Superb II", 2008, 2015), ("Superb III", 2015, Y), ("Kodiaq", 2016, Y), ("Karoq", 2017, Y), ("Yeti", 2009, 2017)],
    "Subaru": [("Forester SJ", 2012, 2018), ("Impreza GJ", 2011, 2017), ("Outback BR", 2009, 2014), ("XV GP", 2011, 2017)],
    "Suzuki": [("Grand Vitara II", 2005, 2015), ("SX4 I", 2006, 2014), ("Swift III", 2004, 2010), ("Vitara IV", 2015, Y)],
    "Toyota": [("Auris E150", 2006, 2012), ("Avensis T27", 2008, 2018), ("Camry XV50", 2011, 2017), ("Camry XV70", 2017, Y),
               ("Corolla E150", 2006, 2013), ("Corolla E170", 2013, 2019), ("Land Cruiser Prado 150", 2009, Y), ("RAV4 XA30", 2005, 2012),
               ("RAV4 XA40", 2012, 2018), ("RAV4 XA50", 2018, Y), ("Yaris XP130", 2010, 2020)],
    "Volkswagen": [("Caddy III", 2004, 2015), ("Caddy IV", 2015, 2020), ("Golf V", 2003, 2008), ("Golf VI", 2008, 2013), ("Golf VII", 2012, 2019),
                   ("Jetta VI", 2010, 2018), ("Passat B6", 2005, 2010), ("Passat B7", 2010, 2015), ("Passat B8", 2014, Y), ("Polo V", 2009, 2017),
                   ("Tiguan I", 2007, 2016), ("Tiguan II", 2016, Y), ("Touareg II", 2010, 2018), ("Transporter T5", 2003, 2015), ("Transporter T6", 2015, 2024)],
    "Volvo": [("S60 II", 2010, 2018), ("V40 II", 2012, 2019), ("XC60 I", 2008, 2017), ("XC90 I", 2002, 2014)],
    "ВАЗ (Lada)": [("2107", 1982, 2012), ("2110", 1995, 2014), ("Granta", 2011, Y), ("Kalina", 2004, 2018), ("Niva 4x4", 1977, Y), ("Priora", 2007, 2018), ("Vesta", 2015, Y)],
    "ЗАЗ": [("Sens", 2004, 2017), ("Slavuta", 1999, 2011), ("Lanos", 1998, 2017), ("Vida", 2012, 2017)],
}

# Розширений каталог (лише від 2000 року): перекриває старі записи тих самих марок.
from . import fleet_data as _fd
_fleet = _fd.parse()
VEHICLES = {mk: lst for mk, lst in VEHICLES.items() if mk not in _fleet and mk != "ВАЗ (Lada)"}
VEHICLES.update(_fleet)
VEHICLES = {mk: [(m, max(a, 2000), b) for m, a, b in lst if b >= 2000] for mk, lst in sorted(VEHICLES.items())}

# Типи деталей для пошуку: категорія → список назв
PART_TYPES = {
    "Фільтри": ["Масляний фільтр", "Повітряний фільтр", "Салонний фільтр", "Паливний фільтр"],
    "Гальма": ["Гальмівні колодки передні", "Гальмівні колодки задні", "Гальмівні диски передні", "Гальмівні диски задні"],
    "Запалювання": ["Свічки запалювання", "Котушка запалювання"],
    "Підвіска": ["Амортизатор передній", "Амортизатор задній", "Пружина підвіски", "Шарова опора", "Сайлентблок", "Підшипник маточини", "Стійка стабілізатора"],
    "Двигун і ГРМ": ["Комплект ГРМ", "Ремінь приводний", "Помпа водяна", "Термостат"],
    "Трансмісія": ["Комплект зчеплення"],
    "Електрика": ["Акумулятор", "Лампа галогенна"],
}


def search_query(part: str, make: str, model: str, year: int | None, engine: str = "") -> str:
    """Текстовий запит для живого пошуку деталі під авто."""
    return " ".join(x for x in (part, make.split(" (")[0], model, str(year or ""), engine.strip()) if x)


def exclude_tokens(make: str, model: str, year: int | None, engine: str = "") -> set[str]:
    return {t for t in f"{make} {model} {year or ''} {engine}".replace("(", " ").replace(")", " ").split() if t}


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    for a, ma, b, mb in EXTRA_CROSS:
        if not con.execute("SELECT 1 FROM cross_refs WHERE mpn_a=? AND mpn_b=?", (ma, mb)).fetchone():
            con.execute("INSERT INTO cross_refs(mfr_a,mpn_a,mfr_b,mpn_b,ref_source) VALUES(?,?,?,?,?)",
                        (a, ma, b, mb, "Демонстраційний довідник аналогів"))


def makes() -> list[str]:
    return sorted(VEHICLES)


def models(make: str) -> list[str]:
    return [m for m, _, _ in VEHICLES.get(make, [])]


def years(make: str, model: str) -> list[int]:
    for m, y0, y1 in VEHICLES.get(make, []):
        if m == model:
            return list(range(y1, y0 - 1, -1))
    return []


def analogs_of(con, mfr: str, mpn: str) -> list[dict]:
    """Аналоги з довідника cross_refs (в обидва боки) — без звернення до інтернету."""
    n = norm_mpn(mpn)
    out, seen = [], set()
    for r in con.execute("SELECT * FROM cross_refs WHERE verified=1"):
        for (ma, pa), (mb, pb) in (((r["mfr_a"], r["mpn_a"]), (r["mfr_b"], r["mpn_b"])),
                                   ((r["mfr_b"], r["mpn_b"]), (r["mfr_a"], r["mpn_a"]))):
            if norm_mpn(pa) == n and (norm_mpn(pb), mb) not in seen:
                seen.add((norm_mpn(pb), mb))
                out.append({"mfr": mb, "mpn": pb, "ref_source": r["ref_source"]})
    return out


def import_fitment(con, make, model, year_from, year_to, engine, items, ref_source, confirmed=1) -> int:
    """Точка підключення API сумісності: items = [(category, part_name, mfr, mpn, is_oem)]."""
    row = con.execute("SELECT id FROM vehicles WHERE make=? AND model=? AND engine=? AND year_from=? AND year_to=?",
                      (make, model, engine, year_from, year_to)).fetchone()
    vid = row[0] if row else con.execute("INSERT INTO vehicles(make,model,year_from,year_to,engine) VALUES(?,?,?,?,?)",
                                         (make, model, year_from, year_to, engine)).lastrowid
    n = 0
    for cat, name, mfr, mpn, oem in items:
        n += con.execute("INSERT OR IGNORE INTO fitment(vehicle_id,category,part_name,mfr,mpn,is_oem,ref_source,confirmed)"
                         " VALUES(?,?,?,?,?,?,?,?)", (vid, cat, name, mfr, mpn, int(oem), ref_source, confirmed)).rowcount
    return n


# ---- довідники з БД (редагуються в адмінці) ----
def db_makes(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT make FROM car_models ORDER BY make")]


def db_models(con, make: str) -> list[str]:
    return [r[0] for r in con.execute("SELECT model FROM car_models WHERE make=? ORDER BY model", (make,))]


def db_years(con, make: str, model: str) -> list[int]:
    r = con.execute("SELECT year_from, year_to FROM car_models WHERE make=? AND model=?", (make, model)).fetchone()
    return list(range(r["year_to"], r["year_from"] - 1, -1)) if r else []


def db_part_types(con) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in con.execute("SELECT category, name FROM part_types ORDER BY sort, id"):
        out.setdefault(r["category"], []).append(r["name"])
    return out
