# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""
Headless XLSX Export — Feature 6.

Server-side generation of .xlsx files from spreadsheet.spreadsheet records,
without requiring a browser session. Addresses the gap described in
odoo/o-spreadsheet Issue #8061 (filed 2026-03-06).

Two rendering strategies:
  1. Static cells: reads cell values from spreadsheet_raw JSON and writes
     them verbatim to the worksheet (preserves text, numbers, booleans).
  2. Pivot sheets: for each ODOO pivot in the spreadsheet JSON, a dedicated
     worksheet is generated with fresh data from _get_pivot_data(). This
     ensures exported pivots reflect the current Odoo database state rather
     than the snapshot saved client-side.

The result is attached to the spreadsheet's Chatter and/or returned as an
ir.actions.act_url download.

Usage from Python:
    xlsx_bytes = SpreadsheetXlsxExporter(env, spreadsheet).render()

Usage from Odoo UI:
    spreadsheet.action_export_xlsx()   # returns download action
"""
import base64
import io
import logging

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from odoo import _, api, fields, models

from .pivot_data import _get_pivot_data

_logger = logging.getLogger(__name__)

# Header row style for pivot sheets
_PIVOT_HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
_PIVOT_HEADER_FONT = Font(color="FFFFFF", bold=True)
_PIVOT_SUBHEADER_FILL = PatternFill(start_color="D6DCF0", end_color="D6DCF0", fill_type="solid")
_PIVOT_SUBHEADER_FONT = Font(bold=True)
_PIVOT_TOTAL_FONT = Font(bold=True, italic=True)


class SpreadsheetXlsxExporter:
    """
    Renders a spreadsheet.spreadsheet record to an openpyxl Workbook.

    Call .render() to get a bytes object suitable for attachment or download.
    """

    def __init__(self, env, spreadsheet):
        self.env = env
        self.spreadsheet = spreadsheet
        self.raw = spreadsheet.sudo().spreadsheet_raw or {}

    def render(self):
        """Return the workbook as a bytes object."""
        wb = openpyxl.Workbook()
        wb.remove(wb.active)  # remove default empty sheet

        sheets = self.raw.get("sheets", [])
        pivots = self.raw.get("pivots", {})

        # ── Render static sheet(s) ────────────────────────────────────────────
        for sheet_def in sheets:
            sheet_name = sheet_def.get("name", "Sheet")[:31]  # Excel limit
            ws = wb.create_sheet(title=sheet_name)
            self._render_static_sheet(ws, sheet_def)

        # ── Render one worksheet per ODOO pivot (with fresh data) ─────────────
        for pivot_id, pivot_def in pivots.items():
            if pivot_def.get("type") != "ODOO":
                continue
            pivot_name = pivot_def.get("name") or f"Pivot #{pivot_id}"
            ws_name = (pivot_name[:28] + " †") if len(pivot_name) > 28 else pivot_name
            # Deduplicate sheet names (Excel requires unique names)
            existing = [s.title for s in wb.worksheets]
            if ws_name in existing:
                ws_name = f"{ws_name[:27]}_{pivot_id}"
            ws = wb.create_sheet(title=ws_name)
            self._render_pivot_sheet(ws, pivot_def, pivot_name)

        if not wb.worksheets:
            ws = wb.create_sheet(title="Empty")
            ws["A1"] = _("This spreadsheet has no sheets.")

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # ── Static sheet renderer ─────────────────────────────────────────────────

    def _render_static_sheet(self, ws, sheet_def):
        """Copy static cell values from the sheet JSON to the worksheet."""
        cells = sheet_def.get("cells", {})
        # cells is {row_str: {col_str: {content, style, ...}}}
        for row_str, row_data in cells.items():
            try:
                row_idx = int(row_str) + 1  # 1-based for openpyxl
            except (ValueError, TypeError):
                continue
            if not isinstance(row_data, dict):
                continue
            for col_str, cell_data in row_data.items():
                try:
                    col_idx = int(col_str) + 1
                except (ValueError, TypeError):
                    continue
                if not isinstance(cell_data, dict):
                    continue
                content = cell_data.get("content", "")
                if content is None or content == "":
                    continue
                # Strip leading "=" for formula cells — write as string
                # (server-side we can't evaluate formulas)
                if isinstance(content, str) and content.startswith("="):
                    # Write formula placeholder so user can see what was there
                    ws.cell(row=row_idx, column=col_idx).value = content
                else:
                    # Try numeric conversion
                    ws.cell(row=row_idx, column=col_idx).value = _coerce_value(content)

        # Apply column auto-width (rough estimate)
        _auto_width(ws)

    # ── Pivot sheet renderer ──────────────────────────────────────────────────

    def _render_pivot_sheet(self, ws, pivot_def, display_name):
        """Fetch fresh pivot data and render a formatted table."""
        model_name = pivot_def.get("model", "")
        if not model_name or model_name not in self.env:
            ws["A1"] = _("Unknown model: %s") % model_name
            return

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
        except Exception:
            _logger.exception("XLSX export: failed to get pivot data for %s", display_name)
            ws["A1"] = _("Failed to load pivot data.")
            return

        row_dims = result.get("rowDimensions", [])
        col_dims = result.get("colDimensions", [])
        groups = result.get("groups", [])
        measure_specs = result.get("measureSpecs", [])

        # Title row
        title_cell = ws.cell(row=1, column=1, value=display_name)
        title_cell.font = Font(bold=True, size=13)
        ws.merge_cells(
            start_row=1, start_column=1,
            end_row=1, end_column=max(1, len(row_dims) + len(col_dims) + len(measure_specs)),
        )

        # Subtitle: model + domain
        model_label = model_name
        try:
            model_label = self.env["ir.model"]._get(model_name).name or model_name
        except Exception:
            pass
        ws.cell(row=2, column=1, value=f"{model_label}").font = Font(italic=True, color="666666")
        current_row = 4

        # Build a flat table: row_headers | col_headers | measures
        if not row_dims and not col_dims:
            # Grand total only
            current_row = self._write_grand_total(ws, groups, measure_specs, current_row)
        elif not col_dims:
            # Row-only pivot (simple breakdown)
            current_row = self._write_row_pivot(ws, groups, row_dims, measure_specs, current_row)
        else:
            # Full cross-tab pivot
            current_row = self._write_crosstab(
                ws, groups, row_dims, col_dims, measure_specs, current_row
            )

        _auto_width(ws)

    def _write_grand_total(self, ws, groups, measure_specs, start_row):
        totals = [g for g in groups if not g["rowGroupBy"] and not g["colGroupBy"]]
        if not totals:
            return start_row
        gt = totals[0]
        # Header
        headers = ["Total"] + [_format_measure_name(m) for m in measure_specs] + ["Count"]
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=start_row, column=ci, value=h)
            cell.fill = _PIVOT_HEADER_FILL
            cell.font = _PIVOT_HEADER_FONT
        # Values
        row_vals = [_("Grand Total")]
        for spec in measure_specs:
            row_vals.append(gt.get("measures", {}).get(spec))
        row_vals.append(gt.get("count", 0))
        for ci, v in enumerate(row_vals, 1):
            ws.cell(row=start_row + 1, column=ci, value=v)
        return start_row + 3

    def _write_row_pivot(self, ws, groups, row_dims, measure_specs, start_row):
        row_gb = [d["fieldName"] for d in row_dims]
        row_groups = sorted(
            [g for g in groups if g["rowGroupBy"] == row_gb and not g["colGroupBy"]],
            key=lambda g: [str(v) for v in g["rowValues"]],
        )
        totals = [g for g in groups if not g["rowGroupBy"] and not g["colGroupBy"]]

        # Header row
        headers = [d["fieldName"] for d in row_dims]
        headers += [_format_measure_name(m) for m in measure_specs]
        headers.append("Count")
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(row=start_row, column=ci, value=h)
            cell.fill = _PIVOT_HEADER_FILL
            cell.font = _PIVOT_HEADER_FONT
        r = start_row + 1

        for g in row_groups:
            for ci, v in enumerate(g["rowValues"], 1):
                ws.cell(row=r, column=ci, value=v)
            offset = len(row_dims)
            for si, spec in enumerate(measure_specs):
                ws.cell(row=r, column=offset + si + 1, value=g.get("measures", {}).get(spec))
            ws.cell(row=r, column=offset + len(measure_specs) + 1, value=g.get("count", 0))
            r += 1

        # Grand total row
        if totals:
            gt = totals[0]
            cell = ws.cell(row=r, column=1, value=_("Grand Total"))
            cell.font = _PIVOT_TOTAL_FONT
            offset = len(row_dims)
            for si, spec in enumerate(measure_specs):
                c = ws.cell(row=r, column=offset + si + 1,
                             value=gt.get("measures", {}).get(spec))
                c.font = _PIVOT_TOTAL_FONT
            ws.cell(row=r, column=offset + len(measure_specs) + 1,
                    value=gt.get("count", 0)).font = _PIVOT_TOTAL_FONT
            r += 1

        return r + 1

    def _write_crosstab(self, ws, groups, row_dims, col_dims, measure_specs, start_row):
        """Write a cross-tabulation with row headers on left, column values across top."""
        row_gb = [d["fieldName"] for d in row_dims]
        col_gb = [d["fieldName"] for d in col_dims]

        # Collect unique col values
        col_groups = sorted(
            [g for g in groups if g["colGroupBy"] == col_gb and not g["rowGroupBy"]],
            key=lambda g: [str(v) for v in g["colValues"]],
        )
        col_keys = [tuple(g["colValues"]) for g in col_groups]

        # Collect unique row values
        row_groups = sorted(
            [g for g in groups if g["rowGroupBy"] == row_gb and not g["colGroupBy"]],
            key=lambda g: [str(v) for v in g["rowValues"]],
        )

        # Cell value lookup: (row_values_tuple, col_values_tuple) → group
        cell_map = {}
        for g in groups:
            if g["rowGroupBy"] == row_gb and g["colGroupBy"] == col_gb:
                cell_map[(tuple(g["rowValues"]), tuple(g["colValues"]))] = g

        grand_totals = [g for g in groups if not g["rowGroupBy"] and not g["colGroupBy"]]

        r = start_row
        num_row_dims = len(row_dims)

        # ── Column headers ────────────────────────────────────────────────────
        # Row: row dim labels | col1_val | col2_val | … | Total
        for ci in range(num_row_dims):
            ws.cell(row=r, column=ci + 1, value=row_dims[ci]["fieldName"]).font = Font(bold=True)

        for ki, col_key in enumerate(col_keys):
            label = " / ".join(str(v) for v in col_key) if col_key else _("(none)")
            col_start = num_row_dims + ki * len(measure_specs) + 1
            if len(measure_specs) > 1:
                ws.merge_cells(
                    start_row=r, start_column=col_start,
                    end_row=r, end_column=col_start + len(measure_specs) - 1,
                )
            cell = ws.cell(row=r, column=col_start, value=label)
            cell.fill = _PIVOT_HEADER_FILL
            cell.font = _PIVOT_HEADER_FONT
            cell.alignment = Alignment(horizontal="center")

        # Total column
        total_col = num_row_dims + len(col_keys) * len(measure_specs) + 1
        ws.cell(row=r, column=total_col, value=_("Total")).fill = _PIVOT_HEADER_FILL
        ws.cell(row=r, column=total_col).font = _PIVOT_HEADER_FONT
        r += 1

        # Measure sub-headers if multiple measures
        if len(measure_specs) > 1:
            for ki in range(len(col_keys)):
                for si, spec in enumerate(measure_specs):
                    col_start = num_row_dims + ki * len(measure_specs) + si + 1
                    cell = ws.cell(row=r, column=col_start, value=_format_measure_name(spec))
                    cell.fill = _PIVOT_SUBHEADER_FILL
                    cell.font = _PIVOT_SUBHEADER_FONT
            r += 1

        # ── Data rows ─────────────────────────────────────────────────────────
        for rg in row_groups:
            row_key = tuple(rg["rowValues"])
            for ci, v in enumerate(rg["rowValues"], 1):
                ws.cell(row=r, column=ci, value=v)
            for ki, col_key in enumerate(col_keys):
                cell_group = cell_map.get((row_key, col_key))
                for si, spec in enumerate(measure_specs):
                    col_pos = num_row_dims + ki * len(measure_specs) + si + 1
                    val = cell_group.get("measures", {}).get(spec) if cell_group else None
                    ws.cell(row=r, column=col_pos, value=val)
            # Row total
            row_total = [g for g in groups
                         if g["rowGroupBy"] == row_gb and not g["colGroupBy"]
                         and tuple(g["rowValues"]) == row_key]
            if row_total and measure_specs:
                val = row_total[0].get("measures", {}).get(measure_specs[0])
                ws.cell(row=r, column=total_col, value=val).font = _PIVOT_TOTAL_FONT
            r += 1

        # Grand total row
        if grand_totals:
            gt = grand_totals[0]
            cell = ws.cell(row=r, column=1, value=_("Grand Total"))
            cell.font = _PIVOT_TOTAL_FONT
            if len(measure_specs) > 0:
                ws.cell(row=r, column=total_col,
                        value=gt.get("measures", {}).get(measure_specs[0])).font = _PIVOT_TOTAL_FONT
            r += 1

        return r + 1


def _coerce_value(v):
    """Try to return v as int or float; otherwise return the string."""
    if isinstance(v, (int, float, bool)):
        return v
    s = str(v).strip()
    try:
        i = int(s)
        return i
    except (ValueError, TypeError):
        pass
    try:
        f = float(s.replace(",", ""))
        return f
    except (ValueError, TypeError):
        pass
    return s


def _format_measure_name(spec):
    """Turn 'amount_total:sum' into 'Amount Total (Sum)'."""
    if ":" in spec:
        field, agg = spec.split(":", 1)
        return f"{field.replace('_', ' ').title()} ({agg.title()})"
    return spec.replace("_", " ").title()


def _auto_width(ws, max_width=60):
    """Set approximate column widths based on cell content length."""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, max_width)


# ── Model method (entry point) ────────────────────────────────────────────────


class SpreadsheetSpreadsheet(models.Model):
    _inherit = "spreadsheet.spreadsheet"

    def action_export_xlsx(self):
        """
        Export this spreadsheet as .xlsx and return a download action.

        Static cells are written verbatim. Each ODOO pivot gets a dedicated
        worksheet with fresh server-side data.
        """
        self.ensure_one()
        exporter = SpreadsheetXlsxExporter(self.env, self)
        xlsx_bytes = exporter.render()

        filename = f"{self.name or 'spreadsheet'}.xlsx"
        attachment = self.env["ir.attachment"].create(
            {
                "name": filename,
                "type": "binary",
                "datas": base64.b64encode(xlsx_bytes),
                "mimetype": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                "res_model": self._name,
                "res_id": self.id,
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    @api.model
    def get_xlsx_bytes(self, spreadsheet_id):
        """
        Return raw .xlsx bytes for a spreadsheet (callable via JSON-RPC).
        Useful for scheduled email attachments.
        """
        spreadsheet = self.browse(spreadsheet_id)
        spreadsheet.check_access("read")
        exporter = SpreadsheetXlsxExporter(self.env, spreadsheet)
        return base64.b64encode(exporter.render()).decode()
