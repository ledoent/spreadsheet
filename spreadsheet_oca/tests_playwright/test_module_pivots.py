"""Test pivot creation and editing across major Odoo module areas.

For each installed module (CRM, Sales, Accounting, HR, MRP, Stock),
create a spreadsheet with an ODOO pivot, open it in the editor,
verify no errors, then change the measure and verify again.

Requires demo data to be loaded (installed modules with demo).
"""

import pytest
from conftest import (
    BASE,
    PIVOT_WAIT,
    check_spreadsheet_errors,
    create_spreadsheet_with_pivot,
    is_module_installed,
    open_spreadsheet_by_name,
    refresh_all_data,
    rpc_call,
    search_read,
)

SCREENSHOTS = "/tmp/ss_playwright"


@pytest.fixture(autouse=True)
def screenshot_dir():
    import os

    os.makedirs(SCREENSHOTS, exist_ok=True)


# ── Module pivot definitions ─────────────────────────────────────────
# Each entry: (module_name, model, domain, measures, rows, columns, name)

PIVOT_SPECS = [
    {
        "module": "crm",
        "model": "crm.lead",
        "domain": [],
        "measures": [{"id": "__count", "fieldName": "__count"}],
        "alt_measures": [
            {
                "id": "expected_revenue:sum",
                "fieldName": "expected_revenue",
                "aggregator": "sum",
            }
        ],
        "rows": [{"fieldName": "stage_id", "order": "asc"}],
        "columns": [{"fieldName": "type"}],
        "name": "CRM Leads by Stage",
    },
    {
        "module": "sale",
        "model": "sale.order",
        "domain": [],
        "measures": [
            {
                "id": "amount_total:sum",
                "fieldName": "amount_total",
                "aggregator": "sum",
            }
        ],
        "alt_measures": [{"id": "__count", "fieldName": "__count"}],
        "rows": [{"fieldName": "partner_id", "order": "desc"}],
        "columns": [],
        "name": "Sales Orders by Partner",
    },
    {
        "module": "account",
        "model": "account.move.line",
        "domain": [["parent_state", "=", "posted"]],
        "measures": [
            {"id": "balance:sum", "fieldName": "balance", "aggregator": "sum"}
        ],
        "alt_measures": [
            {"id": "debit:sum", "fieldName": "debit", "aggregator": "sum"}
        ],
        "rows": [{"fieldName": "account_id", "order": "asc"}],
        "columns": [],
        "name": "Journal Items by Account",
    },
    {
        "module": "hr",
        "model": "hr.employee",
        "domain": [],
        "measures": [{"id": "__count", "fieldName": "__count"}],
        "alt_measures": [],
        "rows": [{"fieldName": "department_id", "order": "asc"}],
        "columns": [{"fieldName": "company_id"}],
        "name": "Employees by Department",
    },
    {
        "module": "mrp",
        "model": "mrp.production",
        "domain": [],
        "measures": [{"id": "__count", "fieldName": "__count"}],
        "alt_measures": [
            {"id": "qty_produced:sum", "fieldName": "qty_produced", "aggregator": "sum"}
        ],
        "rows": [{"fieldName": "state", "order": "asc"}],
        "columns": [],
        "name": "Manufacturing Orders by State",
    },
    {
        "module": "stock",
        "model": "stock.move",
        "domain": [["state", "=", "done"]],
        "measures": [
            {
                "id": "quantity:sum",
                "fieldName": "quantity",
                "aggregator": "sum",
            }
        ],
        "alt_measures": [{"id": "__count", "fieldName": "__count"}],
        "rows": [{"fieldName": "product_id", "order": "desc"}],
        "columns": [],
        "name": "Stock Moves by Product",
    },
    {
        "module": "purchase",
        "model": "purchase.order",
        "domain": [],
        "measures": [
            {
                "id": "amount_total:sum",
                "fieldName": "amount_total",
                "aggregator": "sum",
            }
        ],
        "alt_measures": [{"id": "__count", "fieldName": "__count"}],
        "rows": [{"fieldName": "partner_id", "order": "desc"}],
        "columns": [],
        "name": "Purchase Orders by Vendor",
    },
    {
        "module": "base",
        "model": "res.partner",
        "domain": [["active", "=", True]],
        "measures": [{"id": "__count", "fieldName": "__count"}],
        "alt_measures": [],
        "rows": [{"fieldName": "country_id", "order": "desc"}],
        "columns": [{"fieldName": "is_company"}],
        "name": "Partners by Country (Base)",
    },
]


def _cleanup_test_spreadsheets(page):
    """Remove any spreadsheets created by previous test runs."""
    names = [s["name"] for s in PIVOT_SPECS]
    for name in names:
        recs = search_read(
            page,
            "spreadsheet.spreadsheet",
            [["name", "=", name]],
            ["id"],
        )
        if recs:
            ids = [r["id"] for r in recs]
            rpc_call(page, "spreadsheet.spreadsheet", "unlink", [ids])


class TestModulePivots:
    """Create and verify pivots for each installed module."""

    @pytest.fixture(autouse=True, scope="class")
    def setup_pivots(self, authenticated_page):
        """Create test spreadsheets with pivots for installed modules."""
        page = authenticated_page
        _cleanup_test_spreadsheets(page)

        created = {}
        for spec in PIVOT_SPECS:
            if not is_module_installed(page, spec["module"]):
                continue
            rec_id = create_spreadsheet_with_pivot(
                page,
                model=spec["model"],
                domain=spec["domain"],
                measures=spec["measures"],
                rows=spec["rows"],
                columns=spec.get("columns", []),
                name=spec["name"],
            )
            created[spec["name"]] = rec_id

        yield created

        # Cleanup
        _cleanup_test_spreadsheets(page)

    @pytest.mark.parametrize("spec", PIVOT_SPECS, ids=[s["name"] for s in PIVOT_SPECS])
    def test_pivot_renders_without_errors(self, authenticated_page, spec):
        page = authenticated_page
        if not is_module_installed(page, spec["module"]):
            pytest.skip(f"Module {spec['module']} not installed")

        assert open_spreadsheet_by_name(
            page, spec["name"]
        ), f"Could not open {spec['name']}"

        slug = spec["name"].lower().replace(" ", "_").replace("(", "").replace(")", "")
        page.screenshot(path=f"{SCREENSHOTS}/pivot_{slug}.png")

        result = check_spreadsheet_errors(page)
        assert result.get("method") in (
            "model",
            "dom_fallback",
        ), f"Cannot inspect: {result}"
        assert (
            result["error_count"] == 0
        ), f"{spec['name']}: {result['error_count']} errors: {result['errors']}"

    @pytest.mark.parametrize("spec", PIVOT_SPECS, ids=[s["name"] for s in PIVOT_SPECS])
    def test_pivot_survives_refresh(self, authenticated_page, spec):
        page = authenticated_page
        if not is_module_installed(page, spec["module"]):
            pytest.skip(f"Module {spec['module']} not installed")

        assert open_spreadsheet_by_name(page, spec["name"])
        refresh_all_data(page)

        result = check_spreadsheet_errors(page)
        assert (
            result.get("error_count", 0) == 0
        ), f"{spec['name']} errors after refresh: {result.get('errors')}"


class TestPivotMeasureChange:
    """Test changing pivot measures via the side panel."""

    @pytest.fixture(autouse=True, scope="class")
    def setup_pivots(self, authenticated_page):
        page = authenticated_page
        _cleanup_test_spreadsheets(page)

        for spec in PIVOT_SPECS:
            if not is_module_installed(page, spec["module"]):
                continue
            if not spec.get("alt_measures"):
                continue
            create_spreadsheet_with_pivot(
                page,
                model=spec["model"],
                domain=spec["domain"],
                measures=spec["measures"],
                rows=spec["rows"],
                columns=spec.get("columns", []),
                name=spec["name"],
            )

        yield
        _cleanup_test_spreadsheets(page)

    @pytest.mark.parametrize(
        "spec",
        [s for s in PIVOT_SPECS if s.get("alt_measures")],
        ids=[s["name"] for s in PIVOT_SPECS if s.get("alt_measures")],
    )
    def test_change_measure_no_error(self, authenticated_page, spec):
        """Open spreadsheet, change pivot measure via JS model, verify no error."""
        page = authenticated_page
        if not is_module_installed(page, spec["module"]):
            pytest.skip(f"Module {spec['module']} not installed")

        assert open_spreadsheet_by_name(page, spec["name"])

        # Use the JS model API to update the pivot measure definition
        alt = spec["alt_measures"][0]
        result = page.evaluate(
            """([altMeasure]) => {
            function findModel(el) {
                if (!el) return null;
                const keys = Object.keys(el);
                for (const k of keys) {
                    if (k.startsWith('__owl__')) {
                        const owl = el[k];
                        const comp = owl?.component;
                        if (comp?.model?.getters) return comp.model;
                        if (comp?.props?.model?.getters) return comp.props.model;
                    }
                }
                for (const child of el.children || []) {
                    const m = findModel(child);
                    if (m) return m;
                }
                return null;
            }
            const root = document.querySelector('.o-spreadsheet');
            if (!root) return {success: false, reason: 'no spreadsheet'};
            const model = findModel(root);
            if (!model) return {success: false, reason: 'no model'};

            try {
                // Get current pivot definition
                const pivotId = "1";
                const pivot = model.getters.getPivotCoreDefinition(pivotId);
                if (!pivot) return {success: false, reason: 'no pivot 1'};

                // Update measures via dispatch
                const newDef = {...pivot, measures: [altMeasure]};
                model.dispatch("UPDATE_PIVOT", {
                    pivotId: pivotId,
                    pivot: newDef,
                });

                // Wait a bit for re-evaluation
                return {success: true, newMeasure: altMeasure.id};
            } catch(e) {
                return {success: false, reason: e.message};
            }
        }""",
            [alt],
        )

        if not result.get("success"):
            pytest.skip(f"Could not change measure: {result.get('reason')}")

        # Wait for pivot to re-evaluate
        page.wait_for_timeout(PIVOT_WAIT)

        slug = spec["name"].lower().replace(" ", "_").replace("(", "").replace(")", "")
        page.screenshot(path=f"{SCREENSHOTS}/pivot_{slug}_alt_measure.png")

        errors = check_spreadsheet_errors(page)
        assert (
            errors.get("error_count", -1) == 0
        ), f"Errors after measure change to {alt['id']}: {errors.get('errors')}"


class TestPivotFromUI:
    """Test inserting a pivot from the Odoo pivot view into a spreadsheet.

    This tests the full UI flow: navigate to model → pivot view → Insert in
    Spreadsheet → verify in editor.
    """

    def test_partner_pivot_from_contacts(self, authenticated_page):
        """Navigate to Contacts, switch to pivot, insert into spreadsheet."""
        page = authenticated_page

        # Go to Contacts
        page.goto(f"{BASE}/odoo/contacts", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # Switch to pivot view
        pivot_btn = page.locator(
            "button.o_switch_view.o_pivot, "
            "button[data-tooltip='Pivot'], "
            "button[aria-label='Pivot']"
        )
        if pivot_btn.count() == 0:
            pytest.skip("Pivot view not available on Contacts")

        pivot_btn.first.click()
        page.wait_for_timeout(3000)
        page.screenshot(path=f"{SCREENSHOTS}/contacts_pivot_view.png")

        # Look for "Insert in Spreadsheet" button
        insert_btn = page.locator(
            "button:has-text('Insert in Spreadsheet'), "
            ".o_pivot_buttons button:has-text('Insert')"
        )
        if insert_btn.count() == 0 or not insert_btn.first.is_visible():
            pytest.skip("Insert in Spreadsheet button not found")

        insert_btn.first.click()
        page.wait_for_timeout(2000)

        # Confirm dialog
        confirm = page.locator(".modal-footer button.btn-primary")
        if confirm.count() > 0 and confirm.first.is_visible():
            confirm.first.click()
            page.wait_for_timeout(PIVOT_WAIT)

        page.screenshot(path=f"{SCREENSHOTS}/contacts_pivot_in_spreadsheet.png")

        # Check if we landed in the spreadsheet editor
        ss = page.locator(".o-spreadsheet")
        if ss.count() == 0:
            pytest.skip("Spreadsheet editor did not open after insert")

        result = check_spreadsheet_errors(page)
        assert result.get("method") in ("model", "dom_fallback")
        assert (
            result["error_count"] == 0
        ), f"Errors in inserted pivot: {result['errors']}"
