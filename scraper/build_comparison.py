#!/usr/bin/env python3
"""Build a local SQLite comparison table from the frozen SABER dump and CIC HTML.

This is an audit artifact, not the application catalog. It deliberately omits
student names and emails; raw CIC HTML remains in the ignored data archive.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DUMP = ROOT / "data" / "saber_sip.sql"
DEFAULT_RUN = Path(__file__).resolve().parent / "data" / "cic_raw" / "runs" / "20261018T215744Z"
DEFAULT_DB = Path(__file__).resolve().parent / "data" / "saber_cic_comparison.sqlite"


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_text(fragment: str) -> str:
    parser = TextParser()
    parser.feed(fragment)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def normalize_title(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip("\"“”'")
    value = unicodedata.normalize("NFC", value)
    return re.sub(r"\s+", " ", value).casefold()


def normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    tokens = re.sub(r"[^a-z0-9]+", " ", value).split()
    return " ".join(sorted(tokens))


def decode_copy_field(value: str) -> str | None:
    if value == r"\N":
        return None
    result: list[str] = []
    index = 0
    escapes = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            result.append(char)
            index += 1
            continue
        nxt = value[index + 1]
        if nxt in escapes:
            result.append(escapes[nxt])
            index += 2
        elif (
            nxt == "x"
            and index + 3 < len(value)
            and re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 2 : index + 4])
        ):
            result.append(chr(int(value[index + 2 : index + 4], 16)))
            index += 4
        elif nxt in "01234567":
            match = re.match(r"[0-7]{1,3}", value[index + 1 :])
            assert match is not None
            result.append(chr(int(match.group(0), 8)))
            index += 1 + len(match.group(0))
        else:
            result.append(nxt)
            index += 2
    return "".join(result)


def parse_pg_copy(dump_path: Path) -> dict[str, list[dict[str, str | None]]]:
    text = dump_path.read_text(encoding="utf-8", errors="replace")
    tables: dict[str, list[dict[str, str | None]]] = {}
    pattern = re.compile(
        r"^COPY public\.(\w+) \(([^)]*)\) FROM stdin;\n(.*?)^\\\.\s*$",
        re.MULTILINE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        table, column_text, body = match.groups()
        columns = [column.strip() for column in column_text.split(",")]
        rows: list[dict[str, str | None]] = []
        for line in body.splitlines():
            values = [decode_copy_field(value) for value in line.split("\t")]
            if len(values) != len(columns):
                raise ValueError(
                    f"COPY parse error in {table}: expected {len(columns)} fields, "
                    f"got {len(values)}"
                )
            rows.append(dict(zip(columns, values, strict=True)))
        tables[table] = rows
    return tables


def extract_forms(row_html: str, field_name: str) -> list[str]:
    """Values submitted by `field_name`, independent of attribute order."""
    values: list[str] = []
    for tag in re.findall(r"<input\b[^>]*>", row_html, re.IGNORECASE):
        attributes = dict(re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', tag))
        value = attributes.get("value", "")
        if attributes.get("name") == field_name and value.isdigit():
            values.append(value)
    return values


def extract_cic(run_dir: Path) -> dict[str, object]:
    lab_pages = list((run_dir / "pages" / "laboratories").glob("*.php"))
    researcher_pages = list((run_dir / "pages" / "researchers").glob("*.php"))
    thesis_details: dict[str, str] = {}
    for path in (run_dir / "details" / "thesis").glob("*.html"):
        page = path.read_text(encoding="utf-8", errors="replace")
        title_match = re.search(r"<h2\b[^>]*>(.*?)</h2>", page, re.IGNORECASE | re.DOTALL)
        if title_match:
            thesis_details[path.stem] = html_text(title_match.group(1))

    thesis_roles: dict[str, list[str]] = defaultdict(list)
    publications: dict[str, str] = {}
    projects: dict[str, str] = {}
    researcher_names: set[str] = set()
    profile_by_name: dict[str, str] = {}
    for path in researcher_pages:
        page = path.read_text(encoding="utf-8", errors="replace")
        title_match = re.search(
            r"<title>\s*(?:Investigador|Investigadora)\s+(.*?)</title>",
            page,
            re.IGNORECASE | re.DOTALL,
        )
        researcher = html_text(title_match.group(1)) if title_match else path.stem
        researcher_names.add(researcher)
        profile_by_name[normalize_name(researcher) or ""] = researcher
        for row in re.findall(r"<tr\b[^>]*>.*?</tr>", page, re.IGNORECASE | re.DOTALL):
            cells = [
                html_text(cell)
                for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.IGNORECASE | re.DOTALL)
            ]
            for thesis_id in extract_forms(row, "id_tet"):
                role = next(
                    (value for value in cells if value.casefold() in {"director 1", "director 2"}),
                    "unknown",
                )
                thesis_roles[thesis_id].append(f"{role}: {researcher}")
            for publication_id in extract_forms(row, "id_pub"):
                publications.setdefault(publication_id, cells[0] if cells else "")
            for project_id in extract_forms(row, "id_proy"):
                projects.setdefault(project_id, cells[0] if cells else "")

    return {
        "lab_pages": lab_pages,
        "laboratories": {
            path.stem: html_text(match.group(1))
            for path in lab_pages
            if (
                match := re.search(
                    r"<title>(.*?)</title>",
                    path.read_text(encoding="utf-8", errors="replace"),
                    re.IGNORECASE | re.DOTALL,
                )
            )
        },
        "researcher_pages": researcher_pages,
        "theses": thesis_details,
        "thesis_roles": thesis_roles,
        "publications": publications,
        "projects": projects,
        "researcher_names": researcher_names,
        "profile_by_name": profile_by_name,
    }


def build(dump: Path, run_dir: Path, db_path: Path) -> None:
    cic = extract_cic(run_dir)
    # Validate before the existing comparison is deleted: a missing or empty
    # capture would otherwise write a database reporting zero CIC records.
    if not cic["theses"] and not cic["publications"]:
        raise SystemExit(
            f"Capture at {run_dir} has no parsed CIC records; check --run-dir before rebuilding."
        )
    saber = parse_pg_copy(dump)
    required = {"laboratories", "professors", "research_products", "theses", "academic_programs"}
    missing = required - saber.keys()
    if missing:
        raise ValueError(f"SABER dump is missing tables: {sorted(missing)}")

    saber_professors = {row["id"]: row for row in saber["professors"]}
    saber_programs = {row["id"]: row for row in saber["academic_programs"]}
    saber_theses = {row["id"]: row for row in saber["theses"]}
    saber_products = {row["id"]: row for row in saber["research_products"]}
    cic_theses: dict[str, str] = cic["theses"]  # type: ignore[assignment]
    cic_roles: dict[str, list[str]] = cic["thesis_roles"]  # type: ignore[assignment]
    cic_publications: dict[str, str] = cic["publications"]  # type: ignore[assignment]
    cic_projects: dict[str, str] = cic["projects"]  # type: ignore[assignment]

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE source_summary (
            entity_type TEXT PRIMARY KEY,
            saber_count INTEGER NOT NULL,
            cic_count INTEGER NOT NULL,
            shared_id_count INTEGER,
            saber_only_count INTEGER,
            cic_only_count INTEGER,
            comparison_note TEXT NOT NULL
        );
        CREATE TABLE thesis_comparison (
            thesis_id TEXT PRIMARY KEY,
            record_status TEXT NOT NULL,
            title_status TEXT,
            saber_title TEXT,
            cic_title TEXT,
            saber_advisor_1 TEXT,
            saber_advisor_2 TEXT,
            saber_program TEXT,
            saber_status TEXT,
            cic_supervision_roles TEXT,
            saber_url TEXT,
            saber_url_id TEXT,
            url_id_matches_thesis INTEGER,
            source_note TEXT NOT NULL
        );
        CREATE INDEX thesis_comparison_status_idx ON thesis_comparison(record_status, title_status);
        CREATE TABLE publication_id_comparison (
            product_id TEXT PRIMARY KEY,
            record_status TEXT NOT NULL,
            saber_title TEXT,
            cic_title TEXT,
            title_equal_after_normalization INTEGER
        );
        CREATE TABLE laboratory_comparison (
            cic_slug TEXT PRIMARY KEY,
            cic_name TEXT NOT NULL,
            saber_id TEXT,
            saber_name TEXT,
            reconciliation TEXT NOT NULL,
            note TEXT NOT NULL
        );
        """
    )

    def insert_summary(entity: str, old_ids: set[str], new_ids: set[str], note: str) -> None:
        connection.execute(
            "INSERT INTO source_summary VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entity,
                len(old_ids),
                len(new_ids),
                len(old_ids & new_ids),
                len(old_ids - new_ids),
                len(new_ids - old_ids),
                note,
            ),
        )

    old_labs = {str(row["id"]): row for row in saber["laboratories"]}
    lab_candidates = {
        "lab_cybersecurity": ("76", "same_name"),
        "lab_rycd": ("13", "same_name"),
        "lab_cdyts": ("9", "probable_name_change"),
        "lab_piig": ("12", "probable_expansion"),
        "lab_cdic": ("79", "needs_review"),
        "lab_ccc": ("78", "same_name"),
        "lab_cmc": ("80", "same_name"),
        "lab_ci": ("70", "same_name"),
        "lab_ia": ("10", "same_name"),
        "lab_microse": ("14", "probable_truncation"),
        "lab_pds": ("15", "same_name"),
        "lab_pln": ("11", "same_name"),
        "lab_rym": ("17", "same_name"),
        "lab_sia": ("16", "probable_name_change"),
        "lab_sym": ("8", "same_name"),
    }
    mapped_old_lab_ids: set[str] = set()
    unmapped_slugs: list[str] = []
    for slug, name in sorted(cic["laboratories"].items()):  # type: ignore[union-attr]
        candidate = lab_candidates.get(slug)
        if candidate is None:
            # A slug the mapping does not know about is kept for review instead
            # of aborting the whole rebuild.
            unmapped_slugs.append(slug)
            connection.execute(
                "INSERT INTO laboratory_comparison VALUES (?, ?, ?, ?, ?, ?)",
                (
                    slug,
                    name,
                    None,
                    None,
                    "unmapped",
                    "No SABER candidate is mapped for this CIC laboratory slug.",
                ),
            )
            continue
        old_id, reconciliation = candidate
        mapped_old_lab_ids.add(old_id)
        old_name = old_labs[old_id]["name"]
        connection.execute(
            "INSERT INTO laboratory_comparison VALUES (?, ?, ?, ?, ?, ?)",
            (
                slug,
                name,
                old_id,
                old_name,
                reconciliation,
                "Name-based candidate only; CIC does not expose the old SABER lab ID. "
                "Review before treating as an identity match.",
            ),
        )
    for old_id, row in old_labs.items():
        if old_id not in mapped_old_lab_ids:
            connection.execute(
                "INSERT INTO laboratory_comparison VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"saber-only-{old_id}",
                    "",
                    old_id,
                    row["name"],
                    "saber_only",
                    "No CIC lab candidate was assigned; the old export includes this name.",
                ),
            )
    connection.execute(
        "INSERT INTO source_summary VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "laboratories",
            len(old_labs),
            len(cic["laboratories"]),
            None,
            len(old_labs) - len(mapped_old_lab_ids),
            len(unmapped_slugs),
            f"{len(lab_candidates)} name-based candidates were entered for review; "
            "no shared numeric lab IDs were verified.",
        ),
    )
    old_prof_names = {normalize_name(str(row["name"])) or "" for row in saber["professors"]}
    new_prof_names = {normalize_name(name) or "" for name in cic["researcher_names"]}  # type: ignore[union-attr]
    connection.execute(
        "INSERT INTO source_summary VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "researchers",
            len(saber["professors"]),
            len(cic["researcher_pages"]),
            len(old_prof_names & new_prof_names),
            len(old_prof_names - new_prof_names),
            len(new_prof_names - old_prof_names),
            "Shared count requires accent-insensitive token-set name equality; it does not "
            "catch spelling/order variants and is not a proof of identity.",
        ),
    )
    insert_summary(
        "theses",
        set(saber_theses),
        set(cic_theses),
        "ID intersection is measured; title and SABER URL are compared per ID. "
        "IDs are not presumed infallible.",
    )
    insert_summary(
        "publications",
        set(saber_products),
        set(cic_publications),
        "ID intersection is exploratory; current profile rows provide titles, "
        "not fetched detail pages.",
    )
    connection.execute(
        "INSERT INTO source_summary VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "projects",
            0,
            len(cic_projects),
            0,
            0,
            len(cic_projects),
            "SABER export has no projects table; CIC IDs deduplicated across profiles.",
        ),
    )

    for thesis_id in sorted(set(saber_theses) | set(cic_theses), key=lambda x: int(x)):
        old = saber_theses.get(thesis_id)
        new_title = cic_theses.get(thesis_id)
        if old is None:
            record_status = "cic_only"
        elif new_title is None:
            record_status = "saber_only"
        else:
            record_status = "in_both"
        title_status = None
        if old is not None and new_title is not None:
            title_status = (
                "same_after_normalization"
                if normalize_title(old["title"]) == normalize_title(new_title)
                else "different"
            )
        advisor_1 = saber_professors.get(old["advisor1_id"] or "") if old else None
        advisor_2 = saber_professors.get(old["advisor2_id"] or "") if old else None
        program = saber_programs.get(old["academic_program_id"] or "") if old else None
        saber_url = old["url"] if old else None
        url_match = re.search(r"/(\d+)/?$", saber_url or "")
        url_id = url_match.group(1) if url_match else None
        roles = cic_roles.get(thesis_id, [])
        if title_status == "different":
            note = (
                "Same numeric thesis ID but titles differ after quote/whitespace "
                "normalization; review before merging as one thesis."
            )
        elif url_id and url_id != thesis_id:
            note = (
                "SABER URL ends in a different ID from its thesis row; "
                "CIC title/role are preserved as separate claims."
            )
        elif record_status == "in_both":
            note = (
                "Same numeric ID found in both sources; still retain source "
                "provenance and review role assignments."
            )
        else:
            note = (
                "Record exists in one source only; absence is not proof that the "
                "thesis does not exist."
            )
        connection.execute(
            "INSERT INTO thesis_comparison VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thesis_id,
                record_status,
                title_status,
                old["title"] if old else None,
                new_title,
                advisor_1["name"] if advisor_1 else None,
                advisor_2["name"] if advisor_2 else None,
                program["program"] if program else None,
                program["status"] if program else None,
                json.dumps(roles, ensure_ascii=False),
                saber_url,
                url_id,
                int(url_id == thesis_id) if url_id else None,
                note,
            ),
        )

    for product_id in sorted(set(saber_products) | set(cic_publications), key=lambda x: int(x)):
        old = saber_products.get(product_id)
        new_title = cic_publications.get(product_id)
        if old and new_title is not None:
            status = "in_both"
        elif old:
            status = "saber_only"
        else:
            status = "cic_only"
        equal = None
        if old and new_title is not None:
            equal = int(normalize_title(old["title"]) == normalize_title(new_title))
        connection.execute(
            "INSERT INTO publication_id_comparison VALUES (?, ?, ?, ?, ?)",
            (product_id, status, old["title"] if old else None, new_title, equal),
        )

    connection.commit()
    connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, default=DEFAULT_DUMP)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    build(args.dump, args.run_dir, args.db)
    print(f"Comparison database saved: {args.db}")


if __name__ == "__main__":
    main()
