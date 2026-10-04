"""Daily attendance email for FCC: an HTML summary plus an Excel workbook, sent through Gmail.

Used in two places:
  • scripts/daily_report.py — run by GitHub Actions every evening (the 5pm report);
  • the app's "Send report now" button.

Sending goes through Brevo's free email API (an API key, no Google settings); a Gmail app password also works as a
fallback. Recipients are stored in the database (settings table) and edited in the app.
"""
from __future__ import annotations

import datetime as dt
import html
import io
import re
import smtplib
import ssl
from email.message import EmailMessage

import pandas as pd

import attendance as A

DEFAULT_TO = ["greaterloveauckland@gmail.com"]
APP_URL = "https://fcc-attendance.streamlit.app"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# Email clients ignore <style> blocks and dark mode unpredictably, so everything is inline and light.
C = dict(bg="#f3f6f8", card="#ffffff", ink="#17242e", ink2="#51616d", line="#e3e9ee", brand="#1f6f78",
         green="#1f8f6f", red="#c0392b", amber="#b7791f", blue="#3565c9")


# ---------------------------------------------------------------- recipients
def recipients(store) -> list[str]:
    raw = store.get_setting("report_recipients", "")
    emails = [e.strip() for e in re.split(r"[,\s;]+", raw) if e.strip()]
    return emails or list(DEFAULT_TO)


def save_recipients(store, text: str) -> tuple[list[str], list[str]]:
    """Returns (saved, rejected)."""
    items = [e.strip() for e in re.split(r"[,\s;]+", text) if e.strip()]
    good = list(dict.fromkeys(e.lower() for e in items if EMAIL_RE.match(e)))
    bad = [e for e in items if not EMAIL_RE.match(e)]
    if good and not bad:
        store.set_setting("report_recipients", ", ".join(good))
    return good, bad


# ---------------------------------------------------------------- data
def gather(store, day: dt.date | None = None) -> dict:
    day = day or A.today()
    iso = day.isoformat()
    members = store.list_members()
    mem = {m["id"]: m for m in members}
    services = store.list_services()
    past = sorted([s for s in services if s.get("date", "") <= iso], key=lambda s: s["date"])
    today_svc = next((s for s in past if s["date"] == iso), None)
    shown = today_svc or (past[-1] if past else None)  # no service today → report the latest one

    present = []
    if shown:
        for mid, at in (shown.get("present") or {}).items():
            m = mem.get(mid, {})
            t = pd.to_datetime(at, errors="coerce", utc=True)
            present.append(dict(Time=t.tz_convert(A.TZ).strftime("%H:%M") if pd.notna(t) else "",
                                Name=m.get("full_name", "(removed)"), Church=A.church_of(m) if m else "",
                                Type="First-timer" if m.get("type") == "first_timer" else "Member",
                                Phone=m.get("phone", ""), Group=m.get("group", ""),
                                **{"First visit today": "Yes" if m.get("first_visit") == shown["date"] else ""}))
    present.sort(key=lambda r: (r["Time"] or "99", r["Name"].lower()))

    df = A.missed_streaks(members, services, day)
    follow = [] if df.empty else [
        dict(Status="Red" if r.level == "red" else "Yellow", Name=r.name, Church=r.church, **{"Missed in a row": int(r.missed)},
             **{"Last seen": A.fmt_date(r.last_seen, "%d %b %Y")},
             Phone=r.phone, Group=r.group, **{"Invited by": r.invited_by})
        for r in df[df.level != "ok"].itertuples()]

    regs = store.list_registrations("pending") or []
    signups = [dict(Name=r["full_name"], Phone=r["phone"], Email=r["email"], **{"Invited by": r["invited_by"]},
                    **{"First visit": r["first_visit"]}, Notes=r["notes"],
                    **{"Happy to be contacted": "Yes" if r["wants_contact"] else "No"},
                    Sent=(pd.to_datetime(r["created_at"], errors="coerce", utc=True).tz_convert(A.TZ)
                          .strftime("%d %b %H:%M") if r["created_at"] else "")) for r in regs]

    hist = []
    for s in past[-26:]:
        p = s.get("present") or {}
        ft = sum(1 for mid in p if mem.get(mid, {}).get("type") == "first_timer")
        hist.append(dict(Date=dt.date.fromisoformat(s["date"]).strftime("%a %d %b %Y"), Service=s.get("name", ""),
                         Present=len(p), Members=len(p) - ft, **{"First-timers": ft}))
    return dict(day=day, today_svc=today_svc, shown=shown, present=present, follow=follow, signups=signups,
                history=hist[::-1], red=sum(f["Status"] == "Red" for f in follow),
                yellow=sum(f["Status"] == "Yellow" for f in follow))


# ---------------------------------------------------------------- Excel
def workbook(d: dict) -> bytes:
    sheets = {
        "Checked in": pd.DataFrame(d["present"], columns=["Time", "Name", "Type", "Phone", "Group", "First visit today"]),
        "Follow-up": pd.DataFrame(d["follow"], columns=["Status", "Name", "Missed in a row", "Last seen", "Phone",
                                                        "Group", "Invited by"]),
        "Sign-ups": pd.DataFrame(d["signups"], columns=["Name", "Phone", "Email", "Invited by", "First visit", "Notes",
                                                        "Happy to be contacted", "Sent"]),
        "Services": pd.DataFrame(d["history"], columns=["Date", "Service", "Present", "Members", "First-timers"]),
    }
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            frame.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            from openpyxl.styles import Font, PatternFill
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F6F78")
            for i, col in enumerate(frame.columns, start=1):
                width = max([len(str(col))] + [len(str(v)) for v in frame[col].tolist()[:500]]) + 2
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(max(width, 8), 48)
    return buf.getvalue()


# ---------------------------------------------------------------- HTML email
def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _table(rows: list[dict], cols: list[str], empty: str, limit: int = 25) -> str:
    if not rows:
        return f'<p style="margin:6px 0 0;color:{C["ink2"]};font-size:14px">{_e(empty)}</p>'
    head = "".join(f'<th align="left" style="padding:8px 10px;font-size:12px;white-space:nowrap;color:{C["ink2"]};font-weight:600;'
                   f'border-bottom:1px solid {C["line"]}">{_e(c)}</th>' for c in cols)
    body = ""
    for r in rows[:limit]:
        tds = ""
        for c in cols:
            v = r.get(c, "")
            if c == "Status":
                col = C["red"] if v == "Red" else C["amber"]
                v = f'<span style="color:{col};font-weight:700">●</span> {_e(v)}'
            else:
                v = _e(v)
            wrap = "white-space:nowrap;" if c in ("Phone", "Last seen", "Time", "Status", "Missed") else ""
            tds += f'<td style="padding:8px 10px;font-size:14px;{wrap}border-bottom:1px solid {C["line"]}">{v}</td>'
        body += f"<tr>{tds}</tr>"
    more = (f'<p style="margin:6px 0 0;color:{C["ink2"]};font-size:13px">+ {len(rows) - limit} more in the '
            f'attached spreadsheet.</p>') if len(rows) > limit else ""
    return (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse">'
            f"<tr>{head}</tr>{body}</table>{more}")


def _section(title: str, inner: str) -> str:
    return (f'<tr><td style="padding:0 0 16px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            f'style="background:{C["card"]};border:1px solid {C["line"]};border-radius:12px"><tr><td style="padding:18px 20px">'
            f'<h2 style="margin:0 0 8px;font-size:17px;color:{C["ink"]}">{_e(title)}</h2>{inner}</td></tr></table></td></tr>')


def _tile(label: str, value, colour: str) -> str:
    return (f'<td width="25%" style="padding:6px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            f'style="background:{C["card"]};border:1px solid {C["line"]};border-radius:12px"><tr><td style="padding:14px 14px">'
            f'<div style="font-size:12px;color:{C["ink2"]}">{_e(label)}</div>'
            f'<div style="font-size:26px;font-weight:800;color:{colour};margin-top:4px">{_e(value)}</div>'
            f"</td></tr></table></td>")


def build(store, day: dt.date | None = None) -> dict:
    d = gather(store, day)
    day, shown, today_svc = d["day"], d["shown"], d["today_svc"]
    n = len(d["present"])
    ft_today = sum(r["Type"] == "First-timer" for r in d["present"])
    need = d["red"] + d["yellow"]
    if today_svc:
        headline = f"{n} checked in today"
        when = f"{day:%A %d %B %Y}"
    elif shown:
        headline = f"No service today · last service {dt.date.fromisoformat(shown['date']):%a %d %b}: {n} present"
        when = f"{day:%A %d %B %Y}"
    else:
        headline, when = "No services recorded yet", f"{day:%A %d %B %Y}"
    count = (f"{n} present" if today_svc else
             f"no service today (last: {dt.date.fromisoformat(shown['date']):%a %d %b}, {n} present)" if shown
             else "no services yet")
    subject = (f"FCC attendance · {day:%a %d %b} · {count} · {need} to follow up"
               + (f" · {len(d['signups'])} new sign-up{'s' if len(d['signups']) != 1 else ''}" if d["signups"] else ""))
    svc_label = (f"{dt.date.fromisoformat(shown['date']):%a %d %b}" if shown else "")

    tiles = ("<tr>" + _tile("Checked in" + (f" · {svc_label}" if shown and not today_svc else ""), n, C["green"])
             + _tile("First-timers", ft_today, C["blue"])
             + _tile("Need a call", need, C["red"] if d["red"] else C["amber"])
             + _tile("New sign-ups", len(d["signups"]), C["brand"]) + "</tr>")
    body = (
        f'<!doctype html><html><body style="margin:0;padding:0;background:{C["bg"]};font-family:Arial,Helvetica,sans-serif;'
        f'color:{C["ink"]}"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{C["bg"]}">'
        f'<tr><td align="center" style="padding:20px 12px"><table role="presentation" width="640" cellspacing="0" '
        f'cellpadding="0" style="max-width:640px;width:100%">'
        f'<tr><td style="padding:0 0 16px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'style="background:{C["brand"]};border-radius:14px"><tr><td style="padding:20px 22px;color:#ffffff">'
        f'<div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#ffcf00;font-weight:700">'
        f'Favourite Child Church · Attendance</div>'
        f'<div style="font-size:22px;font-weight:800;margin-top:4px">{_e(headline)}</div>'
        f'<div style="font-size:14px;opacity:.9;margin-top:4px">{_e(when)}</div></td></tr></table></td></tr>'
        f'<tr><td style="padding:0 0 10px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0">{tiles}'
        f"</table></td></tr>"
        + _section(f"Needs a follow-up call ({need})" + (f" · {d['red']} red, {d['yellow']} yellow" if need else ""),
                   _table([{**f, "Last seen": f["Last seen"] if f["Last seen"] == "Not yet" else f["Last seen"][:6], "Missed": f["Missed in a row"]} for f in d["follow"]],
                          ["Status", "Name", "Missed", "Last seen", "Phone"],
                          "Nobody has missed 3 or more services in a row."))
        + _section(f"Welcome-form sign-ups waiting ({len(d['signups'])})",
                   _table(d["signups"], ["Name", "Phone", "Invited by", "Happy to be contacted"],
                          "No new sign-ups waiting.")
                   + (f'<p style="margin:8px 0 0;font-size:13px;color:{C["ink2"]}">Approve them in the app under '
                      f"Members → Sign-ups.</p>" if d["signups"] else ""))
        + _section("Checked in" + (f" · {svc_label}" if shown else ""),
                   _table(d["present"], ["Time", "Name", "Type"], "No one checked in yet.", limit=40))
        + f'<tr><td style="padding:4px 4px 0;font-size:13px;color:{C["ink2"]};line-height:1.5">'
          f'The full lists are in the attached Excel file (opens in Excel or Google Sheets). '
          f'Live dashboard: <a href="{APP_URL}" style="color:{C["brand"]}">{APP_URL.replace("https://", "")}</a><br>'
          f"This email contains members' contact details — please don't forward it outside the leadership team."
          f"</td></tr></table></td></tr></table></body></html>")
    text = (f"{headline}\n{when}\n\nNeed a follow-up call: {need} ({d['red']} red, {d['yellow']} yellow)\n"
            + "".join(f"  - {f['Name']} ({f['Status']}, {f['Missed in a row']} missed) {f['Phone']}\n"
                      for f in d["follow"][:25])
            + f"\nNew sign-ups waiting: {len(d['signups'])}\n"
            + f"\nFull lists in the attached spreadsheet. Dashboard: {APP_URL}\n")
    return dict(subject=subject, html=body, text=text, xlsx=workbook(d),
                filename=f"FCC-attendance-{day.isoformat()}.xlsx", day=day)


# ---------------------------------------------------------------- send
def mail_config(get) -> dict:
    """Read sending settings with get(name) (st.secrets.get or os.environ.get). Brevo preferred, Gmail as fallback."""
    cfg = dict(brevo_api_key=get("brevo_api_key") or get("BREVO_API_KEY"),
               sender=get("report_sender") or get("REPORT_SENDER") or DEFAULT_TO[0],
               smtp_user=get("smtp_user") or get("SMTP_USER"), smtp_password=get("smtp_password") or get("SMTP_PASSWORD"))
    cfg["ready"] = bool(cfg["brevo_api_key"] or (cfg["smtp_user"] and cfg["smtp_password"]))
    return cfg


def _send_brevo(cfg: dict, to: list[str], r: dict):
    """Brevo transactional email API (free: 300 emails/day). The sender address must be verified in Brevo."""
    import base64
    import json
    import urllib.error
    import urllib.request
    payload = dict(sender=dict(name="FCC Attendance", email=cfg["sender"]), to=[dict(email=e) for e in to],
                   subject=r["subject"], htmlContent=r["html"], textContent=r["text"],
                   attachment=[dict(name=r["filename"], content=base64.b64encode(r["xlsx"]).decode())])
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode(),
                                 headers={"api-key": cfg["brevo_api_key"], "content-type": "application/json",
                                          "accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:  # surface Brevo's own message (e.g. "sender not valid")
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Brevo said {e.code}: {detail}") from None


def _send_smtp(cfg: dict, to: list[str], r: dict, host: str = "smtp.gmail.com", port: int = 465):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = r["subject"], f"FCC Attendance <{cfg['smtp_user']}>", ", ".join(to)
    msg.set_content(r["text"])
    msg.add_alternative(r["html"], subtype="html")
    msg.add_attachment(r["xlsx"], maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=r["filename"])
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.send_message(msg)


def send(store, cfg: dict, to: list[str] | None = None, kind: str = "manual", day: dt.date | None = None) -> dict:
    """Build and send the report. Logs every attempt (so the 5pm job never double-sends). Raises on failure."""
    if not cfg.get("ready"):
        raise RuntimeError("Email sending isn't set up yet (add brevo_api_key to the Secrets).")
    to = to or recipients(store)
    r = build(store, day)
    try:
        (_send_brevo if cfg.get("brevo_api_key") else _send_smtp)(cfg, to, r)
    except Exception as e:
        store.log_email(kind, r["day"].isoformat(), to, False, f"{type(e).__name__}: {e}")
        raise
    store.log_email(kind, r["day"].isoformat(), to, True, r["subject"])
    return dict(to=to, subject=r["subject"])
