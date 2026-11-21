"""E1: заморожування, збір, офлайн-відтворення, розмітка, метрики. УСІ дані тут контрольні (тестовий запуск), не результат експерименту."""
import csv, importlib.util, json, pathlib, sys

import pytest

E1 = pathlib.Path(__file__).resolve().parents[1] / "experiments" / "e1"
sys.path.insert(0, str(E1))
import e1lib                                    # noqa: E402
from e1lib import E1Error                       # noqa: E402
from app import create_app, db                   # noqa: E402

QUERIES = ([("full", q) for q in ("GDB1550", "W 719/30", "0986452041", "P 85 114", "OC 47")]
           + [("variant", q) for q in ("GDB-1550", "w719/30", "0 986 452 041", "p85114", "Ford DG9Z-2200-N")]
           + [("partial", q) for q in ("GDB15", "W 719", "0986452", "P 85", "DG9Z-22")]
           + [("text", q) for q in ("колодки trw", "фільтр bosch", "фільтр mann", "свічка ngk", "амортизатор kyb")])


def write_queries(path, status="ЗАТВЕРДЖЕНО", mutate=None):
    rows = [{"id": f"Q{i:02d}", "type": t, "query": q, "expected_rule": "контрольне правило (ТЕСТ)",
             "rationale": "ТЕСТОВИЙ ЗАПУСК: контрольні дані", "evidence_source": "тест", "status": status}
            for i, (t, q) in enumerate(QUERIES, 1)]
    if mutate:
        mutate(rows)
    e1lib.write_csv(path, ["id", "type", "query", "expected_rule", "rationale", "evidence_source", "status"], rows)
    return path


@pytest.fixture()
def env(tmp_path):
    dbp = str(tmp_path / "live.db")
    create_app({"DATABASE": dbp, "TESTING": True})
    con = db.connect(dbp)
    name = con.execute("SELECT name FROM sources ORDER BY id LIMIT 1").fetchone()[0]
    con.close()
    q = write_queries(tmp_path / "queries.csv")
    return {"db": dbp, "src": name, "queries": q, "runs": tmp_path / "runs", "cfg": E1 / "config.json", "tmp": tmp_path}


def item(mpn, brand="TRW", url=None, price=300, title=None):
    return {"title": title or f"Колодки {brand} {mpn}", "mpn": mpn, "brand": brand, "price": price, "currency": "UAH", "pack": 1,
            "url": url or f"https://shop/{mpn}"}


def make_fetch(calls=None, fail_text=None):
    def fn(src, text):
        if calls is not None:
            calls.append((src["name"], text))
        if fail_text and text == fail_text:
            raise RuntimeError("тестовий збій мережі")
        if text == "0986AB1234":                       # аналог повертається ЛИШЕ за власним артикулом
            return [item("0986AB1234", "Bosch")]
        if text == "GDB1550":
            return []                                   # успішна відповідь із нульовою видачею
        if text == "0986452041":
            return [item("0986452041", "Bosch")]
        return []
    return fn


def do_freeze(env, run="e1-test-1", **kw):
    return e1lib.freeze(run, env["queries"], env["cfg"], env["db"], [env["src"]], env["runs"], test_run=True)


# ---------------------------------------------------------------- freeze
def test_shipped_queries_are_drafts_and_block_freeze(env):
    with pytest.raises(E1Error) as e:
        e1lib.freeze("real-1", E1 / "queries.csv", env["cfg"], env["db"], [env["src"]], env["runs"], test_run=True)
    assert "ЧЕРНЕТКА" in str(e.value)
    assert not (env["runs"] / "real-1").exists()


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r[0].update(status="ЧЕРНЕТКА"), "Q01: статус"),
    (lambda r: r[1].update(id="Q01"), "не унікальні"),
    (lambda r: r.pop(), "рівно 20"),
    (lambda r: r[0].update(type="text"), "категорія"),
    (lambda r: r[2].update(expected_rule=""), "правило релевантності"),
    (lambda r: r[3].update(rationale=""), "обґрунтування"),
])
def test_freeze_validation(env, mutate, needle):
    write_queries(env["queries"], mutate=mutate)
    with pytest.raises(E1Error) as e:
        do_freeze(env)
    assert needle in str(e.value)


def test_freeze_records_requests_with_analogs_and_marks_test(env):
    run = do_freeze(env)
    fr = e1lib.load_frozen(run)
    assert fr["test_run"] and "ТЕСТОВИЙ" in fr["label"]
    texts = {(r["qid"], r["text"], r["role"]) for r in fr["requests"]}
    assert ("Q01", "GDB1550", "original") in texts and ("Q01", "0986AB1234", "analog") in texts     # розширення за аналогом
    assert fr["cross_refs"] and fr["aliases"] and "components" in fr["sha256"]
    with pytest.raises(E1Error):
        do_freeze(env)                                  # той самий запуск не перезаписується


# ---------------------------------------------------------------- collect / evaluate
def full_run(env, run="e1-test-1"):
    r = do_freeze(env, run)
    e1lib.collect(r, env["db"], make_fetch(), pause=False)
    e1lib.evaluate(r)
    return r


def rows_of(run, name="methods_internal.csv"):
    return e1lib.read_csv(pathlib.Path(run) / "evaluation" / name)


def test_offline_replay_finds_analog_only_by_own_number(env):
    run = full_run(env)
    rows = rows_of(run)
    pi = [r for r in rows if r["id"] == "Q01" and r["метод"] == "partsinsight" and r["позиція"] == "1"][0]
    base = [r for r in rows if r["id"] == "Q01" and r["метод"] == "baseline" and r["позиція"] == "1"][0]
    assert pi["артикул"] == "0986AB1234" and pi["рівень_системи"] == "CROSS_REFERENCE"
    assert base["артикул"] == "" and base["offer_id"] == ""                  # базовий метод: лише початкова відповідь
    st = json.loads((pathlib.Path(run) / "responses" / e1lib.slug(env["src"]) / f"Q01__{e1lib.slug(env['src'])}__0.json").read_text(encoding="utf-8"))
    assert st["status"] == "ok_empty" and st["query_text"] == "GDB1550"       # успішна нульова видача зафіксована окремо


def test_repeat_evaluation_identical_and_independent_of_live_changes(env):
    run = full_run(env)
    first = (pathlib.Path(run) / "evaluation" / "methods_internal.csv").read_bytes()
    h1 = json.loads((pathlib.Path(run) / "evaluation" / "evaluation.json").read_text(encoding="utf-8"))["positions_sha256"]
    con = db.connect(env["db"])                          # «пізніші правки довідника» та кеш у живій базі
    con.execute("DELETE FROM cross_refs")
    con.execute("INSERT INTO cache VALUES(1,'gdb1550','2030-01-01T00:00:00+00:00','[]')")
    con.commit(); con.close()
    e1lib.evaluate(run)
    assert (pathlib.Path(run) / "evaluation" / "methods_internal.csv").read_bytes() == first
    assert json.loads((pathlib.Path(run) / "evaluation" / "evaluation.json").read_text(encoding="utf-8"))["positions_sha256"] == h1


def test_missing_response_blocks_evaluation_and_keeps_previous_table(env):
    run = do_freeze(env)
    calls = []
    res = e1lib.collect(run, env["db"], make_fetch(calls, fail_text="0986AB1234"), pause=False)
    assert res["failed"] == 2 and res["missing"] == 2          # Q01 і Q06 мають аналог 0986AB1234
    with pytest.raises(E1Error) as e:
        e1lib.evaluate(run)
    msg = str(e.value)
    assert "Q01" in msg and env["src"] in msg and "0986AB1234" in msg and "збій" in msg
    assert not (pathlib.Path(run) / "evaluation").exists()
    # повторний збір повторює ЛИШЕ пропущене
    calls2 = []
    res2 = e1lib.collect(run, env["db"], make_fetch(calls2), pause=False)
    assert calls2 == [(env["src"], "0986AB1234")] * 2 and res2["missing"] == 0
    e1lib.evaluate(run)
    sheet = pathlib.Path(run) / "evaluation" / "expert_sheet.csv"
    before = sheet.read_bytes()
    f = next((pathlib.Path(run) / "responses").rglob("Q05__*__0.json"))
    f.unlink()                                           # від джерела не отримано жодного файла
    with pytest.raises(E1Error) as e2:
        e1lib.evaluate(run)
    assert "Q05" in str(e2.value) and "відповіді немає" in str(e2.value)
    assert sheet.read_bytes() == before                  # попередня таблиця розмітки збережена


def test_zero_results_is_valid_only_with_recorded_success(env):
    run = do_freeze(env)
    e1lib.collect(run, env["db"], make_fetch(), pause=False)
    fr = e1lib.load_frozen(run)
    assert not e1lib.missing_requests(run, fr)           # ok_empty приймається
    f = next((pathlib.Path(run) / "responses").rglob("Q02__*__0.json"))
    d = json.loads(f.read_text(encoding="utf-8")); d["status"] = "failed"; d["items"] = []
    f.write_text(json.dumps(d), encoding="utf-8")
    assert len(e1lib.missing_requests(run, fr)) == 1     # «порожньо» без успіху — не відповідь


def test_conditions_change_requires_new_run(env):
    run = do_freeze(env)
    (pathlib.Path(run) / "queries.csv").write_text((pathlib.Path(run) / "queries.csv").read_text(encoding="utf-8").replace("GDB1550", "GDB1551"), encoding="utf-8")
    with pytest.raises(E1Error) as e:
        e1lib.collect(run, env["db"], make_fetch(), pause=False)
    assert "НОВИЙ запуск" in str(e.value)
    run2 = do_freeze(env, "e1-test-2")                   # новий запуск у окремій папці; попередній лишається
    assert run2 != run and (pathlib.Path(run) / "frozen.json").exists()
    fr = e1lib.load_frozen(run2)
    fr["sha256"]["components"]["app/matching.py"] = "0" * 64
    (pathlib.Path(run2) / "frozen.json").write_text(json.dumps(fr), encoding="utf-8")
    with pytest.raises(E1Error) as e2:
        e1lib.evaluate(run2)
    assert "app/matching.py" in str(e2.value)


def test_source_settings_change_blocks_collect(env):
    run = do_freeze(env)
    con = db.connect(env["db"]); con.execute("UPDATE sources SET search_url=search_url||'&x=1' WHERE name=?", (env["src"],)); con.commit(); con.close()
    with pytest.raises(E1Error) as e:
        e1lib.collect(run, env["db"], make_fetch(), pause=False)
    assert "Налаштування джерела" in str(e.value)


# ---------------------------------------------------------------- розмітка й метрики
def test_expert_sheet_hides_method_and_transfers_labels(env):
    run = full_run(env)
    sheet = rows_of(run, "expert_sheet.csv")
    assert list(sheet[0].keys()) == e1lib.SHEET_FIELDS and not {"метод", "позиція", "рівень_системи"} & set(sheet[0])
    keys = [(r["id"], r["offer_id"]) for r in sheet]
    assert len(keys) == len(set(keys))                   # пропозиція запиту розмічається один раз
    internal = rows_of(run)
    both = {(r["id"], r["offer_id"]) for r in internal if r["offer_id"]}
    assert set(keys) == both
    # експерт: усе «так», крім одного «ні»
    path = pathlib.Path(run) / "evaluation" / "expert_sheet.csv"
    for r in sheet:
        r["рішення"], r["підстава"] = "так", "каталог (тест)"
    sheet[0]["рішення"] = "ні"
    e1lib.write_csv(path, e1lib.SHEET_FIELDS, sheet)
    r = e1lib.apply_labels(run)
    assert r["unlabeled_positions"] == 0
    tab = rows_of(run, "e1_table.csv")
    target = (sheet[0]["id"], sheet[0]["offer_id"])
    hits = [x for x in tab if (x["id"], x["offer_id"] if "offer_id" in x else "") == target]
    same = [x for x in internal if (x["id"], x["offer_id"]) == target]
    assert same and all(x["рішення"] == "ні" for x in tab if x["id"] == target[0] and x["артикул"] == same[0]["артикул"] and x["URL"] == same[0]["URL"])


def test_final_report_blocked_until_all_labeled_then_formulas(env):
    run = full_run(env)
    with pytest.raises(E1Error) as e:
        e1lib.apply_labels(run); e1lib.final_report(run)
    assert "нерозмічена пропозиція" in str(e.value)
    assert not (pathlib.Path(run) / "e1_report.md").exists()
    path = pathlib.Path(run) / "evaluation" / "expert_sheet.csv"
    sheet = e1lib.read_csv(path)
    for r in sheet:
        r["рішення"], r["підстава"] = ("так" if r["артикул"] in ("0986AB1234", "0986452041") else "ні"), "тест"
    e1lib.write_csv(path, e1lib.SHEET_FIELDS, sheet)
    e1lib.apply_labels(run)
    txt = e1lib.final_report(run)
    assert "ТЕСТОВИЙ ЗАПУСК" in txt and (pathlib.Path(run) / "e1_report.md").exists()
    res = e1lib.compute(rows_of(run, "e1_table.csv"))
    pi, base = res["partsinsight"], res["baseline"]
    assert pi["hit1"] > base["hit1"] and pi["p5"] == pi["confirmed"] / 100 and base["p5"] == base["confirmed"] / 100
    assert pi["hits"] <= 20


def _good_rows():
    rows = []
    for m in e1lib.METHODS:
        for i in range(1, 21):
            for p in range(1, 6):
                rows.append({"id": f"Q{i:02d}", "метод": m, "позиція": str(p), "offer_id": "", "артикул": "", "URL": "", "рішення": "порожньо"})
    return rows


QIDS = [f"Q{i:02d}" for i in range(1, 21)]


def test_validate_table_ok_and_blockers():
    rows = _good_rows()
    assert e1lib.validate_table(rows, QIDS) == []
    assert any("відсутній" in p for p in e1lib.validate_table(rows[1:], QIDS))
    assert any("дублікат" in p for p in e1lib.validate_table(rows + [dict(rows[0])], QIDS))
    bad = _good_rows(); bad[0]["рішення"] = "можливо"
    assert any("невідоме рішення" in p for p in e1lib.validate_table(bad, QIDS))
    bad = _good_rows(); bad[0].update({"артикул": "X1", "offer_id": "abc", "рішення": ""})
    assert any("нерозмічена" in p for p in e1lib.validate_table(bad, QIDS))
    bad = _good_rows(); bad[0].update({"артикул": "X1", "offer_id": "abc", "рішення": "порожньо"})
    assert any("пропозиція є" in p for p in e1lib.validate_table(bad, QIDS))
    bad = _good_rows(); bad[0]["рішення"] = "так"
    assert any("порожньої позиції" in p for p in e1lib.validate_table(bad, QIDS))


def test_empty_zero_and_formulas():
    rows = _good_rows()
    for r in rows:
        if r["метод"] == "partsinsight" and r["id"] in ("Q01", "Q02") and r["позиція"] in ("1", "2"):
            r.update({"артикул": "A", "offer_id": "o", "рішення": "так"})
    res = e1lib.compute(rows)
    assert res["partsinsight"]["hit1"] == 2 / 20 and res["partsinsight"]["p5"] == 4 / 100
    assert res["baseline"]["hit1"] == 0 and res["baseline"]["empty_queries"] == 20


def test_control_run_script_smoke(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("control_run", E1 / "control_run.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    sys.argv = ["control_run.py", "--out", str(tmp_path)]
    m.main()
    out = capsys.readouterr().out
    assert "ТЕСТОВИЙ ЗАПУСК" in out and "ВІДМОВА на неповному зборі" in out and "ЗВІТ заблоковано до розмітки" in out
    assert (tmp_path / "runs" / "CONTROL-TEST" / "e1_report.md").exists()


def test_run_all_stops_on_unapproved_queries(tmp_path):
    import subprocess, sys, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / "experiments/e1/run_all.py"), "--db", str(tmp_path / "x.db"),
                        "--runs-dir", str(tmp_path / "runs")], capture_output=True, text=True)
    assert r.returncode == 2 and "не затверджені" in r.stdout
    assert not (tmp_path / "runs").exists()


def test_f4_check_needs_database(tmp_path):
    import subprocess, sys, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / "experiments/f4_check.py"), "--db", str(tmp_path / "none.db")],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "База ще не створена" in r.stdout
