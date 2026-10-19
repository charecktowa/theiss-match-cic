#!/usr/bin/env python3
"""Archive public CIC pages and their research-item detail responses.

The archive is an input snapshot, not the cleaned application catalog. It is
written under scraper/data/cic_raw/ (ignored by git) with a JSONL provenance manifest.
Only public GET pages and the site's own public detail forms are requested.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import sys
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

BASE = "https://www.cic.ipn.mx"
EXTRACTOR_VERSION = "0.1.0"
INVESTIGATION = f"{BASE}/views/investigation/"
SEEDS = [
    f"{INVESTIGATION}laboratories.php",
    f"{INVESTIGATION}researchers.php",
]
LIST_PAGES = {
    "laboratories.php",
    "researchers.php",
    "research_board.php",
    "research_projects.php",
    "patents.php",
}
DETAIL_FIELDS = {
    "id_pub": "publication",
    "id_proy": "project",
    "id_tet": "thesis",
}
USER_AGENT = "ThesisMatchCICResearch/0.1 (+https://github.com/charecktowa/theiss-match-cic)"
ALLOWED_HOSTS = {"www.cic.ipn.mx", "cic.ipn.mx"}
# Transient network errors (the October 2026 run lost one thesis to a TLS
# handshake timeout) are retried; HTTP error responses are recorded as they are.
RETRIES = 2


def allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS


class PageParser(HTMLParser):
    """Collect same-page links and the fields submitted by detail forms."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: set[str] = set()
        self.forms: list[dict[str, object]] = []
        self._form: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "a" and data.get("href"):
            self.links.add(str(data["href"]))
        if tag == "form":
            self._form = {
                "action": data.get("action", ""),
                "method": (data.get("method") or "get").lower(),
                "fields": [],
            }
            self.forms.append(self._form)
        elif self._form is not None and tag in {"input", "button", "select", "textarea"}:
            name = data.get("name")
            if name:
                fields = self._form["fields"]
                assert isinstance(fields, list)
                fields.append((str(name), data.get("value") or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None


class Capture:
    def __init__(self, output: Path, delay: float, retries: int = RETRIES) -> None:
        self.output = output
        self.delay = delay
        self.retries = retries
        self.manifest = (output / "manifest.jsonl").open("a", encoding="utf-8")
        self.seen: set[str] = set()
        self.requests = 0
        self.failures = 0
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def close(self) -> None:
        self.manifest.close()

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        fields: list[tuple[str, str]] | None = None,
        referrer: str | None = None,
        relative_path: str,
    ) -> bytes | None:
        parts = urlparse(url)
        url = urlunparse(
            parts._replace(
                path=quote(parts.path, safe="/%:@-._~!$&'()*+,;=%"),
                query=quote(parts.query, safe="/?@-._~!$&'()*+,;=:%"),
            )
        )
        if not allowed_url(url):
            return None
        body = urlencode(fields or []).encode("utf-8") if method == "POST" else None
        key = f"{method}:{url}:{body!r}"
        if key in self.seen:
            return None
        self.seen.add(key)

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        }
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if referrer:
            headers["Referer"] = referrer
        request = Request(
            url,
            data=body,
            method=method,
            headers=headers,
        )
        for attempt in range(1, self.retries + 2):
            status = None
            final_url = None
            content_type = None
            error = None
            content = None
            captured_at = datetime.now(UTC).isoformat()
            try:
                with self.opener.open(request, timeout=40) as response:
                    status = response.status
                    final_url = response.geturl()
                    content_type = response.headers.get("Content-Type")
                    # urllib follows redirects on its own, so the destination is
                    # checked here, before its body is read or archived.
                    if allowed_url(final_url):
                        content = response.read()
                    else:
                        error = f"redirected outside CIC: {final_url}"
            except HTTPError as exc:
                status = exc.code
                final_url = exc.geturl()
                content_type = exc.headers.get("Content-Type") if exc.headers else None
                error = f"{type(exc).__name__}: {exc}"
            # URLError and TimeoutError are OSError subclasses, so OSError covers
            # them; HTTPException covers a body cut short (IncompleteRead).
            except (OSError, http.client.HTTPException, UnicodeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt <= self.retries:
                    time.sleep(self.delay * 2**attempt)
                    continue
            break
        self.requests += 1
        if error is not None:
            self.failures += 1
        time.sleep(self.delay)

        record: dict[str, object] = {
            "captured_at": captured_at,
            "stored_at": datetime.now(UTC).isoformat(),
            "attempts": attempt,
            "method": method,
            "request_key": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "url": url,
            "final_url": final_url,
            "form": [{"name": name, "value": value} for name, value in fields] if fields else None,
            "referrer": referrer,
            "status": status,
            "content_type": content_type,
            "error": error,
            "path": None,
            "sha256": None,
        }
        if content is not None:
            path = self.output / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            record["path"] = relative_path
            record["sha256"] = hashlib.sha256(content).hexdigest()
        self.manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.manifest.flush()
        if self.requests % 25 == 0:
            print(f"captured {self.requests} requests ({self.failures} failed)", flush=True)
        return content


def parse_page(content: bytes) -> PageParser:
    parser = PageParser()
    parser.feed(content.decode("utf-8", "replace"))
    return parser


def page_slug(url: str) -> str:
    name = Path(urlparse(url).path).name
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "page.html"


def page_urls(parser: PageParser, base_url: str, *, laboratories: bool) -> set[str]:
    found: set[str] = set()
    for href in parser.links:
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        name = Path(parsed.path).name
        if parsed.hostname not in ALLOWED_HOSTS:
            continue
        if not parsed.path.startswith("/views/investigation/") or not name.endswith(".php"):
            continue
        if name in LIST_PAGES:
            continue
        if laboratories != name.startswith("lab_"):
            continue
        found.add(f"{INVESTIGATION}{name}")
    return found


def detail_requests(
    parser: PageParser, page_url: str
) -> list[tuple[str, str, str, list[tuple[str, str]]]]:
    requests: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    for form in parser.forms:
        if form.get("method") != "post":
            continue
        fields = form.get("fields", [])
        if not isinstance(fields, list):
            continue
        field_names = [name for name, _ in fields]
        target = next((name for name in DETAIL_FIELDS if name in field_names), None)
        if target is None:
            continue
        # Do not collect student-directory forms: student identity is not needed
        # to recommend a laboratory or advisor.
        form_fields = [
            (str(name), str(value))
            for name, value in fields
            if name in {target, "projectResearcher", "publicationResearcher", "thesisResearcher"}
        ]
        action = urljoin(page_url, str(form.get("action") or ""))
        item_id = next((value for name, value in form_fields if name == target), "")
        if not item_id.isdigit():
            continue
        kind = DETAIL_FIELDS[target]
        requests.append((kind, item_id, action, form_fields))
    return requests


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--delay", type=float, default=1.0, help="seconds between requests (default: 1)"
    )
    cli.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "cic_raw" / "runs",
    )
    cli.add_argument(
        "--retries",
        type=int,
        default=RETRIES,
        help=f"retries after a network error (default: {RETRIES})",
    )
    cli.add_argument(
        "--details-limit", type=int, default=None, help="limit detail POSTs for a pilot capture"
    )
    cli.add_argument(
        "--kinds",
        default="thesis",
        help=(
            "comma-separated detail types to fetch "
            "(default: thesis; choices: thesis,project,publication)"
        ),
    )
    args = cli.parse_args()
    if args.delay < 0.5:
        cli.error("--delay must be at least 0.5 seconds")
    if args.retries < 0:
        cli.error("--retries must not be negative")
    selected_kinds = {kind.strip() for kind in args.kinds.split(",") if kind.strip()}
    if not selected_kinds or selected_kinds - set(DETAIL_FIELDS.values()):
        cli.error("--kinds must contain thesis, project, and/or publication")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output / run_id
    output.mkdir(parents=True, exist_ok=False)
    capture = Capture(output, args.delay, args.retries)
    (output / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "extractor_version": EXTRACTOR_VERSION,
                "started_at": datetime.now(UTC).isoformat(),
                "base_url": BASE,
                "user_agent": USER_AGENT,
                "delay_seconds": args.delay,
                "retries": args.retries,
                "detail_limit": args.details_limit,
                "detail_kinds": sorted(selected_kinds),
                "scope": (
                    "public labs, researchers, and publication/project/thesis detail pages; "
                    "student detail pages excluded"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        seed_parsers: dict[str, PageParser] = {}
        for url in SEEDS:
            content = capture.fetch(url, relative_path=f"pages/index/{page_slug(url)}")
            if content:
                seed_parsers[url] = parse_page(content)

        lab_pages: set[str] = set()
        researcher_pages: set[str] = set()
        for url, parser in seed_parsers.items():
            lab_pages |= page_urls(parser, url, laboratories=True)
            researcher_pages |= page_urls(parser, url, laboratories=False)

        # Also discover researchers linked from laboratory membership cards.
        page_parsers: dict[str, PageParser] = {}
        for url in sorted(lab_pages | researcher_pages):
            section = "laboratories" if page_slug(url).startswith("lab_") else "researchers"
            content = capture.fetch(url, relative_path=f"pages/{section}/{page_slug(url)}")
            if content:
                page_parsers[url] = parse_page(content)
        for url, parser in page_parsers.items():
            researcher_pages |= page_urls(parser, url, laboratories=False)
        new_researchers = researcher_pages - set(page_parsers)
        for url in sorted(new_researchers):
            content = capture.fetch(url, relative_path=f"pages/researchers/{page_slug(url)}")
            if content:
                page_parsers[url] = parse_page(content)

        detail_jobs: dict[tuple[str, str], tuple[str, str, list[tuple[str, str]]]] = {}
        for url, parser in page_parsers.items():
            for kind, item_id, action, fields in detail_requests(parser, url):
                if kind in selected_kinds:
                    detail_jobs.setdefault((kind, item_id), (action, url, fields))
        jobs = sorted(detail_jobs.items())
        if args.details_limit is not None:
            jobs = jobs[: max(0, args.details_limit)]
        print(
            f"discovered {len(lab_pages)} laboratories, "
            f"{len(researcher_pages)} researcher profiles, and "
            f"{len(detail_jobs)} unique research detail forms; capturing {len(jobs)}",
            flush=True,
        )
        for (kind, item_id), (endpoint, referrer, fields) in jobs:
            capture.fetch(
                endpoint,
                method="POST",
                fields=fields,
                referrer=referrer,
                relative_path=f"details/{kind}/{item_id}.html",
            )
    finally:
        capture.close()

    run_info_path = output / "run.json"
    run_info = json.loads(run_info_path.read_text(encoding="utf-8"))
    run_info.update(
        {
            "completed_at": datetime.now(UTC).isoformat(),
            "request_count": capture.requests,
            "failure_count": capture.failures,
            "laboratory_pages": len(lab_pages),
            "researcher_pages": len(researcher_pages),
            "unique_detail_forms_discovered": len(detail_jobs),
            "detail_forms_selected": len(jobs),
        }
    )
    run_info_path.write_text(
        json.dumps(run_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"finished: {output} ({capture.requests} requests, {capture.failures} failed)",
        flush=True,
    )
    return 1 if capture.failures else 0


if __name__ == "__main__":
    sys.exit(main())
