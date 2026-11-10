# data-mine

A toolbox for appending and verifying lead data. Each script in `tools/` asks one provider one question and writes
the answer back next to the row it came from. Run the one tool the task calls for; chain tools only when asked to.

## Before the first run

1. `cp .env.example .env` and fill in the keys for the tools you will use. A tool names any key it is missing.
2. `pip install -r requirements.txt` (needed by `phone_validate.py` and for `.xlsx` files; the other tools use only the standard library).
3. `python3 -m unittest discover -s tests` runs the test suite. It uses no network and no keys.

## The tools

| Tool | Question it answers | The row needs | Keys in `.env` |
|---|---|---|---|
| `tools/phone_validate.py` | Is this number live, what line type, whose name is on it, is it do-not-call or a litigator, how well does the name match the lead? | phone | `REALVALIDATION_TOKEN`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` |
| `tools/trestle_cnam.py` | Whose name is on this number? (second opinion on caller name) | phone | `TRESTLE_API_KEY` |
| `tools/enformion_enrich.py` | What phones, emails and address does this **person** have? | two of: name, phone, address, email | `ENFORMION_AP_NAME`, `ENFORMION_AP_PASSWORD` |
| `tools/hunter_find.py` | What is this person's work email? | first + last name, and a domain or company name | `HUNTER_API_KEY` |
| `tools/emailable_verify.py` | Is this email deliverable? | email | `EMAILABLE_API_KEY` |
| `tools/middesk.py` | Does this business show an MCA default (lawsuits, judgments, liens, bankruptcies)? | EIN, or name + full address | `MIDDESK_API_KEY` |

`python3 tools/<tool>.py --help` is the reference for each tool: examples, every result column, cost notes and
that tool's own flags. Read it before the first use of a tool.

## Running a tool

Every tool takes exactly one source:

```
python3 tools/trestle_cnam.py --phone 6508235397            one value: prints JSON, writes nothing
python3 tools/trestle_cnam.py --file leads.csv              CSV or XLSX: results appended to the same file
python3 tools/trestle_cnam.py --table public.leads --key id Supabase table: results written to the matching row
```

Flags every tool shares:

| Flag | Effect |
|---|---|
| `--dry-run` | Counts the rows that would be sent. No API call, nothing written. |
| `--limit N` | At most N rows. |
| `--ids A B C` | Only rows whose `--key` column (default `id`) has one of these values. |
| `--force` | Redo rows that already have a result. |
| `--<input>-col NAME` | Read an input from a different column, e.g. `--phone-col business_phone`. |
| `--prefix TEXT` | Put TEXT in front of every result column, e.g. `--prefix biz_`, so one tool can run on two columns of the same sheet. |
| `--sheet NAME` | XLSX sheet to use (default: the first). |
| `--where "SQL"` | With `--table`: only rows matching this condition. |

Default input column names: `owner_cell_phone`, `business_phone`, `owner_first`, `owner_last`,
`legal_name`, `doing_business_as`, `owner_email`, `business_email`, plus `ein`, `website`, `address`, `city`,
`state`, `zip`. When a sheet uses other names, pass the `--<input>-col` flags; `--help` lists them per tool.

## What every tool does

- **Cost.** Each row sent is a call the provider may bill. A batch starts calling as soon as it is run. Run
  `--dry-run` first to see the count when the size of the batch is not already known.
- **Status column.** The first result column of each tool is its status: `found` or a provider verdict, `no_match`,
  or `error: ...`. Rows with a non-error status are skipped on the next run; `error` rows are sent again.
  Rows without usable input are skipped and counted, never sent.
- **Files.** Results are written into the same CSV or XLSX. Before a tool first writes to a file it keeps a copy
  as `<name>.orig.<ext>`. An XLSX is re-saved with openpyxl, which can drop charts and images from the workbook;
  the `.orig` copy still has them.
- **Supabase tables.** A tool writes only into result columns that already exist. If one is missing it stops before
  any API call and prints the `alter table` statements to run. It never changes table structure itself.
- **Phone cache (optional).** `phone_validate.py` stores results in a Supabase table named `real_validation` when the
  project has one, so a number already checked is not billed again. Without that table, or without Supabase keys,
  it calls the APIs every time.
- **Raw responses.** The full provider response for each lookup is saved under `raw/<tool>/`. Read it there when a
  column is not enough.
- **Stops.** A rejected key or exhausted credits ends the run with `STOPPED: ...`; rows finished before that are saved.

## Chaining tools

A tool's result column can be the next tool's input:

```
# find an email, then verify the one that was found
python3 tools/hunter_find.py --file leads.csv
python3 tools/emailable_verify.py --file leads.csv --email-col hunter_email --prefix found_

# find a phone for the owner, then validate the one that was found
python3 tools/enformion_enrich.py --file leads.csv
python3 tools/phone_validate.py --file leads.csv --phone-col enf_phone_1 --prefix enf_
```

## Middesk and MCA default

`tools/middesk.py` runs three steps per business: resolve the EIN to a name and address, create the business with
its searches ordered, then fetch the results and read them for MCA default signals (`mid_mca_default`:
`strong` / `possible` / `mca_activity` / `none`, with the reasons in `mid_mca_signals`). Results take seconds to
minutes. Rows not ready keep `mid_status = ordered`; run the same command again to fetch them. A row with a
`mid_business_id` is never ordered twice. The funder names that make a lawsuit count as an MCA suit are in
`mca_funders.txt`. `--help` has the full rules.

## Not yet proven

These tools were written from each provider's published docs on 2026-11-10 and tested against faked responses.
They have not been run against the live APIs. The first time a tool is used with real keys, run it on one value,
then read the file it saved under `raw/` and compare it with the columns it filled. Enformion is the likeliest to
differ: its Contact Enrich Plus response format is not published.
