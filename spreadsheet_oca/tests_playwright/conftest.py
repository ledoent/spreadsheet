"""Playwright test configuration for spreadsheet_oca.

Provides shared fixtures for browser, authenticated page, and Odoo helpers.
Requires: pip install playwright pytest-playwright
Then: playwright install chromium
"""

import json
import os

import pytest
from playwright.sync_api import Page

BASE = os.environ.get("ODOO_URL", "http://localhost:8069")
DB = os.environ.get("ODOO_DB", "odoo_demo_spreadsheet")
LOGIN = os.environ.get("ODOO_LOGIN", "admin")
PASSWORD = os.environ.get("ODOO_PASSWORD", "admin")
TIMEOUT = 15_000
PIVOT_WAIT = 10_000  # pivot data load + render


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 1400, "height": 900},
    }


@pytest.fixture(scope="session")
def authenticated_page(browser):
    """Login once per session, return a persistent page."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    odoo_login(page)
    yield page
    ctx.close()


def odoo_login(page: Page):
    """Log into Odoo."""
    page.goto(f"{BASE}/web/login?db={DB}", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    # Handle DB selector if shown
    db_link = page.locator(f"a:has-text('{DB}')")
    if db_link.count() > 0 and db_link.first.is_visible():
        db_link.first.click()
        page.wait_for_timeout(2000)
    page.wait_for_selector("input[name='login']", state="visible", timeout=TIMEOUT)
    page.fill("input[name='login']", LOGIN)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_timeout(5000)


# ── Odoo RPC helpers ──────────────────────────────────────────────────


def rpc_call(page: Page, model: str, method: str, args=None, kwargs=None):
    """Call an Odoo JSON-RPC method and return the result."""
    args = args or []
    kwargs = kwargs or {}
    return page.evaluate(
        """([model, method, args, kwargs]) => {
        return fetch('/web/dataset/call_kw', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                jsonrpc: '2.0', method: 'call', id: 1,
                params: { model, method, args, kwargs }
            })
        }).then(r => r.json()).then(d => d.result);
    }""",
        [model, method, args, kwargs],
    )


def is_module_installed(page: Page, module_name: str) -> bool:
    """Check if an Odoo module is installed."""
    result = rpc_call(
        page,
        "ir.module.module",
        "search_read",
        [[["name", "=", module_name], ["state", "=", "installed"]]],
        {"fields": ["id"], "limit": 1},
    )
    return bool(result)


def search_read(page: Page, model: str, domain, fields, limit=0):
    """Shorthand for search_read."""
    return rpc_call(
        page,
        model,
        "search_read",
        [domain],
        {"fields": fields, "limit": limit},
    )


def get_spreadsheet_action_id(page: Page) -> int:
    """Get the action ID for the spreadsheet kanban view."""
    result = search_read(
        page,
        "ir.actions.act_window",
        [["res_model", "=", "spreadsheet.spreadsheet"]],
        ["id"],
        limit=1,
    )
    return result[0]["id"] if result else 143


# ── Spreadsheet interaction helpers ──────────────────────────────────


def open_spreadsheet_by_name(page: Page, name: str, action_id: int = None):
    """Navigate to a spreadsheet editor by record name.

    Returns True if successfully opened, False otherwise.
    """
    if action_id is None:
        action_id = get_spreadsheet_action_id(page)

    rec = search_read(
        page,
        "spreadsheet.spreadsheet",
        [["name", "=", name]],
        ["id"],
        limit=1,
    )
    if not rec:
        return False

    rec_id = rec[0]["id"]
    page.goto(
        f"{BASE}/odoo/action-{action_id}/{rec_id}",
        wait_until="domcontentloaded",
    )
    page.wait_for_timeout(3000)

    edit_btn = page.locator("button:has-text('Edit')").first
    if edit_btn.count() > 0 and edit_btn.is_visible():
        edit_btn.click()
        page.wait_for_timeout(PIVOT_WAIT)
        return True
    return False


def _find_model_js():
    """JS code to find the o-spreadsheet model from OWL components."""
    return """
        function findModel(el) {
            if (!el) return null;
            // Try all own properties for OWL component references
            const keys = Object.getOwnPropertyNames(el);
            for (const k of keys) {
                try {
                    const val = el[k];
                    if (val && typeof val === 'object') {
                        // OWL 2 pattern: __owl__ prefix
                        if (k.startsWith('__owl__')) {
                            const comp = val?.component;
                            if (comp?.model?.getters) return comp.model;
                            if (comp?.props?.model?.getters) return comp.props.model;
                        }
                        // Direct component reference
                        if (val?.component?.model?.getters) return val.component.model;
                    }
                } catch(e) {}
            }
            for (const child of el.children || []) {
                const m = findModel(child);
                if (m) return m;
            }
            return null;
        }

        // Strategy 1: Walk DOM from .o-spreadsheet root
        const root = document.querySelector('.o-spreadsheet');
        if (!root) return null;
        let model = findModel(root);
        if (model) return model;

        // Strategy 2: Try parent elements
        let parent = root.parentElement;
        for (let i = 0; i < 5 && parent; i++) {
            model = findModel(parent);
            if (model) return model;
            parent = parent.parentElement;
        }

        // Strategy 3: Try querying for the OWL app root
        const appRoot = document.querySelector('.o_action_manager, .o_content, [owl-app]');
        if (appRoot) {
            model = findModel(appRoot);
            if (model) return model;
        }

        return null;
    """


def check_spreadsheet_errors(page: Page):
    """Inspect the active spreadsheet for #ERROR cells via the JS model API.

    Returns dict with keys: error_count, ok_count, errors (list), sample (list).
    """
    result = page.evaluate(
        """() => {
        try {
            %s
            const model = (function() { %s })();
            if (!model) {
                // Fallback: scan DOM text for #ERROR/#SPILL
                const body = document.body.innerText;
                const errCount = (body.match(/#ERROR/g) || []).length;
                const spillCount = (body.match(/#SPILL/g) || []).length;
                return {
                    method: 'dom_fallback',
                    error_count: errCount,
                    spill_count: spillCount,
                    ok_count: 0,
                    errors: errCount > 0 ? [{cell: '?', msg: errCount + ' #ERROR in DOM'}] : [],
                    sample: []
                };
            }

            const sheetId = model.getters.getActiveSheetId();
            const cells = model.getters.getCells(sheetId);
            let errors = [], values = [];
            for (const [cellId] of Object.entries(cells)) {
                const pos = model.getters.getCellPosition(cellId);
                const ev = model.getters.getEvaluatedCell(pos);
                const col = String.fromCharCode(65 + pos.col);
                const ref = col + (pos.row + 1);
                if (ev.type === 'error') {
                    errors.push({cell: ref, msg: ev.message || String(ev.value)});
                } else if (ev.value !== undefined && ev.value !== '' && ev.value !== null) {
                    values.push({cell: ref, val: String(ev.value).substring(0, 40)});
                }
            }
            return {
                method: 'model',
                sheet: model.getters.getSheetName(sheetId),
                error_count: errors.length,
                errors: errors.slice(0, 20),
                ok_count: values.length,
                sample: values.slice(0, 10)
            };
        } catch(e) {
            return {method: 'exception', error: e.message};
        }
    }"""
        % ("", _find_model_js())
    )
    return result


def get_all_sheet_errors(page: Page):
    """Check ALL sheets in the spreadsheet for errors, not just the active one."""
    result = page.evaluate(
        """() => {
        try {
            const model = (function() { %s })();
            if (!model) {
                const body = document.body.innerText;
                const errCount = (body.match(/#ERROR/g) || []).length;
                return {
                    method: 'dom_fallback',
                    error_count: errCount,
                    ok_count: 0,
                    errors: errCount > 0 ? [{sheet: '?', cell: '?', msg: errCount + ' #ERROR in DOM'}] : []
                };
            }

            const sheetIds = model.getters.getSheetIds();
            let allErrors = [];
            let totalOk = 0;
            for (const sheetId of sheetIds) {
                const sheetName = model.getters.getSheetName(sheetId);
                const cells = model.getters.getCells(sheetId);
                for (const [cellId] of Object.entries(cells)) {
                    const pos = model.getters.getCellPosition(cellId);
                    const ev = model.getters.getEvaluatedCell(pos);
                    const col = String.fromCharCode(65 + pos.col);
                    const ref = col + (pos.row + 1);
                    if (ev.type === 'error') {
                        allErrors.push({sheet: sheetName, cell: ref, msg: ev.message || String(ev.value)});
                    } else if (ev.value !== undefined && ev.value !== '' && ev.value !== null) {
                        totalOk++;
                    }
                }
            }
            return {
                method: 'model',
                sheets: sheetIds.length,
                error_count: allErrors.length,
                errors: allErrors.slice(0, 30),
                ok_count: totalOk
            };
        } catch(e) {
            return {method: 'exception', error: e.message};
        }
    }"""
        % _find_model_js()
    )
    return result


def refresh_all_data(page: Page):
    """Click Data > Refresh All Data in the spreadsheet toolbar."""
    data_menu = page.locator(
        ".o-spreadsheet-topbar .o-topbar-menu:has-text('Data'), "
        ".o-menu-item:has-text('Data')"
    )
    if data_menu.count() > 0:
        data_menu.first.click()
        page.wait_for_timeout(1000)
        refresh_item = page.locator(
            ".o-menu-item:has-text('Refresh'), " ".o-dropdown-item:has-text('Refresh')"
        )
        if refresh_item.count() > 0:
            refresh_item.first.click()
            page.wait_for_timeout(PIVOT_WAIT)
            return True
        page.keyboard.press("Escape")
    return False


# ── Pivot creation helpers ────────────────────────────────────────────


def navigate_to_pivot_view(page: Page, menu_path: str):
    """Navigate to a view by clicking through the Odoo menu.

    menu_path is like "CRM/Pipeline" or "Sales/Orders".
    Returns True if a list/pivot view is visible.
    """
    parts = menu_path.split("/")
    for i, part in enumerate(parts):
        selector = (
            f".o_menu_entry:has-text('{part}'), .o_menu_header:has-text('{part}')"
        )
        menu = page.locator(selector).first
        if menu.count() > 0 and menu.is_visible():
            menu.click()
            page.wait_for_timeout(1500)
        else:
            # Try the top-level app menu
            app = page.locator(f".o_app:has-text('{part}')").first
            if app.count() > 0 and app.is_visible():
                app.click()
                page.wait_for_timeout(2000)
            else:
                return False
    return True


def switch_to_pivot_view(page: Page) -> bool:
    """Switch the current view to pivot mode."""
    pivot_btn = page.locator(
        "button.o_switch_view.o_pivot, "
        ".o_switch_view[data-tooltip='Pivot'], "
        "button[aria-label='Pivot']"
    )
    if pivot_btn.count() > 0:
        pivot_btn.first.click()
        page.wait_for_timeout(3000)
        return True
    return False


def insert_pivot_in_spreadsheet(page: Page) -> bool:
    """Click 'Insert in Spreadsheet' from a pivot view.

    Returns True if the spreadsheet editor opens.
    """
    insert_btn = page.locator(
        "button:has-text('Insert in Spreadsheet'), "
        "button[aria-label='Add to spreadsheet']"
    )
    if insert_btn.count() > 0 and insert_btn.first.is_visible():
        insert_btn.first.click()
        page.wait_for_timeout(2000)

        # Handle the dialog — click "New Spreadsheet" or confirm
        confirm = page.locator(
            ".modal-footer button.btn-primary, "
            "button:has-text('Confirm'), "
            "button:has-text('New Spreadsheet')"
        )
        if confirm.count() > 0 and confirm.first.is_visible():
            confirm.first.click()
            page.wait_for_timeout(PIVOT_WAIT)

        # Check if we're in the spreadsheet editor
        return page.locator(".o-spreadsheet").count() > 0
    return False


def create_spreadsheet_with_pivot(
    page: Page,
    model: str,
    domain: list,
    measures: list,
    rows: list,
    columns: list = None,
    name: str = None,
):
    """Create a spreadsheet with a pivot via JSON-RPC.

    This is faster and more reliable than UI interaction for test setup.
    Returns the spreadsheet record ID.
    """
    columns = columns or []
    pivot_def = {
        "type": "ODOO",
        "id": "1",
        "formulaId": "1",
        "name": name or f"Test Pivot ({model})",
        "model": model,
        "domain": domain,
        "context": {},
        "measures": measures,
        "rows": rows,
        "columns": columns,
        "sortedColumn": None,
        "fieldMatching": {},
    }

    # Build cell formulas for a basic pivot layout
    cells = {
        "A1": {"style": 1, "content": name or f"Pivot: {model}"},
        "A3": {"style": 2, "content": "Group"},
        "B3": {"style": 2, "content": "Value"},
    }
    # Add 10 rows of pivot data
    for i in range(1, 11):
        row_idx = i + 3
        cells[f"A{row_idx}"] = {
            "content": f'=PIVOT.HEADER(1,"#{rows[0]["fieldName"]}",{i})'
        }
        measure_id = measures[0]["id"]
        cells[f"B{row_idx}"] = {
            "content": f'=PIVOT.VALUE(1,"{measure_id}","#{rows[0]["fieldName"]}",{i})'
        }
    # Grand total
    cells["A15"] = {"style": 2, "content": "Total"}
    measure_id = measures[0]["id"]
    cells["B15"] = {
        "content": f'=PIVOT.VALUE(1,"{measure_id}")',
        "style": 3,
    }

    spreadsheet_data = {
        "version": 21,
        "sheets": [
            {
                "id": "sheet1",
                "name": "Pivot Data",
                "colNumber": 26,
                "rowNumber": 100,
                "rows": {},
                "cols": {"0": {"size": 200}, "1": {"size": 120}},
                "merges": [],
                "cells": cells,
                "conditionalFormats": [],
                "figures": [],
                "filterTables": [],
                "tables": [],
                "dataValidationRules": [],
                "comments": {},
                "headerGroups": {"ROW": [], "COL": []},
                "areGridLinesVisible": True,
                "isVisible": True,
            }
        ],
        "settings": {},
        "customTableStyles": {},
        "styles": {
            "1": {"bold": True, "fontSize": 14},
            "2": {"bold": True, "align": "center", "fillColor": "#E8E8E8"},
            "3": {"bold": True},
        },
        "formats": {},
        "borders": {},
        "revisionId": "START_REVISION",
        "uniqueFigureIds": True,
        "odooVersion": 12,
        "globalFilters": [],
        "pivots": {"1": pivot_def},
        "pivotNextId": 2,
        "lists": {},
        "listNextId": 1,
        "chartOdooMenusReferences": {},
    }

    import base64

    data_b64 = base64.b64encode(json.dumps(spreadsheet_data).encode()).decode()

    result = rpc_call(
        page,
        "spreadsheet.spreadsheet",
        "create",
        [
            {
                "name": name or f"Test Pivot ({model})",
                "spreadsheet_binary_data": data_b64,
            }
        ],
    )
    return result
