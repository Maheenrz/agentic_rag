"""
parsers.py
-----------
One parser per file type. Each returns a list of langchain Documents, one
per "location" the answer can be cited to, with metadata:

    source   file name
    page     running number (1, 2, 3...) -- real page for PDF, section/sheet-block
             index for the others. Keeps the rest of the code (cache, sources,
             eval) working unchanged.
    section  human-readable location: heading text, "Sheet: Budget (rows 1-25)".
             Empty for PDF/TXT. Shown in citations when present.

parse_file() also returns `notes` (things we did to make the file safe, e.g.
"removed 3 hidden HTML elements") so ingest can report them.

Safety checks done BEFORE parsing: extension allow-list, magic-byte sniffing
(a .pdf must really start with %PDF), zip-bomb guard for docx/xlsx, and a cap on
extracted text. All failures are ValueError with a message safe to show the user.
"""

import io
import os
import re
import zipfile

from langchain_core.documents import Document

from app.config import MAX_EXTRACTED_CHARS, MAX_XLSX_ROWS_PER_SHEET, MAX_ZIP_UNCOMPRESSED_MB, logger

SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".md", ".docx", ".html", ".htm", ".xlsx")
_XLSX_ROWS_PER_BLOCK = 25


# ---------------------------------------------------------------- validation
def _check_magic(ext: str, data: bytes) -> None:
    if ext == ".pdf" and not data.lstrip()[:5].startswith(b"%PDF"):
        raise ValueError("File is named .pdf but is not a PDF")
    if ext in (".docx", ".xlsx") and not data.startswith(b"PK"):
        raise ValueError(f"File is named {ext} but is not a valid Office file")
    if ext in (".txt", ".md", ".html", ".htm") and b"\x00" in data[:4096]:
        raise ValueError(f"File is named {ext} but looks like binary data")


def _check_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Corrupt or invalid Office file")
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_ZIP_UNCOMPRESSED_MB * 1024 * 1024:
        raise ValueError("File expands to an unreasonable size (possible zip bomb)")
    if any(i.filename.startswith("/") or ".." in i.filename.split("/") for i in zf.infolist()):
        raise ValueError("File contains unsafe internal paths")
    return zf


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeError:
            continue
    return data.decode("latin-1")


def _doc(text: str, source: str, page: int, section: str = "", **extra) -> Document:
    return Document(page_content=text, metadata={"source": source, "page": page, "section": section, **extra})


# ---------------------------------------------------------------- PDF
def _parse_pdf(name: str, data: bytes) -> tuple[list[Document], list[str]]:
    import fitz
    from pypdf import PdfReader
    notes: list[str] = []
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
        if pdf.needs_pass:
            pdf.close()
            raise ValueError("PDF is password-protected")
        pages = [(i, p.get_text() or "") for i, p in enumerate(pdf, start=1)]
        pdf.close()
    except ValueError:
        raise
    except Exception:
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(i, p.extract_text() or "") for i, p in enumerate(reader.pages, start=1)]
        except Exception:
            raise ValueError("Could not read this PDF (corrupt?)")
    docs = [_doc(t, name, i) for i, t in pages if t.strip()]
    if len(docs) < len(pages):
        notes.append(f"{len(pages) - len(docs)} of {len(pages)} pages had no extractable text (scanned? OCR is not supported yet)")
    return docs, notes


# ---------------------------------------------------------------- TXT / MD
def _parse_txt(name: str, data: bytes) -> tuple[list[Document], list[str]]:
    text = _decode(data)
    return ([_doc(text, name, 1)] if text.strip() else []), []


def _parse_md(name: str, data: bytes) -> tuple[list[Document], list[str]]:
    """Splits on # headings (ignoring '#' lines inside ``` code fences)."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False
    for line in _decode(data).splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            sections.append((m.group(2), [line]))
        else:
            sections[-1][1].append(line)
    docs, n = [], 0
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if body:
            n += 1
            docs.append(_doc(body, name, n, heading))
    return docs, []


# ---------------------------------------------------------------- DOCX
def _parse_docx(name: str, data: bytes) -> tuple[list[Document], list[str]]:
    _check_zip(data)
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception:
        raise ValueError("Could not read this Word file (corrupt or password-protected?)")

    sections: list[tuple[str, list[str]]] = [("", [])]
    for child in document.element.body.iterchildren():       # keeps paragraphs and tables in document order
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or "") if para.style is not None else ""
            if style.lower().startswith("heading") or style.lower() == "title":
                sections.append((text, [text]))
            else:
                sections[-1][1].append(text)
        elif tag == "tbl":
            table = Table(child, document)
            for row in table.rows:
                cells = []
                for cell in row.cells:
                    t = cell.text.strip().replace("\n", " ")
                    if t and (not cells or cells[-1] != t):      # merged cells repeat; drop repeats
                        cells.append(t)
                if cells:
                    sections[-1][1].append(" | ".join(cells))
    docs, n = [], 0
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if body:
            n += 1
            docs.append(_doc(body, name, n, heading))
    return docs, []


# ---------------------------------------------------------------- HTML
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0(\.0+)?\b|"
                           r"(left|top|text-indent)\s*:\s*-\d{3,}", re.I)


def _parse_html(name: str, data: bytes) -> tuple[list[Document], list[str]]:
    from bs4 import BeautifulSoup, Comment
    soup = BeautifulSoup(_decode(data), "html.parser")
    notes: list[str] = []

    for tag in soup(["script", "style", "noscript", "template", "iframe", "object", "embed", "svg"]):
        tag.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()                                            # comments are a classic hiding place
    # Hidden-content removal: text a human can't see but an LLM would read is the
    # standard way to poison a web page / doc. Strip it and say so.
    hidden = [t for t in soup.find_all(True) if t.attrs is not None and (
        t.has_attr("hidden") or t.get("aria-hidden") == "true" or _HIDDEN_STYLE.search(t.get("style", "") or ""))]
    removed = 0
    for tag in hidden:
        if tag.decomposed if hasattr(tag, "decomposed") else False:
            continue
        if tag.get_text(strip=True):
            removed += 1
        tag.decompose()
    if removed:
        notes.append(f"removed {removed} hidden HTML element(s) containing text")

    sections: list[tuple[str, list[str]]] = [("", [])]
    root = soup.body or soup
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "tr", "pre", "blockquote", "td", "th"]):
        if el.name in ("td", "th") and el.find_parent("tr"):
            continue                                            # handled via its <tr>
        if el.name in ("p", "li", "blockquote") and el.find_parent(["li", "blockquote", "tr"]):
            continue
        if el.name == "tr":
            cells = [c.get_text(" ", strip=True) for c in el.find_all(["td", "th"])]
            text = " | ".join(c for c in cells if c)
        else:
            text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in ("h1", "h2", "h3"):
            sections.append((text, [text]))
        else:
            sections[-1][1].append(text)
    docs, n = [], 0
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if body:
            n += 1
            docs.append(_doc(body, name, n, heading))
    if not docs:                                                # fallback for odd markup
        text = root.get_text("\n", strip=True)
        if text:
            docs.append(_doc(text, name, 1))
    return docs, notes


# ---------------------------------------------------------------- XLSX
def _parse_xlsx(name: str, data: bytes) -> tuple[list[Document], list[str]]:
    _check_zip(data)
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)   # cached VALUES, never formulas
    except Exception:
        raise ValueError("Could not read this Excel file (corrupt or password-protected?)")
    notes: list[str] = []
    docs: list[Document] = []
    n = 0
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            notes.append(f"skipped hidden sheet '{ws.title}'")
            continue
        header: list[str] = []
        block: list[str] = []
        block_start = 1
        rows_seen = 0

        def flush(end_row: int) -> None:
            nonlocal block, block_start, n
            if block:
                n += 1
                docs.append(_doc("\n".join(block), name, n, f"Sheet: {ws.title} (rows {block_start}-{end_row})"))
            block = []

        for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if row_idx > MAX_XLSX_ROWS_PER_SHEET:
                notes.append(f"sheet '{ws.title}' truncated at {MAX_XLSX_ROWS_PER_SHEET} rows")
                break
            cells = ["" if v is None else str(v).strip() for v in row]
            if not any(cells):
                continue
            rows_seen += 1
            if not header:
                header = [c or f"col{i + 1}" for i, c in enumerate(cells)]
                block_start = row_idx
                block.append("Columns: " + " | ".join(header))
                continue
            line = "; ".join(f"{h}: {c}" for h, c in zip(header, cells) if c)
            if line:
                block.append(line)
            if len(block) >= _XLSX_ROWS_PER_BLOCK:
                flush(row_idx)
                block_start = row_idx + 1
        flush(row_idx if rows_seen else 1)
    wb.close()
    return docs, notes


_PARSERS = {
    ".pdf": _parse_pdf, ".txt": _parse_txt, ".md": _parse_md, ".docx": _parse_docx,
    ".html": _parse_html, ".htm": _parse_html, ".xlsx": _parse_xlsx,
}


def parse_file(file_name: str, data: bytes) -> tuple[list[Document], list[str]]:
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in _PARSERS:
        logger.warning("Rejected unsupported file type: %s", ext)
        raise ValueError(f"Unsupported file type: {ext or '(none)'}. Supported: {', '.join(SUPPORTED_EXTENSIONS)}")
    if not data:
        raise ValueError("File is empty")
    _check_magic(ext, data)
    docs, notes = _PARSERS[ext](file_name, data)
    if not docs:
        raise ValueError("No extractable text found in this file")
    total = sum(len(d.page_content) for d in docs)
    if total > MAX_EXTRACTED_CHARS:
        raise ValueError(f"Document has too much text ({total:,} characters; limit {MAX_EXTRACTED_CHARS:,})")
    logger.info("Parsed %s: %d section(s), %d chars", file_name, len(docs), total)
    return docs, notes