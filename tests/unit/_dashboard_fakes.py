"""A fake Streamlit module shared by every dashboard page test.

Not a test module itself (no ``test_`` prefix — pytest never collects it).
Records every call a page makes instead of drawing anything, so every
``render()`` function in ``dashboards/`` — which all take the streamlit
module as a plain parameter — can be exercised with zero real Streamlit
dependency at collection time.
"""

from __future__ import annotations

from typing import Any


class _FakeDelta:
    """Stands in for both a column and a container (``with ...:`` block)."""

    def __init__(self, sink: FakeStreamlit) -> None:
        self._sink = sink

    def metric(self, label: str, value: object, *args: object, **kwargs: object) -> None:
        self._sink.metrics.append((label, value))

    def __getattr__(self, name: str) -> Any:
        # Delegate everything else (markdown, caption, write, ...) to the
        # parent sink so nested columns/containers behave like the page.
        return getattr(self._sink, name)

    def __enter__(self) -> _FakeDelta:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeStreamlit:
    """Records every call a page makes."""

    def __init__(self) -> None:
        self.subheaders: list[str] = []
        self.titles: list[str] = []
        self.captions: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.successes: list[str] = []
        self.markdowns: list[str] = []
        self.writes: list[str] = []
        self.metrics: list[tuple[str, object]] = []
        self.dataframes: list[list[dict[str, object]]] = []
        self.download_buttons: list[tuple[str, bytes]] = []
        self.page_links: list[tuple[str, str]] = []
        self.selectbox_return: object = "—"
        self.tab_labels: list[list[str]] = []
        self.expander_labels: list[str] = []
        self.charts: list[object] = []

    # -------------------------------------------------------------- text
    def subheader(self, text: str) -> None:
        self.subheaders.append(text)

    def title(self, text: str) -> None:
        self.titles.append(text)

    def caption(self, text: str) -> None:
        self.captions.append(text)

    def info(self, text: str) -> None:
        self.infos.append(text)

    def error(self, text: str) -> None:
        self.errors.append(text)

    def warning(self, text: str) -> None:
        self.warnings.append(text)

    def success(self, text: str) -> None:
        self.successes.append(text)

    def markdown(self, text: str) -> None:
        self.markdowns.append(text)

    def write(self, text: object) -> None:
        self.writes.append(str(text))

    def divider(self) -> None:
        return None

    # ------------------------------------------------------------ layout
    def columns(self, n: int) -> list[_FakeDelta]:
        return [_FakeDelta(self) for _ in range(n)]

    def container(self, *, border: bool = False) -> _FakeDelta:
        return _FakeDelta(self)

    def expander(self, label: str, **kwargs: object) -> _FakeDelta:
        self.expander_labels.append(label)
        return _FakeDelta(self)

    def tabs(self, labels: list[str]) -> list[_FakeDelta]:
        self.tab_labels.append(list(labels))
        return [_FakeDelta(self) for _ in labels]

    def sidebar(self) -> _FakeDelta:
        return _FakeDelta(self)

    # ------------------------------------------------------------- data
    def metric(self, label: str, value: object, *args: object, **kwargs: object) -> None:
        self.metrics.append((label, value))

    def dataframe(self, data: object, **kwargs: object) -> None:
        self.dataframes.append(data)  # type: ignore[arg-type]

    def line_chart(self, data: object, **kwargs: object) -> None:
        self.charts.append(data)

    def bar_chart(self, data: object, **kwargs: object) -> None:
        self.charts.append(data)

    def download_button(
        self, label: str, data: bytes, *, file_name: str = "", mime: str = "", **kwargs: object
    ) -> bool:
        self.download_buttons.append((label, data))
        return False

    def page_link(self, page: str, *, label: str = "", **kwargs: object) -> None:
        self.page_links.append((page, label))

    def selectbox(self, label: str, options: object, **kwargs: object) -> object:
        return self.selectbox_return

    def set_page_config(self, **kwargs: object) -> None:
        return None
