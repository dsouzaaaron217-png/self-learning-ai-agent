"""
Safe Markdown Rendering Engine for Cognito Notes.
Strictly local, zero-dependency Markdown parser and HTML sanitizer.

Security Architecture:
1. Extract & protect code blocks and inline code.
2. Escape all raw HTML entities (&, <, >, ", ') so no user markup can execute.
3. Parse block elements (headings, blockquotes, lists, paragraphs).
4. Parse inline elements (bold, italic, links).
5. Strictly sanitize link targets against allow-list of schemes (http, https, mailto, relative).
   Disallows javascript:, data:, vbscript:, file:, control chars, and obfuscated URIs.
6. Re-insert safe code elements.
"""

import re
import html
import urllib.parse
from typing import List, Tuple


def is_safe_url(url: str) -> bool:
    """
    Validates that a URL is strictly safe for use in an <a href="..."> attribute.
    Allows only http://, https://, mailto:, or relative paths (/ or #).
    Strictly rejects javascript:, data:, vbscript:, file:, control characters, and obfuscated URIs.
    """
    if not url or not isinstance(url, str):
        return False

    cleaned = url.strip()
    if not cleaned:
        return False

    # Reject control characters and whitespace
    if re.search(r'[\x00-\x20\x7f]', cleaned):
        return False

    # Check for decoded protocol attacks (e.g. %6a%61%76%61%73%63%72%69%70%74:)
    try:
        decoded = urllib.parse.unquote(cleaned).lower()
    except Exception:
        return False

    if any(proto in decoded for proto in ('javascript:', 'data:', 'vbscript:', 'file:')):
        return False

    # Must start with allowed safe schemes or relative paths
    if re.match(r'^(https?://|mailto:|/[^/]|#)', cleaned, re.IGNORECASE):
        return True

    return False


def _format_links(text: str) -> str:
    """
    Parses Markdown links [text](url) with balanced parenthesis support.
    Sanitizes URLs against the allow-list. Unsafe URLs are neutralized.
    """
    res = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '[':
            close_bracket = text.find(']', i + 1)
            if close_bracket != -1 and close_bracket + 1 < n and text[close_bracket + 1] == '(':
                open_paren = close_bracket + 1
                paren_count = 1
                j = open_paren + 1
                while j < n and paren_count > 0:
                    if text[j] == '(':
                        paren_count += 1
                    elif text[j] == ')':
                        paren_count -= 1
                    j += 1
                if paren_count == 0:
                    link_text = text[i + 1:close_bracket]
                    raw_url = text[open_paren + 1:j - 1]
                    unescaped_url = html.unescape(raw_url).strip()
                    if is_safe_url(unescaped_url):
                        safe_href = html.escape(unescaped_url, quote=True)
                        formatted_inner = _format_inline_styles(link_text)
                        res.append(f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer">{formatted_inner}</a>')
                    else:
                        # Neutralized: Render only the link text, no executable link
                        res.append(_format_inline_styles(link_text))
                    i = j
                    continue
        res.append(text[i])
        i += 1
    return ''.join(res)


def _format_inline(text: str) -> str:
    """Formats inline links, bold, and italic."""
    text_with_links = _format_links(text)
    return _format_inline_styles(text_with_links)


def _format_inline_styles(text: str) -> str:
    """Formats bold and italic inline markup."""
    # Bold: **bold** or __bold__
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)

    # Italic: *italic* or _italic_ (avoid matching inside words for underscores)
    text = re.sub(r'\*([^*]+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'(?<!\w)_([^_]+?)_(?!\w)', r'<em>\1</em>', text)

    return text


def render_safe_markdown(raw_markdown: str) -> str:
    """
    Converts raw Markdown text into sanitized, safe HTML.
    Neutralizes all raw HTML and script tags while rendering useful formatting.
    """
    if not raw_markdown or not isinstance(raw_markdown, str):
        return ""

    text = raw_markdown.replace('\r\n', '\n').replace('\r', '\n')

    # Step 1: Protect fenced code blocks (```lang ... ```)
    code_blocks: List[str] = []
    def _save_code_block(match):
        code_content = match.group(2)
        escaped_code = html.escape(code_content)
        idx = len(code_blocks)
        code_blocks.append(f'<pre><code>{escaped_code}</code></pre>')
        return f'@@@CODE_BLOCK_{idx}@@@'

    text = re.sub(r'```([a-zA-Z0-9_\-]*)\n?(.*?)```', _save_code_block, text, flags=re.DOTALL)

    # Step 2: Protect inline code (`code`)
    inline_codes: List[str] = []
    def _save_inline_code(match):
        code_content = match.group(1)
        escaped_code = html.escape(code_content)
        idx = len(inline_codes)
        inline_codes.append(f'<code>{escaped_code}</code>')
        return f'@@@INLINE_CODE_{idx}@@@'

    text = re.sub(r'`([^`\n]+)`', _save_inline_code, text)

    # Step 3: Escape ALL remaining HTML in the text body
    # This renders all <script>, <img>, <div>, <iframe>, <object>, <svg>, onclick, etc. completely inert.
    text = html.escape(text, quote=True)

    # Step 4: Block-level parsing
    lines = text.split('\n')
    output_blocks: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Check for code block placeholder line
        cb_match = re.match(r'^\s*@@@CODE_BLOCK_(\d+)@@@\s*$', line)
        if cb_match:
            output_blocks.append(line.strip())
            i += 1
            continue

        # Blank line
        if not line.strip():
            i += 1
            continue

        # Headings: # Heading 1 to ###### Heading 6
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            level = len(heading_match.group(1))
            h_text = _format_inline(heading_match.group(2).strip())
            output_blocks.append(f'<h{level}>{h_text}</h{level}>')
            i += 1
            continue

        # Blockquote: > text
        if re.match(r'^&gt;\s*(.*)$', line):
            bq_lines = []
            while i < n and re.match(r'^&gt;\s*(.*)$', lines[i]):
                bq_match = re.match(r'^&gt;\s*(.*)$', lines[i])
                bq_lines.append(bq_match.group(1))
                i += 1
            bq_content = '<br>'.join(_format_inline(l) for l in bq_lines if l.strip())
            output_blocks.append(f'<blockquote><p>{bq_content}</p></blockquote>')
            continue

        # Unordered list: - item or * item
        if re.match(r'^[-*]\s+(.+)$', line):
            ul_items = []
            while i < n and re.match(r'^[-*]\s+(.+)$', lines[i]):
                item_match = re.match(r'^[-*]\s+(.+)$', lines[i])
                ul_items.append(f'<li>{_format_inline(item_match.group(1))}</li>')
                i += 1
            output_blocks.append(f'<ul>\n{"".join(ul_items)}\n</ul>')
            continue

        # Ordered list: 1. item
        if re.match(r'^\d+\.\s+(.+)$', line):
            ol_items = []
            while i < n and re.match(r'^\d+\.\s+(.+)$', lines[i]):
                item_match = re.match(r'^\d+\.\s+(.+)$', lines[i])
                ol_items.append(f'<li>{_format_inline(item_match.group(1))}</li>')
                i += 1
            output_blocks.append(f'<ol>\n{"".join(ol_items)}\n</ol>')
            continue

        # Normal paragraph: accumulate non-empty lines
        para_lines = []
        while i < n and lines[i].strip() and not re.match(r'^(#{1,6}\s+|&gt;\s*|[-*]\s+|\d+\.\s+|@@@CODE_BLOCK_)', lines[i]):
            para_lines.append(lines[i])
            i += 1

        if para_lines:
            para_html = '<br>'.join(_format_inline(l) for l in para_lines)
            output_blocks.append(f'<p>{para_html}</p>')

    result = '\n'.join(output_blocks)

    # Step 5: Restore protected code blocks and inline code
    for idx, cb_html in enumerate(code_blocks):
        result = result.replace(f'@@@CODE_BLOCK_{idx}@@@', cb_html)

    for idx, ic_html in enumerate(inline_codes):
        result = result.replace(f'@@@INLINE_CODE_{idx}@@@', ic_html)

    return result
