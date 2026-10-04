"""Opens every page of the app with demo data and fails if any of them shows an error."""
import pytest
from streamlit.testing.v1 import AppTest

PAGES = ["page_dashboard", "page_followup", "page_live", "page_insights", "page_members", "page_reports", "page_sql"]


def _page(name):
    import attendance as A
    getattr(A, name)()


def test_app_starts():
    at = AppTest.from_file("app.py", default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]


@pytest.mark.parametrize("name", PAGES)
def test_page_opens_without_errors(name):
    at = AppTest.from_function(_page, args=(name,), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]
