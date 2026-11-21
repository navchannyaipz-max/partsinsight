"""Публікація проєкту на GitHub «одним кроком»: програма запитує потрібні дані, сама створює репозиторій і надсилає код.

Токен вводиться прихованим вводом, не друкується, не записується у файли й не зберігається в .git/config
(для надсилання використовується одноразовий заголовок авторизації). База даних і ключі до репозиторію не потрапляють.
Запуск: подвійний клік на publish.command (macOS) / publish.bat (Windows) або  python tools/publish.py
Для автоматичних перевірок: --user, --token-env, --repo, --yes, --api-base, --remote-url.
"""
from __future__ import annotations
import argparse, base64, getpass, json, os, pathlib, re, shutil, subprocess, sys, webbrowser
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN_URL = "https://github.com/settings/tokens/new?scopes=repo&description=PartsInsight%20publish"
SECRET_PATTERNS = [re.compile(p) for p in (r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"PRD-[0-9a-f]{8}-", r"AKIA[0-9A-Z]{16}",
                                              r"-----BEGIN (RSA |EC )?PRIVATE KEY-----")]


def say(m=""):
    print(m, flush=True)


def ask(prompt, default=None):
    try:
        s = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    except EOFError:
        s = ""
    return s or (default or "")


def git(*args, check=True, env_extra=None, capture=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", **(env_extra or {}))
    r = subprocess.run(["git", *args], cwd=ROOT, env=env, text=True, capture_output=True)
    if check and r.returncode:
        raise SystemExit("Помилка git: " + (r.stderr or r.stdout).strip().replace(os.environ.get("_PI_TOKEN", "\0"), "***"))
    return r.stdout.strip() if capture else r


def scan_for_secrets() -> list[str]:
    """Перевіряє файли, які потраплять до репозиторію (з урахуванням .gitignore), на бази й ключі."""
    files = git("ls-files", "--cached", "--others", "--exclude-standard", capture=True).splitlines()
    bad = []
    for f in files:
        p = ROOT / f
        if not p.is_file():
            continue
        if f.endswith((".db", ".db-wal", ".db-shm", ".sqlite")) or p.name == ".env":
            bad.append(f"{f}: файл бази/налаштувань не можна публікувати")
            continue
        if p.stat().st_size > 2_000_000 or p.suffix in (".png", ".jpg", ".zip", ".pdf", ".docx"):
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in SECRET_PATTERNS:
            if rx.search(txt):
                bad.append(f"{f}: схоже на ключ або токен ({rx.pattern[:18]}…)")
                break
    return bad


def api(base, token, method, path, **kw):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "PartsInsight-publish"}
    return requests.request(method, base + path, headers=h, timeout=30, **kw)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user"); ap.add_argument("--token-env", help="назва змінної середовища з токеном (для автоматизації)")
    ap.add_argument("--repo"); ap.add_argument("--private", action="store_true"); ap.add_argument("--yes", action="store_true")
    ap.add_argument("--root", help="тільки для перевірок"); ap.add_argument("--api-base", default="https://api.github.com"); ap.add_argument("--remote-url", help="тільки для перевірок")
    a = ap.parse_args()
    global ROOT
    if a.root:
        ROOT = pathlib.Path(a.root).resolve()

    say("=== Публікація PartsInsight на GitHub ===\n")
    if not shutil.which("git"):
        raise SystemExit("Git не встановлено.\n macOS: у Терміналі виконайте  xcode-select --install\n Windows: https://git-scm.com/download/win\nПотім запустіть цю програму знову.")
    if not (ROOT / ".git").exists():
        git("init", "-q"); git("branch", "-M", "main", check=False)

    say("\nКрок 0. Перевіряю, що в репозиторій не потраплять бази даних і ключі…")
    git("add", "-A")
    bad = scan_for_secrets()
    if bad:
        raise SystemExit("ЗУПИНЕНО. Знайдено небезпечне:\n  " + "\n  ".join(bad) + "\nПрибрано з публікації нічого не було; виправте й запустіть знову.")
    say("  Усе чисто.")


    token = os.environ.get(a.token_env, "") if a.token_env else ""
    if not token:
        say("Крок 1. Потрібен токен GitHub (одноразовий «пароль» для програми).")
        say("  Зараз відкриється сторінка GitHub. Увійдіть, нічого не змінюючи натисніть «Generate token» внизу сторінки,")
        say("  скопіюйте токен (починається з ghp_ або github_pat_) і вставте нижче. Він не відображатиметься під час вводу.\n")
        if not a.yes:
            try: webbrowser.open(TOKEN_URL)
            except Exception: say("  (Не вдалося відкрити браузер. Відкрийте вручну: " + TOKEN_URL + ")")
        token = getpass.getpass("Токен GitHub: ").strip()
    if not token:
        raise SystemExit("Токен не введено.")
    os.environ["_PI_TOKEN"] = token

    r = api(a.api_base, token, "GET", "/user")
    if r.status_code != 200:
        raise SystemExit(f"GitHub не прийняв токен (код {r.status_code}). Створіть новий токен із правом «repo» і спробуйте ще раз.")
    me = r.json(); login = me["login"]
    say(f"Увійшли як {login}.")
    if a.user and a.user != login:
        raise SystemExit(f"Токен належить {login}, а очікувався {a.user}.")

    name = a.repo or ask("Крок 2. Назва репозиторію", "partsinsight")
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    private = a.private or (False if a.yes else ask("Репозиторій публічний? (так/ні)", "так").lower().startswith(("н", "n")))

    say("\nКрок 4. Створюю репозиторій…")
    r = api(a.api_base, token, "POST", "/user/repos", json={"name": name, "private": private, "description": "PartsInsight — аналітична система пошуку автозапчастин (магістерська робота)"})
    if r.status_code == 422:
        say(f"  Репозиторій «{name}» уже існує — використаю його.")
        r = api(a.api_base, token, "GET", f"/repos/{login}/{name}")
    if r.status_code not in (200, 201):
        raise SystemExit(f"Не вдалося створити репозиторій (код {r.status_code}): {r.text[:200]}")
    html_url = r.json()["html_url"]; clone_url = a.remote_url or r.json()["clone_url"]

    say("\nКрок 5. Надсилаю код…")
    git("config", "user.name", me.get("name") or login)
    git("config", "user.email", f"{me['id']}+{login}@users.noreply.github.com")
    if git("status", "--porcelain", capture=True):
        git("commit", "-q", "-m", "PartsInsight: аналітична система пошуку автозапчастин")
    git("remote", "remove", "origin", check=False)
    git("remote", "add", "origin", clone_url)
    auth = base64.b64encode(f"{login}:{token}".encode()).decode()
    push = ["-c", f"http.extraheader=Authorization: Basic {auth}", "push", "-u", "origin", "main"]
    git(*push)
    # токен не лишається в налаштуваннях
    say("\nКрок 6. Записую посилання у docs/LINKS.md…")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "LINKS.md").write_text(f"# Посилання\n\n- Репозиторій: {html_url}\n- Сайт: (вставте адресу після розгортання, див. docs/DEPLOY.md)\n", encoding="utf-8")
    git("add", "docs/LINKS.md")
    if git("status", "--porcelain", capture=True):
        git("commit", "-q", "-m", "Додано посилання на репозиторій")
        git(*push)
    say(f"\nГОТОВО. Репозиторій: {html_url}")
    say("Надішліть це посилання мені (його вписуємо в роботу). Далі — розгортання сайту: docs/DEPLOY.md, розділ 2.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nСкасовано.")
