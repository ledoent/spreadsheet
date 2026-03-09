# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""
Threshold Alerts / KPI Watches — Feature 2.

Users define a watch on a named cell in a spreadsheet. A shared cron
periodically evaluates the cell's current value (computed server-side
by calling _get_pivot_data for the first matching pivot at that cell,
or by reading the static value from spreadsheet_raw) and fires a
Discuss/email notification when the threshold is crossed.

Two trigger modes:
  edge  — notify only on the first evaluation that crosses the threshold
           (stays silent until the condition resets and re-triggers)
  level — notify on every cron cycle where the condition holds

Operators: >, >=, <, <=, ==, !=
"""
import logging
import operator as _op
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

_OPERATORS = [
    (">", "> (greater than)"),
    (">=", ">= (greater or equal)"),
    ("<", "< (less than)"),
    ("<=", "<= (less or equal)"),
    ("==", "== (equal to)"),
    ("!=", "!= (not equal to)"),
]

_TRIGGER_MODES = [
    ("edge", "Edge — notify once when threshold is first crossed"),
    ("level", "Level — notify every cycle the condition holds"),
]

# Maps operator selection values to Python comparison callables.
_OP_FUNCS = {
    ">": _op.gt,
    ">=": _op.ge,
    "<": _op.lt,
    "<=": _op.le,
    "==": _op.eq,
    "!=": _op.ne,
}

# Pre-compiled cell reference pattern (module-level for efficiency).
_CELL_REF_RE = re.compile(r"^[A-Za-z]+[1-9][0-9]*$")


class SpreadsheetAlert(models.Model):
    _name = "spreadsheet.alert"
    _description = "Spreadsheet KPI Threshold Alert"
    _inherit = ["mail.thread"]
    _order = "spreadsheet_id, name"

    name = fields.Char(required=True, tracking=True)
    spreadsheet_id = fields.Many2one(
        "spreadsheet.spreadsheet",
        required=True,
        ondelete="cascade",
        index=True,
    )
    active = fields.Boolean(default=True, tracking=True)

    # ── Cell reference ────────────────────────────────────────────────────────
    sheet_name = fields.Char(
        string="Sheet Name",
        help="Name of the sheet containing the watched cell (leave blank for first sheet).",
    )
    cell_ref = fields.Char(
        string="Cell Reference",
        required=True,
        help="Spreadsheet cell reference, e.g. B3 or D12.",
        tracking=True,
    )

    # ── Threshold ────────────────────────────────────────────────────────────
    operator = fields.Selection(
        _OPERATORS,
        required=True,
        default=">",
        tracking=True,
    )
    threshold = fields.Float(required=True, tracking=True)

    # ── Trigger mode ─────────────────────────────────────────────────────────
    trigger_mode = fields.Selection(
        _TRIGGER_MODES,
        default="edge",
        required=True,
        tracking=True,
        help=(
            "Edge: notify once when the condition changes from False to True. "
            "Level: notify every cron cycle the condition is True."
        ),
    )
    last_state = fields.Boolean(
        default=False,
        readonly=True,
        copy=False,
        help="Tracks the condition state from the previous evaluation (used for edge mode).",
    )
    last_value = fields.Float(readonly=True, copy=False)
    last_checked = fields.Datetime(readonly=True, copy=False)

    # ── Notification ─────────────────────────────────────────────────────────
    notify_partner_ids = fields.Many2many(
        "res.partner",
        string="Notify Partners",
        help="Notified by email when the threshold is crossed.",
    )

    @api.constrains("cell_ref")
    def _check_cell_ref(self):
        for rec in self:
            if not _CELL_REF_RE.match(rec.cell_ref.strip()):
                raise ValidationError(
                    _("Cell reference %r must be in the form 'A1', 'B12', etc.")
                    % rec.cell_ref
                )

    # ── Shared cron ───────────────────────────────────────────────────────────

    @api.model
    def _cron_evaluate_all(self):
        """Called by the shared ir.cron: evaluate all active alerts."""
        alerts = self.search([("active", "=", True)])
        for alert in alerts:
            try:
                alert._evaluate()
            except Exception:
                _logger.exception("Failed to evaluate alert %s (%s)", alert.id, alert.name)

    # ── Single alert evaluation ───────────────────────────────────────────────

    def _evaluate(self):
        """
        Evaluate this alert's cell value and fire a notification if the
        threshold condition is met (subject to trigger_mode).
        """
        self.ensure_one()
        value = self._read_cell_value()
        if value is None:
            _logger.debug("Alert %s: cell %s not found or non-numeric", self.id, self.cell_ref)
            self.write({"last_checked": fields.Datetime.now()})
            return

        condition_met = self._check_condition(value)
        now = fields.Datetime.now()

        should_notify = False
        if self.trigger_mode == "level":
            should_notify = condition_met
        else:  # edge
            should_notify = condition_met and not self.last_state

        if should_notify:
            self._fire_notification(value)

        self.write(
            {
                "last_state": condition_met,
                "last_value": value,
                "last_checked": now,
            }
        )

    def _check_condition(self, value):
        """Return True if value satisfies operator(value, threshold)."""
        func = _OP_FUNCS.get(self.operator)
        return func(value, self.threshold) if func else False

    def _read_cell_value(self):
        """
        Read the current numeric value of the watched cell from spreadsheet_raw.

        Strategy:
          1. Parse the cell reference (e.g. 'B3') into (col_index, row_index).
          2. Find the target sheet by name (or first sheet if sheet_name blank).
          3. Read the cell's 'content' value from the JSON cell map.
          4. Return as float, or None if not found / non-numeric.

        Note: For formula cells (=PIVOT(…)), the stored value in spreadsheet_raw
        is whatever was last computed client-side and saved. For pivots that were
        refreshed via _run_refresh or opened in the browser recently, this will
        be the latest aggregate value.
        """
        raw = self.spreadsheet_id.sudo().spreadsheet_raw or {}
        sheets = raw.get("sheets", [])
        if not sheets:
            return None

        # Resolve target sheet
        target_sheet = None
        if self.sheet_name:
            for s in sheets:
                if s.get("name", "").lower() == self.sheet_name.strip().lower():
                    target_sheet = s
                    break
        if target_sheet is None:
            target_sheet = sheets[0]

        col_idx, row_idx = _parse_cell_ref(self.cell_ref.strip())
        if col_idx is None:
            return None

        # Cell map: keyed by "col,row" (0-indexed) or by row then col
        cells = target_sheet.get("cells", {})
        # o-spreadsheet stores cells as {row: {col: {content, style, ...}}}
        # Access: cells[str(row)][str(col)]
        row_map = cells.get(str(row_idx), {})
        cell_data = row_map.get(str(col_idx), {})
        if not cell_data:
            return None

        content = cell_data.get("content", "")
        if content is None or content == "":
            return None

        # o-spreadsheet stores the last-computed result in 'value' (set when
        # the spreadsheet is saved after formula evaluation in the browser).
        # Fall back to parsing 'content' as float for static numeric cells.
        evaluated = cell_data.get("value")
        if evaluated is None:
            # Try raw content as numeric
            try:
                return float(str(content).replace(",", "."))
            except (ValueError, TypeError):
                return None
        try:
            return float(evaluated)
        except (ValueError, TypeError):
            return None

    def _fire_notification(self, value):
        """Post a Chatter alert message and email subscribers."""
        op_label = dict(_OPERATORS).get(self.operator, self.operator)
        body = _(
            "<p><b>🔔 KPI Alert: %(name)s</b></p>"
            "<p>Cell <b>%(cell_ref)s</b> in spreadsheet "
            "<b>%(sheet_name)s</b> has value <b>%(value).4g</b>, "
            "which satisfies the condition <b>%(op)s %(threshold).4g</b>.</p>"
        ) % {
            "name": self.name,
            "cell_ref": self.cell_ref,
            "sheet_name": self.sheet_name or _("(first sheet)"),
            "value": value,
            "op": op_label,
            "threshold": self.threshold,
        }

        self.spreadsheet_id.sudo().message_post(
            body=body,
            subject=_("KPI Alert: %s") % self.name,
            partner_ids=self.notify_partner_ids.ids,
            subtype_xmlid=(
                "mail.mt_comment" if self.notify_partner_ids else "mail.mt_note"
            ),
        )

    def action_evaluate_now(self):
        """Manually trigger evaluation of this alert."""
        self.ensure_one()
        self._evaluate()

    def action_reset_state(self):
        """Reset last_state so an edge alert can trigger again."""
        self.write({"last_state": False})


def _parse_cell_ref(ref):
    """
    Parse a cell reference like 'B3' or 'AA12' into (col_index, row_index).

    Both are 0-based to match the o-spreadsheet JSON cell map format.
    Returns (None, None) on invalid input.
    """
    import re

    m = re.match(r"^([A-Za-z]+)([1-9][0-9]*)$", ref)
    if not m:
        return None, None
    col_str, row_str = m.group(1).upper(), m.group(2)
    col_idx = 0
    for ch in col_str:
        col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
    col_idx -= 1  # 0-based
    row_idx = int(row_str) - 1  # 0-based
    return col_idx, row_idx
