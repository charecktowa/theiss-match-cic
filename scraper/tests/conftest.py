"""A tiny synthetic capture run, shaped like the CIC pages the parsers read.

Every name here is invented; real captures stay out of the repository.
"""

from __future__ import annotations

import hashlib
import json
from html import escape
from pathlib import Path

import pytest

RUN_ID = "20260101T000000Z"


class RunBuilder:
    def __init__(self, root: Path) -> None:
        self.dir = root / RUN_ID
        self.records: list[dict[str, object]] = []
        self.run = {"run_id": RUN_ID, "started_at": "2026-01-01T00:00:00+00:00"}
        self.run["completed_at"] = "2026-01-01T01:00:00+00:00"
        self.run["detail_kinds"] = ["thesis"]

    def page(self, relative: str, html: str, url: str | None = None) -> None:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        self.records.append(self._record(url or f"https://www.cic.ipn.mx/{relative}", relative))

    def failure(self, url: str, form: list[dict[str, str]] | None = None) -> None:
        record = self._record(url, None, form)
        record["error"] = "URLError: timed out"
        self.records.append(record)

    def _record(
        self, url: str, path: str | None, form: list[dict[str, str]] | None = None
    ) -> dict[str, object]:
        method = "POST" if form else "GET"
        key = hashlib.sha256(f"{method}:{url}:{form}".encode()).hexdigest()
        return {
            "captured_at": "2026-01-01T00:30:00+00:00",
            "method": method,
            "request_key": key,
            "url": url,
            "form": form,
            "status": 200 if path else None,
            "error": None,
            "path": path,
            "sha256": "0" * 64 if path else None,
        }

    def lab(self, slug: str, name: str, members: list[str]) -> None:
        links = "".join(f'<a href="/views/investigation/{member}.php">m</a>' for member in members)
        self.page(f"pages/laboratories/{slug}.php", f"<title>{escape(name)}</title>{links}")

    def researcher(
        self,
        slug: str,
        name: str,
        *,
        graduated: list[tuple[str, str]] = (),
        products: list[tuple[str, str, str, str]] = (),
    ) -> None:
        theses = "".join(
            f"<tr><td>Tesis {thesis_id}</td><td>{role}</td>"
            f'<td><input type="hidden" name="id_tet" value="{thesis_id}"></td></tr>'
            for thesis_id, role in graduated
        )
        rows = "".join(
            f"<tr><td>{escape(title)}</td><td>{escape(venue)}</td><td>{date}</td>"
            f'<td><input name="id_pub" value="{product_id}"></td></tr>'
            for product_id, title, venue, date in products
        )
        self.page(
            f"pages/researchers/{slug}.php",
            f"<title>Investigador {escape(name)}</title>"
            f'<div id="tab1"><table>{theses}</table></div><div id="tab2"></div>'
            f"<h3>Artículos</h3><table>{rows}</table>",
            url=f"https://www.cic.ipn.mx/views/investigation/{slug}.php",
        )

    def thesis(self, thesis_id: str, committee: list[str], program: str = "MCC - Maestría") -> None:
        items = "".join(f"<li>{escape(name)}</li>" for name in committee)
        self.page(
            f"details/thesis/{thesis_id}.html",
            f"<h2>Tesis {thesis_id}</h2><p><strong>Programa:</strong> {program}</p>"
            f"<h4>Comité tutorial:</h4><ul>{items}</ul>",
        )

    def write(self) -> Path:
        index = '<div id="lab1">' + "".join(
            f'<a href="/views/investigation/{path.name}">x</a>'
            for path in sorted((self.dir / "pages" / "laboratories").glob("*.php"))
        )
        self.page("pages/index/laboratories.php", index + "</div>")
        (self.dir / "run.json").write_text(json.dumps(self.run), encoding="utf-8")
        with (self.dir / "manifest.jsonl").open("w", encoding="utf-8") as manifest:
            for record in self.records:
                manifest.write(json.dumps(record) + "\n")
        return self.dir


@pytest.fixture
def run_builder(tmp_path: Path) -> RunBuilder:
    return RunBuilder(tmp_path)
