"""Send the 5pm attendance email. Run by .github/workflows/daily-report.yml (or by hand).

Safe to run many times: it only sends once per day (checked in the email_log table), and only inside
the evening window unless FORCE=1. Needs env vars DATABASE_URL and BREVO_API_KEY (REPORT_SENDER optional).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import datetime as dt  # noqa: E402

import attendance as A  # noqa: E402
import report as R  # noqa: E402

WINDOW = (dt.time(16, 40), dt.time(19, 0))  # NZ time; first run inside the window sends


def main() -> int:
    now = dt.datetime.now(A.TZ)
    force = os.environ.get("FORCE") == "1"
    if not force and not (WINDOW[0] <= now.time() < WINDOW[1]):
        print(f"{now:%H:%M} NZ is outside the send window — nothing to do.")
        return 0
    cfg = R.mail_config(os.environ.get)
    if not os.environ.get("DATABASE_URL") or not cfg["ready"]:
        print("Missing secrets: DATABASE_URL and BREVO_API_KEY (or SMTP_USER + SMTP_PASSWORD) are required.")
        return 1
    # the daily email covers the home church (people with no branch set)
    store = A.ChurchStore(A.SqlStore(os.environ["DATABASE_URL"]), os.environ.get("HOME_CHURCH") or A.home_church())
    if not force and store.sent_on(now.date().isoformat(), "daily"):
        print(f"Today's report was already sent — skipping.")
        return 0
    out = R.send(store, cfg, kind="daily" if not force else "manual")
    print(f"Sent “{out['subject']}” to {', '.join(out['to'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
