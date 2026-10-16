from __future__ import annotations

import html
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def md_to_html(md: str) -> str:
    """Lightweight, safe Markdown to HTML converter for legal and documentation pages."""
    lines = md.splitlines()
    out: list[str] = []
    in_list = False
    list_type: str | None = None
    in_quote = False
    quote_lines: list[str] = []
    quote_type = "note"

    def flush_list():
        nonlocal in_list, list_type
        if in_list and list_type:
            out.append(f"</{list_type}>")
            in_list = False
            list_type = None

    def flush_quote():
        nonlocal in_quote, quote_lines, quote_type
        if in_quote:
            content = "<br>".join(quote_lines).strip()
            title = quote_type.upper()
            out.append(
                f'<div class="alert alert-{quote_type}">'
                f'<strong class="alert-title">{title}:</strong> {content}'
                f'</div>'
            )
            in_quote = False
            quote_lines = []
            quote_type = "note"

    def inline_fmt(s: str) -> str:
        s = html.escape(s)
        # bold: **text**
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        # italic: *text* (avoiding double asterisks)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        # inline code: `text`
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        # links: [text](url)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
        return s

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_list()
            flush_quote()
            continue

        # Horizontal rule
        if re.match(r"^---+$", line):
            flush_list()
            flush_quote()
            out.append('<hr class="doc-divider">')
            continue

        # Blockquote / GitHub alerts
        if line.startswith(">"):
            flush_list()
            q_content = line.lstrip("> ").strip()
            m_alert = re.match(r"^\[!(NOTE|IMPORTANT|CAUTION|WARNING|TIP)\]", q_content, re.IGNORECASE)
            if m_alert:
                flush_quote()
                in_quote = True
                quote_type = m_alert.group(1).lower()
                rem = q_content[m_alert.end():].strip()
                if rem:
                    quote_lines.append(inline_fmt(rem))
            else:
                if not in_quote:
                    in_quote = True
                    quote_type = "note"
                quote_lines.append(inline_fmt(q_content))
            continue
        else:
            flush_quote()

        # Headings (# H1, ## H2, ### H3, #### H4)
        m_h = re.match(r"^(#{1,4})\s+(.+)$", line)
        if m_h:
            flush_list()
            level = len(m_h.group(1))
            heading_text = inline_fmt(m_h.group(2))
            out.append(f'<h{level} class="doc-h{level}">{heading_text}</h{level}>')
            continue

        # Unordered list item (- item or * item)
        m_ul = re.match(r"^[-*]\s+(.+)$", line)
        if m_ul:
            if in_list and list_type != "ul":
                flush_list()
            if not in_list:
                out.append('<ul class="doc-ul">')
                in_list = True
                list_type = "ul"
            out.append(f"<li>{inline_fmt(m_ul.group(1))}</li>")
            continue

        # Ordered list item (1. item)
        m_ol = re.match(r"^\d+\.\s+(.+)$", line)
        if m_ol:
            if in_list and list_type != "ol":
                flush_list()
            if not in_list:
                out.append('<ol class="doc-ol">')
                in_list = True
                list_type = "ol"
            out.append(f"<li>{inline_fmt(m_ol.group(1))}</li>")
            continue

        flush_list()
        out.append(f'<p class="doc-p">{inline_fmt(line)}</p>')

    flush_list()
    flush_quote()
    return "\n".join(out)


def get_legal_doc(doc_type: str) -> tuple[str, str]:
    """Return (title, html_content) for 'privacy' or 'terms'."""
    is_privacy = doc_type.lower().startswith("priv")
    filename = "PRIVACY_POLICY.md" if is_privacy else "TERMS_OF_SERVICE.md"
    title = "Privacy Policy" if is_privacy else "Terms of Service"
    path = BASE_DIR / filename
    if not path.is_file():
        return (title, f"<p class=\"doc-p\">The {title} document is currently unavailable.</p>")
    content = path.read_text(encoding="utf-8")
    return (title, md_to_html(content))
