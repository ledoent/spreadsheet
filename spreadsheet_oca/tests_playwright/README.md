# Playwright Browser Tests for spreadsheet_oca

End-to-end tests that verify spreadsheet pivots render correctly in the browser against
a running Odoo instance.

## Setup

```bash
pip install playwright pytest-playwright
playwright install chromium
```

## Running

```bash
# Against local dev instance (default: localhost:8069, DB: odoo_prod)
cd tests_playwright
pytest -v

# Custom Odoo URL
ODOO_URL=https://staging.odoo.ledoweb.com ODOO_DB=odoo_staging pytest -v

# Run a specific test
pytest -v test_demo_spreadsheets.py::TestDemoSpreadsheets::test_partner_pivot_dashboard

# With screenshots saved
pytest -v --screenshot=on  # Screenshots go to /tmp/ss_playwright/
```

## Test Files

- `test_demo_spreadsheets.py` — verify the 3 demo spreadsheets load error-free
- `test_module_pivots.py` — create pivots from CRM, Sales, Accounting, HR, MRP, Stock
  data and verify they render, survive refresh, and handle measure changes

## Environment Variables

| Variable        | Default                 | Description    |
| --------------- | ----------------------- | -------------- |
| `ODOO_URL`      | `http://localhost:8069` | Odoo base URL  |
| `ODOO_DB`       | `odoo_prod`             | Database name  |
| `ODOO_LOGIN`    | `admin`                 | Login user     |
| `ODOO_PASSWORD` | `admin`                 | Login password |
