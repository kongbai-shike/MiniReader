"""Build one real fixture for each of the 11 formats MiniReader can open.

Everything here is produced by the project's own test helpers, so the files are
the same ones the test suite already proves readable - the point of this script
is to hand them to the *packaged exe*, which the test suite never touches.

Output goes to _manual/pack_fixtures/ (gitignored via the usual _manual rule).
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "_manual" / "pack_fixtures"
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

# --- helpers borrowed from the test suite (imported, not copied) ------------
from tests.test_comic_ui import make_png  # noqa: E402
from tests.test_pdf import make_text_pdf, make_scan_pdf  # noqa: E402
from tests.test_rar import build_rar  # noqa: E402
from tests.test_text_formats import (  # noqa: E402
    DOCX_CORE,
    FB2_TEXT,
    make_docx_xml,
    make_html,
)
from tests.test_mobi import build_mobi, html_doc  # noqa: E402
from tests.test_epub import make_opf, item_xml, ref_xml, single_doc_opf  # noqa: E402

made: list[tuple[str, str, int]] = []


def note(kind: str, path: Path, detail: str = "") -> None:
    made.append((kind, str(path), path.stat().st_size))
    print(f"  {kind:<10} {path.name:<22} {path.stat().st_size:>9,} B  {detail}")


# --- 1. TXT ----------------------------------------------------------------
txt = OUT / "a.txt"
txt.write_text(
    "第一章 起点\n" + ("这是一段正文。" * 40) + "\n\n第二章 终点\n" + ("另一段正文。" * 40) + "\n",
    encoding="utf-8",
)
note("txt", txt, "两章")

# --- 2. EPUB (reflowable) --------------------------------------------------
epub = OUT / "a.epub"
with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("mimetype", "application/epub+zip")
    zf.writestr(
        "META-INF/container.xml",
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>',
    )
    zf.writestr(
        "OEBPS/content.opf",
        make_opf(
            title="流式 EPUB",
            author="测试作者",
            items=[item_xml("ch1", "text/ch1.xhtml"), item_xml("ch2", "text/ch2.xhtml")],
            spine=[ref_xml("ch1"), ref_xml("ch2")],
        ),
    )
    for i in (1, 2):
        zf.writestr(
            f"OEBPS/text/ch{i}.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>第 {i} 章</title></head><body>"
            f"<h1>第 {i} 章</h1><p>{'正文内容。' * 30}</p></body></html>",
        )
note("epub", epub, "流式，2 章")

# --- 3. EPUB fixed-layout --------------------------------------------------
fixed = OUT / "fixed.epub"
with zipfile.ZipFile(fixed, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("mimetype", "application/epub+zip")
    zf.writestr(
        "META-INF/container.xml",
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>',
    )
    zf.writestr(
        "OEBPS/content.opf",
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">'
        "<metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\">"
        "<dc:title>固定版式</dc:title><dc:creator>测试作者</dc:creator>"
        '<dc:identifier id="bookid">urn:uuid:fixed</dc:identifier>'
        '<meta property="rendition:layout">pre-paginated</meta>'
        '<meta property="rendition:spread">auto</meta></metadata>'
        '<manifest><item id="p1" href="p1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="p2" href="p2.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="p1"/><itemref idref="p2"/></spine></package>',
    )
    for i, (w, h) in enumerate(((800, 1200), (1600, 1200)), start=1):
        zf.writestr(f"OEBPS/p{i}.png", make_png(w, h))
        zf.writestr(
            f"OEBPS/p{i}.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>p{i}</title></head><body>"
            f'<div><img src="p{i}.png" alt=""/></div></body></html>',
        )
note("epub-fixed", fixed, "固定版式，2 页")

# --- 4. CBZ ----------------------------------------------------------------
cbz = OUT / "a.cbz"
with zipfile.ZipFile(cbz, "w", zipfile.ZIP_STORED) as zf:
    for i in range(1, 5):
        zf.writestr(f"p{i}.png", make_png(800, 1200, (30 + i * 20, 40, 200)))
note("cbz", cbz, "4 页")

# --- 5. PDF text -----------------------------------------------------------
pdf_text = OUT / "text.pdf"
make_text_pdf(pdf_text, pages=3)
note("pdf-text", pdf_text, "文字型，3 页")

# --- 6. PDF scan -----------------------------------------------------------
pdf_scan = OUT / "scan.pdf"
make_scan_pdf(pdf_scan, pages=3)
note("pdf-scan", pdf_scan, "扫描型，3 页")

# --- 7. MOBI ---------------------------------------------------------------
mobi = OUT / "a.mobi"
mobi.write_bytes(build_mobi(html_doc(), title="手搓 MOBI"))
note("mobi", mobi, "未加密")

# --- 8. FB2 ----------------------------------------------------------------
fb2 = OUT / "a.fb2"
fb2.write_text(FB2_TEXT, encoding="utf-8")
note("fb2", fb2, "2 章 + 注释 body")

# --- 9. DOCX ---------------------------------------------------------------
docx = OUT / "a.docx"
with zipfile.ZipFile(docx, "w") as zf:
    zf.writestr("word/document.xml", make_docx_xml(*[f"第 {i} 段。" for i in range(1, 6)]))
    zf.writestr("docProps/core.xml", DOCX_CORE)
note("docx", docx, "5 段")

# --- 10. HTML --------------------------------------------------------------
html = OUT / "a.html"
html.write_text(
    make_html(
        "<h1>第一章</h1><p>" + "正文内容。" * 40 + "</p>"
        "<h1>第二章</h1><p>" + "第二章的正文也在这里，写长一点免得被当成新章节。" * 20 + "</p>",
        title="网页标题",
    ),
    encoding="utf-8",
)
note("html", html, "2 章")

# --- 11. CBR (real RAR5, stored members - no external tool needed) ---------
cbr = OUT / "a.cbr"
build_rar(cbr, {f"p{i}.png": make_png(800, 1200, (20, 30 + i * 30, 180)) for i in range(1, 5)})
note("cbr", cbr, "真 RAR5，STORED，4 页")

# --- 12. CB7 ---------------------------------------------------------------
# 7z 的头是 LZMA 压的，纯 Python 造不出合法样本；本机也没有 7-Zip。
# 所以 CB7 这一格只能靠「格式表认得、打开时给出去装 7-Zip 的提示」来验，
# 这里放一个带真 7z 签名的壳，只为让 looks_like_cb7() 认它。
cb7 = OUT / "a.cb7"
cb7.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 96)
note("cb7", cb7, "只有签名（无真 7z 可造）")

print()
print(f"共 {len(made)} 个夹具，写到 {OUT}")
