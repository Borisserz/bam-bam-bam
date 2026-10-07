"""Чистый Docling/Heron на PDF (OCR выкл., как у нас): что нашла модель раскладки + Markdown Docling + картинки с рамками."""

import sys
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from PIL import ImageDraw

src = Path(sys.argv[1])
out = Path(sys.argv[2] if len(sys.argv) > 2 else "out_heron")
out.mkdir(parents=True, exist_ok=True)

options = PdfPipelineOptions(do_ocr=False, generate_page_images=True, images_scale=2.0)
converter = DocumentConverter(
    allowed_formats=[InputFormat.PDF],
    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
)
result = converter.convert(str(src))

for page in result.pages:
    clusters = page.predictions.layout.clusters if page.predictions.layout else []
    print(f"\n=== page {page.page_no}: Heron found {len(clusters)} regions ===")
    image = page.image.copy() if page.image else None
    draw = ImageDraw.Draw(image) if image else None
    scale = image.width / page.size.width if image else 1
    for c in clusters:
        text = " ".join(cell.text for cell in c.cells)[:70]
        print(f"  {c.label.value:15} conf={c.confidence:.2f}  text={text!r}")
        if draw:
            b = c.bbox.to_top_left_origin(page_height=page.size.height)
            draw.rectangle([b.l * scale, b.t * scale, b.r * scale, b.b * scale], outline="red", width=3)
            draw.text((b.l * scale + 4, b.t * scale + 2), f"{c.label.value} {c.confidence:.2f}", fill="red")
    if image:
        image.save(out / f"page-{page.page_no:03d}-heron.png")

(out / "docling.md").write_text(result.document.export_to_markdown(), encoding="utf-8")
print(f"\nDocling Markdown: {out / 'docling.md'}; pages with boxes: {out}/page-NNN-heron.png")
