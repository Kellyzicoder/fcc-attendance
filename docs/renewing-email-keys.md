# Renewing the email keys

Follow this when a Brevo key is about to expire (the current ones expire in October 2027) or when emails stop
sending. There are no keys in this file, and there never should be: this repository is public.

## There are two different keys

| | SMTP key | API key |
|---|---|---|
| What it sends | Sign-in emails (the code or link) for the new phone app | The attendance email the admin sends with **Send report now** |
| Starts with | `xsmtpsib-` | `xkeysib-` |
| Created in Brevo under | SMTP & API → SMTP tab | SMTP & API → API Keys tab |
| Pasted into | Supabase (1 place) | Streamlit and GitHub (2 places) |
| If it expires | Nobody new can sign in. People already signed in are fine. | **Send report now** fails. |

A key is only shown in full at the moment it is created, so an old key cannot be looked up again. Creating a new
one is always safe. The two kinds are not interchangeable.

## Part A: renew the SMTP key (app sign-in emails)

1. Open https://app.brevo.com/settings/keys/smtp and sign in to Brevo.
2. On the **SMTP** tab, click **Generate a new SMTP key**. Name it, for example, *Supabase sign-in 2027*, and copy it.
3. Open Supabase, choose the church project, then go to **Authentication → Emails → SMTP Settings**.
4. Paste the new key into **Password**. Leave the other fields as they are (listed below) and press **Save changes**.
5. Test: open the app in a private window, enter your email, and sign in with the code that arrives.
6. Back in Brevo, delete the old SMTP key once the test works.

The other fields, for reference: Host `smtp-relay.brevo.com`, Port `587`, Username = the **Login** shown at the top
of Brevo's SMTP tab (it ends in `@smtp-brevo.com`), Sender name *FCC Attendance*, Sender email = the address
verified in Brevo.

## Part B: renew the API key (attendance email)

1. In Brevo, open **SMTP & API** and switch to the **API Keys** tab. Click **Generate a new API key**, name it
   *FCC Attendance*, and copy it.
2. **Streamlit:** go to share.streamlit.io, find *fcc-attendance*, open the ⋮ menu, then **Settings → Secrets**.
   Replace the value on the line `brevo_api_key = "…"` (keep the quotes) and save.
3. **GitHub:** in this repository, open **Settings → Secrets and variables → Actions**. Click the pencil next to
   `BREVO_API_KEY`, paste the new key, and press **Update secret**.
4. Test the app side: in the attendance app, open **Reports** and press **Send report now**.
5. Optional: test sending from GitHub too. Open **Actions → Attendance email (manual) → Run workflow** and run it.
   A green tick after about a minute means it worked.
6. Back in Brevo, delete the old API key once both tests work.

## Keeping the keys safe

- Never put a key in a file in this repository, in a chat message, or in an email.
- Paste keys only into the three places above: Supabase SMTP Settings, Streamlit Secrets, GitHub Actions secrets.
- If a key is ever exposed, delete it in Brevo straight away and follow this guide to replace it.

## If something does not work

- **Supabase will not save, or sign-in emails fail:** check that the key starts with `xsmtpsib-` and that Username
  is the Brevo Login, not your own email.
- **Send report now fails:** check that the Streamlit key starts with `xkeysib-` and still has its quotes.
- **The GitHub run shows a red cross:** click the run, open the red step and read the last lines. A message about
  missing secrets means `BREVO_API_KEY` was not saved.
- **Emails arrive in Spam:** mark one as *Not spam*. The sender address must stay verified in Brevo.
