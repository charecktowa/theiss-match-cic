#!/usr/bin/env python3
"""Build a self-contained PostgreSQL/pgvector database export from CIC HTML.

The generated psql script creates a separate `cic_scrape` database and loads
the parsed CIC catalog plus capture provenance. Student names are not copied
from thesis detail pages into the catalog or export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from runs import capture_problems, latest_run

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_SCHEMA = ROOT / "db" / "cic_scrape" / "01_schema.sql"
DEFAULT_EXPORT = ROOT / "data" / "cic_scrape.sql"
BASE_URL = "https://www.cic.ipn.mx"
INVESTIGATION_URL = BASE_URL + "/views/investigation/"
RUN_JSON = "run.json"
MANIFEST_JSONL = "manifest.jsonl"
CAPTURE_RUN_NOTE = (
    "CIC public-site capture; thesis details fetched, publications/projects read "
    "from researcher profile rows."
)
# data_issue identifiers are namespaced by the entity they describe, so the three
# families cannot collide with each other.
THESIS_DIRECTOR_ISSUE_ID_BASE = 1_000_000
THESIS_LAB_ISSUE_ID_BASE = 2_000_000
CATALOG_ISSUE_ID_BASE = 3_000_000


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def text_of(fragment: str) -> str:
    parser = TextParser()
    parser.feed(fragment)
    return re.sub(r"\s+", " ", " ".join(parser.text)).strip()


def name_key(name: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    value = unicodedata.normalize("NFKD", name.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(sorted(re.sub(r"[^a-z0-9]+", " ", value).split()))


def sql_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return "E'" + escaped + "'"


def sql_value(value: Any, *, jsonb: bool = False) -> str:
    if value is None:
        return "NULL"
    if jsonb:
        return sql_string(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) + "::jsonb"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return sql_string(str(value))


def insert_sql(table: str, values: dict[str, Any], json_columns: set[str] | None = None) -> str:
    json_columns = json_columns or set()
    columns = list(values)
    names = ", ".join(f'"{column}"' for column in columns)
    encoded = ", ".join(
        sql_value(values[column], jsonb=column in json_columns) for column in columns
    )
    if "id" in values:
        return f"INSERT INTO {table} ({names}) OVERRIDING SYSTEM VALUE VALUES ({encoded});"
    return f"INSERT INTO {table} ({names}) VALUES ({encoded});"


def source_document_id(manifest: dict[str, Any], by_path: dict[str, int]) -> int | None:
    path = manifest.get("path")
    return by_path.get(path) if path else None


def row_cells(row_html: str) -> list[str]:
    return [
        text_of(cell)
        for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.IGNORECASE | re.DOTALL)
    ]


def form_id(row_html: str, key: str) -> str | None:
    """Value of the form field `key`, independent of attribute order."""
    for tag in re.findall(r"<input\b[^>]*>", row_html, re.IGNORECASE):
        attributes = dict(re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', tag))
        value = attributes.get("value", "")
        if attributes.get("name") == key and value.isdigit():
            return value
    return None


def page_display_name(page: str) -> str | None:
    """The researcher's name, or None when the page is not a researcher profile."""
    match = re.search(
        r"<title>\s*(?:Investigador|Investigadora)\s+(.*?)</title>",
        page,
        re.IGNORECASE | re.DOTALL,
    )
    return text_of(match.group(1)) if match else None


def parse_detail(path: Path) -> dict[str, Any] | None:
    page = path.read_text(encoding="utf-8", errors="replace")
    title = re.search(r"<h2\b[^>]*>(.*?)</h2>", page, re.IGNORECASE | re.DOTALL)
    if not title:
        return None
    program_match = re.search(
        r"<strong>\s*Programa:\s*</strong>(.*?)</p>", page, re.IGNORECASE | re.DOTALL
    )
    program_label = text_of(program_match.group(1)) if program_match else ""
    program_code = program_label.split(" - ", 1)[0].strip() if program_label else None
    program_name = program_label.split(" - ", 1)[1].strip() if " - " in program_label else None
    if program_code and not program_name:
        program_name = program_label
    committee: list[str] = []
    committee_match = re.search(
        r"<h4>\s*Comité tutorial:\s*</h4>\s*<ul\b[^>]*>(.*?)</ul>", page, re.IGNORECASE | re.DOTALL
    )
    if committee_match:
        committee = [
            text_of(item)
            for item in re.findall(
                r"<li\b[^>]*>(.*?)</li>", committee_match.group(1), re.IGNORECASE | re.DOTALL
            )
        ]
        committee = [name.rstrip(" -") for name in committee if name.rstrip(" -")]
    return {
        "id": path.stem,
        "title": text_of(title.group(1)),
        "program_code": program_code,
        "program_name": program_name,
        "committee": committee,
    }


def load_manifest(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = json.loads((run_dir / RUN_JSON).read_text(encoding="utf-8"))
    lines = (run_dir / MANIFEST_JSONL).read_text(encoding="utf-8").splitlines()
    return run, [json.loads(line) for line in lines if line.strip()]


def archive_root(run_dir: Path) -> str:
    """Where the run's files are, relative to the repository when it is inside it."""
    resolved = run_dir.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def make_catalog(
    run_dir: Path, run: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    run_id = run["run_id"]
    root = archive_root(run_dir)
    for doc_id, record in enumerate(records, start=1):
        path = record.get("path")
        form = record.get("form")
        documents.append(
            {
                "id": doc_id,
                "run_id": 1,
                "request_key": record.get("request_key")
                or hashlib.sha256(
                    f"{record.get('method')}:{record.get('url')}:{doc_id}".encode()
                ).hexdigest(),
                "method": record.get("method", "GET"),
                "request_url": record.get("url") or "",
                "final_url": record.get("final_url"),
                "form_data": form,
                "http_status": record.get("status"),
                "content_type": record.get("content_type"),
                "captured_at": record.get("captured_at"),
                "stored_at": record.get("stored_at")
                or record.get("captured_at")
                or run.get("completed_at"),
                "archive_path": f"{root}/{path}" if path else None,
                "sha256": record.get("sha256"),
                "error": record.get("error"),
            }
        )
    doc_by_path = {
        record.get("path"): index
        for index, record in enumerate(records, start=1)
        if record.get("path")
    }

    laboratory_files = sorted((run_dir / "pages" / "laboratories").glob("*.php"))
    researcher_files = sorted((run_dir / "pages" / "researchers").glob("*.php"))
    lab_index = (run_dir / "pages" / "index" / "laboratories.php").read_text(
        encoding="utf-8", errors="replace"
    )
    lab_line_by_slug: dict[str, str] = {}
    line_names: dict[str, str] = {}
    for number, line_name in [
        (1, "Ciencia y Tecnología de la Información"),
        (2, "Inteligencia Artificial y Cómputo Científico"),
        (3, "Sistemas y Tecnologías de Alto Desempeño"),
    ]:
        line_names[str(number)] = line_name
        panel = re.search(
            rf'<div\s+id="lab{number}"\b(.*?)(?=<div\s+id="lab{number + 1}"\b|$)',
            lab_index,
            re.IGNORECASE | re.DOTALL,
        )
        if panel:
            for href in re.findall(
                r'href=["\'][^"\']*?/((?:lab_)[^/"\']+\.php)["\']', panel.group(1), re.IGNORECASE
            ):
                lab_line_by_slug[Path(href).stem] = line_name

    laboratories: dict[str, dict[str, Any]] = {}
    lab_member_name_keys: dict[str, set[str]] = defaultdict(set)
    for path in laboratory_files:
        page = path.read_text(encoding="utf-8", errors="replace")
        title_match = re.search(r"<title>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
        laboratories[path.stem] = {
            "name": text_of(title_match.group(1)) if title_match else path.stem,
            "source_document_id": doc_by_path.get(f"pages/laboratories/{path.name}"),
            "line": lab_line_by_slug.get(path.stem),
        }
        for href in re.findall(
            r'href=["\'](?:https?://www\.cic\.ipn\.mx)?/views/investigation/([^/"\']+\.php)["\']',
            page,
            re.IGNORECASE,
        ):
            if not href.startswith("lab_"):
                member_slug = Path(unquote(unescape(href))).stem
                lab_member_name_keys[path.stem].add(name_key(member_slug.replace("-", " ")))

    # Each CIC profile is one person, keyed by its slug: a name is only evidence
    # for a match, never an identity, so two people who share a name (or one
    # person whose name is spelled two ways) are not merged on their own.
    data_issues: list[dict[str, Any]] = []
    researchers: dict[str, dict[str, Any]] = {}
    memberships: dict[str, set[str]] = defaultdict(set)
    for path in researcher_files:
        page = path.read_text(encoding="utf-8", errors="replace")
        relative = f"pages/researchers/{path.name}"
        source_id = doc_by_path.get(relative)
        name = page_display_name(page)
        if name is None:
            data_issues.append(
                {
                    "entity_type": "source_document",
                    "entity_key": relative,
                    "code": "not_a_researcher_profile",
                    "detail": "Archived as a researcher page, but its title names no researcher.",
                    "source_document_id": source_id,
                }
            )
            continue
        researchers[path.stem] = {
            "name": name,
            "source_document_id": source_id,
            # The requested URL, not the file name: the archive's file names
            # replace non-ASCII characters, so they do not round-trip to a URL.
            "profile_url": documents[source_id - 1]["request_url"]
            if source_id
            else INVESTIGATION_URL + path.name,
        }
    profiles = {slug: run_dir / "pages" / "researchers" / f"{slug}.php" for slug in researchers}
    profile_slugs_by_name_key: dict[str, list[str]] = defaultdict(list)
    for slug, researcher in researchers.items():
        profile_slugs_by_name_key[name_key(researcher["name"])].append(slug)
    for lab_slug, member_name_keys in sorted(lab_member_name_keys.items()):
        ambiguous, unmatched = [], []
        for member_name_key in sorted(member_name_keys):
            candidates = profile_slugs_by_name_key.get(member_name_key, [])
            if len(candidates) == 1:
                memberships[candidates[0]].add(lab_slug)
            elif candidates:
                ambiguous.append(", ".join(sorted(candidates)))
            else:
                unmatched.append(member_name_key)
        if unmatched:
            data_issues.append(
                {
                    "entity_type": "laboratory",
                    "entity_key": lab_slug,
                    "code": "unmatched_member_link",
                    "detail": "Member links match no archived profile: " + "; ".join(unmatched),
                    "source_document_id": laboratories[lab_slug]["source_document_id"],
                }
            )
        if ambiguous:
            data_issues.append(
                {
                    "entity_type": "laboratory",
                    "entity_key": lab_slug,
                    "code": "ambiguous_member_name",
                    "detail": "Member links match several profiles: " + "; ".join(ambiguous),
                    "source_document_id": laboratories[lab_slug]["source_document_id"],
                }
            )

    persons_by_key: dict[str, dict[str, Any]] = {}
    profile_person_key: dict[str, str] = {}
    for slug, researcher in researchers.items():
        key = f"profile:{slug}"
        profile_person_key[slug] = key
        persons_by_key[key] = {
            "display_name": researcher["name"],
            "name_key": name_key(researcher["name"]),
            "email": None,
            "profile_url": researcher["profile_url"],
            "source_document_id": researcher["source_document_id"],
            "reviewed_identity": False,
            "profile_slug": slug,
        }

    details: dict[str, dict[str, Any]] = {}
    for path in sorted(
        (run_dir / "details" / "thesis").glob("*.html"), key=lambda item: int(item.stem)
    ):
        parsed = parse_detail(path)
        if parsed:
            parsed["source_document_id"] = doc_by_path.get(f"details/thesis/{path.name}")
            details[path.stem] = parsed

    thesis_advisors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    thesis_status: dict[str, str] = {}
    products: dict[str, dict[str, Any]] = {}
    product_authors: dict[str, set[str]] = defaultdict(set)
    projects: dict[str, dict[str, Any]] = {}
    project_members: dict[str, dict[str, str]] = defaultdict(dict)
    # Profiles can describe the same product or project differently. The first
    # profile's value is kept and every other value is reported. data_issue is
    # unique on (entity_type, entity_key, code), so disagreements are collected
    # and emitted as one issue per entity and field, not one per profile.
    field_conflicts: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    def note_conflicts(
        entity_type: str, existing: dict[str, Any], candidate: dict[str, Any], fields: list[str]
    ) -> None:
        for field in fields:
            if existing[field] != candidate[field]:
                field_conflicts[(entity_type, existing["id"], field)].add(str(candidate[field]))

    for slug, path in profiles.items():
        page = path.read_text(encoding="utf-8", errors="replace")
        researcher = researchers[slug]
        person_key = profile_person_key[slug]
        source_id = researcher["source_document_id"]
        for match in re.finditer(r"<tr\b[^>]*>.*?</tr>", page, re.IGNORECASE | re.DOTALL):
            row = match.group(0)
            cells = row_cells(row)
            thesis_id = form_id(row, "id_tet")
            if thesis_id:
                role_label = next(
                    (cell for cell in cells if cell.casefold() in {"director 1", "director 2"}), ""
                )
                role = {"director 1": "primary_advisor", "director 2": "co_advisor"}.get(
                    role_label.casefold(), "unknown"
                )
                thesis_advisors[thesis_id].append(
                    {"person_key": person_key, "role": role, "source_document_id": source_id}
                )
                tab1 = page.rfind('id="tab1"', 0, match.start())
                tab2 = page.rfind('id="tab2"', 0, match.start())
                # tab1 lists concluded theses. tab2 is left `unknown` on purpose:
                # checked against SABER, it mixes enrolled students with
                # graduates and does not separate withdrawals.
                if tab1 > tab2:
                    thesis_status[thesis_id] = "graduated"

            publication_id = form_id(row, "id_pub")
            if publication_id and cells:
                title = cells[0]
                surrounding = page[: match.start()]
                last_heading = list(
                    re.finditer(r"<h3\b[^>]*>(.*?)</h3>", surrounding, re.IGNORECASE | re.DOTALL)
                )
                heading = text_of(last_heading[-1].group(1)).casefold() if last_heading else ""
                kind = "conference" if "congreso" in heading else "publication"
                date_text = cells[2] if len(cells) > 2 else ""
                year_match = re.search(r"\b(?:19|20)\d{2}\b", date_text)
                venue_name = cells[1] if len(cells) > 1 else None
                candidate = {
                    "id": publication_id,
                    "title": title,
                    "kind": kind,
                    "year": int(year_match.group(0)) if year_match else None,
                    "venue": venue_name or None,
                    "source_document_id": source_id,
                }
                existing = products.get(publication_id)
                if existing is None:
                    products[publication_id] = candidate
                else:
                    note_conflicts(
                        "research_product", existing, candidate, ["title", "kind", "year", "venue"]
                    )
                product_authors[publication_id].add(person_key)

            project_id = form_id(row, "id_proy")
            if project_id and cells:
                title = cells[0]
                role = cells[1] if len(cells) > 1 else ""
                before = page[: match.start()]
                preceding_buttons = list(
                    re.finditer(r"<button\b[^>]*>(.*?)</button>", before, re.IGNORECASE | re.DOTALL)
                )
                year_match = (
                    re.search(r"\b(?:19|20)\d{2}\b", text_of(preceding_buttons[-1].group(1)))
                    if preceding_buttons
                    else None
                )
                candidate = {
                    "id": project_id,
                    "title": title,
                    "year": int(year_match.group(0)) if year_match else None,
                    "source_document_id": source_id,
                }
                existing = projects.get(project_id)
                if existing is None:
                    projects[project_id] = candidate
                else:
                    note_conflicts("research_project", existing, candidate, ["title", "year"])
                project_members[project_id][person_key] = role

    for (entity_type, entity_key, field), other_values in sorted(field_conflicts.items()):
        entity = (products if entity_type == "research_product" else projects)[entity_key]
        data_issues.append(
            {
                "entity_type": entity_type,
                "entity_key": entity_key,
                "code": f"conflicting_{field}s_across_profiles",
                "detail": " | ".join([str(entity[field]), *sorted(other_values)]),
                "source_document_id": entity["source_document_id"],
            }
        )

    # A thesis listed on a profile whose detail page is missing cannot be loaded
    # (its title comes from the detail page), so the gap is recorded instead.
    for thesis_key in sorted(set(thesis_advisors) - set(details), key=int):
        rows = thesis_advisors[thesis_key]
        data_issues.append(
            {
                "entity_type": "thesis",
                "entity_key": thesis_key,
                "code": "thesis_detail_missing",
                "detail": (
                    f"{len(rows)} profile row(s) list this thesis, but its detail page was not "
                    "archived or could not be parsed; the thesis and its advisors are not loaded."
                ),
                "source_document_id": rows[0]["source_document_id"],
            }
        )

    # A committee name joins a profile only when exactly one profile has its
    # name key. Otherwise it is an external person keyed by the name, and a
    # name that matches several profiles is reported for review.
    committee_keys: dict[str, list[str]] = {}
    aliases: set[tuple[str, str, int | None]] = set()
    for thesis_key, detail in details.items():
        keys: list[str] = []
        ambiguous: list[str] = []
        for committee_name in detail["committee"]:
            key = name_key(committee_name)
            candidates = profile_slugs_by_name_key.get(key, [])
            if len(candidates) == 1:
                person_key = profile_person_key[candidates[0]]
            else:
                person_key = f"name:{key}"
                if candidates:
                    ambiguous.append(committee_name)
                persons_by_key.setdefault(
                    person_key,
                    {
                        "display_name": committee_name,
                        "name_key": key,
                        "email": None,
                        "profile_url": None,
                        "source_document_id": detail["source_document_id"],
                        "reviewed_identity": False,
                        "profile_slug": None,
                    },
                )
            if committee_name != persons_by_key[person_key]["display_name"]:
                aliases.add((person_key, committee_name, detail["source_document_id"]))
            keys.append(person_key)
        committee_keys[thesis_key] = keys
        if ambiguous:
            data_issues.append(
                {
                    "entity_type": "thesis",
                    "entity_key": thesis_key,
                    "code": "ambiguous_committee_name",
                    "detail": "Committee names match several profiles: " + "; ".join(ambiguous),
                    "source_document_id": detail["source_document_id"],
                }
            )

    return {
        "run_id": run_id,
        "run": run,
        "documents": documents,
        "doc_by_path": doc_by_path,
        "line_names": line_names,
        "laboratories": laboratories,
        "researchers": researchers,
        "memberships": memberships,
        "persons": persons_by_key,
        "aliases": aliases,
        "details": details,
        "committee_keys": committee_keys,
        "thesis_advisors": thesis_advisors,
        "thesis_status": thesis_status,
        "products": products,
        "product_authors": product_authors,
        "projects": projects,
        "project_members": project_members,
        "data_issues": data_issues,
    }


def make_inserts(
    run_dir: Path, run: dict[str, Any], records: list[dict[str, Any]], notes: str = CAPTURE_RUN_NOTE
) -> tuple[list[str], dict[str, int]]:
    data = make_catalog(run_dir, run, records)
    lines: list[str] = []
    lines.append(
        insert_sql(
            "raw.capture_run",
            {
                "id": 1,
                "source_code": "cic",
                "started_at": data["run"].get("started_at"),
                "finished_at": data["run"].get("completed_at"),
                "extractor_version": data["run"].get("extractor_version", "0.1.0"),
                "notes": notes,
            },
        )
    )
    # Source records carry an import timestamp; original site observation dates
    # remain the exact per-response capture times from the manifest.
    for document in data["documents"]:
        lines.append(insert_sql("raw.source_document", document, {"form_data"}))

    org_id = 1
    lines.append(
        insert_sql(
            "catalog.organization",
            {
                "id": org_id,
                "name": "Instituto Politécnico Nacional (IPN)",
                "organization_type": "ipn",
                "source_document_id": None,
            },
        )
    )
    research_line_ids = {
        name: index for index, name in enumerate(data["line_names"].values(), start=1)
    }
    for line_id, name in enumerate(data["line_names"].values(), start=1):
        index_doc = data["doc_by_path"].get("pages/index/laboratories.php")
        lines.append(
            insert_sql(
                "catalog.research_line",
                {"id": line_id, "name": name, "source_document_id": index_doc},
            )
        )

    lab_ids = {slug: index for index, slug in enumerate(sorted(data["laboratories"]), start=1)}
    for slug, lab in sorted(data["laboratories"].items()):
        lab_id = lab_ids[slug]
        lines.append(
            insert_sql(
                "catalog.laboratory",
                {
                    "id": lab_id,
                    "name": lab["name"],
                    "cic_slug": slug,
                    "organization_id": org_id,
                    "is_external": False,
                    "source_document_id": lab["source_document_id"],
                },
            )
        )
        if lab["line"] in research_line_ids:
            lines.append(
                insert_sql(
                    "catalog.laboratory_research_line",
                    {
                        "laboratory_id": lab_id,
                        "research_line_id": research_line_ids[lab["line"]],
                        "source_document_id": data["doc_by_path"].get(
                            "pages/index/laboratories.php"
                        ),
                    },
                )
            )

    programs: dict[str, dict[str, Any]] = {}
    for detail in data["details"].values():
        code = detail["program_code"]
        if code:
            label = detail["program_name"] or code
            level = (
                "doctorate"
                if "doctorado" in label.casefold() or code.upper().startswith("D")
                else "masters"
            )
            programs.setdefault(
                code,
                {"name": label, "level": level, "source_document_id": detail["source_document_id"]},
            )
    for code, program in sorted(programs.items()):
        lines.append(insert_sql("catalog.program", {"code": code, **program}))

    # Ordered by name key, as IDs were before profiles became identities, so a
    # re-export keeps them; the person key breaks ties between shared names.
    person_order = sorted(data["persons"], key=lambda key: (data["persons"][key]["name_key"], key))
    person_ids = {key: index for index, key in enumerate(person_order, start=1)}
    for key in person_order:
        person = data["persons"][key]
        person_id = person_ids[key]
        lines.append(
            insert_sql(
                "catalog.person",
                {
                    "id": person_id,
                    "display_name": person["display_name"],
                    "name_key": person["name_key"],
                    "email": None,
                    "profile_url": person["profile_url"],
                    "source_document_id": person["source_document_id"],
                    "reviewed_identity": False,
                },
            )
        )
        if person["profile_slug"]:
            lines.append(
                insert_sql(
                    "catalog.person_source_identity",
                    {
                        "source_code": "cic",
                        "source_key": f"profile:{person['profile_slug']}",
                        "person_id": person_id,
                        "source_document_id": person["source_document_id"],
                    },
                )
            )

    for person_key, name_seen, source_document_id in sorted(
        data["aliases"], key=lambda alias: (alias[0], alias[1], alias[2] or 0)
    ):
        lines.append(
            insert_sql(
                "catalog.person_alias",
                {
                    "person_id": person_ids[person_key],
                    "name_seen": name_seen,
                    "name_key": name_key(name_seen),
                    "source_document_id": source_document_id,
                    "reviewed_match": False,
                },
            )
        )

    affiliation_ids: dict[str, list[int]] = defaultdict(list)
    affiliation_id = 1
    for profile_slug, lab_slugs in sorted(data["memberships"].items()):
        person_id = person_ids[f"profile:{profile_slug}"]
        for lab_slug in sorted(lab_slugs):
            source_id = data["laboratories"][lab_slug]["source_document_id"]
            lines.append(
                insert_sql(
                    "catalog.person_affiliation",
                    {
                        "id": affiliation_id,
                        "person_id": person_id,
                        "organization_id": org_id,
                        "laboratory_id": lab_ids[lab_slug],
                        "role": "researcher",
                        "is_primary": False,
                        "valid_from": None,
                        "valid_to": None,
                        "source_document_id": source_id,
                    },
                )
            )
            affiliation_ids[profile_slug].append(lab_ids[lab_slug])
            affiliation_id += 1

    lab_source_by_id = {
        lab_ids[slug]: lab["source_document_id"] for slug, lab in data["laboratories"].items()
    }
    # Distinct laboratories per person, sourced from the laboratory page itself
    # (deterministic per laboratory, so there is no source conflict to resolve).
    # A publication or project can have several participants from the same
    # laboratory, so the pairs are deduplicated once here: the catalog's primary
    # keys allow each (entity, laboratory) pair only once.
    labs_by_person_key: dict[str, list[tuple[int, int | None]]] = {
        f"profile:{slug}": [(lab_id, lab_source_by_id.get(lab_id)) for lab_id in sorted(lab_list)]
        for slug, lab_list in affiliation_ids.items()
    }

    thesis_ids = {
        thesis_key: index
        for index, thesis_key in enumerate(sorted(data["details"], key=int), start=1)
    }
    advisor_rows_by_thesis: dict[str, list[dict[str, Any]]] = data["thesis_advisors"]
    # Counted as rows are emitted: the export header must equal the number of
    # catalog.data_issue rows the restore actually ends up holding.
    thesis_issue_rows = 0
    # Theses without a detail page are reported by make_catalog, not loaded.
    for external_thesis_id in sorted(data["details"], key=int):
        detail = data["details"][external_thesis_id]
        thesis_id = thesis_ids[external_thesis_id]
        status = data["thesis_status"].get(external_thesis_id, "unknown")
        lines.append(
            insert_sql(
                "catalog.thesis",
                {
                    "id": thesis_id,
                    "title": detail["title"],
                    "program_code": detail["program_code"],
                    "completion_status": status,
                    "completion_year": None,
                    "url": None,
                    "source_document_id": detail["source_document_id"],
                },
            )
        )
        lines.append(
            insert_sql(
                "catalog.thesis_source_identity",
                {
                    "source_code": "cic",
                    "source_key": external_thesis_id,
                    "thesis_id": thesis_id,
                    "source_document_id": detail["source_document_id"],
                },
            )
        )
        advisor_rows = advisor_rows_by_thesis.get(external_thesis_id, [])
        primary_count = sum(row["role"] == "primary_advisor" for row in advisor_rows)
        if primary_count > 1:
            thesis_issue_rows += 1
            lines.append(
                insert_sql(
                    "catalog.data_issue",
                    {
                        "id": THESIS_DIRECTOR_ISSUE_ID_BASE + thesis_id,
                        "entity_type": "thesis",
                        "entity_key": external_thesis_id,
                        "code": "multiple_director_1_profiles",
                        "detail": (
                            f"{primary_count} CIC profiles list this thesis as Director 1; "
                            "review assignment."
                        ),
                        "source_document_id": detail["source_document_id"],
                    },
                )
            )
        seen_participants: set[tuple[int, str]] = set()
        for row in advisor_rows:
            person_id = person_ids[row["person_key"]]
            role = row["role"]
            if role == "primary_advisor" and primary_count > 1:
                role = "unknown"
            candidate_labs = [lab_id for lab_id, _ in labs_by_person_key.get(row["person_key"], [])]
            assigned_lab = (
                candidate_labs[0]
                if role == "primary_advisor" and len(candidate_labs) == 1
                else None
            )
            if role == "primary_advisor" and len(candidate_labs) != 1:
                thesis_issue_rows += 1
                lines.append(
                    insert_sql(
                        "catalog.data_issue",
                        {
                            "id": THESIS_LAB_ISSUE_ID_BASE + thesis_id,
                            "entity_type": "thesis",
                            "entity_key": external_thesis_id,
                            "code": "primary_lab_not_unambiguous",
                            "detail": (
                                "CIC lists Director 1, but the profile-to-lab mapping does not "
                                "identify exactly one lab."
                            ),
                            "source_document_id": row["source_document_id"],
                        },
                    )
                )
            participant_key = (person_id, role)
            if participant_key in seen_participants:
                continue
            seen_participants.add(participant_key)
            lines.append(
                insert_sql(
                    "catalog.thesis_participant",
                    {
                        "thesis_id": thesis_id,
                        "person_id": person_id,
                        "role": role,
                        "participant_order": None,
                        "assigned_laboratory_id": assigned_lab,
                        "source_document_id": row["source_document_id"],
                    },
                )
            )

        committee_seen: set[int] = set()
        for order, person_key in enumerate(data["committee_keys"][external_thesis_id], start=1):
            person_id = person_ids[person_key]
            if person_id in committee_seen:
                continue
            committee_seen.add(person_id)
            participant_key = (person_id, "committee_member")
            if participant_key not in seen_participants:
                seen_participants.add(participant_key)
                lines.append(
                    insert_sql(
                        "catalog.thesis_participant",
                        {
                            "thesis_id": thesis_id,
                            "person_id": person_id,
                            "role": "committee_member",
                            "participant_order": order,
                            "assigned_laboratory_id": None,
                            "source_document_id": detail["source_document_id"],
                        },
                    )
                )

    venue_ids: dict[str, int] = {}
    for product_id, product in sorted(data["products"].items(), key=lambda item: int(item[0])):
        venue = product["venue"]
        venue_id = None
        if venue:
            if venue not in venue_ids:
                venue_ids[venue] = len(venue_ids) + 1
                lines.append(insert_sql("catalog.venue", {"id": venue_ids[venue], "name": venue}))
            venue_id = venue_ids[venue]
        internal_id = int(product_id)
        lines.append(
            insert_sql(
                "catalog.research_product",
                {
                    "id": internal_id,
                    "kind": product["kind"],
                    "title": product["title"],
                    "description": None,
                    "year": product["year"],
                    "venue_id": venue_id,
                    "source_document_id": product["source_document_id"],
                },
            )
        )
        lines.append(
            insert_sql(
                "catalog.product_source_identity",
                {
                    "source_code": "cic",
                    "source_key": product_id,
                    "product_id": internal_id,
                    "source_document_id": product["source_document_id"],
                },
            )
        )
        # Accumulated across every author: co-authors from the same laboratory
        # would otherwise insert the same (product, laboratory) pair twice.
        product_labs: dict[int, int | None] = {}
        for author_key in sorted(data["product_authors"].get(product_id, [])):
            author_id = person_ids[author_key]
            lines.append(
                insert_sql(
                    "catalog.product_author",
                    {
                        "product_id": internal_id,
                        "person_id": author_id,
                        "author_order": None,
                        "source_document_id": product["source_document_id"],
                    },
                )
            )
            for laboratory_id, lab_source_id in labs_by_person_key.get(author_key, []):
                product_labs.setdefault(laboratory_id, lab_source_id)
        for laboratory_id, lab_source_id in sorted(product_labs.items()):
            lines.append(
                insert_sql(
                    "catalog.product_laboratory",
                    {
                        "product_id": internal_id,
                        "laboratory_id": laboratory_id,
                        "source_document_id": lab_source_id,
                    },
                )
            )

    for project_id, project in sorted(data["projects"].items(), key=lambda item: int(item[0])):
        internal_id = int(project_id)
        lines.append(
            insert_sql(
                "catalog.research_project",
                {
                    "id": internal_id,
                    "title": project["title"],
                    "description": None,
                    "year": project["year"],
                    "source_document_id": project["source_document_id"],
                },
            )
        )
        lines.append(
            insert_sql(
                "catalog.project_source_identity",
                {
                    "source_code": "cic",
                    "source_key": project_id,
                    "project_id": internal_id,
                    "source_document_id": project["source_document_id"],
                },
            )
        )
        # Accumulated across every member, for the same reason as products.
        project_labs: dict[int, int | None] = {}
        for member_key, role in sorted(data["project_members"].get(project_id, {}).items()):
            person_id = person_ids[member_key]
            lines.append(
                insert_sql(
                    "catalog.project_member",
                    {
                        "project_id": internal_id,
                        "person_id": person_id,
                        "role": role or None,
                        "source_document_id": project["source_document_id"],
                    },
                )
            )
            for laboratory_id, lab_source_id in labs_by_person_key.get(member_key, []):
                project_labs.setdefault(laboratory_id, lab_source_id)
        for laboratory_id, lab_source_id in sorted(project_labs.items()):
            lines.append(
                insert_sql(
                    "catalog.project_laboratory",
                    {
                        "project_id": internal_id,
                        "laboratory_id": laboratory_id,
                        "source_document_id": lab_source_id,
                    },
                )
            )

    for issue_index, issue in enumerate(data["data_issues"], start=1):
        lines.append(
            insert_sql("catalog.data_issue", {"id": CATALOG_ISSUE_ID_BASE + issue_index, **issue})
        )

    for table in [
        "raw.capture_run",
        "raw.source_document",
        "catalog.organization",
        "catalog.research_line",
        "catalog.laboratory",
        "catalog.person",
        "catalog.person_affiliation",
        "catalog.thesis",
        "catalog.research_product",
        "catalog.research_project",
        "catalog.venue",
        "catalog.data_issue",
        "catalog.embedding",
    ]:
        schema_name, table_name = table.split(".")
        lines.append(
            "SELECT setval(pg_get_serial_sequence("
            + sql_string(f"{schema_name}.{table_name}")
            + ", 'id'), COALESCE(MAX(id), 1), COUNT(*) > 0) FROM "
            + table
            + ";"
        )

    counts = {
        "capture_documents": len(records),
        "laboratories": len(data["laboratories"]),
        "researchers": len(data["researchers"]),
        "researchers_with_lab": len(data["memberships"]),
        "theses": len(data["details"]),
        "theses_with_advisor_profile": len(data["thesis_advisors"]),
        "publications_and_conferences": len(data["products"]),
        "projects": len(data["projects"]),
        "committee_people": sum(
            1 for person in data["persons"].values() if not person["profile_slug"]
        ),
        "person_aliases": len(data["aliases"]),
        "catalog_issue_rows": len(data["data_issues"]),
        "thesis_issue_rows": thesis_issue_rows,
    }
    # The header must match what the restore ends up holding, so the total counts
    # rows as they are emitted instead of re-deriving them from the source data.
    counts["total_catalog_data_issues"] = counts["catalog_issue_rows"] + counts["thesis_issue_rows"]
    return lines, counts


def build(
    run_dir: Path,
    schema: Path,
    export_path: Path,
    database_name: str,
    *,
    allow_incomplete: bool = False,
) -> dict[str, int]:
    # Lowercase only: an unquoted identifier is folded to lowercase, so any other
    # name would create one database and \connect to another.
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", database_name):
        raise ValueError("Database name must be a lowercase PostgreSQL identifier.")
    run, records = load_manifest(run_dir)
    notes = CAPTURE_RUN_NOTE
    problems = capture_problems(run, records)
    if problems:
        if not allow_incomplete:
            raise SystemExit(
                f"Capture at {run_dir} is incomplete: "
                + "; ".join(problems)
                + ". Re-run the capture, or pass --allow-incomplete to export it anyway."
            )
        notes += " Incomplete capture: " + "; ".join(problems) + "."
    inserts, counts = make_inserts(run_dir, run, records, notes)
    schema_sql = schema.read_text(encoding="utf-8")
    output = [
        "-- Generated CIC catalog database; data snapshot " + run["run_id"],
        "-- Source: https://www.cic.ipn.mx/views/investigation/laboratories.php",
        "-- Non-commercial use requires source attribution; review CIC/IPN terms before other use.",
        "-- Student names and emails are intentionally excluded from this catalog export.",
        "-- Restore into a PostgreSQL instance that has the pgvector package installed:",
        "--   psql -d postgres -f data/cic_scrape.sql",
        "\\set ON_ERROR_STOP on",
        "SET client_encoding = 'UTF8';",
        f"CREATE DATABASE {database_name} TEMPLATE template0 ENCODING 'UTF8';",
        f"\\connect {database_name}",
        "SET client_encoding = 'UTF8';",
        "SET standard_conforming_strings = on;",
        # Everything after CREATE DATABASE is one transaction, so a failed restore
        # leaves an empty database rather than a partial catalog.
        "BEGIN;",
        schema_sql,
        "-- Parsed CIC records and source-provenance manifest.",
        *inserts,
        "COMMIT;",
        "-- Export counts: " + json.dumps(counts, ensure_ascii=False, sort_keys=True),
        "",
    ]
    # Written beside the target and renamed into place, so a failed build never
    # leaves a truncated export where the previous one was.
    export_path.parent.mkdir(parents=True, exist_ok=True)
    partial = export_path.with_name(export_path.name + ".partial")
    partial.write_text("\n".join(output), encoding="utf-8")
    os.replace(partial, export_path)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir", type=Path, default=None, help="capture run (default: the newest finished)"
    )
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--database", default="cic_scrape")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="export a run with unfinished or failed requests, noting it in raw.capture_run",
    )
    args = parser.parse_args()
    run_dir = args.run_dir or latest_run()
    counts = build(
        run_dir, args.schema, args.output, args.database, allow_incomplete=args.allow_incomplete
    )
    print(f"PostgreSQL export of {run_dir}: {args.output}")
    print(json.dumps(counts, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
