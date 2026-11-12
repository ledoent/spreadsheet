# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestSpreadsheetAccess(TransactionCase):
    """The sharing model is only observable as a plain user.

    A TransactionCase runs as SUPERUSER_ID, which returns from check_access
    before any ir.access is read, and admin is in group_manager -- so neither
    exercises these rules.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env, login="spreadsheet_user", groups="spreadsheet_oca.group_user"
        )
        cls.other = new_test_user(
            cls.env, login="spreadsheet_other", groups="spreadsheet_oca.group_user"
        )
        cls.Spreadsheet = cls.env["spreadsheet.spreadsheet"]

    def test_user_may_create_and_owns_it(self):
        sheet = self.Spreadsheet.with_user(self.user).create({"name": "mine"})
        self.assertEqual(sheet.owner_id, self.user)

    def test_user_cannot_see_someone_elses_spreadsheet(self):
        theirs = self.Spreadsheet.with_user(self.other).create({"name": "theirs"})
        self.assertFalse(theirs.with_user(self.user).exists())

    def test_reader_may_read_but_not_write(self):
        theirs = self.Spreadsheet.with_user(self.other).create({"name": "theirs"})
        theirs.reader_ids = [(4, self.user.id)]
        as_reader = theirs.with_user(self.user)
        self.assertTrue(as_reader.exists())
        self.assertFalse(as_reader.has_access("write"))

    def test_contributor_may_write(self):
        theirs = self.Spreadsheet.with_user(self.other).create({"name": "theirs"})
        theirs.contributor_ids = [(4, self.user.id)]
        self.assertTrue(theirs.with_user(self.user).has_access("write"))

    def test_owner_is_not_copied_on_duplicate(self):
        theirs = self.Spreadsheet.with_user(self.other).create({"name": "theirs"})
        theirs.contributor_ids = [(4, self.user.id)]
        copy = theirs.with_user(self.user).copy()
        self.assertEqual(copy.owner_id, self.user)
