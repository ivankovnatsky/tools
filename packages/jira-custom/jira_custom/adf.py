"""Convert between Atlassian Document Format (ADF) and markdown.

Covers what a markdown editor can express: paragraphs, headings, bullet,
ordered and task lists, code blocks, blockquotes, rules, links and
bold/italic/strike/code marks. Anything else (mentions, media, panels,
tables, ...) is rendered best-effort and reported by find_unsupported so
callers can refuse a lossy round-trip.
"""

import re
import uuid

import click
from markdown_it import MarkdownIt

SUPPORTED_NODES = frozenset(
    {
        "doc",
        "paragraph",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "taskList",
        "taskItem",
        "codeBlock",
        "blockquote",
        "rule",
        "text",
        "hardBreak",
        "inlineCard",
    }
)
SUPPORTED_MARKS = frozenset({"strong", "em", "strike", "code", "link"})

# Order marks open in; code is handled separately as the innermost mark
_STYLE_MARKS = ("strong", "em", "strike")
_DELIMS = {"strong": "**", "em": "*", "strike": "~~"}


def find_unsupported(node):
    """Return sorted node/mark types in an ADF tree that markdown can't hold."""
    found = set()

    def walk(n):
        t = n.get("type")
        if t not in SUPPORTED_NODES:
            found.add(t)
        elif t == "inlineCard" and not n.get("attrs", {}).get("url"):
            found.add("inlineCard without url")
        for mark in n.get("marks", []):
            if mark.get("type") not in SUPPORTED_MARKS:
                found.add(mark.get("type"))
            elif mark["type"] == "link" and not mark.get("attrs", {}).get("href"):
                found.add("link without href")
        for child in n.get("content", []):
            walk(child)

    if node:
        walk(node)
    return sorted(t for t in found if t)


# ADF -> markdown


def _indent(text, prefix):
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def _escape(text):
    """Escape characters markdown would read as formatting."""
    text = text.replace("\\", "\\\\")
    text = re.sub(r"([*`\[~])", r"\\\1", text)
    # Intraword underscores never emphasize, so keep snake_case readable
    text = re.sub(r"(?<!\w)_|_(?!\w)", r"\\_", text)
    text = re.sub(r"<(?=[A-Za-z/!?])", r"\\<", text)
    return text


def _escape_line_start(text):
    """Escape a leading token that would start a heading, list, quote or rule."""
    m = re.match(
        r"(\s*)(#{1,6}(?=\s|$)|[-+>](?=\s|$)|\d+(?=[.)](\s|$))|-{3,}|={3,})", text
    )
    if not m:
        return text
    lead, token = m.group(1), m.group(2)
    rest = text[len(lead) + len(token) :]
    if token[0].isdigit():
        return f"{lead}{token}\\{rest}"
    return f"{lead}\\{token}{rest}"


def _code_span(text):
    """Inline code wrapped in a backtick run longer than any inside it."""
    longest = max((len(r) for r in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _styled_run(nodes):
    """Render text nodes sharing one link target, opening/closing style marks
    only where they change so adjacent runs don't produce '****'."""
    out = []
    open_marks = []

    def close_to(n):
        # Keep trailing spaces outside closing delimiters
        buf = "".join(out)
        stripped = buf.rstrip(" ")
        trailing = buf[len(stripped) :]
        out[:] = [stripped]
        while len(open_marks) > n:
            out.append(_DELIMS[open_marks.pop()])
        out.append(trailing)

    for node in nodes:
        if node.get("type") == "hardBreak":
            out.append("\\\n")
            continue
        if node.get("type") == "inlineCard":
            close_to(0)
            out.append(f"<{node.get('attrs', {}).get('url', '')}>")
            continue
        marks = {m.get("type") for m in node.get("marks", [])}
        wanted = [m for m in _STYLE_MARKS if m in marks]
        keep = 0
        while (
            keep < len(open_marks)
            and keep < len(wanted)
            and open_marks[keep] == wanted[keep]
        ):
            keep += 1
        close_to(keep)

        raw = node.get("text", "")
        body = _code_span(raw) if "code" in marks else _escape(raw)
        if len(open_marks) < len(wanted):
            # Keep leading spaces outside opening delimiters
            stripped = body.lstrip(" ")
            out.append(body[: len(body) - len(stripped)])
            body = stripped
            for m in wanted[len(open_marks) :]:
                out.append(_DELIMS[m])
                open_marks.append(m)
        out.append(body)
    close_to(0)
    return "".join(out)


def _inline(nodes):
    out = []
    run = []
    run_href = None

    def flush():
        if not run:
            return
        text = _styled_run(run)
        out.append(f"[{text}]({run_href})" if run_href else text)
        run.clear()

    for n in nodes or []:
        t = n.get("type")
        if t in ("text", "hardBreak", "inlineCard"):
            href = None
            for m in n.get("marks", []):
                if m.get("type") == "link":
                    href = m.get("attrs", {}).get("href")
            if href != run_href or t == "inlineCard":
                flush()
                run_href = href
            run.append(n)
            continue
        flush()
        run_href = None
        if t == "mention":
            out.append(n.get("attrs", {}).get("text", "@mention"))
        elif t == "emoji":
            attrs = n.get("attrs", {})
            out.append(attrs.get("text") or attrs.get("shortName", ""))
        elif t == "status":
            out.append(f"[{n.get('attrs', {}).get('text', '')}]")
        elif t == "date":
            out.append(str(n.get("attrs", {}).get("timestamp", "")))
        else:
            out.append(_inline(n.get("content")))
    flush()
    return "".join(out)


def _paragraph_md(nodes):
    return "\n".join(_escape_line_start(line) for line in _inline(nodes).split("\n"))


def _list_item(marker, blocks):
    """Render list item blocks after a marker, indenting continuation lines."""
    pad = " " * len(marker)
    if not blocks:
        return marker.rstrip()
    text = _block(blocks[0])
    for prev, block in zip(blocks, blocks[1:]):
        # Nested lists hug their item; other blocks need a blank line or
        # they merge into the previous paragraph
        tight = block.get("type") in ("bulletList", "orderedList", "taskList")
        text += ("\n" if tight else "\n\n") + _block(block)
    return marker + _indent(text, pad)[len(pad) :]


def _task_list(node):
    lines = []
    for child in node.get("content", []):
        if child.get("type") == "taskItem":
            box = "[x]" if child.get("attrs", {}).get("state") == "DONE" else "[ ]"
            text = _indent(_inline(child.get("content")), "  ")[2:]
            lines.append(f"- {box} {text}".rstrip())
        else:
            # Nested taskList: ADF puts it after the item it belongs to
            lines.append(_indent(_block(child), "  "))
    return "\n".join(lines)


def _block(node):
    t = node.get("type")
    content = node.get("content", [])
    if t == "paragraph":
        return _paragraph_md(content)
    if t == "heading":
        return "#" * node.get("attrs", {}).get("level", 1) + " " + _inline(content)
    if t == "bulletList":
        return "\n".join(_list_item("- ", li.get("content", [])) for li in content)
    if t == "orderedList":
        start = node.get("attrs", {}).get("order", 1)
        return "\n".join(
            _list_item(f"{start + i}. ", li.get("content", []))
            for i, li in enumerate(content)
        )
    if t == "taskList":
        return _task_list(node)
    if t == "codeBlock":
        lang = node.get("attrs", {}).get("language") or ""
        text = "".join(c.get("text", "") for c in content)
        longest = max((len(r) for r in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}{lang}\n{text}\n{fence}"
    if t == "blockquote":
        return _indent(_blocks(content), "> ").replace("\n\n", "\n>\n")
    if t == "rule":
        return "---"
    if t == "mediaSingle" or t == "mediaGroup":
        return "[media]"
    if content and all(c.get("type") == "text" for c in content):
        return _inline(content)
    return _blocks(content)


def _blocks(nodes):
    return "\n\n".join(b for b in (_block(n) for n in nodes or []) if b != "")


def adf_to_markdown(doc):
    """Render an ADF document as markdown. None/empty gives an empty string."""
    if not doc:
        return ""
    return _blocks(doc.get("content")) + "\n"


# markdown -> ADF


def _text_node(text, marks):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [dict(m) for m in marks]
    return node


def _inline_to_adf(children):
    out = []
    marks = []
    for tok in children or []:
        t = tok.type
        if t == "text" and tok.content:
            out.append(_text_node(tok.content, marks))
        elif t == "code_inline":
            out.append(_text_node(tok.content, marks + [{"type": "code"}]))
        elif t in ("strong_open", "em_open", "s_open"):
            marks.append(
                {"type": {"strong_open": "strong", "em_open": "em"}.get(t, "strike")}
            )
        elif t in ("strong_close", "em_close", "s_close"):
            name = {"strong_close": "strong", "em_close": "em"}.get(t, "strike")
            marks = [m for m in marks if m["type"] != name]
        elif t == "link_open":
            href = tok.attrGet("href") or ""
            if tok.markup == "autolink":
                marks.append({"type": "_autolink", "attrs": {"url": href}})
            else:
                marks.append({"type": "link", "attrs": {"href": href}})
        elif t == "link_close":
            last = marks.pop() if marks else None
            if last and last["type"] == "_autolink":
                # Drop the text child; render as a smart link like the UI does
                if out and out[-1].get("type") == "text":
                    out.pop()
                out.append({"type": "inlineCard", "attrs": last["attrs"]})
        elif t == "hardbreak":
            out.append({"type": "hardBreak"})
        elif t == "softbreak":
            out.append(_text_node(" ", marks))
        elif t == "image":
            raise click.ClickException(
                "Images aren't supported in markdown descriptions; attach them instead"
            )
        elif t == "html_inline":
            out.append(_text_node(tok.content, marks))
    return out


def _paragraph(inline_tok):
    content = _inline_to_adf(inline_tok.children)
    node = {"type": "paragraph"}
    if content:
        node["content"] = content
    return node


def _strip_task_prefix(inline_tok):
    """Return 'DONE'/'TODO' and strip '[x] '/'[ ] ' from a list item's text."""
    children = inline_tok.children or []
    if not children or children[0].type != "text":
        return None
    first = children[0].content
    for box, state in (("[ ] ", "TODO"), ("[x] ", "DONE"), ("[X] ", "DONE")):
        if first.startswith(box) or first == box.strip():
            children[0].content = first[len(box) :]
            return state
    return None


def _close_index(tokens, i):
    """Index of the token closing the block opened at tokens[i]."""
    depth = 0
    for j in range(i, len(tokens)):
        depth += tokens[j].nesting
        if depth == 0:
            return j
    return len(tokens) - 1


def _list_items(tokens, start, end):
    """Yield (first, last) token ranges of each list item between start/end."""
    i = start
    while i < end:
        if tokens[i].type == "list_item_open":
            j = _close_index(tokens, i)
            yield i + 1, j
            i = j + 1
        else:
            i += 1


def _is_task_list(tokens, start, end):
    items = list(_list_items(tokens, start, end))
    if not items:
        return False
    for first, _ in items:
        tok = tokens[first + 1] if tokens[first].type == "paragraph_open" else None
        children = tok.children if tok is not None else None
        if not children or children[0].type != "text":
            return False
        text = children[0].content
        if not (
            text.startswith(("[ ] ", "[x] ", "[X] ")) or text in ("[ ]", "[x]", "[X]")
        ):
            return False
    return True


def _task_list_to_adf(tokens, start, end):
    content = []
    for first, last in _list_items(tokens, start, end):
        para_end = _close_index(tokens, first)
        inline = tokens[first + 1]
        state = _strip_task_prefix(inline)
        item = {
            "type": "taskItem",
            "attrs": {"state": state, "localId": uuid.uuid4().hex},
        }
        inline_content = _inline_to_adf(inline.children)
        if inline_content:
            item["content"] = inline_content
        content.append(item)
        for nested in _blocks_to_adf(tokens, para_end + 1, last):
            if nested["type"] != "taskList":
                raise click.ClickException(
                    "Only task lists can be nested under a task item"
                )
            content.append(nested)
    return {
        "type": "taskList",
        "attrs": {"localId": uuid.uuid4().hex},
        "content": content,
    }


def _blocks_to_adf(tokens, start, end):
    out = []
    i = start
    while i < end:
        tok = tokens[i]
        t = tok.type
        if t == "paragraph_open":
            out.append(_paragraph(tokens[i + 1]))
            i = _close_index(tokens, i) + 1
        elif t == "heading_open":
            node = {"type": "heading", "attrs": {"level": int(tok.tag[1])}}
            content = _inline_to_adf(tokens[i + 1].children)
            if content:
                node["content"] = content
            out.append(node)
            i = _close_index(tokens, i) + 1
        elif t in ("bullet_list_open", "ordered_list_open"):
            j = _close_index(tokens, i)
            if t == "bullet_list_open" and _is_task_list(tokens, i + 1, j):
                out.append(_task_list_to_adf(tokens, i + 1, j))
            else:
                items = []
                for first, last in _list_items(tokens, i + 1, j):
                    blocks = _blocks_to_adf(tokens, first, last)
                    items.append(
                        {
                            "type": "listItem",
                            "content": blocks or [{"type": "paragraph"}],
                        }
                    )
                if t == "ordered_list_open":
                    node = {
                        "type": "orderedList",
                        "attrs": {"order": int(tok.attrGet("start") or 1)},
                        "content": items,
                    }
                else:
                    node = {"type": "bulletList", "content": items}
                out.append(node)
            i = j + 1
        elif t in ("fence", "code_block"):
            node = {"type": "codeBlock", "attrs": {}}
            lang = tok.info.strip().split(" ")[0] if tok.info else ""
            if lang:
                node["attrs"]["language"] = lang
            text = tok.content.rstrip("\n")
            if text:
                node["content"] = [{"type": "text", "text": text}]
            out.append(node)
            i += 1
        elif t == "blockquote_open":
            j = _close_index(tokens, i)
            out.append(
                {"type": "blockquote", "content": _blocks_to_adf(tokens, i + 1, j)}
            )
            i = j + 1
        elif t == "hr":
            out.append({"type": "rule"})
            i += 1
        elif t == "html_block":
            out.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": tok.content.rstrip("\n")}],
                }
            )
            i += 1
        else:
            i += 1
    return out


def markdown_to_adf(text):
    """Parse markdown into an ADF document. Blank text gives None (clears)."""
    if not text or not text.strip():
        return None
    md = MarkdownIt("commonmark").enable("strikethrough")
    tokens = md.parse(text)
    return {
        "type": "doc",
        "version": 1,
        "content": _blocks_to_adf(tokens, 0, len(tokens)),
    }


# REST API v3 helpers


def get_description_adf(jira, issue_key):
    """Fetch an issue's description as ADF via REST API v3."""
    url = f"{jira._options['server']}/rest/api/3/issue/{issue_key}"
    response = jira._session.get(url, params={"fields": "description"})
    if response.status_code != 200:
        raise click.ClickException(f"Failed to get description: {response.text}")
    return response.json()["fields"].get("description")


def set_description_adf(jira, issue_key, doc):
    """Save an ADF document as the issue description via REST API v3."""
    url = f"{jira._options['server']}/rest/api/3/issue/{issue_key}"
    response = jira._session.put(url, json={"fields": {"description": doc}})
    if response.status_code not in (200, 204):
        raise click.ClickException(f"Failed to update description: {response.text}")
