# Copyright 2026 Ledo Enterprises
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""
Pivot data spike — Phase 1.

Replicates the read_group strategy used by the Odoo web PivotModel
(addons/web/static/src/views/pivot/pivot_model.js) to produce pivot table
data server-side, without executing any JavaScript.

The JS pivot loads data by:
  1. Computing all row-groupby prefixes ("sections"):
       rows=["partner_id","date:month"]  →  [[], ["partner_id"],
                                              ["partner_id","date:month"]]
  2. Computing all col-groupby prefixes ("sections"):
       cols=["stage_id"]                 →  [[], ["stage_id"]]
  3. Taking the cartesian product (row_prefix × col_prefix) for "divisors".
  4. For each divisor [rowPrefix, colPrefix], calling:
       read_group(domain, fields=measureSpecs,
                  groupby=rowPrefix+colPrefix, lazy=False)

This module replicates that strategy in Python and exposes:
  - ``get_pivot_data(model, domain, context, rows, columns, measures)``

Rows / columns are lists of dimension dicts:
  {"fieldName": "date_order", "granularity": "month"}
  {"fieldName": "partner_id"}                            (no granularity)

Measures are lists of measure dicts:
  {"fieldName": "amount_total", "aggregator": "sum"}
  {"fieldName": "__count"}
"""

import itertools
import logging

from odoo import api, models
from odoo.osv.expression import AND

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers mirroring the JS helpers in pivot_model.js
# ---------------------------------------------------------------------------

DATE_GRANULARITIES = {"day", "week", "month", "quarter", "year"}


def _dimension_to_groupby(dim):
    """Convert a dimension dict to an Odoo read_group groupby string.

    {"fieldName": "date_order", "granularity": "month"}  →  "date_order:month"
    {"fieldName": "partner_id"}                           →  "partner_id"
    """
    name = dim["fieldName"]
    gran = dim.get("granularity")
    return f"{name}:{gran}" if gran else name


def _sections(lst):
    """Return all prefixes of lst including the empty prefix.

    sections(["a", "b", "c"]) → [[], ["a"], ["a", "b"], ["a", "b", "c"]]

    Mirrors the JS ``sections()`` helper.
    """
    return [lst[:i] for i in range(len(lst) + 1)]


def _measure_to_field_spec(measure):
    """Convert a measure dict to a read_group ``fields`` element.

    {"fieldName": "amount_total", "aggregator": "sum"}  →  "amount_total:sum"
    {"fieldName": "__count"}                             →  "__count"
    """
    if measure["fieldName"] == "__count":
        return "__count"
    agg = measure.get("aggregator") or "sum"
    return f"{measure['fieldName']}:{agg}"


def _measure_key(measure):
    """Return the key under which read_group stores a measure's value.

    For most measures this matches the field spec; for Many2one fields used
    as measures read_group returns a (id, name) tuple, so we just use the
    field name — the caller must handle that.
    """
    if measure["fieldName"] == "__count":
        return "__count"
    agg = measure.get("aggregator")
    if agg:
        return f"{measure['fieldName']}_{agg}"
    return measure["fieldName"]


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------


def _get_pivot_data(env, model_name, domain, context, row_dims, col_dims, measures):
    """Compute pivot table data using the same read_group strategy as the JS.

    Returns a dict:
    {
        "fields": {fieldName: {type, string, ...}},
        "groups": [
            {
                "rowValues": ["2026-01", ...],    # normalised group key values
                "colValues": ["Confirmed", ...],
                "rowGroupBy": ["date_order:month"],
                "colGroupBy": ["stage_id"],
                "count": 12,
                "measures": {"amount_total:sum": 9800.0, ...},
            },
            ...
        ],
        "rowDimensions":  [{"fieldName": ..., "granularity": ...}, ...],
        "colDimensions":  [{"fieldName": ..., "granularity": ...}, ...],
        "measureSpecs":   ["amount_total:sum", ...],
    }
    """
    Model = env[model_name].with_context(**(context or {}))

    # ── 1. Fields metadata (needed for label resolution) ─────────────────
    all_field_names = (
        [d["fieldName"] for d in row_dims + col_dims]
        + [m["fieldName"] for m in measures if m["fieldName"] != "__count"]
    )
    # fields_get returns {fieldName: {type, string, selection, ...}}
    fields_meta = Model.fields_get(all_field_names, attributes=["type", "string", "selection"])

    # ── 2. Build groupby strings ──────────────────────────────────────────
    row_groupbys = [_dimension_to_groupby(d) for d in row_dims]
    col_groupbys = [_dimension_to_groupby(d) for d in col_dims]
    measure_specs = [_measure_to_field_spec(m) for m in measures]

    # Ensure count is always fetched (JS always adds __count implicitly)
    field_specs_with_count = measure_specs + (
        [] if "__count" in measure_specs else ["__count"]
    )

    # ── 3. Compute divisors (cartesian product of all prefixes) ──────────
    row_sections = _sections(row_groupbys)
    col_sections = _sections(col_groupbys)
    divisors = list(itertools.product(row_sections, col_sections))

    # ── 4. Fire read_group for each divisor ──────────────────────────────
    groups = []
    for row_prefix, col_prefix in divisors:
        groupby = row_prefix + col_prefix
        try:
            results = Model.read_group(
                domain=domain or [],
                fields=field_specs_with_count,
                groupby=groupby,
                lazy=False,
            )
        except Exception:
            _logger.exception(
                "read_group failed for model=%s groupby=%s", model_name, groupby
            )
            continue

        for rg in results:
            group_entry = {
                "rowGroupBy": row_prefix,
                "colGroupBy": col_prefix,
                "rowValues": _extract_group_values(rg, row_prefix, fields_meta),
                "colValues": _extract_group_values(rg, col_prefix, fields_meta),
                "count": rg.get("__count", 0),
                "measures": _extract_measures(rg, measures, fields_meta),
                "domain": rg.get("__domain", []),
            }
            groups.append(group_entry)

    return {
        "fields": fields_meta,
        "groups": groups,
        "rowDimensions": row_dims,
        "colDimensions": col_dims,
        "measureSpecs": measure_specs,
    }


def _extract_group_values(rg_row, groupby_list, fields_meta):
    """Extract normalised group values from a read_group result row.

    Many2one fields return (id, display_name) — we normalise to the id (int).
    Date/datetime fields return a formatted string (Odoo already handles
    granularity in the groupby key).
    """
    values = []
    for gb_spec in groupby_list:
        field_name = gb_spec.split(":")[0]
        raw = rg_row.get(gb_spec) or rg_row.get(field_name)
        if raw is False or raw is None:
            values.append(False)
        elif isinstance(raw, (list, tuple)) and len(raw) == 2:
            # Many2one: (id, display_name) — store id; JS uses id for grouping
            values.append(raw[0])
        else:
            values.append(raw)
    return values


def _extract_measures(rg_row, measures, fields_meta):
    """Extract measure values from a read_group result row."""
    result = {}
    for measure in measures:
        fname = measure["fieldName"]
        agg = measure.get("aggregator")
        if fname == "__count":
            result["__count"] = rg_row.get("__count", 0)
            continue
        # read_group key: field_name (no aggregator suffix in result keys)
        raw = rg_row.get(fname, 0)
        if isinstance(raw, (list, tuple)):
            # Many2one used as measure — count distinct occurrences
            raw = 1 if raw else 0
        if raw is False:
            raw = 0
        spec_key = f"{fname}:{agg}" if agg else fname
        result[spec_key] = raw
    return result


# ---------------------------------------------------------------------------
# Model method (entry point for the controller)
# ---------------------------------------------------------------------------


class SpreadsheetSpreadsheet(models.Model):
    _inherit = "spreadsheet.spreadsheet"

    @api.model
    def get_pivot_data(self, model_name, domain, context, row_dims, col_dims, measures):
        """Return pivot table data computed server-side.

        Called by the forthcoming JS OcaPivotDataSource loader via JSON-RPC.

        Args:
            model_name  (str):  Technical model name, e.g. "sale.order".
            domain      (list): Odoo domain list (already evaluated).
            context     (dict): Additional context for the model.
            row_dims    (list): Dimension dicts for row groupbys.
            col_dims    (list): Dimension dicts for column groupbys.
            measures    (list): Measure dicts.

        Returns:
            dict: See _get_pivot_data() docstring.
        """
        self.ensure_one_if_in_multi()
        return _get_pivot_data(
            self.env, model_name, domain, context, row_dims, col_dims, measures
        )

    def ensure_one_if_in_multi(self):
        """No-op — get_pivot_data is a model-level call, not record-level."""
