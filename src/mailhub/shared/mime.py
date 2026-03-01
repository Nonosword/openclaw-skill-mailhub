from __future__ import annotations

import hashlib
from dataclasses import dataclass
from email.message import Message
from typing import List, Optional, Tuple

@dataclass
class ParsedEmail:
    body_text: Optional[str]
    body_html: Optional[str]
    attachments: List[dict]  # {filename, content_type, bytes}

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def parse_mime(msg: Message) -> ParsedEmail:
    body_text = None
    body_html = None
    attachments: List[dict] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            filename = part.get_filename()

            if filename or "attachment" in disp:
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    {"filename": filename or "attachment.bin", "content_type": ctype, "bytes": payload}
                )
                continue

            if ctype == "text/plain" and body_text is None:
                payload = part.get_payload(decode=True) or b""
                body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ctype == "text/html" and body_html is None:
                payload = part.get_payload(decode=True) or b""
                body_html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        ctype = (msg.get_content_type() or "").lower()
        payload = msg.get_payload(decode=True) or b""
        text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/html":
            body_html = text
        else:
            body_text = text

    return ParsedEmail(body_text=body_text, body_html=body_html, attachments=attachments)