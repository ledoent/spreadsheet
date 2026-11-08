# Copyright 2026 ForgeFlow S.L.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from io import BytesIO

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSpreadsheet(TransactionCase):
    def test_empty_spreadsheet_data(self):
        spreadsheet = self.env["spreadsheet.spreadsheet"].create({"name": "Empty"})
        data = spreadsheet.get_spreadsheet_data()
        self.assertEqual(data["name"], "Empty")
        self.assertEqual(data["mode"], "normal")
        self.assertEqual(data["spreadsheet_raw"]["sheets"][0]["id"], "sheet1")
        self.assertEqual(data["revisions"], [])

    def test_revisions(self):
        spreadsheet = self.env["spreadsheet.spreadsheet"].create({"name": "Rev"})
        message = {
            "type": "REMOTE_REVISION",
            "clientId": "client",
            "nextRevisionId": "rev-2",
            "serverRevisionId": "START_REVISION",
            "commands": [
                {
                    "type": "UPDATE_CELL",
                    "sheetId": "sheet1",
                    "col": 0,
                    "row": 0,
                    "content": "42",
                }
            ],
        }
        self.assertTrue(spreadsheet.send_spreadsheet_message(message))
        self.env.invalidate_all()
        revisions = spreadsheet.get_spreadsheet_data()["revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["nextRevisionId"], "rev-2")
        self.assertEqual(revisions[0]["commands"][0]["content"], "42")
        # Saving the data drops the revisions
        spreadsheet.spreadsheet_raw = {"sheets": [{"id": "sheet1", "name": "S"}]}
        self.assertFalse(spreadsheet.spreadsheet_revision_ids)
        self.assertEqual(spreadsheet.spreadsheet_raw["sheets"][0]["name"], "S")

    def test_presence_messages_are_not_persisted(self):
        """Cursor pings are ephemeral: they must never become revisions."""
        spreadsheet = self.env["spreadsheet.spreadsheet"].create({"name": "Presence"})
        for message_type in ("CLIENT_JOINED", "CLIENT_MOVED", "CLIENT_LEFT"):
            message = {
                "type": message_type,
                "clientId": "client",
                "client": {"id": "client", "position": {"col": 0, "row": 0}},
            }
            self.assertTrue(spreadsheet.send_spreadsheet_message(message))
        self.assertFalse(spreadsheet.spreadsheet_revision_ids)
        self.assertEqual(spreadsheet.get_spreadsheet_data()["revisions"], [])

    def test_unknown_message_type_is_rejected(self):
        spreadsheet = self.env["spreadsheet.spreadsheet"].create({"name": "Unknown"})
        self.assertFalse(spreadsheet.send_spreadsheet_message({"type": "NOPE"}))

    def test_reader_may_send_presence_but_not_revisions(self):
        """o-spreadsheet expects a read-only user to still broadcast a cursor."""
        reader = self.env["res.users"].create(
            {
                "name": "Reader",
                "login": "spreadsheet_oca_reader",
                "group_ids": [(4, self.env.ref("spreadsheet_oca.group_user").id)],
            }
        )
        spreadsheet = (
            self.env["spreadsheet.spreadsheet"]
            .create({"name": "Shared"})
            .with_user(reader)
        )
        spreadsheet.sudo().reader_ids = [(4, reader.id)]

        self.assertTrue(
            spreadsheet.send_spreadsheet_message(
                {
                    "type": "CLIENT_MOVED",
                    "clientId": "client",
                    "client": {"id": "client", "position": {"col": 0, "row": 0}},
                }
            )
        )
        with self.assertRaises(AccessError):
            spreadsheet.send_spreadsheet_message(
                {
                    "type": "REMOTE_REVISION",
                    "clientId": "client",
                    "nextRevisionId": "rev-2",
                    "serverRevisionId": "START_REVISION",
                    "commands": [],
                }
            )

    def test_import_xlsx(self):
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active["A1"] = "Hello"
        buffer = BytesIO()
        workbook.save(buffer)
        attachment = self.env["ir.attachment"].create(
            {"name": "test.xlsx", "raw": buffer.getvalue()}
        )
        action = self.env["spreadsheet.spreadsheet"].create_document_from_attachment(
            attachment.ids
        )
        spreadsheet = self.env["spreadsheet.spreadsheet"].browse(action["res_id"])
        self.assertEqual(spreadsheet.name, "test.xlsx")
        self.assertIn("xl/workbook.xml", spreadsheet.spreadsheet_raw)
        self.assertFalse(attachment.exists())
