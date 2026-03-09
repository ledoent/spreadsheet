# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""
Scheduled Data Refresh — Feature 1.

Allows users to configure a cron-based schedule that periodically:
  1. Reads all ODOO-type pivot definitions from a spreadsheet's JSON.
  2. Fetches fresh aggregate data via _get_pivot_data().
  3. Posts a Chatter summary on the spreadsheet record and emails
     subscribed partners.

This fills a gap that neither Odoo CE nor Enterprise address:
auto-refresh without a user opening the browser.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .pivot_data import _get_pivot_data

_logger = logging.getLogger(__name__)

_INTERVAL_TYPES = [
    ("hours", "Hour(s)"),
    ("days", "Day(s)"),
    ("weeks", "Week(s)"),
    ("months", "Month(s)"),
]


class SpreadsheetRefreshSchedule(models.Model):
    _name = "spreadsheet.refresh.schedule"
    _description = "Spreadsheet Scheduled Data Refresh"
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
    cron_id = fields.Many2one(
        "ir.cron",
        string="Cron Job",
        ondelete="set null",
        readonly=True,
        copy=False,
    )
    last_run = fields.Datetime(readonly=True, copy=False)
    notify_partner_ids = fields.Many2many(
        "res.partner",
        string="Notify Partners",
        help="These partners receive an email summary after each refresh.",
    )
    interval_number = fields.Integer(
        default=1,
        string="Every",
        tracking=True,
    )
    interval_type = fields.Selection(
        _INTERVAL_TYPES,
        default="weeks",
        string="Interval",
        tracking=True,
        required=True,
    )

    @api.constrains("interval_number")
    def _check_interval_number(self):
        for rec in self:
            if rec.interval_number < 1:
                raise ValidationError(_("Interval must be at least 1."))

    # ── Cron lifecycle ────────────────────────────────────────────────────────

    def action_activate(self):
        """Create or reactivate the cron job for this schedule."""
        for rec in self:
            if rec.cron_id:
                rec.cron_id.sudo().write(
                    {
                        "active": True,
                        "interval_number": rec.interval_number,
                        "interval_type": rec.interval_type,
                    }
                )
            else:
                model_id = (
                    self.env["ir.model"].sudo()._get(self._name).id
                )
                cron = self.env["ir.cron"].sudo().create(
                    {
                        "name": _("Spreadsheet Refresh: %s") % rec.spreadsheet_id.name,
                        "model_id": model_id,
                        "state": "code",
                        "code": "model.browse(%d)._run_refresh()" % rec.id,
                        "interval_number": rec.interval_number,
                        "interval_type": rec.interval_type,
                        "active": True,
                    }
                )
                rec.cron_id = cron

    def action_deactivate(self):
        """Pause (deactivate) the cron job without deleting it."""
        for rec in self:
            if rec.cron_id:
                rec.cron_id.sudo().write({"active": False})

    def action_run_now(self):
        """Manually trigger a refresh immediately."""
        self.ensure_one()
        self._run_refresh()

    def unlink(self):
        crons = self.mapped("cron_id").sudo()
        result = super().unlink()
        crons.unlink()
        return result

    # ── Refresh execution ────────────────────────────────────────────────────

    def _run_refresh(self):
        """
        Execute one refresh cycle:
          - Read pivot definitions from spreadsheet_raw JSON.
          - Compute fresh data for each ODOO pivot.
          - Post Chatter summary; email notify_partner_ids.
          - Record last_run timestamp.
        """
        self.ensure_one()
        spreadsheet = self.spreadsheet_id
        raw = spreadsheet.sudo().spreadsheet_raw or {}
        pivots = raw.get("pivots", {})

        if not pivots:
            _logger.info(
                "Spreadsheet refresh %s: no pivots found in spreadsheet %s",
                self.id,
                spreadsheet.id,
            )
            self.sudo().write({"last_run": fields.Datetime.now()})
            return

        summaries = []
        failed_pivot_names = []
        for pivot_id, pivot_def in pivots.items():
            if pivot_def.get("type") != "ODOO":
                continue
            model_name = pivot_def.get("model")
            pivot_name = pivot_def.get("name") or _("Pivot #%s") % pivot_id
            if not model_name or model_name not in self.env:
                _logger.warning(
                    "Spreadsheet refresh %s: unknown model %r — skipping pivot %s",
                    self.id,
                    model_name,
                    pivot_id,
                )
                failed_pivot_names.append(pivot_name)
                continue
            try:
                result = _get_pivot_data(
                    self.env,
                    model_name,
                    pivot_def.get("domain", []),
                    pivot_def.get("context", {}),
                    pivot_def.get("rows", []),
                    pivot_def.get("columns", []),
                    pivot_def.get("measures", []),
                )
                summaries.append(
                    {
                        "name": pivot_name,
                        "model": model_name,
                        "result": result,
                    }
                )
            except Exception:
                _logger.exception(
                    "Spreadsheet refresh %s: failed to refresh pivot %s",
                    self.id,
                    pivot_id,
                )
                failed_pivot_names.append(pivot_name)

        html = self._render_refresh_html(summaries)
        partner_ids = self.notify_partner_ids.ids

        spreadsheet.sudo().message_post(
            body=html,
            subject=_("Data refresh: %s") % spreadsheet.name,
            partner_ids=partner_ids,
            subtype_xmlid="mail.mt_comment" if partner_ids else "mail.mt_note",
        )

        if failed_pivot_names:
            spreadsheet.sudo().message_post(
                body=_(
                    "<p>⚠️ Refresh schedule <b>%(schedule)s</b> could not load "
                    "pivot(s): %(pivots)s. Check server logs for details.</p>"
                )
                % {
                    "schedule": self.name,
                    "pivots": ", ".join(f"<b>{n}</b>" for n in failed_pivot_names),
                },
                subtype_xmlid="mail.mt_note",
            )

        self.sudo().write({"last_run": fields.Datetime.now()})

    # ── HTML rendering ────────────────────────────────────────────────────────

    @api.model
    def _render_refresh_html(self, summaries):
        """Render a compact HTML summary of fresh pivot data."""
        if not summaries:
            return _("<p>No ODOO pivot data sources found in this spreadsheet.</p>")

        parts = ['<div style="font-family:sans-serif;font-size:14px">']
        for summary in summaries:
            result = summary["result"]
            name = summary["name"]
            model = summary["model"]

            parts.append(
                f'<h3 style="margin-bottom:4px">{name}'
                f' <small style="color:#888;font-weight:normal">({model})</small></h3>'
            )

            row_dims = result.get("rowDimensions", [])
            col_dims = result.get("colDimensions", [])
            groups = result.get("groups", [])

            # Grand total row (no groupby on either axis)
            grand_totals = [
                g
                for g in groups
                if g["rowGroupBy"] == [] and g["colGroupBy"] == []
            ]
            if grand_totals:
                gt = grand_totals[0]
                count = gt.get("count", 0)
                parts.append(f"<p style='margin:2px 0'><b>Total records:</b> {count}</p>")
                for key, val in gt.get("measures", {}).items():
                    if key != "__count" and val is not None:
                        parts.append(
                            f"<p style='margin:2px 0'><b>{key}:</b> {val}</p>"
                        )

            # Row breakdown (first row dimension only for brevity)
            if row_dims:
                row_gb = [d["fieldName"] for d in row_dims]
                row_groups = [
                    g
                    for g in groups
                    if g["rowGroupBy"] == row_gb and g["colGroupBy"] == []
                ]
                if row_groups:
                    parts.append(
                        f"<p style='margin:2px 0'><b>Rows ({', '.join(row_gb)}):</b>"
                        f" {len(row_groups)} group(s)</p>"
                    )
                    # Small inline table — max 10 rows for readability
                    parts.append(
                        '<table style="border-collapse:collapse;font-size:12px;margin:4px 0">'
                    )
                    measure_keys = [
                        k for k in (row_groups[0].get("measures") or {}) if k != "__count"
                    ]
                    headers = ["Group"] + measure_keys + ["Count"]
                    parts.append("<tr>")
                    for h in headers:
                        parts.append(
                            f'<th style="border:1px solid #ccc;padding:3px 6px'
                            f';background:#f5f5f5">{h}</th>'
                        )
                    parts.append("</tr>")
                    for g in row_groups[:10]:
                        parts.append("<tr>")
                        label = ", ".join(str(v) for v in g["rowValues"])
                        parts.append(
                            f'<td style="border:1px solid #ccc;padding:3px 6px">{label}</td>'
                        )
                        for mk in measure_keys:
                            val = g.get("measures", {}).get(mk, "")
                            parts.append(
                                f'<td style="border:1px solid #ccc;padding:3px 6px'
                                f';text-align:right">{val}</td>'
                            )
                        parts.append(
                            f'<td style="border:1px solid #ccc;padding:3px 6px'
                            f';text-align:right">{g.get("count","")}</td>'
                        )
                        parts.append("</tr>")
                    if len(row_groups) > 10:
                        colspan = len(headers)
                        parts.append(
                            f'<tr><td colspan="{colspan}" style="padding:3px 6px'
                            f';color:#888">… and {len(row_groups)-10} more rows</td></tr>'
                        )
                    parts.append("</table>")

            if col_dims:
                col_gb = [d["fieldName"] for d in col_dims]
                col_groups = [
                    g
                    for g in groups
                    if g["colGroupBy"] == col_gb and g["rowGroupBy"] == []
                ]
                parts.append(
                    f"<p style='margin:2px 0'><b>Columns ({', '.join(col_gb)}):</b>"
                    f" {len(col_groups)} group(s)</p>"
                )

            parts.append("<hr style='border:none;border-top:1px solid #eee;margin:8px 0'>")

        parts.append("</div>")
        return "".join(parts)
