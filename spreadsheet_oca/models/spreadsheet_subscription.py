# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""
Dashboard Subscriptions — Feature 8.

Allows partners to subscribe to a periodic email digest of a spreadsheet.
A shared daily cron evaluates all active subscriptions and sends those that
are due (based on frequency and last_sent timestamp).

Each digest email optionally includes a compact pivot data summary rendered
with inline CSS, suitable for email clients.
"""
import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

_FREQUENCY_SELECTION = [
    ("daily", "Daily"),
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
]

_FREQUENCY_DELTA = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


class SpreadsheetSubscription(models.Model):
    _name = "spreadsheet.subscription"
    _description = "Spreadsheet Dashboard Subscription"
    _inherit = ["mail.thread"]
    _order = "spreadsheet_id, partner_id"

    name = fields.Char(
        compute="_compute_name",
        store=True,
        readonly=False,
    )
    spreadsheet_id = fields.Many2one(
        "spreadsheet.spreadsheet",
        required=True,
        ondelete="cascade",
        index=True,
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Subscriber",
        required=True,
        ondelete="restrict",
        index=True,
    )
    active = fields.Boolean(default=True, tracking=True)
    frequency = fields.Selection(
        _FREQUENCY_SELECTION,
        default="weekly",
        required=True,
        tracking=True,
    )
    last_sent = fields.Datetime(readonly=True, copy=False)
    include_pivot_data = fields.Boolean(
        default=True,
        string="Include Pivot Data",
        help="Include a live pivot data summary in the email.",
    )

    _sql_constraints = [
        (
            "unique_spreadsheet_partner",
            "UNIQUE(spreadsheet_id, partner_id)",
            "A partner can only have one subscription per spreadsheet.",
        ),
    ]

    # ── Default name computation ───────────────────────────────────────────────

    @api.depends("spreadsheet_id", "partner_id")
    def _compute_name(self):
        for rec in self:
            spreadsheet_name = rec.spreadsheet_id.name or _("Unnamed Spreadsheet")
            partner_name = rec.partner_id.name or _("Unknown Partner")
            rec.name = "%s — %s" % (spreadsheet_name, partner_name)

    # ── Shared cron ───────────────────────────────────────────────────────────

    @api.model
    def _cron_send_digests(self):
        """Called by the shared ir.cron: send digests that are due."""
        active_subs = self.search([("active", "=", True)])
        now = fields.Datetime.now()
        for sub in active_subs:
            try:
                sub._send_digest_if_due(now)
            except Exception:
                _logger.exception(
                    "Failed to send digest for subscription %s (%s)",
                    sub.id,
                    sub.name,
                )

    def _send_digest_if_due(self, now=None):
        """Send this subscription's digest if it is due, otherwise skip."""
        self.ensure_one()
        if now is None:
            now = fields.Datetime.now()
        delta = _FREQUENCY_DELTA.get(self.frequency, timedelta(weeks=1))
        if self.last_sent and (now - self.last_sent) < delta:
            return  # not due yet
        self._send_digest()

    # ── Digest sending ────────────────────────────────────────────────────────

    def _send_digest(self):
        """Generate and send a digest email for this subscription."""
        self.ensure_one()
        from .pivot_data import _get_pivot_data  # local import to avoid circular

        spreadsheet = self.spreadsheet_id.sudo()
        summaries = []

        if self.include_pivot_data:
            raw = spreadsheet.spreadsheet_raw or {}
            pivots = raw.get("pivots", {})
            for pivot_id, pivot_def in pivots.items():
                if pivot_def.get("type") != "ODOO":
                    continue
                model_name = pivot_def.get("model")
                pivot_name = pivot_def.get("name") or (_("Pivot #%s") % pivot_id)
                if not model_name or model_name not in self.env:
                    _logger.warning(
                        "Subscription %s: unknown model %r — skipping pivot %s",
                        self.id,
                        model_name,
                        pivot_id,
                    )
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
                        "Subscription %s: failed to compute pivot %s",
                        self.id,
                        pivot_id,
                    )

        body_html = self._render_digest_html(spreadsheet, summaries)
        subject = _("Spreadsheet Digest: %s") % spreadsheet.name

        partner = self.partner_id
        email_to = partner.email
        if not email_to:
            _logger.warning(
                "Subscription %s: partner %s has no email — skipping",
                self.id,
                partner.name,
            )
            return

        self.env["mail.mail"].sudo().create(
            {
                "subject": subject,
                "body_html": body_html,
                "email_to": email_to,
            }
        ).send()

        self.sudo().write({"last_sent": fields.Datetime.now()})

    # ── HTML rendering ────────────────────────────────────────────────────────

    def _render_digest_html(self, spreadsheet, summaries):
        """Return a styled HTML email body for the digest.

        Args:
            spreadsheet: browse record of spreadsheet.spreadsheet (already sudo'd).
            summaries:   list of {"name", "model", "result"} dicts from _get_pivot_data.

        Returns:
            str — complete HTML body suitable for sending via mail.mail.
        """
        now_str = fields.Datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        parts = [
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'color:#333;max-width:700px;margin:0 auto">',
            '<h2 style="background:#4a4a8a;color:#fff;padding:16px 20px;margin:0;'
            'border-radius:4px 4px 0 0">',
            "%s" % (spreadsheet.name or _("Spreadsheet Digest")),
            "</h2>",
            '<div style="background:#f7f7f7;padding:8px 20px;border-bottom:1px solid #ddd;'
            'font-size:12px;color:#777">',
            _("Generated: %s") % now_str,
            "</div>",
            '<div style="padding:16px 20px">',
        ]

        if not summaries:
            parts.append(
                '<p style="color:#888">'
                + _("No pivot data sources are available in this spreadsheet.")
                + "</p>"
            )
        else:
            for summary in summaries:
                parts.extend(self._render_pivot_summary_html(summary))

        parts += [
            "</div>",
            '<div style="background:#f0f0f0;padding:10px 20px;font-size:11px;'
            'color:#aaa;border-radius:0 0 4px 4px;text-align:center">',
            _("You are receiving this because you subscribed to digest emails for this spreadsheet."),
            "</div>",
            "</div>",
        ]
        return "".join(parts)

    def _render_pivot_summary_html(self, summary):
        """Render a single pivot summary as a list of HTML string parts."""
        result = summary["result"]
        name = summary["name"]
        model = summary["model"]
        parts = [
            '<div style="margin-bottom:20px">',
            '<h3 style="margin:0 0 6px 0;font-size:15px;color:#4a4a8a">',
            "%s" % name,
            ' <span style="font-size:12px;color:#aaa;font-weight:normal">(%s)</span>' % model,
            "</h3>",
        ]

        row_dims = result.get("rowDimensions", [])
        groups = result.get("groups", [])

        # Grand total row
        grand_totals = [
            g for g in groups if g["rowGroupBy"] == [] and g["colGroupBy"] == []
        ]
        if grand_totals:
            gt = grand_totals[0]
            count = gt.get("count", 0)
            parts.append(
                '<p style="margin:2px 0"><strong>%s</strong> %s</p>'
                % (_("Total records:"), count)
            )
            for key, val in gt.get("measures", {}).items():
                if key != "__count" and val is not None:
                    parts.append(
                        '<p style="margin:2px 0"><strong>%s:</strong> %s</p>' % (key, val)
                    )

        # Row breakdown table (first row dimension only, max 10 rows)
        if row_dims:
            row_gb = [d["fieldName"] for d in row_dims]
            row_groups = [
                g
                for g in groups
                if g["rowGroupBy"] == row_gb and g["colGroupBy"] == []
            ]
            if row_groups:
                measure_keys = [
                    k
                    for k in (row_groups[0].get("measures") or {})
                    if k != "__count"
                ]
                headers = [_("Group")] + measure_keys + [_("Count")]
                parts.append(
                    '<table style="border-collapse:collapse;font-size:12px;'
                    'width:100%;margin-top:8px">'
                )
                parts.append("<thead><tr>")
                for h in headers:
                    parts.append(
                        '<th style="border:1px solid #ddd;padding:5px 8px;'
                        'background:#eef;text-align:left;white-space:nowrap">%s</th>' % h
                    )
                parts.append("</tr></thead><tbody>")
                for g in row_groups[:10]:
                    label = ", ".join(str(v) for v in g["rowValues"])
                    parts.append("<tr>")
                    parts.append(
                        '<td style="border:1px solid #ddd;padding:4px 8px">%s</td>' % label
                    )
                    for mk in measure_keys:
                        val = g.get("measures", {}).get(mk, "")
                        parts.append(
                            '<td style="border:1px solid #ddd;padding:4px 8px;'
                            'text-align:right">%s</td>' % val
                        )
                    parts.append(
                        '<td style="border:1px solid #ddd;padding:4px 8px;'
                        'text-align:right">%s</td>' % g.get("count", "")
                    )
                    parts.append("</tr>")
                if len(row_groups) > 10:
                    colspan = len(headers)
                    parts.append(
                        '<tr><td colspan="%d" style="padding:4px 8px;color:#888">'
                        "… %s</td></tr>"
                        % (colspan, _("and %d more rows") % (len(row_groups) - 10))
                    )
                parts.append("</tbody></table>")

        parts.append("</div>")
        return parts

    # ── Manual send ───────────────────────────────────────────────────────────

    def action_send_now(self):
        """Manually send the digest immediately, bypassing the due check."""
        self.ensure_one()
        self._send_digest()
