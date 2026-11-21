# Публікація: GitHub і безкоштовний хостинг

## 0. Найпростіше: програма-помічник
Подвійний клік на **publish.command** (macOS) або **publish.bat** (Windows). Програма сама: перевірить, що в репозиторій не потраплять бази й ключі →
відкриє сторінку створення токена GitHub (вам лише натиснути «Generate token» і вставити токен) → створить репозиторій → надішле код →
запише посилання в `docs/LINKS.md`. Токен не зберігається. Якщо Git не встановлено, програма підкаже, що зробити.
Ручний варіант нижче потрібен лише якщо помічник не запрацював.

## 1. GitHub вручну (один раз, ~5 хвилин)
1. Зареєструйтесь на https://github.com та натисніть **New repository** → назва `partsinsight`, **Public**, без README/.gitignore (вони вже є в проєкті).
2. Розпакуйте архів. У Терміналі (macOS) або PowerShell (Windows):
   ```
   cd шлях/до/partsinsight
   git init
   git add .
   git commit -m "PartsInsight: аналітична система пошуку автозапчастин"
   git branch -M main
   git remote add origin https://github.com/ВАШ_ЛОГІН/partsinsight.git
   git push -u origin main
   ```
   (GitHub попросить увійти в браузері або створити Personal Access Token — пароль від акаунта більше не приймається.)
3. Посилання на репозиторій: `https://github.com/ВАШ_ЛОГІН/partsinsight` — його вписуємо в роботу.
Перед `git add` переконайтесь, що в папці немає файлів `*.db`, `.env` — вони вже в `.gitignore`. Ключі API в репозиторій потрапити не можуть: вони зберігаються лише в базі поза папкою програми.

## 2. PythonAnywhere (рекомендовано: безкоштовно, диск постійний, SQLite працює)
1. Зареєструйтесь на https://www.pythonanywhere.com (тариф Beginner, безкоштовно). Адреса сайту: `ВАШ_ЛОГІН.pythonanywhere.com`.
2. **Consoles → Bash**:
   ```
   git clone https://github.com/ВАШ_ЛОГІН/partsinsight.git
   cd partsinsight
   python3 -m venv ~/venv && source ~/venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Web → Add a new web app → Manual configuration → Python 3.x**. Вкажіть Virtualenv: `/home/ВАШ_ЛОГІН/venv`.
4. У розділі **Code** відкрийте WSGI configuration file, замініть вміст на:
   ```python
   import sys, os
   sys.path.insert(0, "/home/ВАШ_ЛОГІН/partsinsight")
   os.environ["PARTSINSIGHT_DATA"] = "/home/ВАШ_ЛОГІН/PartsInsightData"
   os.environ["PARTSINSIGHT_DEMO"] = "0"
   os.environ["BASE_URL"] = "https://ВАШ_ЛОГІН.pythonanywhere.com"
   from wsgi import app as application
   ```
5. Натисніть **Reload**. Відкрийте сайт, увійдіть в адмінку й **одразу змініть пароль** адміністратора; ключі джерел вводьте лише у веб-інтерфейсі.
6. Оновлення після змін: у Bash `cd partsinsight && git pull`, потім **Reload**.

Обмеження безкоштовного тарифу PythonAnywhere: вихідні запити лише до сайтів зі «білого списку» (https://www.pythonanywhere.com/whitelist/). Публічні API (eBay, SerpApi) зазвичай доступні; довільні інтернет-магазини для HTML-парсингу можуть бути заблоковані. Це слід зазначити в розділі про обмеження.

## 3. Render (запасний варіант)
New → Blueprint → оберіть репозиторій (файл `render.yaml`). Безкоштовний сервіс «засинає» і втрачає диск при перезапуску, тому ключі доведеться вводити знову — підходить лише для короткої демонстрації.


## 4. Перенесення вже зібраних даних (щоб нічого не втратити)
Ваша база лежить у `~/PartsInsightData` (поза папкою програми) і в GitHub **не потрапляє** — це правильно: там можуть бути ключі.
1. На своєму комп’ютері: `python3 tools/migrate_data.py export` → створить папку `~/PartsInsightExport` з чистою копією `partsinsight.db`
   (без ключів, пароля адміністратора та журналу запитів; архів деталей, ціни, довідники й кросс-референси збережено).
2. PythonAnywhere → вкладка **Files** → папка `PartsInsightData` → **Upload a file** → завантажте `partsinsight.db` (якщо на сайті вже є свої дані, завантажте файл під іншою назвою, наприклад `import.db`).
3. Якщо на сайті вже були свої дані: у **Consoles → Bash**: `cd partsinsight && source ~/venv/bin/activate && python tools/migrate_data.py merge ~/PartsInsightData/import.db` — додасть ваші деталі й ціни, нічого не перезаписуючи й не дублюючи.
4. Відкрийте сайт → адмінка: пароль адміністратора та ключі джерел введіть заново (вони свідомо не переносяться).
Перевірити кількість записів: `python3 tools/migrate_data.py info`.
