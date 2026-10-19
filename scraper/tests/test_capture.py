from __future__ import annotations

from capture import allowed_url, detail_requests, parse_page


def test_only_https_cic_urls_are_allowed() -> None:
    assert allowed_url("https://www.cic.ipn.mx/views/investigation/lab_ia.php")
    assert allowed_url("https://cic.ipn.mx/")
    assert not allowed_url("http://www.cic.ipn.mx/")
    assert not allowed_url("https://example.com/views/investigation/lab_ia.php")


def test_detail_forms_keep_only_research_fields() -> None:
    page = parse_page(
        b'<form method="post" action="/controller/control.php">'
        b'<input name="id_tet" value="724"><input name="thesisResearcher" value="">'
        b'<input name="name" value="Student Name"></form>'
        # A student-directory form has none of the detail fields.
        b'<form method="post" action="/controller/control.php">'
        b'<input name="id_alumno" value="9"></form>'
    )
    [(kind, item_id, action, fields)] = detail_requests(page, "https://www.cic.ipn.mx/views/x.php")
    assert (kind, item_id) == ("thesis", "724")
    assert action == "https://www.cic.ipn.mx/controller/control.php"
    assert fields == [("id_tet", "724"), ("thesisResearcher", "")]
