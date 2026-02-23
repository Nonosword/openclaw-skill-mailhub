from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import pdfplumber


@dataclass
class PDFExtract:
    text: str
    tables: List[List[List[str]]]


def extract_pdf(path: str) -> PDFExtract:
    text_parts: List[str] = []
    tables: List[List[List[str]]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                text_parts.append(t)
            try:
                tbls = page.extract_tables() or []
                for tbl in tbls:
                    tables.append(tbl)
            except Exception:
                pass
    return PDFExtract(text="\n\n".join(text_parts), tables=tables)