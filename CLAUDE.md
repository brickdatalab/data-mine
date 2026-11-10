# data-mine

A toolbox of command-line tools that append and verify lead data. One tool per job. Run the tool that matches what
the user asked for, on the file, table or value they named.

| The user wants | Run |
|---|---|
| A phone number checked: live or dead, line type, name on it, do-not-call, litigator | `tools/phone_validate.py` |
| The name on a phone number from a second source | `tools/trestle_cnam.py` |
| A person's phones, emails and address found | `tools/enformion_enrich.py` |
| A person's work email found | `tools/hunter_find.py` |
| An email checked for deliverability | `tools/emailable_verify.py` |
| A business checked for an MCA default: lawsuits, judgments, liens, bankruptcies | `tools/middesk.py` |

Every tool runs the same three ways:

```
python3 tools/<tool>.py --<input> VALUE            one value: prints JSON
python3 tools/<tool>.py --file leads.csv           CSV or XLSX: results appended to the file
python3 tools/<tool>.py --table public.leads       Supabase table: results written to the row
```

`python3 tools/<tool>.py --help` gives that tool's inputs, result columns and examples.
`README.md` covers setup, the shared flags, and chaining one tool into the next.

## Rules that cost money when ignored

- Every row a tool sends is an API call the provider may bill. Run the tool the user named, on the rows the user named.
- When the user has not said how many rows, run the same command with `--dry-run` first and report the count.
- The first time a tool is used with real keys, run it on one value, then compare the file saved under `raw/<tool>/`
  with the columns it filled, before running it on a file or table.
- `tools/middesk.py` places paid orders. A row with a `mid_business_id` has already been ordered: rerun the same
  command to fetch its results. Blank that cell only when the user asks to order the business again.
- Keys go in `.env` (copy `.env.example`). `.env`, `raw/` and lead files stay out of git; `.gitignore` covers them.
