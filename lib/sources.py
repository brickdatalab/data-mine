"""Where rows come from and where results go back: a CSV file, an XLSX file, a Supabase table, or one row built
from command-line values. Every source has: kind, where, fields, rows (list of dicts of text),
ensure(cols), write(row, cols), flush()."""
import csv
import json
import shutil
import sys
from pathlib import Path

from lib import supabase
from lib.supabase import ident, q


def text(v):
    """Any cell or JSON value as the text a tool works with."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))          # Excel hands back 6508235397.0 for a phone number
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def backup(path):
    """One copy of the file as it was before this toolbox first wrote to it."""
    b = path.with_name(f"{path.stem}.orig{path.suffix}")
    if not b.exists():
        shutil.copyfile(path, b)
        print(f"original kept as {b.name}", file=sys.stderr)


class CsvSource:
    kind = "csv"

    def __init__(self, path):
        self.path = Path(path)
        self.where = str(path)
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            self.rows = [{k: (v or "") for k, v in r.items() if k is not None} for r in reader]
            self.fields = list(reader.fieldnames or [])
        self.dirty = False

    def ensure(self, cols):
        for c in cols:
            if c not in self.fields:
                self.fields.append(c)
                for r in self.rows:
                    r[c] = ""

    def write(self, row, cols):
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        backup(self.path)
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.rows)
        self.dirty = False


class XlsxSource:
    kind = "xlsx"

    def __init__(self, path, sheet=None):
        try:
            import openpyxl
        except ImportError:
            sys.exit("openpyxl is not installed. Run: pip install -r requirements.txt")
        self.path = Path(path)
        self.where = str(path)
        self.wb = openpyxl.load_workbook(self.path, keep_vba=self.path.suffix.lower() == ".xlsm")
        if sheet and sheet not in self.wb.sheetnames:
            sys.exit(f"Sheet {sheet!r} not found in {path}. Sheets: {', '.join(self.wb.sheetnames)}")
        self.ws = self.wb[sheet] if sheet else self.wb.active
        header = [text(c.value).strip() for c in self.ws[1]]
        self.col = {h: i for i, h in enumerate(header, 1) if h}
        self.width = len(header)
        self.fields = list(self.col)
        self.rows = []
        for n, vals in enumerate(self.ws.iter_rows(min_row=2, values_only=True), 2):
            if any(v is not None for v in vals):
                row = {h: text(vals[i - 1]) if i <= len(vals) else "" for h, i in self.col.items()}
                row["__row"] = n
                self.rows.append(row)
        self.dirty = False

    def ensure(self, cols):
        for c in cols:
            if c not in self.col:
                self.width += 1
                self.col[c] = self.width
                self.fields.append(c)
                self.ws.cell(row=1, column=self.width, value=c)
                for r in self.rows:
                    r[c] = ""

    def write(self, row, cols):
        for c in cols:
            v = row.get(c, "")
            self.ws.cell(row=row["__row"], column=self.col[c], value=None if v == "" else v)
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        backup(self.path)
        self.wb.save(self.path)
        self.dirty = False


class SupabaseSource:
    """Reads the table; writes only into result columns that already exist (never changes table structure)."""
    kind = "supabase"

    def __init__(self, table, key, where=None):
        self.cfg = supabase.config(required=True)
        schema, _, name = table.rpartition(".")
        schema = schema or "public"
        self.where, self.key = f"{schema}.{name}", key
        self.qt = f"{ident(schema)}.{ident(name)}"
        try:
            cols = supabase.run_sql(self.cfg, "select column_name from information_schema.columns "
                                              f"where table_schema = {q(schema)} and table_name = {q(name)} order by ordinal_position")
            self.fields = [c["column_name"] for c in cols]
            if not self.fields:
                sys.exit(f"Supabase table {schema}.{name} was not found.")
            if key not in self.fields:
                sys.exit(f"Key column {key!r} is not in {schema}.{name}. Pass --key with the column that identifies a row.")
            rows = supabase.run_sql(self.cfg, f"select * from {self.qt}" + (f" where {where}" if where else "") + f" order by {ident(key)}")
        except RuntimeError as e:
            sys.exit(f"Supabase read failed: {e}")
        self.rows = [{k: text(v) for k, v in r.items()} for r in rows]

    def ensure(self, cols):
        missing = [c for c in cols if c not in self.fields]
        if missing:
            sys.exit(f"{self.where} is missing result columns, and this toolbox does not change table structure. "
                     "No API call was made. Run this SQL once, then rerun:\n"
                     + "\n".join(f"  alter table {self.qt} add column if not exists {ident(c)} text;" for c in missing))

    def write(self, row, cols):
        sets = ", ".join(f"{ident(c)} = {q(text(row.get(c)))}" for c in cols)
        try:
            supabase.run_sql(self.cfg, f"update {self.qt} set {sets} where {ident(self.key)}::text = {q(row[self.key])}")
        except RuntimeError as e:
            print(f"WARNING: could not write {self.key}={row[self.key]} to {self.where}: {e}", file=sys.stderr)

    def flush(self):
        pass


class SingleSource:
    """One row held in memory, built from command-line values. Nothing is written anywhere."""
    kind = "single"
    where = "stdout"

    def __init__(self, row):
        self.rows = [dict(row)]
        self.fields = list(row)

    def ensure(self, cols):
        for c in cols:
            self.rows[0].setdefault(c, "")

    def write(self, row, cols):
        pass

    def flush(self):
        pass
