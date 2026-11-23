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
1. В MonsterASP: **Overview → Visual Studio access** – там са потребителят (напр. `site110353`)
   и паролата за Web Deploy. Сървърът и името на сайта са записани в workflow-а.
2. В GitHub: **Settings → Secrets and variables → Actions → Secrets → New repository secret**:
   `WEBDEPLOY_USER` и `WEBDEPLOY_PASSWORD`.
3. Не ползвай **Git deploy → Publish** в панела: той копира изходния код, а не build-а.
4. **API ключ**: измисли дълъг случаен ключ (поне 16 символа). В PowerShell:
   `[guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")`.
   С **File Manager** създай `wwwroot/App_Data/apikey.txt` (до `BgApteka.Web.dll`) и сложи
   ключа в него. Деплоят никога не трие файлове на сървъра, така че ключът и данните остават.
5. Провери, че https://bgapteka.runasp.net се отваря. В началото ще е празен, докато не качиш данни.

### База данни (MSSQL)
Сайтът пази продуктите в **MSSQL**, ако има connection string. Без такъв ги пази във файлове
в `App_Data/sites/` (така работи и преди да има база).
1. В MonsterASP: **Databases → MS SQL → Create database**. Копирай connection string-а, който дава панелът.
2. С **File Manager** създай `wwwroot/App_Data/sql.txt` и сложи в него connection string-а на един ред.
   Вместо файла можеш да зададеш променлива на средата `BGAPTEKA_SQL`.
3. Рестартирай сайта (**Overview → Restart**). Таблиците `Sites` и `Products` се създават сами при първа
   заявка. Ако вече има данни във файлове, те се пренасят в базата автоматично.
4. Провери https://bgapteka.runasp.net/api/status – трябва да пише `"storage":"mssql"`.

Можеш да деплойваш и от Visual Studio: отвори `BgApteka.sln` → Publish → Web Deploy
(профила се сваля от панела на MonsterASP).

## 2. Събирането на цените (scraper/) – на твоя компютър

Нужен е Python 3.10 или по-нов.

1. В `scraper/` пусни **start.bat**. Първия път той инсталира всичко и създава `config.json`.
2. Отвори `scraper/config.json` и попълни `api_key`, като сложиш същия ключ като в `apikey.txt`.
   Този файл **не се качва в git**.
3. В отворения сайт http://127.0.0.1:8000 натисни **Данни ▾ → Обнови всички аптеки**.
   Докато обхождането върви, на всеки 1000 нови продукта (`push_every` в `config.json`) събраното
   досега се качва в bgapteka.runasp.net, а накрая – всичко (`auto_push`). Можеш да ги качиш и ръчно с бутона **Качи в сайта**.

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

## 3. Брошури на супермаркети (scraper/flyers.py)

Началната страница на сайта има два раздела: **Аптеки** и **Супермаркети**. В „Супермаркети“
се търси в артикулите от седмичните брошури на Kaufland и Lidl (само валидните към днешна дата).

`flyers.py` взима брошурата от същите данни, които зарежда viewer-ът на Kaufland/Lidl, и
разчита артикулите **безплатно от текста в PDF-а** на брошурата (`pharma/flyer_pdf.py`): име,
количество, промоционална и стандартна цена, отстъпка, условия (напр. „ОТ ВТОРНИК 24.11.“) и
къде на страницата е артикулът. Сайтът показва изрязана снимка на артикула от страницата.
Записват се в таблиците `Flyers` и `FlyerItems` (MSSQL) заедно с периода на брошурата.

1. Сложи адресите на брошурите за седмицата в `scraper/config.json` → `"flyers": [...]`.
2. `.venv\Scripts\python flyers.py` – разчита и качва всички брошури (около минута).

PDF-ът хваща ~90% от артикулите. Ако някой магазин смени оформлението или няма текст в PDF-а,
има и платен вариант през Claude API: `flyers.py АДРЕС --source claude` (нужен е
`ANTHROPIC_API_KEY`, около $0.05–0.10 на страница; разчетените страници се пазят в
`scraper/data/flyers/` и не се плащат повторно).

## Структура
```
BgApteka.sln
web/                  ASP.NET Core 8 (+ Microsoft.Data.SqlClient)
  Program.cs          API: /api/search, /api/status, POST /api/import/{site}
  Catalog.cs          общо класиране + FileCatalog (данни във файлове и в паметта)
  SqlCatalog.cs       MSSQL хранилище (таблици Sites и Products)
  Flyers.cs           брошури на супермаркети (таблици Flyers и FlyerItems)
  Grouping.cs         групиране (същият алгоритъм като в Python)
  TextNorm.cs         кирилица→латиница, за да работи и „нурофен“, и „nurofen“
  wwwroot/index.html  страницата (обща за двете части)
scraper/
  pharma/sites.py     настройки на аптеките
  pharma/extract.py   извличане на име/цена/баркод
  pharma/scraper.py   обхождане
  pharma/push.py      качване в публичния сайт
  flyers.py           брошури Kaufland/Lidl → артикули (от PDF или с Claude) → сайта
  pharma/flyer_pdf.py разчитане на артикулите от PDF-а на Lidl и Kaufland
  tests/test_all.py   тестове
```
