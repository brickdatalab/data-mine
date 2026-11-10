"""Middesk: the three steps (EIN -> name/address, create business + orders, fetch) and the MCA default reading."""
import json
import unittest

from helpers import ToolCase

import middesk

FUNDERS = ["kalamata", "xyz capital"]
SUIT = {"case_name": "XYZ Capital LLC v. Example Company Inc. et al", "case_number": "EF2026-001928", "case_status": "OPEN",
        "case_type": "Debt Collection", "filing_date": "2026-06-14", "party_type": "Defendant", "jurisdiction_state": "NY",
        "parties": [{"name": "XYZ Capital LLC", "role": "Plaintiff"}, {"name": "Example Company Inc.", "role": "Defendant"}],
        "judgments": [{"docket_entry_date": "2026-08-02", "amount_cents": 6450000, "text": "JUDGMENT ENTERED IN FAVOR OF PLAINTIFF"}]}
UCC = {"type": "ucc", "status_category": "active", "filing_date": "2026-05-20", "state": "CA", "collateral_type": "All Receivables",
       "secured_parties": [{"name": "CORPORATION SERVICE COMPANY, AS REPRESENTATIVE"}]}


def read(litigations=(), liens=(), bankruptcies=()):
    return middesk.mca_signals({"litigations": list(litigations), "liens": list(liens), "bankruptcies": list(bankruptcies)}, FUNDERS)


class McaSignalTests(unittest.TestCase):
    def test_example_from_the_notes_reads_as_strong_with_funder_judgment_and_ucc(self):
        r = read([SUIT], [UCC])
        self.assertEqual(r["mid_mca_default"], "strong")
        self.assertEqual((r["mid_lit_defendant"], r["mid_judgment_total"], r["mid_ucc_active"]), (1, "64500.00", 1))
        self.assertIn("XYZ Capital LLC", r["mid_mca_signals"])
        self.assertIn("representative", r["mid_mca_signals"].lower())

    def test_sued_by_a_listed_funder_without_a_judgment_is_strong(self):
        self.assertEqual(read([{**SUIT, "judgments": []}])["mid_mca_default"], "strong")

    def test_judgment_against_the_business_from_an_unlisted_plaintiff_is_strong(self):
        suit = {**SUIT, "parties": [{"name": "Acme Supply", "role": "Plaintiff"}]}
        self.assertEqual(read([suit])["mid_mca_default"], "strong")

    def test_judgment_or_attachment_lien_is_strong(self):
        r = read(liens=[{"type": "judgment", "status_category": "open"}, {"type": "attachment"}])
        self.assertEqual((r["mid_mca_default"], r["mid_liens_enforcement"]), ("strong", 2))

    def test_debt_collection_suit_from_an_unlisted_plaintiff_without_judgment_is_possible(self):
        suit = {**SUIT, "judgments": [], "parties": [{"name": "Acme Supply", "role": "Plaintiff"}]}
        self.assertEqual(read([suit])["mid_mca_default"], "possible")

    def test_bankruptcy_alone_is_possible(self):
        r = read(bankruptcies=[{"chapter": 11, "filing_date": "2026-03-01", "status": "open"}])
        self.assertEqual((r["mid_mca_default"], r["mid_bankruptcies"]), ("possible", 1))

    def test_active_receivables_ucc_alone_is_mca_activity_not_default(self):
        self.assertEqual(read(liens=[UCC])["mid_mca_default"], "mca_activity")

    def test_suit_where_the_business_is_the_plaintiff_does_not_count(self):
        r = read([{**SUIT, "party_type": "Plaintiff"}])
        self.assertEqual((r["mid_mca_default"], r["mid_lit_defendant"]), ("none", 0))

    def test_terminated_ucc_and_no_records_read_as_none(self):
        r = read(liens=[{**UCC, "status_category": "terminated"}])
        self.assertEqual((r["mid_mca_default"], r["mid_ucc_active"], r["mid_mca_signals"]), ("none", 0, ""))


PREFILL = ("POST", "/v1/prefill/businesses")
CREATE = ("POST", "/v1/businesses")
GET = ("GET", "/v1/businesses/")
PREFILLED = (200, {"object": "business", "names": [{"name": "Example Co", "type": "dba"}, {"name": "Example Company Inc.", "type": "legal"}],
                   "addresses": [{"full_address": "123 Main St, San Francisco, CA 94105", "address_line1": "123 Main St",
                                  "address_line2": None, "city": "San Francisco", "state": "CA", "postal_code": "94105", "labels": []}],
                   "people": [{"name": "John Doe", "titles": ["Owner"]}]})
CREATED = (201, {"id": "biz_1", "status": "open"})
DONE_ORDERS = [{"product": "litigations", "status": "completed"}, {"product": "liens", "status": "completed"}]
READY = (200, {"id": "biz_1", "status": "in_review", "orders": DONE_ORDERS, "litigations": [SUIT], "liens": [UCC], "bankruptcies": []})
WAITING = (200, {"id": "biz_1", "status": "open", "orders": [{"product": "litigations", "status": "pending"}], "litigations": []})


class ThreeStepTests(ToolCase):
    def setUp(self):
        super().setUp()
        self._patch("middesk.load_funders", return_value=FUNDERS)

    def ein_only(self):
        return self.csv("leads.csv", ["id", "ein"], [["1", "81-1234567"]])

    def test_ein_only_row_runs_all_three_steps_and_gets_the_mca_reading(self):
        p = self.ein_only()
        self.fake([(PREFILL, PREFILLED), (CREATE, CREATED), (GET, READY)])
        self.run_tool(middesk, "--file", p)
        r = self.read_csv(p)[0]
        self.assertEqual((r["mid_status"], r["mid_business_id"], r["mid_name"], r["mid_mca_default"]),
                         ("done", "biz_1", "Example Company Inc.", "strong"))
        self.assertEqual(r["mid_orders"], "litigations:completed, liens:completed")
        self.assertEqual([(c["method"], c["url"].split("/v1/")[1]) for c in self.http.calls],
                         [("POST", "prefill/businesses"), ("POST", "businesses"), ("GET", "businesses/biz_1")])

    def test_step_one_sends_only_the_nine_digit_ein(self):
        self.fake([(PREFILL, PREFILLED), (CREATE, CREATED), (GET, READY)])
        self.run_tool(middesk, "--file", self.ein_only())
        self.assertEqual(self.http.last(*PREFILL)["body"], {"tin": "811234567"})

    def test_step_two_sends_prefilled_name_address_people_nested_tin_and_the_bundled_orders(self):
        self.fake([(PREFILL, PREFILLED), (CREATE, CREATED), (GET, READY)])
        self.run_tool(middesk, "--file", self.ein_only())
        c = self.http.last(*CREATE)
        b = c["body"]
        self.assertEqual((b["name"], b["tin"], b["people"]), ("Example Company Inc.", {"tin": "811234567"}, [{"name": "John Doe"}]))
        self.assertEqual(b["addresses"], [{"address_line1": "123 Main St", "city": "San Francisco", "state": "CA", "postal_code": "94105"}])
        self.assertEqual(b["orders"], [{"product": "business_verification_verify"}, {"product": "tin"}, {"product": "litigations"},
                                       {"product": "people_litigations"}, {"product": "liens", "subproducts": ["ucc_documents"]},
                                       {"product": "bankruptcies"}])
        self.assertEqual(c["headers"]["Authorization"], "Bearer test-middesk_api_key")

    def test_row_that_already_has_name_and_full_address_skips_step_one(self):
        p = self.csv("leads.csv", ["id", "ein", "legal_name", "address", "city", "state", "zip", "owner_first", "owner_last"],
                     [["1", "811234567", "Roe Plumbing LLC", "12 Oak St", "Albany", "NY", "12207", "Jane", "Roe"]])
        self.fake([(CREATE, CREATED), (GET, READY)])
        self.run_tool(middesk, "--file", p)
        self.assertEqual(self.http.count(*PREFILL), 0)
        b = self.http.last(*CREATE)["body"]
        self.assertEqual((b["name"], b["people"]), ("Roe Plumbing LLC", [{"name": "Jane Roe"}]))

    def test_ein_that_middesk_cannot_resolve_is_no_match_and_nothing_is_ordered(self):
        p = self.ein_only()
        self.fake([(PREFILL, (200, None))])
        self.run_tool(middesk, "--file", p)
        self.assertEqual(self.read_csv(p)[0]["mid_status"], "no_match")
        self.assertEqual(self.http.count(*CREATE), 0)

    def test_results_not_ready_leaves_the_row_ordered_with_its_business_id(self):
        p = self.ein_only()
        self.fake([(PREFILL, PREFILLED), (CREATE, CREATED), (GET, WAITING)])
        self.run_tool(middesk, "--file", p, "--wait", "0")
        r = self.read_csv(p)[0]
        self.assertEqual((r["mid_status"], r["mid_business_id"], r["mid_mca_default"]), ("ordered", "biz_1", ""))

    def test_rerun_fetches_results_without_ordering_the_same_business_twice(self):
        p = self.ein_only()
        self.fake([(PREFILL, PREFILLED), (CREATE, CREATED), (GET, [WAITING, READY])])
        self.run_tool(middesk, "--file", p, "--wait", "0")
        self.run_tool(middesk, "--file", p, "--wait", "0")
        self.assertEqual(self.http.count(*CREATE), 1)
        self.assertEqual(self.http.count(*PREFILL), 1)
        self.assertEqual(self.read_csv(p)[0]["mid_status"], "done")

    def test_force_refetches_but_never_orders_again(self):
        p = self.ein_only()
        self.fake([(PREFILL, PREFILLED), (CREATE, CREATED), (GET, READY)])
        self.run_tool(middesk, "--file", p)
        self.run_tool(middesk, "--file", p, "--force")
        self.assertEqual(self.http.count(*CREATE), 1)
        self.assertEqual(self.http.count(*GET), 2)

    def test_rejected_order_is_recorded_as_an_error_with_middesks_message(self):
        p = self.ein_only()
        self.fake([(PREFILL, PREFILLED), (CREATE, (422, {"errors": [{"message": "litigations is not enabled", "parameter": "orders"}]}))])
        self.run_tool(middesk, "--file", p)
        r = self.read_csv(p)[0]
        self.assertTrue(r["mid_status"].startswith("error"))
        self.assertIn("litigations is not enabled", r["mid_status"])
        self.assertEqual(r["mid_business_id"], "")

    def test_dry_run_makes_no_calls(self):
        self.fake([])
        out, _ = self.run_tool(middesk, "--file", self.ein_only(), "--dry-run")
        self.assertEqual(self.http.calls, [])
        self.assertIn("1 rows would be sent", out)

    def test_single_ein_prints_the_reading_as_json(self):
        self.fake([(PREFILL, PREFILLED), (CREATE, CREATED), (GET, READY)])
        out, _ = self.run_tool(middesk, "--ein", "811234567")
        r = json.loads(out)
        self.assertEqual((r["mid_status"], r["mid_business_id"], r["mid_mca_default"]), ("done", "biz_1", "strong"))

    def test_business_id_alone_fetches_without_ordering(self):
        self.fake([(GET, READY)])
        out, _ = self.run_tool(middesk, "--business-id", "biz_1")
        self.assertEqual(json.loads(out)["mid_mca_default"], "strong")
        self.assertEqual(len(self.http.calls), 1)

    def test_sandbox_flag_switches_host(self):
        self.fake([(PREFILL, PREFILLED), (CREATE, CREATED), (GET, READY)])
        self.run_tool(middesk, "--ein", "811234567", "--sandbox")
        self.assertTrue(all(c["url"].startswith("https://api-sandbox.middesk.com/v1/") for c in self.http.calls))


if __name__ == "__main__":
    unittest.main()
