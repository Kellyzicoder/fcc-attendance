"""Send the attendance email from GitHub. Run by hand: .github/workflows/daily-report.yml has no schedule.

The usual way to send is the admin's "Send report now" button in the app; this does the same thing. It will not
send a second report for the same day unless FORCE=1. Needs env vars DATABASE_URL and BREVO_API_KEY
(REPORT_SENDER optional).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import datetime as dt  # noqa: E402

import attendance as A  # noqa: E402
import report as R  # noqa: E402


def main() -> int:
    day = dt.datetime.now(A.TZ).date()
    force = os.environ.get("FORCE") == "1"
    cfg = R.mail_config(os.environ.get)
    if not os.environ.get("DATABASE_URL") or not cfg["ready"]:
        print("Missing secrets: DATABASE_URL and BREVO_API_KEY (or SMTP_USER + SMTP_PASSWORD) are required.")
        return 1
    # the email covers the home church (people with no branch set)
    store = A.ChurchStore(A.SqlStore(os.environ["DATABASE_URL"]), os.environ.get("HOME_CHURCH") or A.home_church())
    if not force and any(store.sent_on(day.isoformat(), kind) for kind in ("daily", "manual")):
        print(f"A report for {day:%a %d %b} was already sent — skipping. Tick the box to send it again.")
        return 0
    out = R.send(store, cfg, kind="manual", day=day)
    print(f"Sent “{out['subject']}” to {', '.join(out['to'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
