# Walk2AI waitlist page

The page that tests the "Walk your AI" concept ([docs/business/CONCEPT.md](../docs/business/CONCEPT.md)):
one sentence, a companion you can try with a step slider, and an email sign-up with one
optional question: *would you pay about €4 a month?* English and European Portuguese,
light and dark, phone to desktop. No trackers, no cookies, no third-party scripts.

| File | What it is |
|---|---|
| `page.html` | The page: the single source. Also used as-is for previews. |
| `build.py` | Turns `page.html` into the live site in `site/` (standard library only). |
| `config.json` | Settings template: where sign-ups go and the privacy details. |
| `config.local.json` | Your filled-in copy. **Not committed** — the repository is public. |
| `fonts/` | Bricolage Grotesque and Figtree (SIL Open Font License; licences included). Served by the site itself, so visitors' browsers never contact Google. |
| `site/` | The built site to upload. Not committed; rebuild it any time. |

## Going live (free), step by step

Everything below uses free plans. Check each service's current free allowance before
relying on it — they change.

### 1. Decide who is behind it

The privacy notice must name who is responsible for the email addresses and how to
reach them. Both appear **publicly** on the page. Consider a dedicated email address for
Walk2AI rather than your personal one.

### 2. Fill in your settings

Copy `config.json` to `config.local.json` and fill in:

```json
{
  "mode": "netlify",
  "controller_name": "Your Name",
  "contact_email": "hello@your-domain.example",
  "host": "Netlify"
}
```

### 3. Build the site

On Windows, with Docker Desktop running, in PowerShell from the project folder:

```powershell
docker run --rm -v "${PWD}:/work" -w /work python:3.13-slim python landing/build.py
```

(Or, with Python installed: `python landing/build.py`.)

It writes `landing/site/`. It refuses to build while the name or email is missing, so a
page without a privacy contact can't go live by accident.

### 4. Put it online with Netlify (recommended: hosting and sign-up storage in one)

1. Create a free account at <https://app.netlify.com>.
2. Open <https://app.netlify.com/drop> and drag the `landing/site` folder onto the page.
   You get an address like `https://something.netlify.app` (you can rename it in the
   site settings).
3. In the new site: **Forms** → enable **form detection** if it is off, then drag the
   folder onto the site's **Deploys** page again so Netlify sees the form.
4. Sign up yourself with a test address. It should appear under **Forms → waitlist**,
   where you can also export everything as CSV.

Netlify's free plan has a monthly allowance of form submissions. If the test goes well
and you outgrow it, switch to a Google Form (below) instead of paying.

### Alternative: GitHub Pages and a Google Form

1. Create a Google Form with three short-answer questions: email, would pay, language.
2. In the form's menu choose **Get pre-filled link**, type anything in each field, and
   copy the link: it contains the field ids, like `entry.123456789`.
3. In `config.local.json` set `"mode": "google-form"`, `"host": "GitHub and Google"`,
   `"google_form_url"` to the form's address with `/viewform` replaced by
   `/formResponse`, and the three `entry.` ids in `"google_form_fields"`.
4. Build, then publish `landing/site/` with GitHub Pages: for example from a separate
   public repository, or a `gh-pages` branch of this one.

Answers land in the form's responses (and a Google Sheet if you link one). Google gives
no confirmation to the page, so test once by hand.

## Before you share it

- **Check the name.** Search the app stores and the EU trademark register (EUIPO's
  eSearch) for "Walk2AI" before promoting it.
- **Send yourself a test sign-up** and check it arrives.
- **Read the privacy section on the live page.** It must match what you actually do with
  the addresses: only Walk2AI news and occasional questions, deleted within 12 months.
  This page was written carefully but is not legal advice.

## What to measure (from the concept, section 10)

For four weeks: number of sign-ups (100 or more is promising, fewer than about 20 means
rethink) and the share answering *yes* or *maybe* to the price (40% or more is promising,
under 15% means rethink). Then email the people who signed up and talk to about ten.

## Changing the page

Edit `page.html` (both languages live side by side: `data-l="en"` and `data-l="pt"`),
then build again. Texts the script writes (the companion's mood and the form's messages)
are in the `TEXT` table at the bottom of the file.
