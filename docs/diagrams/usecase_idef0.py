"""UML Use Case (рис. 2.1), IDEF0 A-0 і декомпозиція A0 — чорно-біла графіка за стандартами UML 2.5 та IDEF0 (FIPS 183)."""
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, Rectangle, Circle, FancyArrowPatch
plt.rcParams.update({"font.family": "FreeSerif", "text.color": "black", "axes.edgecolor": "black"})

def actor(ax, x, y, label):
    ax.add_patch(Circle((x, y + 0.55), 0.2, fill=False, lw=1.4, ec="k"))
    for a, b in [((x, y + 0.35), (x, y - 0.2)), ((x - 0.35, y + 0.15), (x + 0.35, y + 0.15)), ((x, y - 0.2), (x - 0.3, y - 0.7)), ((x, y - 0.2), (x + 0.3, y - 0.7))]:
        ax.plot(*zip(a, b), "k", lw=1.4)
    ax.text(x, y - 1.0, label, ha="center", va="top", fontsize=13)

def uc_diagram():
    fig, ax = plt.subplots(figsize=(12, 7.4), dpi=220); ax.set_xlim(0, 12); ax.set_ylim(0, 7.4); ax.axis("off")
    ax.add_patch(Rectangle((2.4, 0.3), 7.2, 6.8, fill=False, lw=1.5, ec="k"))
    ax.text(6, 6.8, "Аналітична система пошуку автозапчастин", ha="center", fontsize=14)
    def uc(x, y, t, w=3.3):
        ax.add_patch(Ellipse((x, y), w, 0.78, fill=False, lw=1.2, ec="k")); ax.text(x, y, t, ha="center", va="center", fontsize=11.5); return x, y
    U = {k: uc(*v) for k, v in {
        "search": (4.1, 6.0, "Пошук за артикулом,\nописом або авто"), "view": (4.1, 5.05, "Перегляд результатів\nі підстав відповідності"),
        "filt": (4.1, 4.1, "Фільтрація та сортування"), "cmp": (8.0, 6.0, "Порівняння тотожних\nпропозицій (до 3)"),
        "hist": (8.0, 5.05, "Перегляд історії\nцін і доставки"), "state": (8.0, 4.1, "Перегляд стану джерел")}.items()}
    A = {k: uc(*v) for k, v in {
        "src": (4.1, 2.7, "Підключення джерел\nта ключів доступу"), "dic": (4.1, 1.7, "Керування довідниками"),
        "xref": (8.0, 2.7, "Керування кросс-\nреференсами"), "arch": (8.0, 1.7, "Перегляд архіву")}.items()}
    actor(ax, 1.0, 5.0, "Користувач"); actor(ax, 11.0, 2.9, "Адміністратор")
    for k, (x, y) in U.items():
        ax.plot([1.45, x - 1.65 if x < 6 else x - 1.65], [5.2, y], "k", lw=0.8, zorder=0)
    for k, (x, y) in A.items():
        ax.plot([10.55, x + 1.65], [3.1, y], "k", lw=0.8, zorder=0)
    ax.plot([10.55, 8.0 + 1.65], [3.1, 4.1], "k", lw=0.8, zorder=0)
    # include: перегляд результатів -> пошук (UML <<include>>)
    ax.annotate("", xy=(4.1, 5.62), xytext=(4.1, 5.44), arrowprops=dict(arrowstyle="->", lw=1.0, ls=(0, (4, 3))))
    ax.text(4.12, 5.53, "«include»", fontsize=9, ha="left", va="center", style="italic")
    fig.savefig("png/usecase.png", bbox_inches="tight", facecolor="white"); plt.close(fig)

def arrow(ax, p, q, **kw):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=14, lw=1.3, color="k", shrinkA=0, shrinkB=0, **kw))

def idef0_a0():
    fig, ax = plt.subplots(figsize=(12, 7), dpi=220); ax.set_xlim(0, 12); ax.set_ylim(0, 7); ax.axis("off")
    ax.add_patch(Rectangle((0.2, 0.2), 11.6, 6.6, fill=False, lw=1.2, ec="k"))
    ax.add_patch(Rectangle((3.8, 2.3), 4.4, 2.4, fill=False, lw=2, ec="k"))
    ax.text(6, 3.5, "Забезпечити пошук і аналіз\nпропозицій автомобільних\nзапчастин", ha="center", va="center", fontsize=14)
    ax.text(8.1, 2.4, "A0", ha="right", va="bottom", fontsize=12)
    arrow(ax, (0.6, 3.5), (3.8, 3.5)); ax.text(0.7, 3.65, "Пошукові запити;\nвідповіді джерел\n(JSON API / HTML)", fontsize=12, va="bottom")
    arrow(ax, (8.2, 3.5), (11.4, 3.5)); ax.text(8.3, 3.65, "Знайдені деталі;\nрівні відповідності;\nгрупи пропозицій;\nаналітика; архів цін", fontsize=12, va="bottom")
    arrow(ax, (6, 6.3), (6, 4.7)); ax.text(6.1, 5.9, "Вимоги FR/NFR; правила нормалізації;\nпороги; robots.txt і ліміти запитів", fontsize=12, va="center", ha="left")
    arrow(ax, (5.0, 0.7), (5.0, 2.3)); ax.text(0.9, 1.35, "Програмні модулі\nFlask, SQLite", fontsize=12, va="center")
    arrow(ax, (7.0, 0.7), (7.0, 2.3)); ax.text(7.1, 1.35, "Адміністратор", fontsize=12, va="center")
    ax.text(0.35, 6.55, "Вузол:  A-0        Назва:  Контекстна діаграма IDEF0", fontsize=11, va="center")
    fig.savefig("png/idef0_a0.png", bbox_inches="tight", facecolor="white"); plt.close(fig)

def idef0_decomp():
    fig, ax = plt.subplots(figsize=(14, 7.6), dpi=220); ax.set_xlim(0, 16); ax.set_ylim(0, 7.6); ax.axis("off")
    ax.add_patch(Rectangle((0.15, 0.15), 15.7, 7.3, fill=False, lw=1.2, ec="k"))
    names = ["A1\nОтримання, перевірка\nта нормалізація даних", "A2\nФормування\nпошукової видачі", "A3\nВизначення рівня\nвідповідності, групи", "A4\nАналітичне порівняння\nпропозицій"]
    pos = [(2.4, 5.0), (5.5, 3.8), (8.6, 2.6), (11.7, 1.4)]; w, h = 2.6, 1.3
    for (x, y), n in zip(pos, names):
        ax.add_patch(Rectangle((x, y), w, h, fill=False, lw=1.8, ec="k")); ax.text(x + w / 2, y + h / 2, n, ha="center", va="center", fontsize=11.5)
    arrow(ax, (0.5, 5.65), (2.4, 5.65)); ax.text(0.5, 5.8, "Запити;\nвідповіді джерел", fontsize=10.5, va="bottom")
    links = [((2.4 + w, 5.45), (5.5, 4.6), "нормалізовані записи"), ((5.5 + w, 4.25), (8.6, 3.4), "кандидати"), ((8.6 + w, 3.05), (11.7, 2.2), "групи тотожних")]
    for (x, y), (x2, y2), lab in links:
        xm = x + (x2 - x) / 2
        ax.plot([x, xm, xm], [y, y, y2], "k", lw=1.3); arrow(ax, (xm, y2), (x2, y2))
        ax.text(x + 0.08, y - 0.12, lab, fontsize=9.5, va="bottom")
    arrow(ax, (11.7 + w, 1.8), (15.6, 1.8)); ax.text(14.35, 1.95, "Пропозиції,\nстатистика,\nпояснення", fontsize=10, ha="left", va="bottom")
    for (x, y) in pos:
        arrow(ax, (x + w / 2, y + h + 0.5), (x + w / 2, y + h))
        arrow(ax, (x + w / 2, y - 0.55), (x + w / 2, y))
    ax.text(0.35, 7.05, "Керування: вимоги FR/NFR, правила, пороги, robots.txt", fontsize=10.5, va="center")
    ax.text(0.35, 0.5, "Механізми: модулі Flask, SQLite, адміністратор", fontsize=10.5, va="center")
    ax.text(15.7, 7.05, "Вузол: A0   Декомпозиція", fontsize=11, ha="right", va="center")
    fig.savefig("png/idef0_decomp.png", bbox_inches="tight", facecolor="white"); plt.close(fig)

uc_diagram(); idef0_a0(); idef0_decomp(); print("ok")
