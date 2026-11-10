"""Trestle, Enformion, Hunter, Emailable: what each sends and how each answer is turned into columns."""
import json
import unittest

from helpers import ToolCase

import emailable_verify
import enformion_enrich
import hunter_find
import trestle_cnam


class TrestleTests(ToolCase):
    def test_number_with_no_name_on_file_is_no_match(self):
        self.fake([(("GET", "/3.1/cnam"), (200, {"is_valid": True, "belongs_to": None, "error": None, "warnings": []}))])
        out, _ = self.run_tool(trestle_cnam, "--phone", "6508235397")
        self.assertEqual((json.loads(out)["trestle_status"], json.loads(out)["trestle_valid"]), ("no_match", "true"))

    def test_key_goes_in_the_x_api_key_header(self):
        self.fake([(("GET", "/3.1/cnam"), (200, {"belongs_to": None}))])
        self.run_tool(trestle_cnam, "--phone", "6508235397")
        self.assertEqual(self.http.calls[0]["headers"]["x-api-key"], "test-trestle_api_key")


ENF = ("POST", "/Contact/EnrichPlus")
PERSON = {"person": {"personId": "G123", "name": {"firstName": "Jane", "middleName": "Q", "lastName": "Roe"}, "age": 44,
                     "addresses": [{"street": "12 Oak St", "unit": "4", "city": "Albany", "state": "NY", "zip": "12207"}],
                     "phones": [{"number": "(650) 823-5397", "type": "mobile", "isConnected": True},
                                {"number": "(212) 555-0100", "type": "landline", "isConnected": False}],
                     "emails": [{"email": "jane@roe.com", "isValidated": True}]},
          "identityScore": 95, "isError": False}


class EnformionTests(ToolCase):
    def test_person_found_fills_phones_emails_and_address(self):
        self.fake([(ENF, (200, PERSON))])
        out, _ = self.run_tool(enformion_enrich, "--first", "Jane", "--last", "Roe", "--email", "jane@roe.com")
        r = json.loads(out)
        self.assertEqual((r["enf_status"], r["enf_name"], r["enf_identity_score"]), ("found", "Jane Q Roe", 95))
        self.assertEqual((r["enf_phone_1"], r["enf_phone_1_type"], r["enf_phone_2"]), ("6508235397", "mobile", "2125550100"))
        self.assertEqual((r["enf_email_1"], r["enf_address"]), ("jane@roe.com", "12 Oak St 4, Albany, NY 12207"))

    def test_request_uses_enrich_plus_search_type_and_documented_body_names(self):
        self.fake([(ENF, (200, PERSON))])
        self.run_tool(enformion_enrich, "--first", "Jane", "--last", "Roe", "--address", "12 Oak St", "--city", "Albany",
                      "--state", "NY", "--zip", "12207", "--phone", "6508235397")
        c = self.http.calls[0]
        self.assertEqual(c["headers"]["galaxy-search-type"], "DevAPIContactEnrichPlus")
        self.assertEqual((c["headers"]["galaxy-ap-name"], c["headers"]["galaxy-ap-password"]),
                         ("test-enformion_ap_name", "test-enformion_ap_password"))
        self.assertEqual(c["body"], {"FirstName": "Jane", "LastName": "Roe", "Phone": "650-823-5397",
                                     "Address": {"addressLine1": "12 Oak St", "addressLine2": "Albany, NY 12207"}})

    def test_row_with_fewer_than_two_of_name_phone_address_email_is_not_sent(self):
        p = self.csv("leads.csv", ["id", "owner_first", "owner_last"], [["1", "Jane", "Roe"]])
        self.fake([])
        self.run_tool(enformion_enrich, "--file", p)
        self.assertEqual(self.http.calls, [])

    def test_empty_person_is_no_match(self):
        self.fake([(ENF, (200, {"person": None, "message": "No strong matches", "isError": False}))])
        out, _ = self.run_tool(enformion_enrich, "--first", "Jane", "--last", "Roe", "--email", "jane@roe.com")
        self.assertEqual(json.loads(out)["enf_status"], "no_match")


HUNTER = ("GET", "/v2/email-finder")


class HunterTests(ToolCase):
    def test_email_found_fills_email_score_and_verification(self):
        self.fake([(HUNTER, (200, {"data": {"email": "jane@roe.com", "score": 97, "position": "Owner",
                                            "verification": {"date": "2026-01-01", "status": "valid"}}}))])
        out, _ = self.run_tool(hunter_find, "--first", "Jane", "--last", "Roe", "--domain", "https://www.Roe.com/about")
        r = json.loads(out)
        self.assertEqual((r["hunter_status"], r["hunter_email"], r["hunter_score"], r["hunter_verification"], r["hunter_position"]),
                         ("found", "jane@roe.com", 97, "valid", "Owner"))
        c = self.http.calls[0]
        self.assertEqual(c["params"], {"first_name": "Jane", "last_name": "Roe", "domain": "roe.com"})
        self.assertEqual(c["headers"]["X-API-KEY"], "test-hunter_api_key")

    def test_company_name_is_sent_when_there_is_no_domain(self):
        self.fake([(HUNTER, (200, {"data": {"email": None}}))])
        out, _ = self.run_tool(hunter_find, "--first", "Jane", "--last", "Roe", "--company", "Roe Plumbing LLC")
        self.assertEqual(self.http.calls[0]["params"]["company"], "Roe Plumbing LLC")
        self.assertEqual(json.loads(out)["hunter_status"], "no_match")

    def test_http_404_is_no_match(self):
        self.fake([(HUNTER, (404, {"errors": [{"id": "not_found", "code": 404, "details": "none"}]}))])
        out, _ = self.run_tool(hunter_find, "--first", "Jane", "--last", "Roe", "--domain", "roe.com")
        self.assertEqual(json.loads(out)["hunter_status"], "no_match")

    def test_usage_limit_stops_the_run(self):
        self.fake([(HUNTER, (429, {"errors": [{"id": "too_many_requests", "code": 429, "details": "usage limit"}]}))])
        _, code = self.run_tool(hunter_find, "--first", "Jane", "--last", "Roe", "--domain", "roe.com")
        self.assertIn("STOPPED", str(code))


VERIFY = ("GET", "/v1/verify")
DELIVERABLE = (200, {"email": "jane@roe.com", "state": "deliverable", "reason": "accepted_email", "score": 98, "did_you_mean": None,
                     "disposable": False, "role": True, "free": False, "accept_all": True, "mailbox_full": False, "no_reply": False})


class EmailableTests(ToolCase):
    def test_verdict_fills_state_reason_score_and_flags(self):
        self.fake([(VERIFY, DELIVERABLE)])
        out, _ = self.run_tool(emailable_verify, "--email", "jane@roe.com")
        r = json.loads(out)
        self.assertEqual((r["emailable_state"], r["emailable_reason"], r["emailable_score"], r["emailable_flags"]),
                         ("deliverable", "accepted_email", 98, "accept_all,role"))
        self.assertEqual(self.http.calls[0]["headers"]["Authorization"], "Bearer test-emailable_api_key")

    def test_http_249_is_resent_until_the_answer_arrives(self):
        slow = (249, {"message": "Your request is taking longer than normal. Please send your request again."})
        self.fake([(VERIFY, [slow, slow, DELIVERABLE])])
        out, _ = self.run_tool(emailable_verify, "--email", "jane@roe.com")
        self.assertEqual(len(self.http.calls), 3)
        self.assertEqual(json.loads(out)["emailable_state"], "deliverable")

    def test_out_of_credits_stops_the_run(self):
        self.fake([(VERIFY, (402, {"message": "Insufficient credits"}))])
        _, code = self.run_tool(emailable_verify, "--email", "jane@roe.com")
        self.assertIn("STOPPED", str(code))

    def test_cell_without_an_at_sign_is_not_sent(self):
        p = self.csv("leads.csv", ["id", "owner_email"], [["1", "n/a"]])
        self.fake([])
        self.run_tool(emailable_verify, "--file", p)
        self.assertEqual(self.http.calls, [])


if __name__ == "__main__":
    unittest.main()
