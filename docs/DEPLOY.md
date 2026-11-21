# Публікація: GitHub і безкоштовний хостинг

## 1. GitHub (один раз, ~5 хвилин)
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
