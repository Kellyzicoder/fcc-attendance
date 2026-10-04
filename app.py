"""FCC Attendance Tracker — Streamlit app for Favourite Child Church.

Pages (sidebar): Dashboard (home) · Follow-up & Check-in · Live · Insights; admins also get Members · Reports · SQL.
All data lives in Postgres (Supabase) via `database_url` in Streamlit secrets; see attendance.py.
"""
from pathlib import Path

import streamlit as st

LOGO = Path(__file__).parent / "static" / "logo.png"
st.set_page_config(page_title="FCC Attendance", page_icon=str(LOGO) if LOGO.exists() else "⛪", layout="wide",
                   initial_sidebar_state="expanded")
if LOGO.exists():
    st.logo(str(LOGO), size="large")

# ---------- look & feel (colours from the church logo) ----------
st.html("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root {--card: #141c22; --card-line: rgba(255,255,255,.07); --ink: #e8eef2; --ink-2: #9fb0bd; --ink-3: #6b7c89;
       --green: #2aa686; --blue: #5a8ef0; --amber: #fab219; --red: #d03b3b; --gold: #ffcf00;}
html, body, .stApp, .stMarkdown, [data-testid="stMetric"], [data-testid="stSidebar"] {font-family: 'Inter', system-ui, sans-serif;}
.block-container {padding-top: 3.2rem; padding-bottom: 3rem; max-width: 1480px;}
/* Streamlit's top toolbar: keep the menu, drop the dark strip that covered the banner */
[data-testid="stHeader"] {background: transparent; box-shadow: none;}
[data-testid="stDecoration"] {display: none;}
[data-testid="stSidebar"] {border-right: 1px solid var(--card-line);}

/* live panels re-check the database every few seconds in the background: don't fade them while they do */
div[class*="st-key-live_"] [data-testid="stElementContainer"], div[class*="st-key-live_"] [data-stale] {
       opacity: 1 !important; transition: none !important;}

/* cards: every bordered container made with card() */
div[class*="st-key-card_"] {background: var(--card); border: 1px solid var(--card-line) !important;
       border-radius: 18px !important; padding: .35rem .5rem; box-shadow: 0 8px 24px rgba(0,0,0,.25);}
[data-testid="stMetric"] {background: var(--card); border-radius: 16px;}

/* hero header */
.hero {background: linear-gradient(120deg, #1c3354 0%, #17616a 55%, #1f8a6b 100%); color: #fff;
       border-radius: 18px; padding: 1.1rem 1.6rem; margin-bottom: .6rem; display: flex; gap: 1.2rem; align-items: center;
       border: 1px solid rgba(255,255,255,.08);}
.hero .hero-logo {width: 54px; height: auto; flex: none; filter: drop-shadow(0 2px 6px rgba(0,0,0,.35));}
.hero .hero-text {min-width: 0;}
.hero .eyebrow {font-size: .72rem; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; color: var(--gold);}
.hero h1 {font-size: 1.65rem; font-weight: 800; margin: .1rem 0 0; letter-spacing: -.02em; color: #fff; padding: 0;}
.hero p {margin: .25rem 0 0; opacity: .85; font-size: .93rem;}
.chip {display: inline-block; background: rgba(255,255,255,.14); border: 1px solid rgba(255,255,255,.25);
       border-radius: 999px; padding: .15rem .65rem; margin: .5rem .35rem 0 0; font-size: .78rem; font-weight: 500;}
.live-dot {display: inline-block; width: .55rem; height: .55rem; border-radius: 50%; background: var(--gold);
           margin-right: .5rem; vertical-align: middle; animation: pulse 1.6s infinite;}
@keyframes pulse {0% {box-shadow: 0 0 0 0 rgba(255,207,0,.6);} 70% {box-shadow: 0 0 0 .5rem rgba(255,207,0,0);}
                  100% {box-shadow: 0 0 0 0 rgba(255,207,0,0);}}
@media (max-width: 640px) { .hero {padding: .9rem 1rem; gap: .8rem;} .hero .hero-logo {width: 40px;} .hero h1 {font-size: 1.3rem;} }

/* KPI tiles */
.kpi-grid {display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin: .2rem 0 .4rem;}
@media (max-width: 1000px) { .kpi-grid {grid-template-columns: repeat(2, minmax(0, 1fr));} }
@media (max-width: 520px) { .kpi-grid {grid-template-columns: 1fr;} }
.kpi {background: var(--card); border: 1px solid var(--card-line); border-radius: 18px; padding: 16px 18px;
      box-shadow: 0 8px 24px rgba(0,0,0,.25); min-width: 0;}
.kpi-top {display: flex; align-items: center; gap: 8px; color: var(--ink-2); font-size: .84rem; font-weight: 500;}
.kpi-icon {width: 28px; height: 28px; border-radius: 9px; display: grid; place-items: center; font-size: .95rem;
           background: rgba(42,166,134,.14);}
.kpi-label {white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.kpi-value {font-size: 2.1rem; font-weight: 700; color: var(--ink); letter-spacing: -.03em; margin: .35rem 0 .2rem;
            font-variant-numeric: tabular-nums;}
.kpi-foot {font-size: .8rem; color: var(--ink-2); display: flex; gap: 6px; flex-wrap: wrap; align-items: center;}
.kpi-delta {font-weight: 600; color: var(--ink);} .kpi-delta i {font-style: normal; font-size: .7rem;}
.kpi-delta.up i {color: var(--green);} .kpi-delta.down i {color: #e0605e;} .kpi-delta.flat i {color: var(--ink-3);}
.kpi-sub {color: var(--ink-3);}
.pill {display: inline-flex; align-items: center; gap: 4px; padding: .12rem .55rem; border-radius: 999px;
       font-size: .76rem; font-weight: 600; color: var(--ink); white-space: nowrap;}
.pill.red {background: rgba(208,59,59,.22);} .pill.amber {background: rgba(250,178,25,.20);}
.pill.blue {background: rgba(90,142,240,.22);}

/* follow-up table */
.dash-table {width: 100%; border-collapse: collapse; font-size: .88rem;}
.dash-table th {text-align: left; color: var(--ink-3); font-weight: 500; font-size: .76rem; text-transform: uppercase;
                letter-spacing: .05em; padding: .45rem .4rem; border-bottom: 1px solid var(--card-line);}
.dash-table td {padding: .55rem .4rem; border-bottom: 1px solid var(--card-line); color: var(--ink);}
.dash-table tr:last-child td {border-bottom: 0;} .dash-table .muted {color: var(--ink-2);}
.dot {display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; vertical-align: middle;}
.dot.red {background: var(--red);} .dot.yellow {background: var(--amber);} .dot.ok {background: var(--green);}

/* side panel feeds */
.feed h4 {font-size: .95rem; font-weight: 700; color: var(--ink); margin: .4rem 0 .5rem; padding: 0;}
.feed ul {list-style: none; margin: 0 0 1rem; padding: 0;}
.feed li {display: flex; gap: 10px; align-items: flex-start; padding: .45rem 0; font-size: .86rem; color: var(--ink);
          border-bottom: 1px solid var(--card-line);}
.feed li:last-child {border-bottom: 0;}
.feed li small {color: var(--ink-3); font-size: .75rem;}
.feed-ic {width: 28px; height: 28px; flex: none; border-radius: 50%; display: grid; place-items: center;
          background: rgba(255,255,255,.06); font-size: .85rem;}
.feed-empty {color: var(--ink-3);}
.dash-table td:nth-child(2), .dash-table td:nth-child(3), .dash-table td:nth-child(4) {white-space: nowrap;}

/* check-in names: a grid that reads A to Z across; fewer columns on small screens, one on a phone */
.st-key-ci_grid {display: grid !important; grid-template-columns: repeat(var(--ci-cols, 3), minmax(0, 1fr));
                 gap: .15rem 1rem; align-items: start;}
.st-key-ci_grid > div {width: auto !important; min-width: 0;}
.st-key-ci_grid > div:has(style) {display: none;}
@media (max-width: 900px) { .st-key-ci_grid {grid-template-columns: repeat(2, minmax(0, 1fr));} }
@media (max-width: 560px) { .st-key-ci_grid {grid-template-columns: 1fr;} }

/* account badge in the sidebar */
.acct {display: flex; gap: 10px; align-items: center; padding: .2rem 0 .1rem;}
.acct-pic {width: 38px; height: 38px; flex: none; border-radius: 50%; display: grid; place-items: center;
           background: linear-gradient(135deg, #17616a, #2aa686); color: #fff; font-weight: 700; font-size: .9rem;
           border: 2px solid rgba(255,255,255,.18);}
.acct-text {display: flex; flex-direction: column; min-width: 0; line-height: 1.25;}
.acct-text b {color: var(--ink); font-size: .92rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.acct-text small {color: var(--ink-2); font-size: .76rem;}

/* medium screens: stack the side panel under the charts; narrow: stack the two charts too */
@media (max-width: 1180px) {
  [data-testid="stHorizontalBlock"]:has(.st-key-dash_charts) {flex-direction: column;}
  [data-testid="stHorizontalBlock"]:has(.st-key-dash_charts) > [data-testid="stColumn"] {width: 100% !important; flex: 1 1 100% !important; min-width: 100%;}
}
@media (max-width: 900px) {
  .st-key-dash_charts [data-testid="stHorizontalBlock"] {flex-direction: column;}
  .st-key-dash_charts [data-testid="stColumn"] {width: 100% !important; flex: 1 1 100% !important; min-width: 100%;}
  .dash-table th:nth-child(4), .dash-table td:nth-child(4) {display: none;}
}
</style>
""")

import importlib  # noqa: E402

import attendance as A  # noqa: E402

# Streamlit Cloud pulls new code on every push, but an already-imported module can stay in memory.
# Reload attendance.py whenever the file on disk is newer than the copy we're running.
import report as R  # noqa: E402

_stamp = tuple(Path(m.__file__).stat().st_mtime for m in (A, R))
if getattr(A, "_loaded_stamp", None) not in (None, _stamp):
    A = importlib.reload(A)
    R = importlib.reload(R)  # report.py uses attendance.py, so reload it after
A._loaded_stamp = _stamp

store = A.base_store()
who = A.role(store)
overview = st.Page(A.page_overview, title="All churches", icon=":material/public:", url_path="overview",
                   default=who == "bishop")
if who == "bishop":  # the Bishop sees numbers for every church and nothing else
    pages = {"Overview": [overview]}
else:
    pages = {
        "Attendance": [
            st.Page(A.page_dashboard, title="Dashboard", icon=":material/space_dashboard:", url_path="dashboard",
                    default=True),
            st.Page(A.page_followup, title="Follow-up & Check-in", icon=":material/how_to_reg:", url_path="followup"),
            st.Page(A.page_live, title="Live", icon=":material/sensors:", url_path="live"),
            st.Page(A.page_insights, title="Insights", icon=":material/insights:", url_path="insights"),
        ]}
    if who == "admin":  # Admin pages only appear for people signed in with the admin password (see A.role)
        pages["Admin"] = [
            overview,
            st.Page(A.page_members, title="Members", icon=":material/badge:", url_path="members"),
            st.Page(A.page_reports, title="Reports", icon=":material/forward_to_inbox:", url_path="reports"),
            st.Page(A.page_sql, title="SQL", icon=":material/database:", url_path="sql"),
        ]
    elif who == "lead":  # a church's own admin: their Members page, nothing from other churches
        pages["Admin"] = [st.Page(A.page_members, title="Members", icon=":material/badge:", url_path="members")]
A.account_box(store)  # before the pages run, so the church an admin picks applies straight away
st.navigation(pages).run()
