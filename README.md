# ingest-parse

`.txt` / `.doc` / `.docx` / `.docm` / `.rtf` / `.pdf` → **Markdown-строка** для RAG (chunking, embeddings,
цитирование) + папка `media/` с картинками. DOCX и PDF разбирает [Docling](https://github.com/docling-project/docling)
(`docling-slim`, без OCR); `.doc` и `.rtf` сначала конвертируются LibreOffice в `.docx`. Born-digital PDF
(с текстовым слоем) даёт Markdown того же вида, что из `.docx`; страницы-сканы — PNG + текст от VLM (`--vision`),
без него — честная заглушка.
Опционально (`--vision`) над каждой картинкой ставится описание от VLM.

## Установка (Windows)

Нужно: **Python ≥ 3.12**, [uv](https://docs.astral.sh/uv/), **LibreOffice** (для `.doc`, `.rtf` и картинок-страниц
со схемами). Остальное (`docling-slim` с моделями раскладки для PDF — torch CPU, `opencv-python-headless`,
`pypdfium2`, `pillow`, `pylatexenc`) ставит `uv sync`; виртуальное окружение — около 1 ГБ.

PowerShell:

```powershell
winget install --id=astral-sh.uv -e
winget install TheDocumentFoundation.LibreOffice
cd C:\path\to\bam-bam-bam
uv sync
```

`soffice.exe` ищется так: `LIBREOFFICE_PATH` / `SOFFICE` (полный путь к `soffice.exe`) → `PATH` →
`C:\Program Files\LibreOffice\program\soffice.exe` → `C:\Program Files (x86)\…` → пути macOS/Linux.
Нестандартная установка:

```powershell
$env:LIBREOFFICE_PATH = "D:\Apps\LibreOffice\program\soffice.exe"
```

```bat
set LIBREOFFICE_PATH=D:\Apps\LibreOffice\program\soffice.exe
```

LibreOffice запускается с отдельным временным профилем — работает и когда LibreOffice открыт.
Без LibreOffice `.txt` / `.docx` / `.docm` разбираются, схемы в `.docx` пропускаются, `.doc` / `.rtf` →
`LibreOfficeNotFoundError` с подсказкой по установке.

macOS / Linux: `brew install --cask libreoffice` / `sudo apt install libreoffice-writer`, затем `uv sync`.

## Использование

PowerShell:

```powershell
uv run ingest-parse report.docx                          # Markdown в stdout (всегда UTF-8)
uv run ingest-parse report.docx -o out\report.md         # + картинки в out\media\report\
uv run ingest-parse report.rtf  -o out\report.md
uv run ingest-parse report.pdf  -o out\report.md          # born-digital PDF
uv run ingest-parse scan.pdf    -o out\scan.md --vision   # сканы: страница → VLM (рабочий ПК)
uv run ingest-parse report.docx -o out\report.md --media-dir D:\m   # картинки в D:\m
```

cmd:

```bat
uv run ingest-parse report.docx -o out\report.md
uv run ingest-parse report.docx > report.md
```

Python:

```python
from ingest_parse import parse_document

md: str = parse_document("report.docx")
md = parse_document(data, filename="report.docx")                  # bytes: расширение обязательно
md = parse_document("report.docx", media_dir="out/media/report")   # + картинки в папку
md = parse_document("report.docx", media_dir="out/media/report", vision=True)  # + описания VLM
md = parse_document("report.pdf", media_dir="out/media/report")    # сканы → scan-NNN.png + заглушка
md = parse_document("scan.pdf", media_dir="out/media/scan", vision=True)  # сканы → текст от VLM
```

Ошибки: `UnsupportedFormatError` (подкласс `ValueError`), `FileNotFoundError`, `ValueError` (bytes без `filename`,
запароленный или битый PDF),
`LibreOfficeNotFoundError`, `RuntimeError` (сбой конвертации LibreOffice), `VisionConfigError` (`vision=True` без
адреса API). Коды CLI: `0` — успех, `1` — ошибка разбора, `2` — `--vision` без `VISION_API_BASE_URL`.

## Что получается

| Формат | Результат |
|---|---|
| `.docx` | заголовки `#`…`######`, списки `-` / `1.` (вложенность — отступ 3 пробела), таблицы, формулы `$…$` / `$$…$$`, картинки |
| `.docm` | как `.docx`, макросы игнорируются (LibreOffice не нужен) |
| `.doc` | LibreOffice → `.docx` → как выше |
| `.rtf` | LibreOffice → `.docx` → как выше |
| `.pdf` | born-digital: тот же Markdown, что из `.docx`; страницы-сканы — `scan-NNN.png` + текст от VLM (см. [PDF](#pdf)) |
| `.txt` | абзацы по пустым строкам, текст как есть |

**Заголовки DOCX**: стиль Word «Заголовок N» / «Название», а также набранные руками:
жирная или нумерованная КАПСОМ короткая строка (`1 ЦЕЛЬ РАБОТЫ`), продолжение нумерации (`4.2 …` после `4 …`),
склеенный заголовок `2.1.1 Тема. Текст…` → `### 2.1.1 Тема` + абзац `Текст…`. Подписи «Таблица 6», «Рис. 1 – …»
никогда не становятся `#`.

**Таблицы DOCX** — по виду таблицы:

| Таблица | Markdown |
|---|---|
| сетка с шапкой (жирная строка, объединения или просто первая строка) | GFM-таблица `\| … \|` |
| многоуровневая шапка (`Выручка` над `2024 \| 2025`) | столбцы `Выручка / 2024`, `Выручка / 2025` |
| объединение по вертикали / по горизонтали | значение повторяется в каждой строке / продолжение пустое |
| строка на всю ширину | первая — подпись `**…**` над таблицей, внутри — строка-раздел `**…**` |
| «поле — значение» (2 столбца, короткие уникальные ключи) | список `- **Поле:** значение` |
| вёрстка: титульник, подписи «Выполнил: / Проверил:», 1 столбец, почти пустые | обычные абзацы (подписи — по столбцам) |

Абзацы внутри ячейки → `<br>`, списки в ячейке → `- …<br>- …`, вложенная таблица → строки `a; b`, `|` экранируется.
Пустые таблицы, строки и столбцы выбрасываются.

**Формулы**: Word OMML → LaTeX (Docling); кириллица внутри формул восстанавливается. Формулы старого редактора
Equation 3.0 / MathType — OLE без текста: вместо них ссылка на картинку страницы (см. ниже).

**Номера списков — как в Word** (считаются по `numbering.xml`: формат уровня, `start`, перезапуски, продолжение
списка после абзаца между пунктами):

| В Word | Markdown |
|---|---|
| `3.` / `3)` | `3. текст` (настоящий номер, не `1.`) |
| `1.2.1.`, `а)`, `IV.`, `(1)` | `- 1.2.1. текст`, `- а) текст` (вложенность — отступом) |
| маркер • | `- текст` |
| нумерованный заголовок | `## 1.2 Тема` (номер Word, а не счётчик Docling) |

Пункты внутри ячеек таблицы — так же: `1. Собрать данные<br>2. Проверить данные`.

**Индексы**: верхний / нижний → `10<sup>38</sup>`, `x<sub>1</sub> + x<sub>2</sub>`, `H<sub>2</sub>O` (пробелы вокруг
сохраняются; соседние индексы склеиваются в один тег).

**Текст внутри абзаца**: `**жирный**` / `*курсив*` — если выделена часть абзаца; ссылки → `[текст](url)`.
Режим правок: берётся текст с принятыми правками.

**TXT — намеренно без разметки** (в TXT нет стилей). Кодировки: UTF-8 (с BOM и без), UTF-16 (BOM), иначе cp1251.

Пустой документ → `""`. Непустой результат заканчивается одним `\n`. Один и тот же файл → тот же Markdown.

### Картинки (`media/`)

Каждая вставленная картинка пишется как `img-001.png`, `img-002.png`… (Docling перекодирует растр в PNG), в Markdown
на том же месте — `![подпись](media/report/img-001.png)`:

- библиотека: `media_dir=None` (по умолчанию) — файлы **не** создаются; с `media_dir` — ссылка = путь как передан;
- CLI: с `-o` картинки в `<папка out.md>/media/<имя документа>/`, ссылки относительно `out.md` (всегда с `/`);
  без `-o` — только с `--media-dir`;
- alt: «Таблица N …» над картинкой или «Рисунок N …» под ней; нет подписи → `image`.

### Схемы, графики, OLE → картинка страницы

Нарисованное в Word (автофигуры, линии, группы, полотна, SmartArt, диаграммы, EMF/WMF, OLE: Equation, Visio, Excel)
в текст не восстанавливается: документ переводится LibreOffice в PDF, страница рендерится `pypdfium2` в
`page-NNN.png` (NNN — номер страницы PDF с 1), на месте фигуры — ссылка:

```markdown
![Схема (стр. 3) – Рисунок 5 – Схема БД](media/report/page-003.png)
```

- вид: `Схема` / `График` / `Рисунок` (EMF/WMF) / `Формула` / `Объект`; подпись рядом — после тире;
- несколько фигур на одной странице — один файл; подряд идущие фигуры одной страницы — одна ссылка;
- `media_dir=None` или нет LibreOffice → рендер не выполняется, ошибок нет;
- номер страницы — по раскладке LibreOffice, может отличаться от Word на ±1.

### RTF

`.rtf` всегда идёт через LibreOffice → `.docx` → тот же разбор. Простые RTF (текст, таблицы, картинки) выходят как
из Word. В сложных RTF рисунки-фигуры и диаграммы LibreOffice может потерять или превратить в обычную картинку.

### PDF

Страницы с текстовым слоем (экспорт из Word / LibreOffice / LaTeX, где текст выделяется мышью) разбирает Docling
(модели раскладки и таблиц, OCR выключен) → тот же обход, что для `.docx`: заголовки, списки, GFM-таблицы, подписи,
картинки `img-NNN.png`, `--vision`. Страницы-сканы рендерятся в PNG и уходят на VLM (см. [Сканы](#сканы-в-pdf)).

```powershell
uv run ingest-parse report.pdf -o out\report.md
uv run ingest-parse report.pdf -o out\report.md --vision
```

**Первый запуск** скачивает модели с HuggingFace (~0,5 ГБ: раскладка `docling-layout-heron` и таблицы
TableFormer из `docling-models`) в `%USERPROFILE%\.cache\huggingface\hub\`, дальше работает
офлайн. Если `huggingface.co` на ПК закрыт — скопируйте папки `models--docling-project--*` из этого кэша с машины,
где модели уже скачаны. Разбор: ~5–10 с на документ (CPU), модели грузятся один раз на процесс.

Перед Docling каждая страница проверяется `pypdfium2` (без OCR):

| Страница | Что в Markdown | stderr (CLI) / `PdfPageWarning` |
|---|---|---|
| digital — есть текстовый слой | разбирается как обычно | — |
| hybrid — скан с невидимым OCR-слоем | разбирается по этому слою как есть | `warning: pdf page N classified as hybrid …` |
| full_scan — картинка без текста или мусорный слой | блок `pdf-scan` (см. ниже) | `warning: pdf page N classified as full_scan; rendered + vision` / `placeholder emitted (…)` |
| blank — пустая | ничего | `warning: pdf page N classified as blank; skipped` |

PDF целиком из сканов / пустых страниц → Docling не запускается, только блоки сканов и заглушки (не пустая
строка), код выхода `0`. Запароленный PDF → `ValueError` («password-protected»).

#### Сканы в PDF

Каждая страница `full_scan` рендерится `pypdfium2` (масштаб 2) в `media/…/scan-NNN.png` (NNN — номер страницы
с 1) и на месте страницы ставится блок:

```markdown
<!-- pdf-scan:begin page="3" source="vision" sha256="…" kind="text_scan" -->

## Страница 3            ← только если в извлечённом тексте нет своего заголовка

Текст страницы в Markdown: заголовки, абзацы, списки, GFM-таблицы

<!-- vision:begin id="scan-003" sha256="…" kind="text_scan" model="-" -->
**Описание:** скан страницы отчёта
**Анализ:** о чём страница
<!-- vision:end id="scan-003" -->

![Страница 3 (скан)](media/doc/scan-003.png)

<!-- pdf-scan:end page="3" -->
```

Текст сканов даёт только VLM (OCR в пакете нет):

| Запуск | Скан |
|---|---|
| `--vision` (рабочий ПК с VLM) | страница → VLM с промптом «перепиши страницу в Markdown», `source="vision"` |
| без `--vision` | `source="none" status="error" reason="vision_off"` + ссылка на PNG; перезапустите с `--vision` |
| VLM ответил ошибкой / пусто | `reason="vision_error"` / `"vision_empty"` + причина + ссылка на PNG |

Без `--vision` в сеть ничего не уходит, как и для `.docx`. Ответ VLM кэшируется в `.vision-cache/` по sha256
PNG: повторный запуск не спрашивает API (`--vision-force` — спросить заново). Ошибка одной страницы не роняет
документ. Без `media_dir` PNG пишется во временную папку: текст есть, ссылки на картинку нет. Качество текста —
как у вашей VLM; рукопись и сильный перекос страницы не поддерживаются.

Текст абзацев, пунктов, подписей и заголовков сверяется с текстовым слоем `pypdfium2`: оттуда берутся
`**жирный**` / `*курсив*` (по имени и флагам шрифта), ссылки `[текст](url)` (аннотации-ссылки PDF) и исходные
символы — тире `–`, `−` в подписях «Рисунок 1 – …» не превращаются в дефис. Если слой с текстом Docling не
совпадает (меньше 60 % слов), остаётся текст Docling без разметки.

Отличия от `.docx` (свойства PDF):

- в ячейках таблиц тире — как отдал Docling (обычно дефис), разметки внутри ячеек нет;
- колонтитулы и номера страниц выбрасываются; уровень нумерованного заголовка — по номеру (`1.2` → `##`);
  строки шапки титульного листа (министерство, учреждение образования, кафедра, «(обязательное)») — обычные абзацы;
- склеенные моделью раскладки абзацы разрезаются, только если в PDF видна граница: короткая последняя строка,
  красная строка или увеличенный интервал после `.` `!` `?` `:` `;`. Без таких признаков абзацы остаются склеенными;
- формулы — текстом из PDF внутри `$$…$$`, без LaTeX (распознавание формул Docling — отдельная модель
  CodeFormula, она не подключена);
- блоки кода — обычными абзацами (Docling их в born-digital PDF почти не распознаёт);
- векторные схемы (фигуры Word, Visio) модель раскладки находит на отрендеренной странице и вырезает в
  `img-NNN.png`, как растровые картинки; без подписи «Рисунок …» рядом alt — `image`;
- картинки внутри ячеек таблицы не выводятся; страница целиком из векторной графики без текста →
  `warning: pdf page N has only vector graphics …`.

## Vision: описания картинок (опционально)

По умолчанию парсер работает **офлайн** и никуда ничего не отправляет. С `--vision` после разбора каждая картинка из
`media/` отправляется на VLM (OpenAI-совместимый `POST {VISION_API_BASE_URL}/v1/chat/completions`), и над ссылкой
вставляется блок:

```markdown
<!-- vision:begin id="page-004" sha256="…" kind="diagram" model="-" -->
**Описание:** …
**Текст с изображения:** …
**Анализ:** …
<!-- vision:end id="page-004" -->

![Схема (стр. 4)](media/report/page-004.png)
```

> **Конфиденциальность:** с `--vision` картинки документа (в т. ч. целые страницы со схемами) уходят на указанный API.

Настройка (PowerShell):

```powershell
$env:VISION_API_BASE_URL = "http://localhost:8080"        # на рабочем ПК, где запущена модель
# $env:VISION_API_BASE_URL = "http://192.168.4.101:8080"  # с других машин в LAN
uv run ingest-parse report.docx -o out\report.md --vision
```

cmd:

```bat
set VISION_API_BASE_URL=http://192.168.4.101:8080
uv run ingest-parse report.docx -o out\report.md --vision
```

Проверка сервера: `curl http://localhost:8080/v1/models`.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `VISION_API_BASE_URL` (или `INGEST_VISION_API_BASE_URL`) | — (обязательна для `--vision`) | адрес сервера; `/v1` в конце можно не писать |
| `VISION_API_KEY` (или `INGEST_VISION_API_KEY`) | пусто | если задан — `Authorization: Bearer …` |
| `VISION_MODEL` (или `INGEST_VISION_MODEL`) | не передаётся | поле `model` — только если сервер его требует |
| `INGEST_VISION_TIMEOUT_S` | `60` | таймаут запроса, с |
| `INGEST_VISION_MAX_RETRIES` | `2` | повторы при сетевой ошибке / 5xx / 429 |
| `INGEST_VISION_MAX_LONG_EDGE` | `2048` | картинка больше — уменьшается в памяти перед отправкой (файл не меняется) |

Тело запроса: `messages` (пустой `system` + `user` с текстом и `image_url` = `data:image/png;base64,…`),
`temperature: 0.2`, `chat_template_kwargs: {"enable_thinking": true, "resolved_reasoning_effort": "high"}`.
`<think>…</think>` в ответе отбрасывается.

- страницы-сканы PDF (`scan-*`) уходят на VLM ещё при разборе, с промптом «перепиши страницу в Markdown»
  (см. [Сканы](#сканы-в-pdf)); второй раз как картинка они не описываются;
- вид картинки (`kind`): `logo` (мелкая), `photo` (JPEG) — только описание; `diagram` (`page-*`, «схема»),
  `table_scan` («Таблица»), `chart` («График»), `equation_img` («Формула», OLE), `unknown` — описание + текст + анализ;
- ответы кэшируются в `<media>/.vision-cache/<sha256>-<режим>.json`: повторный разбор того же документа API не
  дёргает; `--vision-force` — спросить заново;
- уже описанная картинка в том же `.md` не описывается повторно; одна и та же картинка несколько раз — один блок
  над первой ссылкой; картинка в ячейке таблицы — блок над таблицей;
- ошибка API у картинки → блок `status="error"` с причиной, остальные картинки обрабатываются, CLI завершается
  с кодом 0; при следующем `--vision` такие блоки переспрашиваются;
- без `VISION_API_BASE_URL` → `--vision` сразу завершается с кодом 2, разбор не начинается.

## Windows: все команды подряд

PowerShell, из папки проекта. Первые два блока — один раз на ПК.

**1. Установка**

```powershell
winget install --id=astral-sh.uv -e
winget install TheDocumentFoundation.LibreOffice          # нужен для .doc / .rtf и схем в .docx
git clone https://github.com/Borisserz/bam-bam-bam.git    # репозиторий приватный — git попросит вход в GitHub
cd bam-bam-bam
uv sync                                                   # ~1 ГБ: Docling, torch CPU, OpenCV, pypdfium2
uv run python -c "import docling, torch, cv2, pypdfium2; print('deps ok')"
```

Обновить код: `git pull` и `uv sync`.

**2. Модели для PDF** — скачиваются сами при первом разборе PDF (~0,5 ГБ, HuggingFace), дальше офлайн:

```powershell
uv run ingest-parse report.pdf -o out\report.md
dir $env:USERPROFILE\.cache\huggingface\hub | findstr docling   # docling-layout-heron, docling-models
```

Если `huggingface.co` закрыт — скопируйте папки `models--docling-project--*` с другой машины в
`%USERPROFILE%\.cache\huggingface\hub\`.

**3. Один документ**

```powershell
uv run ingest-parse report.docx -o out\report.md          # .docx / .docm / .doc / .rtf / .txt / .pdf
uv run ingest-parse report.pdf  -o out\report.md          # картинки → out\media\report\
uv run ingest-parse report.docx > out\report.md           # Markdown в stdout, без картинок
uv run ingest-parse report.docx -o out\report.md --media-dir D:\m
```

**4. Модель (VLM): сканы PDF и описания картинок**

```powershell
curl.exe http://localhost:8080/v1/models                  # сервер модели жив?
$env:VISION_API_BASE_URL = "http://localhost:8080"        # на ПК с моделью
# $env:VISION_API_BASE_URL = "http://192.168.4.101:8080"  # с другой машины в LAN
uv run ingest-parse scan.pdf -o out\scan.md --vision      # сканы → текст, картинки → описания
uv run ingest-parse scan.pdf -o out\scan.md --vision-force  # мимо кэша .vision-cache
```

**5. Вся папка**

```powershell
Get-ChildItem examples\input -File | Where-Object Name -notlike ".*" | ForEach-Object {
    uv run ingest-parse $_.FullName -o "out\$($_.Name).md"            # добавьте --vision для сканов
}
```

`examples\output` — эталоны для тестов; пишите в `out\`, чтобы их не перезаписать.

**6. Проверить скан: наш пайплайн и «чистый» Docling/Heron**

```powershell
# сделать скан из любого PDF: первые 3 страницы → картинки без текстового слоя
@'
import pypdfium2 as pdfium
pdf = pdfium.PdfDocument("report.pdf")
pages = [pdf[i].render(scale=150/72).to_pil().convert("RGB") for i in range(min(3, len(pdf)))]
pages[0].save("scan.pdf", save_all=True, append_images=pages[1:], resolution=150)
'@ | uv run python -

uv run ingest-parse scan.pdf -o out\scan.md               # без модели: заглушки reason="vision_off" + scan-NNN.png
uv run ingest-parse scan.pdf -o out\scan_vision.md --vision   # через модель: текст страниц
uv run python scripts\heron_raw.py scan.pdf out\heron     # что нашёл Heron: регионы в консоли,
                                                          # out\heron\page-NNN-heron.png с рамками, docling.md

# Heron + модель (--scan-layout): Heron размечает скан, рисунки и таблицы уходят в модель вырезами
uv run ingest-parse scan.pdf -o out\scan_layout.md --scan-layout            # только Heron, без модели
uv run ingest-parse scan.pdf -o out\scan_layout.md --scan-layout --vision   # Heron + модель
```

Что смотреть после `--scan-layout` (папка `out\media\scan\debug\`):

- `scan-NNN-heron.png` — страница с рамками: синие — рисунки, зелёные — таблицы, красные — текст, серые — колонтитулы;
- `scan-NNN-heron.json` — список областей: метка, уверенность, рамка, что с ней сделали (`use`);
- `scan-NNN-masked.png` — что увидела модель: рисунки и таблицы закрашены, вместо них `[FIGURE K]` / `[TABLE K]`.

Перед моделью скан готовится (выключить: `--no-preprocess`):

- рендер в родном dpi скана, но не меньше 300;
- лист, лежащий боком, ставится вертикально, наклон выпрямляется;
- выравнивается свет, убирается шум, бумага становится белой;
- модель по миниатюре отвечает, на сколько градусов повернуть страницу (0/90/180/270), без tesseract;
- `scan-NNN.png` — уже выровненная страница.

Длинные вещи режутся, чтобы модель не падала по таймауту:

- таблица любого размера (хоть 80 % листа) вырезается; больше 10 строк — куски по 10 строк,
  у каждого сверху шапка таблицы (`scan-NNN-table-K-J.png`); в Markdown куски склеены в одну таблицу;
- высокая страница читается полосами (`часть 1 из 2` …), разрез — по пустому месту между блоками.

В `out\media\scan\` лежат вырезы `scan-NNN-fig-K.png` и `scan-NNN-table-K.png`.
В Markdown таблица со скана встаёт на место `[TABLE K]`, рисунок — на место `[FIGURE K]`
(описание модели + картинка); блок страницы помечен `layout="heron"`.

Обычный Docling на скане оставляет только регионы-картинки (`<!-- image -->`): он выбрасывает области
без текстового слоя. С `--scan-layout` мы включаем `keep_empty_clusters`, и Heron отдаёт всю раскладку.

**7. Накладные ТН-2 / ТТН-1 (Беларусь): `ingest-ttn`**

Отдельная команда для сканов накладных: на выходе поля в JSON и Markdown для проверки глазами.

```powershell
uv sync                                                    # после git pull: появится команда ingest-ttn
$env:VISION_API_BASE_URL = "http://localhost:8080"
$env:INGEST_VISION_MAX_LONG_EDGE = "2560"                  # таблица ТТН широкая: меньше ужатия; сервер должен принять

uv run ingest-ttn ttn.pdf -o out\ttn                       # один файл
uv run ingest-ttn D:\scans\ttn -o out\ttn                  # вся папка (рекурсивно)
uv run ingest-ttn ttn.pdf -o out\ttn --no-vision           # без модели: только предобработка и зоны
uv run ingest-ttn ttn.pdf -o out\ttn --vision-force        # переспросить модель мимо кэша
```

В консоли по каждому файлу: `OK` (все проверки сошлись) или `CHECK: N errors`.

| Файл | Что внутри |
|---|---|
| `out\ttn\<имя>.ttn.md` | реквизиты, товарный раздел (✓/✗ по строкам), итоги, список проверок, картинки страниц |
| `out\ttn\<имя>.ttn.json` | то же в JSON: `status` (`ok` / `check` / `no_vision`), `waybill`, `issues`, `repaired` |
| `out\ttn\<имя>\page-NN-prepared.png` | страница после предобработки |
| `out\ttn\<имя>\page-NN-zones.png` | зоны: синяя — шапка, зелёная — таблица (линии строк), оранжевая — низ, красная — конец шапки таблицы |
| `out\ttn\<имя>\page-NN-table-K.png` | куски таблицы, ровно то, что видела модель |

Как работает:

1. Страница рендерится в родном разрешении скана (300–450 dpi). Затем: лист на боку → поворот,
   наклон → выравнивание, тени и жёлтая бумага → белый фон, шум → убран; «вверх ногами» определяет модель.
2. Heron находит таблицу (без него — по линиям бланка). Строки таблицы ищутся по линиям. Таблица режется
   на куски по 10 строк, и к каждому куску сверху приклеивается шапка таблицы.
3. Модель читает шапку накладной, каждый кусок таблицы и низ (суммы прописью, подписи). Ответ — JSON.
4. Проверки: УНП 9 цифр, серия 2 буквы, номер 7 цифр, дата; количество × цена = стоимость, НДС,
   стоимость с НДС; сумма строк = «Итого»; «Итого» = сумма прописью.
5. Строка не сошлась → модель перечитывает только её (вырез с шапкой таблицы, потом крупнее), до 2 раз.
   Принимается только ответ, который проходит проверку. Так же для итогов и реквизитов.

Ответы модели кэшируются в `out\ttn\<имя>\.vision-cache`, повторный прогон бесплатный.
Модель здесь вызывается с `temperature=0` и таймаутом не меньше 180 с.

**8. Тесты** (нужны `tests/` и `examples/` из zip; в GitHub их нет)

```powershell
uv run pytest -q
uv run pytest -q tests\test_pdf.py                        # только PDF
uv run pytest -q tests\test_ttn.py                        # только накладные
```

**9. Если что-то не так**

| Симптом | Что сделать |
|---|---|
| `LibreOfficeNotFoundError` | `$env:LIBREOFFICE_PATH = "D:\Apps\LibreOffice\program\soffice.exe"` |
| ошибка загрузки с `huggingface.co` | скопировать кэш моделей (блок 2) |
| `No module named cv2` | `uv sync --reinstall-package opencv-python-headless` |
| `error: Vision API is not configured`, код 2 | задать `VISION_API_BASE_URL` (блок 4) |
| `reason="vision_error"` в блоке скана | `curl.exe http://localhost:8080/v1/models`, перезапустить с `--vision` |
| кракозябры в консоли | писать в файл через `-o`, stdout всегда UTF-8 |
| `ingest-ttn`: `CHECK` на хорошем скане | открыть `page-NN-zones.png`: найдена ли таблица и строки; затем `.ttn.md` → «Проверки» |
| `ingest-ttn`: HTTP 400/413 от модели | сервер не принимает большие картинки: `$env:INGEST_VISION_MAX_LONG_EDGE = "2048"` |
| `ingest-ttn`: таймауты | `$env:INGEST_VISION_TIMEOUT_S = "300"` |

## Не делает (осознанно)

- OCR (Tesseract, RapidOCR, `do_ocr` Docling) — сканы читает только VLM; ColPali (поиск по картинкам страниц —
  отдельная задача); PyMuPDF; Marker / MinerU; рукопись;
- схемы → текст/Mermaid (только картинка страницы + опционально описание VLM);
- сноски, колонтитулы;
- номера списков, набранные полями `SEQ` / `LISTNUM`, и нумерация внутри надписей схем;
- `.odt`; особые таблицы («Итого», «Продолжение таблицы», формы с флажками);
- JSON-вывод — результат только Markdown-строка.

## Примеры

`examples/input/` и `examples/output/` в репозитории пустые: положите свои документы в `input/` и запустите

```powershell
uv run ingest-parse examples\input\report.docx -o examples\output\report.md
```

Все команды для Windows — в разделе [Windows: все команды подряд](#windows-все-команды-подряд).

## Лицензии

Docling / docling-slim — MIT; модели Docling — Apache-2.0 / CDLA; torch — BSD-3; OpenCV — Apache-2.0;
pylatexenc — MIT; pypdfium2 — Apache-2.0 / BSD-3; Pillow — MIT-CMU;
LibreOffice — внешняя программа (MPL), в пакет не входит. PyMuPDF (AGPL) не используется и не должен добавляться.
