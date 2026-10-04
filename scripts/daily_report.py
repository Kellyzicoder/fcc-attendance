"""Send the 5pm attendance email. Run by .github/workflows/daily-report.yml (or by hand).

Safe to run many times: it only sends once per day (checked in the email_log table), from 4:40pm NZ onwards
unless FORCE=1. If GitHub starts the run late, even after midnight, that day's report is still sent. Needs env vars DATABASE_URL and BREVO_API_KEY (REPORT_SENDER optional).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import datetime as dt  # noqa: E402

import attendance as A  # noqa: E402
import report as R  # noqa: E402

START = dt.time(16, 40)   # NZ time: the first run after this sends
CATCH_UP = dt.time(6, 0)  # GitHub sometimes starts scheduled runs hours late; a run before 6am sends yesterday's


def main() -> int:
    now = dt.datetime.now(A.TZ)
    force = os.environ.get("FORCE") == "1"
    late = now.time() < CATCH_UP
    day = now.date() - dt.timedelta(days=1) if late and not force else now.date()
    if not force and not (late or now.time() >= START):
        print(f"{now:%H:%M} NZ is before the 4:40pm send time — nothing to do.")
        return 0
    cfg = R.mail_config(os.environ.get)
    if not os.environ.get("DATABASE_URL") or not cfg["ready"]:
        print("Missing secrets: DATABASE_URL and BREVO_API_KEY (or SMTP_USER + SMTP_PASSWORD) are required.")
        return 1
    # the daily email covers the home church (people with no branch set)
    store = A.ChurchStore(A.SqlStore(os.environ["DATABASE_URL"]), os.environ.get("HOME_CHURCH") or A.home_church())
    if not force and store.sent_on(day.isoformat(), "daily"):
        print(f"The report for {day:%a %d %b} was already sent — skipping.")
        return 0
    out = R.send(store, cfg, kind="daily" if not force else "manual", day=day)
    print(f"Sent “{out['subject']}” to {', '.join(out['to'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
