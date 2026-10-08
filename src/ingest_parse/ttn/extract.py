"""Оркестрация: страница → предобработка → зоны → VLM по кускам → склейка → проверки → ремонт."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import warnings
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter

from ingest_parse.ttn import prompts
from ingest_parse.ttn.preprocess import prepare
from ingest_parse.ttn.raster import rasterize
from ingest_parse.ttn.schema import (
    HEADER_FIELDS,
    PARTY_FIELDS,
    Item,
    Party,
    Totals,
    Waybill,
    item_from_dict,
    totals_from_dict,
    waybill_from_dict,
)
from ingest_parse.ttn.validate import Issue, check_item, check_requisites, check_totals, validate
from ingest_parse.ttn.zones import (
    Box,
    Zones,
    draw_layout,
    draw_zones,
    fields_image,
    find_zones,
    form_fields,
    row_image,
    stack,
    table_chunks,
)

_TOTAL_ROW = re.compile(r"^\s*(итого|всего)\b", re.I)
_REQUISITE_CODES = {"series", "number", "date", "shipper_unp", "consignee_unp"}
_FIELD_KEYS = {"shipper", "consignee", "carrier_customer", "basis", "loading_point", "unloading_point", "vehicle",
               "trailer", "waybill", "driver"}  # то, что есть в полосе полей формы; реквизиты бланка — нет


class TtnWarning(UserWarning):
    pass


@dataclass(frozen=True)
class TtnOptions:
    client: Any = None  # VisionClient; None — только предобработка и зоны (debug)
    force: bool = False
    max_long_edge: int = 2048
    repair_rounds: int = 2
    orientation: bool = True
    denoise: bool = True
    heron: bool = True
    debug: bool = False  # кроме разметки страницы сохранять предобработку, зоны и вырезы для VLM
    on_page: Any = None  # callable(PageReport) после каждой страницы — прогресс в консоли


@dataclass
class PageReport:
    number: int
    kind: str
    dpi: float
    native_dpi: float | None
    steps: list[str]
    zones: str
    rows: int
    chunks: int
    images: dict[str, str] = field(default_factory=dict)  # подпись → путь относительно out_dir
    doc: int | None = None  # номер накладной в файле (с 1); None — страница не из накладной
    markdown: str | None = None  # страница целиком (оборот, письмо): то, что прочитала модель
    error: str | None = None  # сбой обработки страницы
    copy_of: int | None = None  # те же строки товаров, что на этой странице накладной — в накладную не добавлены


@dataclass
class TtnDoc:
    """Одна накладная из пачки: лицевая страница и её продолжения."""

    waybill: Waybill = field(default_factory=Waybill)
    pages: list[int] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {"pages": self.pages, "status": "ok" if self.ok else "check", "waybill": self.waybill.to_dict(),
                "issues": [asdict(i) for i in self.issues], "repaired": self.repaired}


@dataclass
class TtnResult:
    source: Path
    docs: list[TtnDoc]
    pages: list[PageReport]
    vision: bool
    calls: int = 0
    errors: list[str] = field(default_factory=list)  # сбои VLM

    @property
    def waybill(self) -> Waybill:
        return self.docs[0].waybill if self.docs else Waybill()

    @property
    def issues(self) -> list[Issue]:
        return [i for d in self.docs for i in d.issues]

    @property
    def repaired(self) -> list[str]:
        return [r for d in self.docs for r in d.repaired]

    @property
    def ok(self) -> bool:
        return (self.vision and all(d.ok for d in self.docs) and not self.errors
                and not any(p.error for p in self.pages))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "status": "ok" if self.ok else ("no_vision" if not self.vision else "check"),
            "documents": [d.to_dict() for d in self.docs],
            "vision_errors": self.errors,
            "vision_calls": self.calls,
            "pages": [asdict(p) for p in self.pages],
        }


@dataclass
class _Context:
    image: Image.Image
    zones: Zones
    text: str


@dataclass
class _RowRef:
    page: int
    rows: list[tuple[int, int]]  # полосы строки на странице; пусто — использовать кусок целиком
    chunk: Image.Image


class _Asker:
    """VLM с кэшем: вырез — в work/<имя>.png (без debug — во временную папку), ответ — в work/.vision-cache."""

    def __init__(self, options: TtnOptions, work: Path) -> None:
        from ingest_parse.vision.cache import VisionCache

        self.options = options
        self._tmp = None if options.debug else tempfile.TemporaryDirectory(prefix="ingest-ttn-")
        self.work = work if self._tmp is None else Path(self._tmp.name)
        self.cache = VisionCache(work / ".vision-cache")
        self.calls = 0
        self.errors: list[str] = []

    def _complete(self, path: Path, prompt: str, sha: str) -> str:
        from ingest_parse.vision.enrich import image_payload

        model = getattr(self.options.client, "model", None)
        mode = "ttn-" + hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
        answer = None if self.options.force else self.cache.get(sha, mode, model)
        if answer is None:
            self.calls += 1
            answer = self.options.client.complete(prompt, image_payload(path, max_long_edge=self.options.max_long_edge))
            self.cache.put(sha, mode, model, answer)
        return answer

    def text(self, image: Image.Image, prompt: str, name: str) -> str | None:
        """Ответ модели как есть (Markdown страницы), без разбора JSON."""
        from ingest_parse.vision import VisionError

        path = self.work / f"{name}.png"
        image.save(path)
        try:
            return self._complete(path, prompt, hashlib.sha256(path.read_bytes()).hexdigest())
        except VisionError as exc:
            self.errors.append(f"{name}: {exc}")
            return None

    def ask(self, image: Image.Image, prompt: str, name: str) -> dict[str, Any] | None:
        from ingest_parse.vision import VisionError

        path = self.work / f"{name}.png"
        image.save(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            data = prompts.parse_json(self._complete(path, prompt, sha))
            if data is None:
                data = prompts.parse_json(self._complete(path, prompt + "\nВерни ТОЛЬКО JSON-объект.", sha))
        except VisionError as exc:
            self.errors.append(f"{name}: {exc}")
            return None
        if data is None:
            self.errors.append(f"{name}: no JSON in answer")
        return data


def _orient(asker: _Asker, name: str):
    """VLM сравнивает страницу и её поворот на 180° (Heron для этого ненадёжен: разница уверенности ~0.05)."""
    from ingest_parse.ttn.preprocess import upright_pair

    def check(image: Image.Image) -> int:
        answer = asker.text(upright_pair(image), prompts.UPRIGHT, name)
        return prompts.parse_upright_answer(answer or "")

    return check


def ttn_layout(image: Image.Image, stamps: Any, heron: bool = True) -> list[tuple[str, float, Box]]:
    """Разметка как в pdf_scan: Heron (страница + половины), сетка линий бланка без печатей, слияние."""
    import numpy as np

    from ingest_parse.ttn.layout import fuse, heron_layout, ruled_tables
    from ingest_parse.ttn.preprocess import without_stamps

    gray = without_stamps(np.asarray(image.convert("L"), dtype=np.uint8), stamps)
    dets = heron_layout(image) if heron else []
    return [(d.role, d.score, d.box) for d in fuse(image.size, dets, [], ruled_tables(gray), gray)]


def _crop(image: Image.Image, box: Box) -> Image.Image:
    return image.crop(box)


def _merge_header(target: Waybill, data: dict[str, Any]) -> None:
    """Пустые поля target заполняются из data (страницы по порядку — первая непустая побеждает)."""
    parsed = waybill_from_dict(data)
    for name in HEADER_FIELDS:
        if name in PARTY_FIELDS:
            mine, theirs = getattr(target, name), getattr(parsed, name)
            for f in fields(Party):
                if getattr(mine, f.name) is None:
                    setattr(mine, f.name, getattr(theirs, f.name))
        elif getattr(target, name) is None:
            setattr(target, name, getattr(parsed, name))


def _page_kind(header: dict | None, default: str) -> str:
    words = str((header or {}).get("page_kind") or "").split()
    kind = words[0].strip(" —-|") if words else default
    return kind if kind in ("front", "continuation", "back", "other") else default


def _read_fields(
    asker: _Asker, image: Image.Image, regions: list[tuple[str, float, Box]], zones: Zones, page: int,
    waybill: Waybill, report: PageReport, work: Path, rel: Any,
) -> None:
    """Стороны не прочитаны по всей шапке — поля формы «метка → значение» стопкой, крупнее; заполняются только пустые."""
    strip = fields_image(image, form_fields(regions, zones.header, image.width))
    if strip is None:
        return
    name = f"page-{page:02d}-header-fields"
    data = asker.ask(strip, prompts.FIELDS.format(page=page), name)
    if asker.options.debug:
        report.images["поля формы"] = rel(work / f"{name}.png")
    if data:
        _merge_header(waybill, {k: v for k, v in data.items() if k in _FIELD_KEYS})


def _merge_totals(target: Totals, data: dict[str, Any] | None) -> None:
    if not isinstance(data, dict):
        return
    for f in fields(Totals):
        value = totals_from_dict(data).__dict__[f.name]
        if value is not None:
            setattr(target, f.name, value)


def _same(a: Item, b: Item) -> bool:
    return (a.n, a.name, a.cost, a.quantity) == (b.n, b.name, b.cost, b.quantity) and a.name is not None


@dataclass
class _Open:
    """Накладная, которую сейчас собираем: строки со ссылками на вырезы и данные низа страниц."""

    doc: TtnDoc
    refs: list[_RowRef] = field(default_factory=list)
    footer: dict[str, Any] = field(default_factory=dict)

    @property
    def started(self) -> bool:
        return bool(self.doc.waybill.number or self.doc.waybill.items)


def _fenced(text: str) -> str:
    return re.sub(r"^```[\w-]*\n|\n?```$", "", text.strip()).strip()


def extract_ttn(path: Path, out_dir: Path, options: TtnOptions) -> TtnResult:
    """PDF с одной или пачкой накладных → TtnResult: накладные по лицевым страницам, отчёт по каждой странице;
    картинки — в out_dir/<имя>/."""
    work = out_dir / path.stem
    work.mkdir(parents=True, exist_ok=True)
    asker = _Asker(options, work) if options.client is not None else None
    pages: list[PageReport] = []
    contexts: dict[int, _Context] = {}
    opened: list[_Open] = []

    def rel(p: Path) -> str:
        return p.relative_to(out_dir).as_posix()

    def process(page: Any, report: PageReport) -> None:
        n = page.number
        tag = f"page-{n:02d}"
        check = _orient(asker, f"{tag}-orientation") if options.orientation and asker is not None else None
        prep = prepare(page.image, page.dpi, orient=check, denoise=options.denoise)
        image = prep.image
        report.dpi, report.steps = prep.dpi, prep.steps
        try:
            regions = ttn_layout(image, prep.stamps, heron=options.heron)
        except Exception as exc:  # noqa: BLE001 — модель раскладки не скачана / сбой Docling
            warnings.warn(f"{path.name} page {n}: heron failed ({exc}); using ruled lines", TtnWarning, stacklevel=2)
            regions = ttn_layout(image, prep.stamps, heron=False)
        draw_layout(image, regions).save(work / f"{tag}-layout.png")
        report.images = {"разметка страницы": rel(work / f"{tag}-layout.png")}
        zones = find_zones(image, regions)
        contexts[n] = _Context(image, zones, page.text)
        chunks = table_chunks(image, zones)
        report.zones, report.rows, report.chunks = zones.source, len(zones.rows), len(chunks)
        if options.debug:
            image.save(work / f"{tag}-prepared.png")
            draw_zones(image, zones).save(work / f"{tag}-zones.png")
            report.images |= {"страница после предобработки": rel(work / f"{tag}-prepared.png"),
                              "зоны и строки": rel(work / f"{tag}-zones.png")}
        if asker is None:
            if not options.debug:
                return
            for k, chunk in enumerate(chunks, 1):
                chunk.image.save(work / f"{tag}-table-{k}.png")
            _crop(image, zones.header).save(work / f"{tag}-header.png")
            strip = fields_image(image, form_fields(regions, zones.header, image.width))
            if strip is not None:
                strip.save(work / f"{tag}-header-fields.png")
            return

        header = asker.ask(_crop(image, zones.header), prompts.with_text_layer(prompts.HEADER.format(page=n), page.text), f"{tag}-header")
        report.kind = _page_kind(header, "continuation" if opened else "front")
        if report.kind == "back" or (report.kind == "other" and zones.source != "layout"):
            # оборот, письмо, акт — не товарный раздел: страница целиком в Markdown
            answer = asker.text(image, prompts.with_text_layer(prompts.PAGE.format(page=n), page.text), f"{tag}-page")
            report.markdown = _fenced(answer) if answer else None
            report.doc = len(opened) if report.kind == "back" and opened else None
            return
        if not opened or (report.kind == "front" and opened[-1].started):
            opened.append(_Open(TtnDoc()))
        cur = opened[-1]
        waybill = cur.doc.waybill
        cur.doc.pages.append(n)
        report.doc = len(opened)
        if header and report.kind == "front":
            _merge_header(waybill, header)
            if not (waybill.shipper.name and waybill.consignee.name):
                _read_fields(asker, image, regions, zones, n, waybill, report, work, rel)
        found: list[tuple[Item, _RowRef]] = []
        for k, chunk in enumerate(chunks, 1):
            prompt = prompts.ITEMS.format(page=n) + prompts.grid_hint(len(zones.columns) - 1)
            data = asker.ask(chunk.image, prompts.with_text_layer(prompt, page.text), f"{tag}-table-{k}")
            rows = [r for r in (data or {}).get("items") or [] if isinstance(r, dict)]
            for j, row in enumerate(rows):
                item = item_from_dict(row, page=n)
                if item.name and _TOTAL_ROW.match(item.name):
                    _merge_totals(waybill.totals, row)
                    continue
                if found and _same(found[-1][0], item):
                    continue  # перекрытие кусков без линий
                band = chunk.rows[j : j + 1] if len(rows) == len(chunk.rows) else []
                found.append((item, _RowRef(n, band, chunk.image)))
            _merge_totals(waybill.totals, (data or {}).get("totals"))
        key = [(i.name, i.quantity, i.cost) for i, _ in found]
        report.copy_of = next((q for q in cur.doc.pages[:-1]
                               if key and key == [(i.name, i.quantity, i.cost) for i in waybill.items if i.page == q]), None)
        if report.copy_of is None:  # вторая копия той же страницы в пачке — строки уже есть
            waybill.items += [i for i, _ in found]
            cur.refs += [r for _, r in found]
        if zones.footer:
            data = asker.ask(_crop(image, zones.footer), prompts.FOOTER.format(page=n), f"{tag}-footer") or {}
            _merge_totals(waybill.totals, data.get("totals"))
            cur.footer.update({k: v for k, v in data.items() if v not in (None, "") and k != "totals"})

    for page in rasterize(path):
        report = PageReport(page.number, "other", page.dpi, page.native_dpi, [], "—", 0, 0)
        pages.append(report)
        try:
            process(page, report)
        except Exception as exc:  # noqa: BLE001 — одна битая страница не роняет пачку
            report.error = f"{type(exc).__name__}: {exc}"
            warnings.warn(f"{path.name} page {page.number}: {report.error}", TtnWarning, stacklevel=2)
        if options.on_page is not None:
            options.on_page(report)

    result = TtnResult(path, [o.doc for o in opened], pages, asker is not None)
    kinds = {p.number: p.kind for p in pages}
    for k, cur in enumerate(opened, 1):
        waybill = cur.doc.waybill
        for key in ("vat_words", "cost_with_vat_words", "mass_words", "places_words"):
            if cur.footer.get(key):
                setattr(waybill.totals, key, str(cur.footer[key]).strip())
        for key in ("released_by", "handed_by", "accepted_by", "power_of_attorney", "seal"):
            if cur.footer.get(key) and getattr(waybill, key) is None:
                setattr(waybill, key, " ".join(str(cur.footer[key]).split()))
        if asker is not None:
            front = cur.doc.pages[0] if kinds[cur.doc.pages[0]] == "front" else None
            _repair(cur.doc, cur.refs, contexts, asker, options, front, f"doc-{k:02d}")
            cur.doc.issues = validate(waybill)
    if asker is not None:
        result.calls, result.errors = asker.calls, asker.errors
    return result


def _item_json(item: Item) -> str:
    data = {k: v for k, v in asdict(item).items() if k != "page"}
    return json.dumps(data, ensure_ascii=False)


def _repair(
    doc: TtnDoc, refs: list[_RowRef], contexts: dict[int, _Context], asker: _Asker, options: TtnOptions,
    front: int | None, tag: str,
) -> None:
    w = doc.waybill
    for round_ in range(1, options.repair_rounds + 1):
        bad = [(i, check_item(item, i)) for i, item in enumerate(w.items)]
        bad = [(i, issues) for i, issues in bad if issues]
        if not bad:
            break
        for i, issues in bad:
            ref, item = refs[i], w.items[i]
            ctx = contexts[ref.page]
            image = row_image(ctx.image, ctx.zones, ref.rows) if ref.rows else ref.chunk
            if round_ > 1:  # тот же вырез повторно бесполезен (кэш) — крупнее и резче
                image = image.resize((int(image.width * 1.6), int(image.height * 1.6)), Image.LANCZOS)
                image = image.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
            prompt = prompts.ROW_REPAIR.format(previous=_item_json(item), problem="; ".join(x.message for x in issues))
            data = asker.ask(image, prompt, f"page-{ref.page:02d}-row-{i + 1}-repair-{round_}")
            if not data:
                continue
            fixed = item_from_dict(data, page=item.page)
            if not check_item(fixed, i):
                w.items[i] = fixed
                doc.repaired.append(f"строка {item.n or i + 1}: {_item_json(item)} → {_item_json(fixed)}")

    totals_issues = check_totals(w)
    if totals_issues and refs:
        last = refs[-1]
        ctx = contexts[last.page]
        parts = [last.chunk] + ([_crop(ctx.image, ctx.zones.footer)] if ctx.zones.footer else [])
        previous = json.dumps(asdict(w.totals), ensure_ascii=False)
        for round_ in range(1, options.repair_rounds + 1):
            prompt = prompts.TOTALS_REPAIR.format(previous=previous, problem="; ".join(x.message for x in totals_issues))
            data = asker.ask(stack(parts), prompt, f"{tag}-totals-repair-{round_}")
            if not data:
                break
            trial = Totals(**asdict(w.totals))
            _merge_totals(trial, data.get("totals"))
            for key in ("vat_words", "cost_with_vat_words"):
                if data.get(key):
                    setattr(trial, key, str(data[key]).strip())
            candidate = Waybill(items=w.items, totals=trial)
            if len(check_totals(candidate)) < len(totals_issues):
                doc.repaired.append(f"итоги: {previous} → {json.dumps(asdict(trial), ensure_ascii=False)}")
                w.totals = trial
                totals_issues = check_totals(w)
                if not totals_issues:
                    break

    bad_codes = {i.code for i in check_requisites(w)} & _REQUISITE_CODES
    if bad_codes and front is not None:
        _repair_requisites(doc, bad_codes, contexts[front], asker, tag)


def _repair_requisites(doc: TtnDoc, codes: set[str], ctx: _Context, asker: _Asker, tag: str) -> None:
    """Верх страницы — две увеличенные половины; поле берётся, только если новое значение проходит проверку."""
    w = doc.waybill
    width, height = ctx.image.size
    top = ctx.zones.header[3] if ctx.zones.header[3] < 0.6 * height else int(0.4 * height)
    halves = [(0, 0, int(0.55 * width), top), (int(0.45 * width), 0, width, top)]
    names = {"series": "серия", "number": "номер", "date": "дата", "shipper_unp": "УНП грузоотправителя",
             "consignee_unp": "УНП грузополучателя"}
    problem = "; ".join(i.message for i in check_requisites(w) if i.code in codes)
    prompt = prompts.REQUISITES_REPAIR.format(fields=", ".join(names[c] for c in sorted(codes)), problem=problem)
    for k, box in enumerate(halves, 1):
        data = asker.ask(ctx.image.crop(box), prompt, f"{tag}-requisites-{k}")
        if not data:
            continue
        found = waybill_from_dict(data)
        for code in sorted(codes):
            trial = Waybill(**{f.name: getattr(w, f.name) for f in fields(Waybill)})
            trial.shipper, trial.consignee = Party(**asdict(w.shipper)), Party(**asdict(w.consignee))
            if code.endswith("_unp"):
                party = code.removesuffix("_unp")
                value = getattr(found, party).unp
                getattr(trial, party).unp = value
            else:
                value = getattr(found, code)
                setattr(trial, code, value)
            if value and code not in {i.code for i in check_requisites(trial)}:
                doc.repaired.append(f"{names[code]}: {value}")
                if code.endswith("_unp"):
                    getattr(w, code.removesuffix("_unp")).unp = value
                else:
                    setattr(w, code, value)
        codes = {i.code for i in check_requisites(w)} & codes
        if not codes:
            break
