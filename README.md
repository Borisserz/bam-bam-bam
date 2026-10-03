# ingest-parse

`.txt` / `.doc` / `.docx` / `.docm` / `.rtf` / `.pdf` → **Markdown-строка** для RAG (chunking, embeddings,
цитирование) + папка `media/` с картинками. DOCX и PDF разбирает [Docling](https://github.com/docling-project/docling)
(`docling-slim`, без OCR); `.doc` и `.rtf` сначала конвертируются LibreOffice в `.docx`. PDF — только
**born-digital** (с текстовым слоем), Markdown того же вида, что из `.docx`.
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
md = parse_document("report.pdf", media_dir="out/media/report")    # сканы → PdfPageWarning + заглушки
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
| `.pdf` | born-digital: тот же Markdown, что из `.docx` (см. [PDF](#pdf)); страницы-сканы — комментарий-заглушка |
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

Только **born-digital** PDF — экспорт из Word / LibreOffice / LaTeX, где текст выделяется мышью. Docling
(модели раскладки и таблиц, OCR выключен) → тот же обход, что для `.docx`: заголовки, списки, GFM-таблицы, подписи,
картинки `img-NNN.png`, `--vision`.

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
| full_scan — картинка без текста или мусорный слой | `<!-- page N: scanned / no usable text layer; skipped -->` | `warning: pdf page N classified as full_scan; placeholder emitted` |
| blank — пустая | ничего | `warning: pdf page N classified as blank; skipped` |

PDF целиком из сканов / пустых страниц → Markdown из одних заглушек (не пустая строка), код выхода `0`.
Запароленный PDF → `ValueError` («password-protected»).

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
- картинки внутри ячеек таблицы не выводятся; векторные схемы без растра — только подпись, страница целиком
  из векторной графики без текста → `warning: pdf page N has only vector graphics …`.

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

- вид картинки (`kind`): `logo` (мелкая), `photo` (JPEG) — только описание; `diagram` (`page-*`, «схема»),
  `table_scan` («Таблица»), `chart` («График»), `equation_img` («Формула», OLE), `unknown` — описание + текст + анализ;
- ответы кэшируются в `<media>/.vision-cache/<sha256>-<режим>.json`: повторный разбор того же документа API не
  дёргает; `--vision-force` — спросить заново;
- уже описанная картинка в том же `.md` не описывается повторно; одна и та же картинка несколько раз — один блок
  над первой ссылкой; картинка в ячейке таблицы — блок над таблицей;
- ошибка API у картинки → блок `status="error"` с причиной, остальные картинки обрабатываются, CLI завершается
  с кодом 0; при следующем `--vision` такие блоки переспрашиваются;
- без `VISION_API_BASE_URL` → `--vision` сразу завершается с кодом 2, разбор не начинается.

## Не делает (осознанно)

- сканы на входе: OCR (Tesseract, RapidOCR и т. п.), страницы-картинки целиком, ColPali; PyMuPDF; Marker / MinerU;
- схемы → текст/Mermaid (только картинка страницы + опционально описание VLM);
- сноски, колонтитулы;
- номера списков, набранные полями `SEQ` / `LISTNUM`, и нумерация внутри надписей схем;
- `.odt`; особые таблицы («Итого», «Продолжение таблицы», формы с флажками);
- JSON-вывод — результат только Markdown-строка.

## Примеры

`examples/input/` и `examples/output/` в репозитории пустые: положите свои документы в `input/` и запустите

```powershell
python -m uv run ingest-parse examples\input\report.docx -o examples\output\report.md
```

## Лицензии

Docling / docling-slim — MIT; модели Docling — Apache-2.0 / CDLA; torch — BSD-3; OpenCV — Apache-2.0;
pylatexenc — MIT; pypdfium2 — Apache-2.0 / BSD-3; Pillow — MIT-CMU;
LibreOffice — внешняя программа (MPL), в пакет не входит. PyMuPDF (AGPL) не используется и не должен добавляться.
