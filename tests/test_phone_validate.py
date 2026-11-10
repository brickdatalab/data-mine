"""phone_validate: which service to call, what the cache keeps, and the score."""
import json
import unittest

from helpers import KEYS, SUPABASE_KEYS, ToolCase

import phone_validate as pv

DNC = ("GET", "DNCPlus.php")
TW = ("GET", "lookups.twilio.com")
SQL = ("POST", "/database/query")


def dnc(status="connected", iscell="Y", litigator="N"):
    return 200, {"status": status, "error_text": {}, "iscell": iscell, "national_dnc": "Y", "state_dnc": "N", "dma": "N", "litigator": litigator}


def tw(name="JANE ROE", kind="CONSUMER"):
    return 200, {"caller_name": {"caller_name": name, "caller_type": kind, "error_code": None}}


class NeedsTests(unittest.TestCase):
    """Which service a number still needs, from what the cache already holds."""

    def test_number_not_in_cache_needs_both(self):
        self.assertEqual(pv.needs(None), (True, True))

    def test_status_type_and_name_present_needs_neither(self):
        self.assertEqual(pv.needs({"phone_status": "connected", "line_type": "cellular", "caller_name": "JANE ROE"}), (False, False))

    def test_missing_name_needs_twilio_only(self):
        self.assertEqual(pv.needs({"phone_status": "connected", "line_type": "cellular", "caller_name": None}), (False, True))

    def test_missing_line_type_needs_dnc_plus_only(self):
        self.assertEqual(pv.needs({"phone_status": "connected-75", "line_type": "", "caller_name": "JANE ROE"}), (True, False))

    def test_dead_line_needs_neither(self):
        self.assertEqual(pv.needs({"phone_status": "disconnected", "line_type": None, "caller_name": None}), (False, False))

    def test_force_needs_both(self):
        self.assertEqual(pv.needs({"phone_status": "connected", "line_type": "cellular", "caller_name": "X"}, force=True), (True, True))


class NoCacheTests(ToolCase):
    """No Supabase keys in .env: API only."""

    def one(self, *argv):
        out, code = self.run_tool(pv, "--phone", "6508235397", "--owner-first", "Jane", "--owner-last", "Roe", *argv)
        return json.loads(out)

    def test_connected_mobile_with_the_owners_name_scores_100(self):
        self.fake([(DNC, dnc()), (TW, tw())])
        r = self.one()
        self.assertEqual((r["status"], r["phone_type"], r["caller_name"], r["score"]), ("connected", "Mobile", "JANE ROE", 100))
        self.assertEqual((r["litigator"], r["natdnc"], r["stdnc"], r["dma"]), ("false", "true", "false", "false"))

    def test_litigator_scores_minus_25_even_when_the_name_matches(self):
        self.fake([(DNC, dnc(litigator="Y")), (TW, tw())])
        r = self.one()
        self.assertEqual((r["score"], r["litigator"]), (-25, "true"))

    def test_disconnected_scores_zero(self):
        self.fake([(DNC, dnc(status="disconnected")), (TW, tw())])
        self.assertEqual(self.one()["score"], 0)

    def test_twilio_null_name_is_recorded_as_unavailable_and_scores_90_when_connected(self):
        self.fake([(DNC, dnc()), (TW, tw(name=None, kind=None))])
        r = self.one()
        self.assertEqual((r["caller_name"], r["score"]), ("Unavailable", 90))

    def test_same_last_name_different_first_name_scores_75(self):
        self.fake([(DNC, dnc()), (TW, tw(name="ROBERT ROE"))])
        self.assertEqual(self.one()["score"], 75)

    def test_keys_are_sent_as_dnc_token_and_twilio_basic_auth(self):
        self.fake([(DNC, dnc()), (TW, tw())])
        self.one()
        self.assertEqual(self.http.last(*DNC)["params"], {"output": "json", "phone": "6508235397", "token": "test-realvalidation_token"})
        self.assertTrue(self.http.last(*TW)["headers"]["Authorization"].startswith("Basic "))
        self.assertIn("%2B16508235397?Fields=caller_name", self.http.last(*TW)["url"])

    def test_csv_rows_get_result_columns_and_duplicate_numbers_are_called_once(self):
        p = self.csv("leads.csv", ["id", "owner_cell_phone", "owner_first", "owner_last", "legal_name", "doing_business_as"],
                     [["1", "6508235397", "Jane", "Roe", "Roe Plumbing LLC", ""], ["2", "(650) 823-5397", "Jane", "Roe", "", ""]])
        self.fake([(DNC, dnc()), (TW, tw())])
        self.run_tool(pv, "--file", p)
        rows = self.read_csv(p)
        self.assertEqual([(r["status"], r["phone_type"], r["caller_name"], r["score"]) for r in rows], [("connected", "Mobile", "JANE ROE", "100")] * 2)
        self.assertEqual((self.http.count(*DNC), self.http.count(*TW)), (1, 1))

    def test_biz_flag_reads_business_phone_and_writes_biz_columns(self):
        p = self.csv("leads.csv", ["id", "business_phone", "owner_first", "owner_last", "legal_name"], [["1", "6508235397", "Jane", "Roe", "Roe Plumbing LLC"]])
        self.fake([(DNC, dnc(iscell="N")), (TW, tw(name="ROE PLUMBING", kind="BUSINESS"))])
        self.run_tool(pv, "--file", p, "--biz")
        r = self.read_csv(p)[0]
        self.assertEqual((r["biz_status"], r["biz_phone_type"], r["biz_caller_name"], r["biz_score"]), ("connected", "Landline", "ROE PLUMBING", "100"))

    def test_dnc_plus_failure_leaves_the_row_blank(self):
        p = self.csv("leads.csv", ["id", "owner_cell_phone", "owner_first", "owner_last"], [["1", "6508235397", "Jane", "Roe"]])
        self.fake([(DNC, (500, {"oops": 1})), (TW, tw())])
        out, _ = self.run_tool(pv, "--file", p)
        self.assertEqual(self.read_csv(p)[0].get("status", ""), "")
        self.assertIn("failed: 1", out)

    def test_unauthorized_token_stops_the_run(self):
        self.fake([(DNC, (200, {"status": "unauthorized", "error_text": "Invalid token"})), (TW, tw())])
        _, code = self.run_tool(pv, "--phone", "6508235397")
        self.assertIn("STOPPED", str(code))


class CacheTests(ToolCase):
    """Supabase keys present: the real_validation table is read first and kept current."""

    def setUp(self):
        super().setUp()
        self.env.return_value = {**KEYS, **SUPABASE_KEYS}
        self.cached = []
        self.writes = []

    def sql(self, method, url, params, body):
        query = body["query"]
        if query.startswith("select"):
            return 201, self.cached
        self.writes.append(query)
        return 201, []

    def test_number_fully_in_the_cache_costs_no_api_call(self):
        self.cached = [{"phone": "6508235397", "phone_status": "connected", "line_type": "cellular", "caller_name": "JANE ROE",
                        "caller_type": "CONSUMER", "carrier": None, "natdnc": False, "stdnc": None, "dma": False, "litigator": False}]
        self.fake([(SQL, self.sql), (DNC, dnc()), (TW, tw())])
        out, _ = self.run_tool(pv, "--phone", "6508235397", "--owner-first", "Jane", "--owner-last", "Roe")
        r = json.loads(out)
        self.assertEqual((self.http.count(*DNC), self.http.count(*TW)), (0, 0))
        self.assertEqual((r["status"], r["phone_type"], r["score"], r["stdnc"]), ("connected", "Mobile", 100, ""))
        self.assertEqual(self.writes, [])

    def test_cached_number_missing_only_the_name_calls_twilio_only(self):
        self.cached = [{"phone": "6508235397", "phone_status": "connected", "line_type": "cellular", "caller_name": None,
                        "caller_type": None, "carrier": None, "natdnc": None, "stdnc": None, "dma": None, "litigator": None}]
        self.fake([(SQL, self.sql), (DNC, dnc()), (TW, tw())])
        self.run_tool(pv, "--phone", "6508235397")
        self.assertEqual((self.http.count(*DNC), self.http.count(*TW)), (0, 1))

    def test_new_number_is_upserted_into_the_cache_and_null_never_erases(self):
        self.fake([(SQL, self.sql), (DNC, dnc()), (TW, tw())])
        self.run_tool(pv, "--phone", "6508235397")
        self.assertEqual(len(self.writes), 1)
        w = self.writes[0]
        self.assertTrue(w.startswith("insert into public.real_validation (phone, phone_status, line_type, caller_name"))
        self.assertIn("('6508235397','connected','Mobile','JANE ROE','CONSUMER',NULL,true,false,false,false,", w)
        self.assertIn("caller_name = coalesce(excluded.caller_name, real_validation.caller_name)", w)

    def test_stored_line_type_is_never_overwritten(self):
        rec = pv.record_from("6508235397", {"status": "connected", "iscell": "V"}, {"line_type": "landline"})
        self.assertEqual(rec["line_type"], "landline")

    def test_dry_run_reads_the_cache_but_calls_no_provider_and_writes_nothing(self):
        p = self.csv("leads.csv", ["id", "owner_cell_phone"], [["1", "6508235397"]])
        self.fake([(SQL, self.sql)])
        out, _ = self.run_tool(pv, "--file", p, "--dry-run")
        self.assertIn("need the API: 1", out)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.http.count(*DNC) + self.http.count(*TW), 0)

    def test_project_without_the_cache_table_runs_api_only(self):
        def no_table(method, url, params, body):
            return 400, {"message": 'Failed to run sql query: ERROR:  42P01: relation "public.real_validation" does not exist'}
        self.fake([(SQL, no_table), (DNC, dnc()), (TW, tw())])
        out, code = self.run_tool(pv, "--phone", "6508235397", "--owner-first", "Jane", "--owner-last", "Roe")
        self.assertIsNone(code)
        self.assertEqual(json.loads(out)["status"], "connected")
        self.assertEqual(self.http.count(*SQL), 1)


if __name__ == "__main__":
    unittest.main()
