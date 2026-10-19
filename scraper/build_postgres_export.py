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
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from typing import Any


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_RUN = HERE / "data" / "cic_raw" / "runs" / "20261018T215744Z"
DEFAULT_SCHEMA = ROOT / "db" / "cic_scrape" / "01_schema.sql"
DEFAULT_EXPORT = ROOT / "data" / "cic_scrape.sql"
BASE_URL = "https://www.cic.ipn.mx"
INVESTIGATION_URL = BASE_URL + "/views/investigation/"


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
    encoded = ", ".join(sql_value(values[column], jsonb=column in json_columns) for column in columns)
    if "id" in values:
        return f"INSERT INTO {table} ({names}) OVERRIDING SYSTEM VALUE VALUES ({encoded});"
    return f"INSERT INTO {table} ({names}) VALUES ({encoded});"


def source_document_id(manifest: dict[str, Any], by_path: dict[str, int]) -> int | None:
    path = manifest.get("path")
    return by_path.get(path) if path else None


def row_cells(row_html: str) -> list[str]:
    return [text_of(cell) for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.IGNORECASE | re.DOTALL)]


def form_id(row_html: str, key: str) -> str | None:
    match = re.search(rf'name="{re.escape(key)}"\s+value="(\d+)"', row_html, re.IGNORECASE)
    return match.group(1) if match else None


def page_display_name(page: str, fallback: str) -> str:
    match = re.search(r"<title>\s*(?:Investigador|Investigadora)\s+(.*?)\s*</title>", page, re.IGNORECASE | re.DOTALL)
    return text_of(match.group(1)) if match else fallback


def parse_detail(path: Path) -> dict[str, Any] | None:
    page = path.read_text(encoding="utf-8", errors="replace")
    title = re.search(r"<h2\b[^>]*>(.*?)</h2>", page, re.IGNORECASE | re.DOTALL)
    if not title:
        return None
    program_match = re.search(r"<strong>\s*Programa:\s*</strong>\s*(.*?)</p>", page, re.IGNORECASE | re.DOTALL)
    program_label = text_of(program_match.group(1)) if program_match else ""
    program_code = program_label.split(" - ", 1)[0].strip() if program_label else None
    program_name = program_label.split(" - ", 1)[1].strip() if " - " in program_label else None
    if program_code and not program_name:
        program_name = program_label
    committee: list[str] = []
    committee_match = re.search(r"<h4>\s*Comité tutorial:\s*</h4>\s*<ul\b[^>]*>(.*?)</ul>", page, re.IGNORECASE | re.DOTALL)
    if committee_match:
        committee = [text_of(item) for item in re.findall(r"<li\b[^>]*>(.*?)</li>", committee_match.group(1), re.IGNORECASE | re.DOTALL)]
        committee = [name for name in committee if name]
    return {
        "id": path.stem,
        "title": text_of(title.group(1)),
        "program_code": program_code,
        "program_name": program_name,
        "committee": committee,
    }


def load_manifest(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (run_dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return run, records


def make_catalog(run_dir: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    document_ids: dict[str, int] = {}
    documents: list[dict[str, Any]] = []
    run_id = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["run_id"]
    for doc_id, record in enumerate(records, start=1):
        path = record.get("path")
        if path:
            document_ids.setdefault(path, doc_id)
        form = record.get("form")
        documents.append({
            "id": doc_id,
            "run_id": 1,
            "request_key": record.get("request_key") or hashlib.sha256(f"{record.get('method')}:{record.get('url')}:{doc_id}".encode()).hexdigest(),
            "method": record.get("method", "GET"),
            "request_url": record.get("url") or "",
            "final_url": record.get("final_url"),
            "form_data": form,
            "http_status": record.get("status"),
            "content_type": record.get("content_type"),
            "captured_at": record.get("captured_at"),
            "stored_at": record.get("stored_at") or record.get("captured_at") or run.get("completed_at"),
            "archive_path": f"scraper/data/cic_raw/runs/{run_id}/{path}" if path else None,
            "sha256": record.get("sha256"),
            "error": record.get("error"),
        })
    doc_by_path = {record.get("path"): index for index, record in enumerate(records, start=1) if record.get("path")}

    laboratory_files = sorted((run_dir / "pages" / "laboratories").glob("*.php"))
    researcher_files = sorted((run_dir / "pages" / "researchers").glob("*.php"))
    lab_index = (run_dir / "pages" / "index" / "laboratories.php").read_text(encoding="utf-8", errors="replace")
    lab_line_by_slug: dict[str, str] = {}
    line_names: dict[str, str] = {}
    for number, line_name in [(1, "Ciencia y Tecnología de la Información"), (2, "Inteligencia Artificial y Cómputo Científico"), (3, "Sistemas y Tecnologías de Alto Desempeño")]:
        line_names[str(number)] = line_name
        panel = re.search(rf'<div\s+id="lab{number}"\b(.*?)(?=<div\s+id="lab{number + 1}"\b|$)', lab_index, re.IGNORECASE | re.DOTALL)
        if panel:
            for href in re.findall(r'href=["\'][^"\']*?/((?:lab_)[^/"\']+\.php)["\']', panel.group(1), re.IGNORECASE):
                lab_line_by_slug[Path(href).stem] = line_name

    laboratories: dict[str, dict[str, Any]] = {}
    lab_member_name_keys: dict[str, set[str]] = defaultdict(set)
    for path in laboratory_files:
        page = path.read_text(encoding="utf-8", errors="replace")
        title_match = re.search(r"<title>\s*(.*?)\s*</title>", page, re.IGNORECASE | re.DOTALL)
        laboratories[path.stem] = {
            "name": text_of(title_match.group(1)) if title_match else path.stem,
            "source_document_id": doc_by_path.get(f"pages/laboratories/{path.name}"),
            "line": lab_line_by_slug.get(path.stem),
        }
        for href in re.findall(r'href=["\'](?:https?://www\.cic\.ipn\.mx)?/views/investigation/([^/"\']+\.php)["\']', page, re.IGNORECASE):
            if not href.startswith("lab_"):
                member_slug = Path(unquote(unescape(href))).stem
                lab_member_name_keys[path.stem].add(name_key(member_slug.replace("-", " ")))

    researchers: dict[str, dict[str, Any]] = {}
    memberships: dict[str, set[str]] = defaultdict(set)
    profiles = {path.stem: path for path in researcher_files}
    for path in researcher_files:
        page = path.read_text(encoding="utf-8", errors="replace")
        name = page_display_name(page, path.stem)
        researchers[path.stem] = {
            "name": name,
            "source_document_id": doc_by_path.get(f"pages/researchers/{path.name}"),
            "profile_url": INVESTIGATION_URL + path.name,
        }
    profile_slug_by_name_key = {name_key(person["name"]): slug for slug, person in researchers.items()}
    for lab_slug, member_name_keys in lab_member_name_keys.items():
        for member_name_key in member_name_keys:
            profile_slug = profile_slug_by_name_key.get(member_name_key)
            if profile_slug is not None:
                memberships[profile_slug].add(lab_slug)

    persons_by_key: dict[str, dict[str, Any]] = {}
    profile_person_key: dict[str, str] = {}
    for slug, researcher in researchers.items():
        key = name_key(researcher["name"])
        profile_person_key[slug] = key
        persons_by_key.setdefault(key, {
            "display_name": researcher["name"], "name_key": key, "email": None,
            "profile_url": researcher["profile_url"], "source_document_id": researcher["source_document_id"],
            "reviewed_identity": False, "profile_slug": slug,
        })

    details: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "details" / "thesis").glob("*.html"), key=lambda item: int(item.stem)):
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
    data_issues: list[dict[str, Any]] = []

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
                role_label = next((cell for cell in cells if cell.casefold() in {"director 1", "director 2"}), "")
                role = {"director 1": "primary_advisor", "director 2": "co_advisor"}.get(role_label.casefold(), "unknown")
                thesis_advisors[thesis_id].append({"person_key": person_key, "role": role, "source_document_id": source_id})
                tab1 = page.rfind('id="tab1"', 0, match.start())
                tab2 = page.rfind('id="tab2"', 0, match.start())
                if tab1 > tab2:
                    thesis_status[thesis_id] = "graduated"

            publication_id = form_id(row, "id_pub")
            if publication_id and cells:
                title = cells[0]
                surrounding = page[:match.start()]
                last_heading = list(re.finditer(r"<h3\b[^>]*>(.*?)</h3>", surrounding, re.IGNORECASE | re.DOTALL))
                heading = text_of(last_heading[-1].group(1)).casefold() if last_heading else ""
                kind = "conference" if "congreso" in heading else "publication"
                date_text = cells[2] if len(cells) > 2 else ""
                year_match = re.search(r"\b(?:19|20)\d{2}\b", date_text)
                venue_name = cells[1] if len(cells) > 1 else None
                candidate = {
                    "id": publication_id, "title": title, "kind": kind,
                    "year": int(year_match.group(0)) if year_match else None,
                    "venue": venue_name or None, "source_document_id": source_id,
                }
                existing = products.get(publication_id)
                if existing is None:
                    products[publication_id] = candidate
                elif existing["title"] != title:
                    data_issues.append({"entity_type": "research_product", "entity_key": publication_id,
                                        "code": "conflicting_titles_across_profiles", "detail": f"{existing['title']} | {title}",
                                        "source_document_id": source_id})
                product_authors[publication_id].add(person_key)

            project_id = form_id(row, "id_proy")
            if project_id and cells:
                title = cells[0]
                role = cells[1] if len(cells) > 1 else ""
                before = page[:match.start()]
                preceding_buttons = list(re.finditer(r"<button\b[^>]*>(.*?)</button>", before, re.IGNORECASE | re.DOTALL))
                year_match = re.search(r"\b(?:19|20)\d{2}\b", text_of(preceding_buttons[-1].group(1))) if preceding_buttons else None
                candidate = {"id": project_id, "title": title,
                             "year": int(year_match.group(0)) if year_match else None,
                             "source_document_id": source_id}
                existing = projects.get(project_id)
                if existing is None:
                    projects[project_id] = candidate
                elif existing["title"] != title:
                    data_issues.append({"entity_type": "research_project", "entity_key": project_id,
                                        "code": "conflicting_titles_across_profiles", "detail": f"{existing['title']} | {title}",
                                        "source_document_id": source_id})
                project_members[project_id][person_key] = role

    for thesis_id, detail in details.items():
        for committee_name in detail["committee"]:
            key = name_key(committee_name)
            persons_by_key.setdefault(key, {
                "display_name": committee_name, "name_key": key, "email": None,
                "profile_url": None, "source_document_id": detail["source_document_id"],
                "reviewed_identity": False, "profile_slug": None,
            })

    return {
        "run_id": run_id,
        "run": json.loads((run_dir / "run.json").read_text(encoding="utf-8")),
        "documents": documents,
        "document_ids": document_ids,
        "doc_by_path": doc_by_path,
        "line_names": line_names,
        "laboratories": laboratories,
        "researchers": researchers,
        "memberships": memberships,
        "persons": persons_by_key,
        "details": details,
        "thesis_advisors": thesis_advisors,
        "thesis_status": thesis_status,
        "products": products,
        "product_authors": product_authors,
        "projects": projects,
        "project_members": project_members,
        "data_issues": data_issues,
    }


def make_inserts(run_dir: Path, run: dict[str, Any], records: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    data = make_catalog(run_dir, records)
    lines: list[str] = []
    lines.append("INSERT INTO raw.capture_run (id, source_code, started_at, finished_at, extractor_version, notes) OVERRIDING SYSTEM VALUE VALUES (1, 'cic', "
                 + sql_value(data["run"].get("started_at")) + ", " + sql_value(data["run"].get("completed_at"))
                 + ", " + sql_value(data["run"].get("extractor_version", "0.1.0"))
                 + ", " + sql_value("CIC public-site capture; thesis details fetched, publications/projects read from researcher profile rows.") + ");")
    # Source records carry an import timestamp; original site observation dates
    # remain the exact per-response capture times from the manifest.
    for document in data["documents"]:
        fields = dict(document)
        if fields["form_data"] is not None:
            fields["form_data"] = fields["form_data"]
        lines.append(insert_sql("raw.source_document", fields, {"form_data"}))

    org_id = 1
    lines.append(insert_sql("catalog.organization", {"id": org_id, "name": "Instituto Politécnico Nacional (IPN)",
                                                       "organization_type": "ipn", "source_document_id": None}))
    research_line_ids = {name: index for index, name in enumerate(data["line_names"].values(), start=1)}
    for line_id, name in enumerate(data["line_names"].values(), start=1):
        index_doc = data["doc_by_path"].get("pages/index/laboratories.php")
        lines.append(insert_sql("catalog.research_line", {"id": line_id, "name": name, "source_document_id": index_doc}))

    lab_ids = {slug: index for index, slug in enumerate(sorted(data["laboratories"]), start=1)}
    for slug, lab in sorted(data["laboratories"].items()):
        lab_id = lab_ids[slug]
        lines.append(insert_sql("catalog.laboratory", {"id": lab_id, "name": lab["name"], "cic_slug": slug,
                                                         "organization_id": org_id, "is_external": False,
                                                         "source_document_id": lab["source_document_id"]}))
        if lab["line"] in research_line_ids:
            lines.append(insert_sql("catalog.laboratory_research_line", {
                "laboratory_id": lab_id, "research_line_id": research_line_ids[lab["line"]],
                "source_document_id": data["doc_by_path"].get("pages/index/laboratories.php"),
            }))

    programs: dict[str, dict[str, Any]] = {}
    for detail in data["details"].values():
        code = detail["program_code"]
        if code:
            label = detail["program_name"] or code
            level = "doctorate" if "doctorado" in label.casefold() or code.upper().startswith("D") else "masters"
            programs.setdefault(code, {"name": label, "level": level, "source_document_id": detail["source_document_id"]})
    for code, program in sorted(programs.items()):
        lines.append(insert_sql("catalog.program", {"code": code, **program}))

    person_ids = {key: index for index, key in enumerate(sorted(data["persons"]), start=1)}
    for key, person in sorted(data["persons"].items()):
        person_id = person_ids[key]
        lines.append(insert_sql("catalog.person", {
            "id": person_id, "display_name": person["display_name"], "name_key": person["name_key"],
            "email": None, "profile_url": person["profile_url"],
            "source_document_id": person["source_document_id"], "reviewed_identity": False,
        }))
        if person["profile_slug"]:
            lines.append(insert_sql("catalog.person_source_identity", {
                "source_code": "cic", "source_key": f"profile:{person['profile_slug']}",
                "person_id": person_id, "source_document_id": person["source_document_id"],
            }))

    affiliation_ids: dict[str, list[int]] = defaultdict(list)
    profile_slugs_by_person_key: dict[str, list[str]] = defaultdict(list)
    for profile_slug, researcher in data["researchers"].items():
        profile_slugs_by_person_key[name_key(researcher["name"])].append(profile_slug)
    affiliation_id = 1
    for profile_slug, lab_slugs in sorted(data["memberships"].items()):
        person_id = person_ids[data["researchers"][profile_slug]["name_key"]] if "name_key" in data["researchers"][profile_slug] else person_ids[name_key(data["researchers"][profile_slug]["name"])]
        for lab_slug in sorted(lab_slugs):
            source_id = data["laboratories"][lab_slug]["source_document_id"]
            lines.append(insert_sql("catalog.person_affiliation", {
                "id": affiliation_id, "person_id": person_id, "organization_id": org_id,
                "laboratory_id": lab_ids[lab_slug], "role": "researcher", "is_primary": False,
                "valid_from": None, "valid_to": None, "source_document_id": source_id,
            }))
            affiliation_ids[profile_slug].append(lab_ids[lab_slug])
            affiliation_id += 1

    thesis_ids = {thesis_key: index for index, thesis_key in enumerate(sorted(data["details"], key=int), start=1)}
    advisor_rows_by_thesis: dict[str, list[dict[str, Any]]] = data["thesis_advisors"]
    for external_thesis_id in sorted(set(data["details"]) | set(advisor_rows_by_thesis), key=int):
        detail = data["details"].get(external_thesis_id)
        if not detail:
            continue
        thesis_id = thesis_ids[external_thesis_id]
        status = data["thesis_status"].get(external_thesis_id, "unknown")
        lines.append(insert_sql("catalog.thesis", {
            "id": thesis_id, "title": detail["title"], "program_code": detail["program_code"],
            "completion_status": status, "completion_year": None, "url": None,
            "source_document_id": detail["source_document_id"],
        }))
        lines.append(insert_sql("catalog.thesis_source_identity", {
            "source_code": "cic", "source_key": external_thesis_id, "thesis_id": thesis_id,
            "source_document_id": detail["source_document_id"],
        }))
        advisor_rows = advisor_rows_by_thesis.get(external_thesis_id, [])
        primary_count = sum(row["role"] == "primary_advisor" for row in advisor_rows)
        if primary_count > 1:
            lines.append(insert_sql("catalog.data_issue", {
                "id": 1_000_000 + thesis_id, "entity_type": "thesis", "entity_key": external_thesis_id,
                "code": "multiple_director_1_profiles", "detail": f"{primary_count} CIC profiles list this thesis as Director 1; review assignment.",
                "source_document_id": detail["source_document_id"],
            }))
        seen_participants: set[tuple[int, str]] = set()
        for row in advisor_rows:
            person_id = person_ids[row["person_key"]]
            role = row["role"]
            if role == "primary_advisor" and primary_count > 1:
                role = "unknown"
            candidate_labs = sorted({
                lab_id
                for profile_slug in profile_slugs_by_person_key.get(row["person_key"], [])
                for lab_id in affiliation_ids.get(profile_slug, [])
            })
            assigned_lab = candidate_labs[0] if role == "primary_advisor" and len(candidate_labs) == 1 else None
            if role == "primary_advisor" and len(candidate_labs) != 1:
                lines.append(insert_sql("catalog.data_issue", {
                    "id": 2_000_000 + thesis_id, "entity_type": "thesis", "entity_key": external_thesis_id,
                    "code": "primary_lab_not_unambiguous", "detail": "CIC lists Director 1, but the profile-to-lab mapping does not identify exactly one lab.",
                    "source_document_id": row["source_document_id"],
                }))
            participant_key = (person_id, role)
            if participant_key in seen_participants:
                continue
            seen_participants.add(participant_key)
            lines.append(insert_sql("catalog.thesis_participant", {
                "thesis_id": thesis_id, "person_id": person_id, "role": role,
                "participant_order": None, "assigned_laboratory_id": assigned_lab,
                "source_document_id": row["source_document_id"],
            }))

        committee_seen: set[int] = set()
        for order, committee_name in enumerate(detail["committee"], start=1):
            person_id = person_ids[name_key(committee_name)]
            if person_id in committee_seen:
                continue
            committee_seen.add(person_id)
            participant_key = (person_id, "committee_member")
            if participant_key not in seen_participants:
                seen_participants.add(participant_key)
                lines.append(insert_sql("catalog.thesis_participant", {
                    "thesis_id": thesis_id, "person_id": person_id, "role": "committee_member",
                    "participant_order": order, "assigned_laboratory_id": None,
                    "source_document_id": detail["source_document_id"],
                }))

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
        lines.append(insert_sql("catalog.research_product", {
            "id": internal_id, "kind": product["kind"], "title": product["title"],
            "description": None, "year": product["year"], "venue_id": venue_id,
            "source_document_id": product["source_document_id"],
        }))
        lines.append(insert_sql("catalog.product_source_identity", {
            "source_code": "cic", "source_key": product_id, "product_id": internal_id,
            "source_document_id": product["source_document_id"],
        }))
        for author_key in sorted(data["product_authors"].get(product_id, [])):
            author_id = person_ids[author_key]
            lines.append(insert_sql("catalog.product_author", {
                "product_id": internal_id, "person_id": author_id, "author_order": None,
                "source_document_id": product["source_document_id"],
            }))
            for slug, researcher in data["researchers"].items():
                if name_key(researcher["name"]) == author_key:
                    for lab_slug in sorted(data["memberships"].get(slug, [])):
                        lines.append(insert_sql("catalog.product_laboratory", {
                            "product_id": internal_id, "laboratory_id": lab_ids[lab_slug],
                            "source_document_id": data["laboratories"][lab_slug]["source_document_id"],
                        }))

    for project_id, project in sorted(data["projects"].items(), key=lambda item: int(item[0])):
        internal_id = int(project_id)
        lines.append(insert_sql("catalog.research_project", {
            "id": internal_id, "title": project["title"], "description": None,
            "year": project["year"], "source_document_id": project["source_document_id"],
        }))
        lines.append(insert_sql("catalog.project_source_identity", {
            "source_code": "cic", "source_key": project_id, "project_id": internal_id,
            "source_document_id": project["source_document_id"],
        }))
        for member_key, role in sorted(data["project_members"].get(project_id, {}).items()):
            person_id = person_ids[member_key]
            lines.append(insert_sql("catalog.project_member", {
                "project_id": internal_id, "person_id": person_id, "role": role or None,
                "source_document_id": project["source_document_id"],
            }))
            for slug, researcher in data["researchers"].items():
                if name_key(researcher["name"]) == member_key:
                    for lab_slug in sorted(data["memberships"].get(slug, [])):
                        lines.append(insert_sql("catalog.project_laboratory", {
                            "project_id": internal_id, "laboratory_id": lab_ids[lab_slug],
                            "source_document_id": data["laboratories"][lab_slug]["source_document_id"],
                        }))

    for issue_index, issue in enumerate(data["data_issues"], start=1):
        lines.append(insert_sql("catalog.data_issue", {"id": 3_000_000 + issue_index, **issue}))

    for table in ["raw.capture_run", "raw.source_document", "catalog.organization", "catalog.research_line",
                  "catalog.laboratory", "catalog.person", "catalog.person_affiliation",
                  "catalog.thesis", "catalog.research_product", "catalog.research_project", "catalog.venue",
                  "catalog.data_issue", "catalog.embedding"]:
        schema_name, table_name = table.split(".")
        lines.append(
            "SELECT setval(pg_get_serial_sequence(" + sql_string(f"{schema_name}.{table_name}") +
            ", 'id'), COALESCE(MAX(id), 1), COUNT(*) > 0) FROM " + table + ";"
        )

    counts = {
        "capture_documents": len(records), "laboratories": len(data["laboratories"]),
        "researchers": len(data["researchers"]), "researchers_with_lab": len(data["memberships"]),
        "theses": len(data["details"]), "theses_with_advisor_profile": len(data["thesis_advisors"]),
        "publications_and_conferences": len(data["products"]), "projects": len(data["projects"]),
        "committee_people": len(data["persons"]) - len(data["researchers"]),
        "source_conflict_issues": len(data["data_issues"]),
        "unresolved_primary_lab_assignments": sum(
            1 for thesis_id, advisors in data["thesis_advisors"].items()
            if any(advisor["role"] == "primary_advisor" for advisor in advisors)
            and not any(
                len({lab_id for slug in profile_slugs_by_person_key.get(advisor["person_key"], [])
                     for lab_id in affiliation_ids.get(slug, [])}) == 1
                for advisor in advisors if advisor["role"] == "primary_advisor"
            )
        ),
    }
    counts["total_catalog_data_issues"] = counts["source_conflict_issues"] + counts["unresolved_primary_lab_assignments"]
    return lines, counts


def build(run_dir: Path, schema: Path, export_path: Path, database_name: str) -> dict[str, int]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database_name):
        raise ValueError("Database name must be a simple PostgreSQL identifier.")
    run, records = load_manifest(run_dir)
    inserts, counts = make_inserts(run_dir, run, records)
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
        schema_sql,
        "-- Parsed CIC records and source-provenance manifest.",
        *inserts,
        "-- Export counts: " + json.dumps(counts, ensure_ascii=False, sort_keys=True),
        "",
    ]
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text("\n".join(output), encoding="utf-8")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--database", default="cic_scrape")
    args = parser.parse_args()
    counts = build(args.run_dir, args.schema, args.output, args.database)
    print(f"PostgreSQL export: {args.output}")
    print(json.dumps(counts, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
