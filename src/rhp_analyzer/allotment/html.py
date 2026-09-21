"""Small, dependency-free HTML readers for registrar status pages.

The registrars serve inconsistent markup and sometimes leave stale options in
HTML comments, so these readers use ``html.parser`` rather than regular
expressions. HTML comments are ignored automatically.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _OptionParser(HTMLParser):
    def __init__(self, select_id: str | None, select_name: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self._select_id = select_id
        self._select_name = select_name
        self._in_target = False
        self._depth = 0
        self._in_option = False
        self._value: str | None = None
        self._text: list[str] = []
        self.options: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "select":
            if self._in_target:
                self._depth += 1
            elif (
                self._select_id is not None and attributes.get("id") == self._select_id
            ) or (
                self._select_name is not None
                and attributes.get("name") == self._select_name
            ):
                self._in_target = True
                self._depth = 1
        elif self._in_target and tag == "option":
            self._in_option = True
            self._value = attributes.get("value")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._in_option:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._in_option:
            self._in_option = False
            text = " ".join("".join(self._text).split())
            if self._value is None:
                # Some pages omit the value attribute and use the label as
                # the id. Skip the usual placeholder labels.
                lowered = text.lower()
                value = (
                    text
                    if text
                    and not text.startswith("-")
                    and not lowered.startswith(("select", "choose"))
                    else ""
                )
            else:
                value = self._value.strip()
            if value and text:
                self.options.append((value, text))
        elif tag == "select" and self._in_target:
            self._depth -= 1
            if self._depth <= 0:
                self._in_target = False


def select_options(
    html: str,
    *,
    select_id: str | None = None,
    select_name: str | None = None,
) -> list[tuple[str, str]]:
    """Return ``(value, label)`` pairs for the matching ``<select>``."""

    parser = _OptionParser(select_id, select_name)
    parser.feed(html)
    return parser.options


class _RowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._row: list[str] = []
        self._in_cell = False
        self._cell: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._in_cell = True
            self._cell = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"br", "hr"} and self._in_cell:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._in_cell:
            self._in_cell = False
            self._row.append(" ".join("".join(self._cell).split()))
        elif tag == "tr":
            if any(cell for cell in self._row):
                self.rows.append(self._row)


def table_rows(html: str) -> list[list[str]]:
    """Return the text cells of every table row in the document."""

    parser = _RowParser()
    parser.feed(html)
    return parser.rows


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "template", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template", "noscript"}:
            self._ignored_depth = max(self._ignored_depth - 1, 0)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and data.strip():
            self.parts.append(data)


def visible_text(html: str) -> str:
    """Return rendered text while excluding scripts, styles, and comments."""

    parser = _VisibleTextParser()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


def hidden_input(html: str, name: str) -> str | None:
    """Return the value of the first hidden input with this name."""

    parser = _HiddenInputParser(name)
    parser.feed(html)
    return parser.value


class _HiddenInputParser(HTMLParser):
    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self._name = name
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input" or self.value is not None:
            return
        attributes = dict(attrs)
        if attributes.get("name") == self._name:
            self.value = attributes.get("value") or ""
