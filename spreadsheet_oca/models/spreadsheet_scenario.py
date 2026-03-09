# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""
Named Scenarios / What-If Manager — Feature 5.

A *scenario* stores a named set of cell overrides for a spreadsheet.
Users can define multiple "what-if" variants (e.g. "Optimistic", "Pessimistic",
"Base Case") without duplicating the spreadsheet.

The ``action_apply_to_copy()`` method materialises a scenario into a new
spreadsheet record with the overrides written into the raw JSON — giving
immediate value with no JavaScript required.

Long-term target: a JS side-panel overlay that applies overrides live in
the o-spreadsheet editor without creating a copy.
"""
import copy
import json
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .cell_ref import parse_cell_key, read_cell_value

# Matches optional "SheetName!" prefix followed by a standard cell address.
# Sheet name can contain any non-'!' characters (spaces, digits, etc.).
_OVERRIDE_KEY_RE = re.compile(r"^(?:[^!]+!)?[A-Za-z]+[1-9][0-9]*$")

# Aliases for backward compatibility and internal use.
_parse_override_key = parse_cell_key


class SpreadsheetScenario(models.Model):
    _name = "spreadsheet.scenario"
    _description = "Spreadsheet What-If Scenario"
    _inherit = ["mail.thread"]
    _order = "spreadsheet_id, is_base desc, name"

    name = fields.Char(required=True, tracking=True)
    spreadsheet_id = fields.Many2one(
        "spreadsheet.spreadsheet",
        required=True,
        ondelete="cascade",
        index=True,
    )
    description = fields.Char(
        help="Brief note on what this scenario represents",
    )
    is_base = fields.Boolean(
        default=False,
        tracking=True,
        help="Mark as the baseline scenario. Only one active scenario per spreadsheet "
        "may be designated as the base.",
    )
    active = fields.Boolean(default=True)
    cell_overrides = fields.Text(
        help=(
            "JSON dict mapping cell references to override values.\n"
            "Supported formats:\n"
            '  {"Sheet1!B3": 125000, "C5": 0.15, "D7": "text", "E9": null}\n'
            "Keys may include an optional sheet name prefix (SheetName!ColRow) "
            "or use a bare column+row reference that applies to the first sheet.\n"
            "Values must be numbers, strings, booleans, or null."
        ),
    )
    override_count = fields.Integer(
        compute="_compute_override_count",
        store=False,
        string="# Overrides",
    )

    # ── Computed fields ───────────────────────────────────────────────────────

    def _compute_override_count(self):
        for rec in self:
            raw = rec.cell_overrides
            if raw and raw.strip():
                try:
                    parsed = json.loads(raw)
                    rec.override_count = len(parsed) if isinstance(parsed, dict) else 0
                except (ValueError, TypeError):
                    rec.override_count = 0
            else:
                rec.override_count = 0

    # ── Constraints ───────────────────────────────────────────────────────────

    @api.constrains("is_base", "spreadsheet_id")
    def _check_single_base(self):
        """Ensure at most one active base scenario exists per spreadsheet."""
        for rec in self:
            if not rec.is_base:
                continue
            conflict = self.search(
                [
                    ("spreadsheet_id", "=", rec.spreadsheet_id.id),
                    ("is_base", "=", True),
                    ("active", "=", True),
                    ("id", "!=", rec.id),
                ],
                limit=1,
            )
            if conflict:
                raise ValidationError(
                    _(
                        "Spreadsheet %(sheet)s already has a base scenario: %(base)s. "
                        "Only one active scenario per spreadsheet can be marked as base. "
                        "Please unmark the existing base scenario first."
                    )
                    % {
                        "sheet": rec.spreadsheet_id.name,
                        "base": conflict.name,
                    }
                )

    @api.constrains("cell_overrides")
    def _check_cell_overrides(self):
        """Validate cell_overrides JSON structure and cell reference format."""
        for rec in self:
            raw = rec.cell_overrides
            if not raw or not raw.strip():
                continue

            # Must be valid JSON.
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise ValidationError(
                    _("Cell overrides is not valid JSON: %s") % exc
                ) from exc

            # Top-level must be a dict.
            if not isinstance(parsed, dict):
                raise ValidationError(
                    _(
                        "Cell overrides must be a JSON object (dict), "
                        "e.g. {\"B3\": 125000}. Got: %s"
                    )
                    % type(parsed).__name__
                )

            # Validate each key and value.
            for key, value in parsed.items():
                if not _OVERRIDE_KEY_RE.match(key):
                    raise ValidationError(
                        _(
                            "Invalid cell reference key %(key)r in cell overrides. "
                            "Expected format: 'B3' or 'Sheet1!B3' "
                            "(letters followed by a positive integer, "
                            "with an optional 'SheetName!' prefix)."
                        )
                        % {"key": key}
                    )
                if value is not None and not isinstance(value, (int, float, str, bool)):
                    raise ValidationError(
                        _(
                            "Invalid value %(value)r for key %(key)r in cell overrides. "
                            "Values must be numbers, strings, booleans, or null."
                        )
                        % {"value": value, "key": key}
                    )

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_apply_to_copy(self):
        """Create a new spreadsheet with this scenario's cell overrides applied.

        Returns an ir.actions.act_window pointing to the new spreadsheet record.
        Does not modify the original spreadsheet.

        Implementation note:
        The current approach deep-copies spreadsheet_raw (the o-spreadsheet JSON
        blob) and writes the overridden values directly into the cell map, then
        creates a new ``spreadsheet.spreadsheet`` record with the modified JSON.
        This gives immediate value with no frontend JavaScript required.

        Long-term target: a JS side-panel that applies overrides live in the
        o-spreadsheet editor without creating a persistent copy.
        """
        self.ensure_one()
        spreadsheet = self.spreadsheet_id

        # Parse overrides (empty → nothing to apply, but we still create a copy).
        overrides = {}
        raw_overrides = self.cell_overrides
        if raw_overrides and raw_overrides.strip():
            overrides = json.loads(raw_overrides)

        # Deep-copy the source raw JSON so we never mutate the original.
        source_raw = spreadsheet.sudo().spreadsheet_raw or {}
        new_raw = copy.deepcopy(source_raw)

        sheets = new_raw.get("sheets", [])

        for key, value in overrides.items():
            sheet_name, col_idx, row_idx = _parse_override_key(key)
            if col_idx is None:
                # Already validated by constraint, but be defensive.
                continue

            # Resolve the target sheet.
            target_sheet = None
            if sheet_name:
                for s in sheets:
                    if s.get("name", "").lower() == sheet_name.lower():
                        target_sheet = s
                        break
            if target_sheet is None:
                # Fall back to first sheet (covers both "no qualifier" and
                # "sheet name not found" cases).
                if sheets:
                    target_sheet = sheets[0]

            if target_sheet is None:
                continue

            # Ensure nested dicts exist: cells → row_key → col_key
            cells = target_sheet.setdefault("cells", {})
            row_key = str(row_idx)
            col_key = str(col_idx)
            row_map = cells.setdefault(row_key, {})

            # o-spreadsheet cell data: preserve existing keys (style, format, …)
            # but update/set 'content' to the override value.
            cell_data = row_map.setdefault(col_key, {})
            cell_data["content"] = str(value) if value is not None else ""

        new_spreadsheet = self.env["spreadsheet.spreadsheet"].create(
            {
                "name": _("%(sheet)s \u2014 %(scenario)s")
                % {"sheet": spreadsheet.name, "scenario": self.name},
                "spreadsheet_raw": new_raw,
            }
        )

        return new_spreadsheet.get_formview_action()

    def action_export_comparison(self):
        """Generate a comparison of this scenario vs the base values.

        Reads base (current) cell values from spreadsheet_raw for each key in
        this scenario's overrides and returns a display_notification action
        with an HTML summary showing: cell ref, base value, override value.

        If no base scenario is found, the comparison is made against the raw
        values currently stored in the spreadsheet.
        """
        self.ensure_one()
        overrides = {}
        raw_overrides = self.cell_overrides
        if raw_overrides and raw_overrides.strip():
            overrides = json.loads(raw_overrides)

        if not overrides:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("No Overrides"),
                    "message": _("This scenario has no cell overrides defined."),
                    "type": "info",
                    "sticky": False,
                },
            }

        source_raw = self.spreadsheet_id.sudo().spreadsheet_raw or {}

        rows_html = []
        for key, override_val in overrides.items():
            sheet_name, col_idx, row_idx = _parse_override_key(key)
            if col_idx is None:
                continue
            # Reconstruct a cell_ref string that read_cell_value can parse.
            addr = key if not sheet_name else key.split("!", 1)[1]
            base_val = read_cell_value(source_raw, addr, sheet_name or None)
            base_display = str(base_val) if base_val is not None else _("(empty)")
            override_display = str(override_val) if override_val is not None else _("(null/clear)")
            rows_html.append(
                "<tr>"
                f"<td style='padding:3px 8px;border:1px solid #ddd'><b>{key}</b></td>"
                f"<td style='padding:3px 8px;border:1px solid #ddd'>{base_display}</td>"
                f"<td style='padding:3px 8px;border:1px solid #ddd'>{override_display}</td>"
                "</tr>"
            )

        table = (
            "<table style='border-collapse:collapse;font-size:13px;margin-top:8px'>"
            "<thead><tr>"
            "<th style='padding:4px 8px;border:1px solid #ccc;background:#f0f0f0'>Cell</th>"
            "<th style='padding:4px 8px;border:1px solid #ccc;background:#f0f0f0'>Base Value</th>"
            "<th style='padding:4px 8px;border:1px solid #ccc;background:#f0f0f0'>Override</th>"
            "</tr></thead>"
            "<tbody>" + "".join(rows_html) + "</tbody>"
            "</table>"
        )
        message = (
            _("<p>Scenario <b>%(name)s</b> overrides %(count)d cell(s):</p>")
            % {"name": self.name, "count": len(overrides)}
            + table
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Scenario Comparison: %s") % self.name,
                "message": message,
                "type": "info",
                "sticky": True,
            },
        }
