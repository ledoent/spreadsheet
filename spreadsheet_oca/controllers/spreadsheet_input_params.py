# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""
JSON endpoint for spreadsheet input parameters — Feature 4.

Provides a lightweight API so that a future JS plugin can read the current
named-parameter values without having to parse spreadsheet_raw itself.
"""
from odoo.http import Controller, request, route


class SpreadsheetInputParamsController(Controller):
    @route(
        "/spreadsheet/input_params/<int:spreadsheet_id>",
        type="json",
        auth="user",
        methods=["POST"],
    )
    def get_input_params(self, spreadsheet_id):
        """Return ``{name: current_value}`` for all active parameters on this spreadsheet."""
        params = request.env["spreadsheet.input_param"].search(
            [
                ("spreadsheet_id", "=", spreadsheet_id),
                ("active", "=", True),
            ]
        )
        return {p.name: p.current_value for p in params}
