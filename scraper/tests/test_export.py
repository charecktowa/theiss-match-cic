from __future__ import annotations

import pytest
from conftest import RunBuilder

from build_postgres_export import build, load_manifest, make_catalog, name_key


def catalog(run_dir):
    run, records = load_manifest(run_dir)
    return make_catalog(run_dir, run, records)


def issues(data, code):
    return [issue for issue in data["data_issues"] if issue["code"] == code]


def test_profiles_that_share_a_name_stay_two_people(run_builder: RunBuilder) -> None:
    run_builder.researcher("juan-perez", "Juan Pérez", graduated=[("10", "Director 1")])
    run_builder.researcher("juan-perez-lopez", "Juan Pérez")
    run_builder.lab("lab_a", "Laboratorio A", ["juan-perez"])
    run_builder.thesis("10", ["Juan Pérez"])
    data = catalog(run_builder.write())

    profiles = [key for key in data["persons"] if key.startswith("profile:")]
    assert sorted(profiles) == ["profile:juan-perez", "profile:juan-perez-lopez"]
    # The lab link and the committee name each match both profiles, so neither
    # is attached to either one; both are reported instead.
    assert data["memberships"] == {}
    assert len(issues(data, "ambiguous_member_name")) == 1
    assert data["committee_keys"]["10"] == [f"name:{name_key('Juan Pérez')}"]
    assert len(issues(data, "ambiguous_committee_name")) == 1


def test_committee_name_variant_joins_its_profile_as_an_alias(run_builder: RunBuilder) -> None:
    run_builder.researcher("maria-lopez", "María López", graduated=[("11", "Director 1")])
    run_builder.lab("lab_a", "Laboratorio A", ["maria-lopez"])
    run_builder.thesis("11", ["Maria Lopez", "Pedro Externo -"])
    data = catalog(run_builder.write())

    assert data["memberships"] == {"maria-lopez": {"lab_a"}}
    keys = data["committee_keys"]["11"]
    assert keys[0] == "profile:maria-lopez"
    assert {alias[:2] for alias in data["aliases"]} == {("profile:maria-lopez", "Maria Lopez")}
    # A stray trailing dash in a committee entry is not part of the name.
    assert data["persons"][keys[1]]["display_name"] == "Pedro Externo"


def test_disagreeing_profiles_are_reported_per_field(run_builder: RunBuilder) -> None:
    run_builder.researcher("ana-ruiz", "Ana Ruiz", products=[("100", "Paper", "Venue", "2020")])
    run_builder.researcher("luis-mora", "Luis Mora", products=[("100", "Paper", "Venue", "2021")])
    data = catalog(run_builder.write())

    assert data["products"]["100"]["year"] == 2020
    assert data["product_authors"]["100"] == {"profile:ana-ruiz", "profile:luis-mora"}
    [issue] = data["data_issues"]
    assert issue["code"] == "conflicting_years_across_profiles"
    assert issue["detail"] == "2020 | 2021"


def test_page_that_is_not_a_researcher_profile_is_skipped(run_builder: RunBuilder) -> None:
    run_builder.page("pages/researchers/directorio.php", "<title>Directorio</title>")
    data = catalog(run_builder.write())

    assert data["researchers"] == {}
    assert issues(data, "not_a_researcher_profile")[0]["entity_key"] == (
        "pages/researchers/directorio.php"
    )


def test_profile_url_is_the_requested_url(run_builder: RunBuilder) -> None:
    # The archive replaces non-ASCII characters in file names.
    url = "https://www.cic.ipn.mx/views/investigation/ana-salda%C3%B1a.php"
    run_builder.page(
        "pages/researchers/ana-salda_C3_B1a.php", "<title>Investigadora Ana</title>", url
    )
    data = catalog(run_builder.write())

    assert data["persons"]["profile:ana-salda_C3_B1a"]["profile_url"] == url


def incomplete_run(run_builder: RunBuilder) -> RunBuilder:
    run_builder.researcher(
        "ana-ruiz", "Ana Ruiz", graduated=[("10", "Director 1"), ("12", "Director 1")]
    )
    run_builder.thesis("10", [])
    run_builder.failure(
        "https://www.cic.ipn.mx/controller/control.php", [{"name": "id_tet", "value": "12"}]
    )
    return run_builder


def test_incomplete_capture_is_refused(run_builder: RunBuilder, tmp_path) -> None:
    run_dir = incomplete_run(run_builder).write()
    schema = tmp_path / "schema.sql"
    schema.write_text("")
    with pytest.raises(SystemExit, match="1 request"):
        build(run_dir, schema, tmp_path / "out.sql", "cic_scrape")
    assert not (tmp_path / "out.sql").exists()


def test_incomplete_capture_can_be_exported_with_its_gaps(
    run_builder: RunBuilder, tmp_path
) -> None:
    run_dir = incomplete_run(run_builder).write()
    schema = tmp_path / "schema.sql"
    schema.write_text("")
    build(run_dir, schema, tmp_path / "out.sql", "cic_scrape", allow_incomplete=True)
    export = (tmp_path / "out.sql").read_text(encoding="utf-8")

    assert "Incomplete capture: 1 request(s) never archived a response" in export
    assert "thesis_detail_missing" in export
    assert [issue["entity_key"] for issue in issues(catalog(run_dir), "thesis_detail_missing")] == [
        "12"
    ]


def test_failure_retried_later_is_resolved(run_builder: RunBuilder, tmp_path) -> None:
    run_builder.researcher("ana-ruiz", "Ana Ruiz", graduated=[("12", "Director 1")])
    form = [{"name": "id_tet", "value": "12"}]
    run_builder.failure("https://www.cic.ipn.mx/controller/control.php", form)
    run_builder.thesis("12", [])
    # The retry is the same request, so it carries the failure's request key.
    run_builder.records[-1]["request_key"] = run_builder.records[-2]["request_key"]
    run_dir = run_builder.write()
    schema = tmp_path / "schema.sql"
    schema.write_text("")
    build(run_dir, schema, tmp_path / "out.sql", "cic_scrape")
    assert "Incomplete capture" not in (tmp_path / "out.sql").read_text(encoding="utf-8")


def test_database_name_must_be_lowercase(run_builder: RunBuilder, tmp_path) -> None:
    with pytest.raises(ValueError, match="lowercase"):
        build(run_builder.write(), tmp_path / "schema.sql", tmp_path / "out.sql", "CicScrape")
