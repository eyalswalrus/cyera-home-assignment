"""Minimal plain text -> Atlassian Document Format (ADF), the rich-text format Jira's v3 API requires
for issue descriptions.

Text is never interpreted as markup: user input such as `svc_deploy_prod` stays literal, unlike
the v2 API's wiki markup, where underscores can turn into italics.
"""

import re
from typing import Any

Node = dict[str, Any]


def text(value: str, *marks: str) -> Node:
    node: Node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": mark} for mark in marks]
    return node


def link(label: str, href: str) -> Node:
    return {"type": "text", "text": label, "marks": [{"type": "link", "attrs": {"href": href}}]}


def paragraph(*content: Node) -> Node:
    return {"type": "paragraph", "content": list(content)}


def plain_paragraphs(value: str) -> list[Node]:
    """Blank lines separate paragraphs; single newlines become line breaks."""
    nodes = []
    for block in re.split(r"\n\s*\n", value.strip()):
        content: list[Node] = []
        for i, line in enumerate(block.split("\n")):
            if i:
                content.append({"type": "hardBreak"})
            if line:  # ADF rejects empty text nodes
                content.append(text(line))
        if content:
            nodes.append(paragraph(*content))
    return nodes


def document(*content: Node) -> Node:
    return {"type": "doc", "version": 1, "content": list(content)}
