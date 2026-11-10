"""How every tool reads rows and writes results back: CSV, XLSX, Supabase table, single value. Trestle is the tool used to drive it."""
import json
import unittest

from helpers import KEYS, SUPABASE_KEYS, ToolCase

import trestle_cnam

FOUND = (200, {"id": "Phone.1", "is_valid": True, "phone_number": "+16508235397",
               "belongs_to": {"id": "Person.1", "name": "Jane Q Roe", "firstname": "Jane", "middlename": "Q", "lastname": "Roe"},
               "error": None, "warnings": []})
CNAM = ("GET", "/3.1/cnam")


class CsvTests(ToolCase):
    def leads(self):
        return self.csv("leads.csv", ["id", "owner_cell_phone", "note"], [["1", "(650) 823-5397", "keep me"], ["2", "2125550100", ""]])

    def test_results_are_appended_as_new_columns_and_existing_cells_are_untouched(self):
        p = self.leads()
        self.fake([(CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--file", p)
        rows = self.read_csv(p)
        self.assertEqual(rows[0]["owner_cell_phone"], "(650) 823-5397")
        self.assertEqual(rows[0]["note"], "keep me")
        self.assertEqual((rows[0]["trestle_status"], rows[0]["trestle_name"], rows[0]["trestle_last"]), ("found", "Jane Q Roe", "Roe"))
        self.assertEqual(self.http.calls[0]["params"]["phone"], "6508235397")

    def test_original_file_is_copied_before_the_first_write(self):
        p = self.leads()
        before = p.read_text()
        self.fake([(CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--file", p)
        self.assertEqual((self.tmp / "leads.orig.csv").read_text(), before)

    def test_rows_already_done_are_not_sent_again(self):
        p = self.leads()
        self.fake([(CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--file", p)
        self.assertEqual(len(self.http.calls), 2)
        self.run_tool(trestle_cnam, "--file", p)
        self.assertEqual(len(self.http.calls), 2)

    def test_force_sends_done_rows_again(self):
        p = self.leads()
        self.fake([(CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--file", p)
        self.run_tool(trestle_cnam, "--file", p, "--force")
        self.assertEqual(len(self.http.calls), 4)

    def test_rows_that_errored_are_sent_again_on_the_next_run(self):
        p = self.leads()
        self.fake([(CNAM, [(500, {"error": "boom", "message": "x"}), (500, {"error": "boom", "message": "x"}), FOUND])])
        self.run_tool(trestle_cnam, "--file", p)
        self.assertTrue(self.read_csv(p)[0]["trestle_status"].startswith("error"))
        self.run_tool(trestle_cnam, "--file", p)
        self.assertEqual(self.read_csv(p)[0]["trestle_status"], "found")

    def test_dry_run_makes_no_calls_and_leaves_the_file_alone(self):
        p = self.leads()
        before = p.read_text()
        self.fake([])
        out, _ = self.run_tool(trestle_cnam, "--file", p, "--dry-run")
        self.assertEqual(self.http.calls, [])
        self.assertEqual(p.read_text(), before)
        self.assertIn("2 rows would be sent", out)

    def test_limit_and_ids_restrict_which_rows_are_sent(self):
        p = self.leads()
        self.fake([(CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--file", p, "--limit", "1")
        self.assertEqual(len(self.http.calls), 1)
        self.run_tool(trestle_cnam, "--file", p, "--ids", "2")
        rows = self.read_csv(p)
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(rows[1]["trestle_status"], "found")

    def test_rows_with_no_usable_phone_are_skipped_without_a_call(self):
        p = self.csv("leads.csv", ["id", "owner_cell_phone"], [["1", "555"], ["2", ""]])
        self.fake([])
        self.run_tool(trestle_cnam, "--file", p)
        self.assertEqual(self.http.calls, [])

    def test_missing_api_key_stops_before_any_call_and_names_the_key(self):
        p = self.leads()
        self.env.return_value = {}
        self.fake([])
        _, code = self.run_tool(trestle_cnam, "--file", p)
        self.assertIn("TRESTLE_API_KEY", str(code))
        self.assertEqual(self.http.calls, [])

    def test_column_and_prefix_flags_point_the_tool_at_a_second_phone_column(self):
        p = self.csv("leads.csv", ["id", "business_phone"], [["1", "6508235397"]])
        self.fake([(CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--file", p, "--phone-col", "business_phone", "--prefix", "biz_")
        self.assertEqual(self.read_csv(p)[0]["biz_trestle_name"], "Jane Q Roe")

    def test_rejected_key_stops_the_run_and_keeps_rows_already_done(self):
        p = self.leads()
        self.fake([(CNAM, [FOUND, (403, {"errorCode": "AUTHENTICATION_FAILED", "message": "bad key"})])])
        _, code = self.run_tool(trestle_cnam, "--file", p)
        self.assertIn("STOPPED", str(code))
        rows = self.read_csv(p)
        self.assertEqual(rows[0]["trestle_status"], "found")
        self.assertEqual(rows[1]["trestle_status"], "")


class XlsxTests(ToolCase):
    def test_xlsx_rows_are_read_and_results_written_back_into_the_same_sheet(self):
        import openpyxl
        p = self.tmp / "leads.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["id", "owner_cell_phone", "note"])
        ws.append([1, 6508235397, "keep me"])     # Excel stores phones as numbers
        wb.save(p)
        self.fake([(CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--file", p)
        ws = openpyxl.load_workbook(p).active
        header = [c.value for c in ws[1]]
        row = dict(zip(header, [c.value for c in ws[2]]))
        self.assertEqual(self.http.calls[0]["params"]["phone"], "6508235397")
        self.assertEqual((row["note"], row["trestle_status"], row["trestle_name"]), ("keep me", "found", "Jane Q Roe"))
        self.assertTrue((self.tmp / "leads.orig.xlsx").exists())


class SupabaseTests(ToolCase):
    def setUp(self):
        super().setUp()
        self.env.return_value = {**KEYS, **SUPABASE_KEYS}
        self.columns = ["id", "owner_cell_phone", "trestle_status", "trestle_name", "trestle_first", "trestle_middle", "trestle_last", "trestle_valid"]
        self.updates = []

    def sql(self, method, url, params, body):
        query = body["query"]
        if "information_schema.columns" in query:
            return 201, [{"column_name": c} for c in self.columns]
        if query.startswith("select *"):
            return 201, [{"id": 7, "owner_cell_phone": "6508235397", **{c: None for c in self.columns[2:]}}]
        self.updates.append(query)
        return 201, []

    def test_results_go_into_existing_columns_of_the_row_matched_by_key(self):
        self.fake([(("POST", "/database/query"), self.sql), (CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--table", "public.leads", "--key", "id")
        self.assertEqual(len(self.updates), 1)
        q = self.updates[0]
        self.assertTrue(q.startswith('update "public"."leads" set '))
        self.assertIn("\"trestle_name\" = 'Jane Q Roe'", q)
        self.assertIn("where \"id\"::text = '7'", q)
        self.assertNotIn("owner_cell_phone", q)

    def test_missing_result_column_stops_with_the_alter_table_and_no_provider_call(self):
        self.columns.remove("trestle_last")
        self.fake([(("POST", "/database/query"), self.sql), (CNAM, FOUND)])
        _, code = self.run_tool(trestle_cnam, "--table", "public.leads")
        self.assertIn('alter table "public"."leads" add column if not exists "trestle_last" text;', str(code))
        self.assertEqual(self.http.count("GET", "/3.1/cnam"), 0)
        self.assertEqual(self.updates, [])
        self.assertFalse(any("alter" in c["body"]["query"].lower() for c in self.http.calls))

    def test_table_mode_without_supabase_keys_names_them(self):
        self.env.return_value = dict(KEYS)
        self.fake([])
        _, code = self.run_tool(trestle_cnam, "--table", "public.leads")
        self.assertIn("SUPABASE_ACCESS_TOKEN", str(code))


class SingleValueTests(ToolCase):
    def test_single_value_prints_json_and_nothing_else(self):
        self.fake([(CNAM, FOUND)])
        out, code = self.run_tool(trestle_cnam, "--phone", "650-823-5397")
        self.assertIsNone(code)
        self.assertEqual(json.loads(out)["trestle_name"], "Jane Q Roe")

    def test_full_response_is_kept_in_the_raw_folder(self):
        self.fake([(CNAM, FOUND)])
        self.run_tool(trestle_cnam, "--phone", "6508235397")
        saved = json.loads((self.tmp / "raw" / "trestle_cnam" / "6508235397.json").read_text())
        self.assertEqual(saved["belongs_to"]["id"], "Person.1")

    def test_source_must_be_exactly_one_of_file_table_or_single_values(self):
        self.fake([])
        _, code = self.run_tool(trestle_cnam)
        self.assertIn("--file", str(code))


if __name__ == "__main__":
    unittest.main()
