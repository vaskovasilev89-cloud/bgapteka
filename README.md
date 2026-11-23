# bgapteka – сравнение на цени в аптеките

Търсачка, която показва в коя аптека има даден продукт и къде е най-евтин.
Аптеки: SOpharmacy, Ремедиум, Гален, 36.6, BENU, Farmavision, Framar, Subra.

```
 твоят компютър (Python)                         MonsterASP (ASP.NET Core 8)
┌──────────────────────────────┐   POST /api/import  ┌───────────────────────────┐
│ scraper/  → чете аптеките     │ ──────────────────► │ web/  → bgapteka.runasp.net│
│ локална база SQLite           │   (gzip, API ключ)  │ търсачка, данни в паметта  │
│ локална търсачка :8000        │                     │ App_Data/sites/*.json.gz   │
└──────────────────────────────┘                     └───────────────────────────┘
```

Защо е разделено така: MonsterASP не поддържа Python, а безплатният план има 256 MB RAM
и IIS рестартира приложенията. Пълното обхождане (~100 000 продукта) отнема 1–2 часа и
върви по-надеждно от твоя компютър. Някои аптеки блокират заявки от сървъри в датацентрове,
но пропускат домашен интернет.

---

## 1. Публичният сайт (web/) – автоматичен деплой с GitHub Actions

Всеки `git push` в `main`, който променя нещо в `web/`, пуска
[.github/workflows/deploy.yml](.github/workflows/deploy.yml): GitHub build-ва проекта и го
качва в bgapteka.runasp.net чрез Web Deploy. Прогресът се вижда в GitHub → **Actions**.
Ръчно пускане: Actions → *Deploy to MonsterASP* → **Run workflow**.

Еднократна настройка:
1. В MonsterASP: **Deploy (FTP / Visual Studio)** → включи **Web Deploy** и запиши
   сървъра, името на сайта, потребителя и паролата.
2. В GitHub: **Settings → Secrets and variables → Actions → New repository secret** и добави:
   `WEBDEPLOY_SERVER` (напр. `site110353.siteasp.net`), `WEBDEPLOY_SITE` (напр. `site110353`),
   `WEBDEPLOY_USER`, `WEBDEPLOY_PASSWORD`.
3. Не ползвай **Git deploy → Publish** в панела: той копира изходния код, а не build-а.
4. **API ключ**: измисли дълъг случаен ключ (поне 16 символа). В PowerShell:
   `[guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")`.
   С **File Manager** създай `wwwroot/App_Data/apikey.txt` (до `BgApteka.Web.dll`) и сложи
   ключа в него. Деплоят никога не трие файлове на сървъра, така че ключът и данните остават.
5. Провери, че https://bgapteka.runasp.net се отваря. В началото ще е празен, докато не качиш данни.

Можеш да деплойваш и от Visual Studio: отвори `BgApteka.sln` → Publish → Web Deploy
(профила се сваля от панела на MonsterASP).

## 2. Събирането на цените (scraper/) – на твоя компютър

Нужен е Python 3.10 или по-нов.

1. В `scraper/` пусни **start.bat**. Първия път той инсталира всичко и създава `config.json`.
2. Отвори `scraper/config.json` и попълни `api_key`, като сложиш същия ключ като в `apikey.txt`.
   Този файл **не се качва в git**.
3. В отворения сайт http://127.0.0.1:8000 натисни **Данни ▾ → Обнови всички аптеки**.
   Когато обхождането свърши, данните се качват в bgapteka.runasp.net автоматично
   (`auto_push`). Можеш да ги качиш и ръчно с бутона **Качи в сайта**.

От командния ред:
```
.venv\Scripts\python scrape.py                  # всички аптеки
.venv\Scripts\python scrape.py galen benu       # само избрани
.venv\Scripts\python scrape.py --limit 30 subra # бърз тест
.venv\Scripts\python push.py                    # качи в публичния сайт
```
Ако искаш автоматично обновяване, сложи `scrape.py` в Task Scheduler на Windows.

### Как се взимат данните
| Аптека | Метод | Приблизително продукти |
|---|---|---|
| Farmavision | WooCommerce JSON API (100 на заявка) | ~12 000 |
| SOpharmacy, 36.6, Гален, BENU, Framar, Subra, Ремедиум | sitemap → всяка продуктова страница → schema.org JSON-LD / `product:price:amount` | 5 000 – 25 000 на аптека |

Цените са в евро. Ако някоя аптека даде цена в лева, тя се превръща по курс 1.95583.
Един и същ продукт от различни аптеки се групира по баркод (EAN), ако аптеката го показва.
Иначе се групира по сходно име с еднаква доза и количество.

Скоростта е по 4 паралелни заявки към всяка аптека с пауза 0,3 сек. Не я увеличавай много,
за да не натоварваш сайтовете и да не те блокират.

### Блокиране
Ако за някоя аптека пише „сайтът отказва достъп“: изчакай и опитай пак, намали
`--concurrency 2` или пусни само нея (`scrape.py remedium`). Ремедиум и Framar имат по-строга
защита. Не можах да проверя Ремедиум докрай, затова парсърът там е общ (Magento) и може да
се наложи донастройка в `pharma/sites.py`.

### Важно
Цените са публична информация, но използването им е по правилата на всеки сайт.
Провери условията им, преди да направиш сайта публичен или търговски.

## Структура
```
BgApteka.sln
web/                  ASP.NET Core 8, без NuGet пакети
  Program.cs          API: /api/search, /api/status, POST /api/import/{site}
  Catalog.cs          данни в паметта + търсене
  Grouping.cs         групиране (същият алгоритъм като в Python)
  TextNorm.cs         кирилица→латиница, за да работи и „нурофен“, и „nurofen“
  wwwroot/index.html  страницата (обща за двете части)
scraper/
  pharma/sites.py     настройки на аптеките
  pharma/extract.py   извличане на име/цена/баркод
  pharma/scraper.py   обхождане
  pharma/push.py      качване в публичния сайт
  tests/test_all.py   тестове
```
