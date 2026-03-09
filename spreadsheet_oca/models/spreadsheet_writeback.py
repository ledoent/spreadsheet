# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""
Writeback — Feature 7: Edit List cells → Update Odoo Records.

Allows users to write Odoo record field values directly from a
spreadsheet's List view.  Each cell edit in the browser posts to
/spreadsheet/writeback (controllers/spreadsheet_writeback.py) which
calls the target model's write() and records an audit log entry here.

JavaScript integration (not yet implemented): the JS list cell-edit
handler will POST to /spreadsheet/writeback with the spreadsheet_id,
model, record_id, field_name and new_value.  The controller returns a
JSON dict with {success, old_value, new_value, log_id} or {error}.

Limitations of the old_value capture:
  - Field values are converted to str() before storage in the Char
    field.  For Many2one fields str() gives e.g. "product.product(42,)"
    which is not directly re-writable; rollback of many2one fields is
    therefore only possible for integer / char / float / selection fields
    where str→original type conversion is unambiguous.
  - The rollback helper (action_rollback_writeback) writes old_value as
    a raw string; callers that need type-safe rollback for relational
    fields should implement their own conversion before calling write().
"""
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class SpreadsheetWritebackLog(models.Model):
    _name = "spreadsheet.writeback.log"
    _description = "Spreadsheet Writeback Audit Log"
    _order = "writeback_at desc"

    spreadsheet_id = fields.Many2one(
        "spreadsheet.spreadsheet",
        required=True,
        ondelete="cascade",
        index=True,
        string="Spreadsheet",
    )
    model = fields.Char(required=True, string="Model")
    record_id = fields.Integer(required=True, string="Record ID")
    field_name = fields.Char(required=True, string="Field")
    old_value = fields.Char(string="Previous Value")
    new_value = fields.Char(required=True, string="New Value")
    user_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        readonly=True,
        string="User",
    )
    writeback_at = fields.Datetime(
        default=fields.Datetime.now,
        readonly=True,
        string="Written At",
    )
    status = fields.Selection(
        [
            ("ok", "Success"),
            ("error", "Error"),
            ("rolled_back", "Rolled Back"),
        ],
        default="ok",
        string="Status",
    )
    error_message = fields.Char(string="Error")

    def action_rollback(self):
        """
        Roll back this log entry by restoring old_value to the target record.

        Called from the form view "Roll Back" button (type="object").
        Delegates to SpreadsheetSpreadsheet.action_rollback_writeback so the
        rollback logic lives in one place.
        """
        self.ensure_one()
        self.spreadsheet_id.action_rollback_writeback(self.id)


class SpreadsheetSpreadsheet(models.Model):
    _inherit = "spreadsheet.spreadsheet"

    writeback_enabled = fields.Boolean(
        default=False,
        tracking=True,
        string="Writeback Enabled",
        help=(
            "Allow users to write Odoo record values directly from this "
            "spreadsheet's List views.  Each change is logged and reversible."
        ),
    )
    writeback_log_count = fields.Integer(
        compute="_compute_writeback_log_count",
        string="Writeback Logs",
    )

    def _compute_writeback_log_count(self):
        counts = self.env["spreadsheet.writeback.log"].read_group(
            [
                ("spreadsheet_id", "in", self.ids),
                ("status", "!=", "error"),
            ],
            ["spreadsheet_id"],
            ["spreadsheet_id"],
        )
        count_map = {c["spreadsheet_id"][0]: c["spreadsheet_id_count"] for c in counts}
        for rec in self:
            rec.writeback_log_count = count_map.get(rec.id, 0)

    def action_open_writeback_log(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Writeback Log"),
            "res_model": "spreadsheet.writeback.log",
            "view_mode": "list,form",
            "domain": [("spreadsheet_id", "=", self.id)],
            "context": {"default_spreadsheet_id": self.id},
        }

    @api.model
    def action_rollback_writeback(self, log_id):
        """
        Roll back a single writeback operation identified by log_id.

        Reads the audit log entry, validates that old_value is known (not
        False / None), writes the old value back to the record, marks the
        log entry as 'rolled_back', and posts a chatter note.

        Returns True on success, raises on failure.
        """
        log = self.env["spreadsheet.writeback.log"].sudo().browse(log_id)
        if not log.exists():
            raise ValueError(_("Writeback log entry %d not found.") % log_id)

        if log.old_value is False or log.old_value is None:
            raise ValueError(
                _("Cannot roll back log %d: previous value is unknown.") % log_id
            )

        if log.model not in self.env:
            raise ValueError(
                _("Cannot roll back log %d: model %r is not available.") % (log_id, log.model)
            )

        record = self.env[log.model].sudo().browse(log.record_id)
        if not record.exists():
            raise ValueError(
                _("Cannot roll back log %d: record %s(%d) no longer exists.")
                % (log_id, log.model, log.record_id)
            )

        record.write({log.field_name: log.old_value})
        log.write({"status": "rolled_back"})

        spreadsheet = log.spreadsheet_id.sudo()
        spreadsheet.message_post(
            body=_(
                "Writeback rolled back: field <b>%(field)s</b> on "
                "<b>%(model)s</b> #%(record_id)d restored to "
                "<b>%(old_value)s</b> (was <b>%(new_value)s</b>)."
            )
            % {
                "field": log.field_name,
                "model": log.model,
                "record_id": log.record_id,
                "old_value": log.old_value,
                "new_value": log.new_value,
            },
            subtype_xmlid="mail.mt_note",
        )

        return True
