"""Test demo spreadsheets load and render without errors."""
import pytest
from conftest import (
    BASE,
    check_spreadsheet_errors,
    get_all_sheet_errors,
    get_spreadsheet_action_id,
    open_spreadsheet_by_name,
    refresh_all_data,
)

SCREENSHOTS = "/tmp/ss_playwright"


@pytest.fixture(autouse=True)
def screenshot_dir():
    import os
    os.makedirs(SCREENSHOTS, exist_ok=True)


class TestDemoSpreadsheets:
    """Verify the 3 demo spreadsheets render correctly."""

    def test_kanban_shows_all_demos(self, authenticated_page):
        page = authenticated_page
        action_id = get_spreadsheet_action_id(page)
        page.goto(
            f"{BASE}/odoo/action-{action_id}",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(3000)

        expected = [
            "Sales Pipeline Summary",
            "KPI Dashboard",
            "Partner Pivot Dashboard",
        ]
        for name in expected:
            cards = page.locator(f".o_kanban_record:has-text('{name}')")
            assert cards.count() > 0, f"Missing kanban card: {name}"

    def test_partner_pivot_dashboard(self, authenticated_page):
        page = authenticated_page
        assert open_spreadsheet_by_name(page, "Partner Pivot Dashboard")
        page.screenshot(path=f"{SCREENSHOTS}/pivot_dashboard.png")

        result = check_spreadsheet_errors(page)
        assert result.get("method") in ("model", "dom_fallback"), (
            f"Could not inspect: {result}"
        )
        assert result["error_count"] == 0, (
            f"Pivot dashboard has {result['error_count']} errors: {result['errors']}"
        )

    def test_partner_pivot_refresh(self, authenticated_page):
        page = authenticated_page
        assert open_spreadsheet_by_name(page, "Partner Pivot Dashboard")
        assert refresh_all_data(page), "Could not trigger Data > Refresh"
        page.screenshot(path=f"{SCREENSHOTS}/pivot_after_refresh.png")

        result = check_spreadsheet_errors(page)
        assert result.get("error_count", 0) == 0, (
            f"Errors after refresh: {result.get('errors')}"
        )

    def test_sales_pipeline_summary(self, authenticated_page):
        page = authenticated_page
        assert open_spreadsheet_by_name(page, "Sales Pipeline Summary")
        page.screenshot(path=f"{SCREENSHOTS}/sales_pipeline.png")

        result = get_all_sheet_errors(page)
        assert result.get("method") in ("model", "dom_fallback")
        assert result["error_count"] == 0, (
            f"Pipeline has {result['error_count']} errors: {result['errors']}"
        )

    def test_kpi_dashboard(self, authenticated_page):
        page = authenticated_page
        assert open_spreadsheet_by_name(page, "KPI Dashboard")
        page.screenshot(path=f"{SCREENSHOTS}/kpi_dashboard.png")

        result = get_all_sheet_errors(page)
        assert result.get("method") in ("model", "dom_fallback")
        assert result["error_count"] == 0, (
            f"KPI dashboard has {result['error_count']} errors: {result['errors']}"
        )
