from __future__ import annotations

from conftest import RunBuilder

from build_comparison import capture_gaps, extract_cic, parse_pg_copy


def test_copy_rows_split_only_on_newline(tmp_path) -> None:
    dump = tmp_path / "dump.sql"
    dump.write_text(
        "COPY public.theses (id, title) FROM stdin;\n"
        "1\tUna línea\u2028con separador\n"
        "2\tSegunda\\nfila\n"
        "\\.\n"
        "COPY public.students (id) FROM stdin;\n"
        "\\.\n",
        encoding="utf-8",
    )
    tables = parse_pg_copy(dump)
    assert tables["theses"] == [
        {"id": "1", "title": "Una línea\u2028con separador"},
        {"id": "2", "title": "Segunda\nfila"},
    ]
    assert tables["students"] == []


def test_partial_capture_is_not_compared(run_builder: RunBuilder) -> None:
    run_builder.researcher("ana-ruiz", "Ana Ruiz")
    run_builder.run.pop("completed_at")
    run_dir = run_builder.write()
    assert capture_gaps(run_dir, extract_cic(run_dir)) == [
        "the capture did not finish (run.json has no completed_at)",
        "no laboratory pages",
        "no parsed thesis details",
    ]


def test_unresolved_failed_request_is_not_compared(run_builder: RunBuilder) -> None:
    run_builder.researcher("ana-ruiz", "Ana Ruiz", graduated=[("10", "Director 1")])
    run_builder.lab("lab_a", "Laboratorio A", ["ana-ruiz"])
    run_builder.thesis("11", [])
    run_builder.failure(
        "https://www.cic.ipn.mx/controller/control.php", [{"name": "id_tet", "value": "10"}]
    )
    run_dir = run_builder.write()
    [gap] = capture_gaps(run_dir, extract_cic(run_dir))
    assert gap.startswith("1 request(s) never archived a response")


def test_thesis_details_are_only_required_when_requested(run_builder: RunBuilder) -> None:
    run_builder.researcher("ana-ruiz", "Ana Ruiz")
    run_builder.lab("lab_a", "Laboratorio A", ["ana-ruiz"])
    run_builder.run["detail_kinds"] = ["project"]
    run_dir = run_builder.write()
    assert capture_gaps(run_dir, extract_cic(run_dir)) == []
