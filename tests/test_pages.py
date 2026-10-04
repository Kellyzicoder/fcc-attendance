"""Opens every page of the app with demo data and fails if any of them shows an error."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")

PAGES = ["page_dashboard", "page_followup", "page_live", "page_insights", "page_members", "page_reports", "page_sql",
         "page_overview"]


def _page(name):
    import attendance as A
    getattr(A, name)()


def test_app_starts():
    at = AppTest.from_file(APP, default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]


@pytest.mark.parametrize("name", PAGES)
def test_page_opens_without_errors(name):
    at = AppTest.from_function(_page, args=(name,), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def test_the_bishop_only_gets_the_overview():
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["demo_as"] = "Bishop"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.checkbox  # no check-in list, no names to tick


def test_a_branch_team_cannot_open_admin_pages():
    def script():
        import streamlit as st
        import attendance as A
        st.session_state["demo_as"] = "Branch team"
        A.page_members()
    at = AppTest.from_function(script, default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.warning and not at.tabs


def test_a_church_admin_gets_members_but_not_reports_or_sql():
    def script():
        import streamlit as st
        import attendance as A
        st.session_state["demo_as"] = "Church admin"
        A.page_members()
        A.page_sql()
    at = AppTest.from_function(script, default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.tabs and len(at.warning) == 1
