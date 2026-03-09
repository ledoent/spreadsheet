# Dynamic Pivot Research — ledoent/spreadsheet

Branch: `18.0-research-dynamic-pivot`
Based on: OCA/spreadsheet `18.0` @ `spreadsheet_oca` v18.0.1.2.3
Status: **Research / Pre-implementation**

> **Policy:** No PR to OCA until we have a working implementation and confirmed
> value. This branch is for exploration and planning only.

---

## 1. Problem Statement

OCA's `spreadsheet_oca` inserts Odoo pivot data as a **one-time static
snapshot**. Once inserted, the table never updates — new months, new records,
new groupby values do not appear. The user must delete the entire table and
re-insert to see fresh data.

Odoo Enterprise solves this with **live data sources**: pivots store their
query definition (model, domain, groupbys, measures) and re-fetch data on
demand — automatically when global filters change, or manually via "Refresh
all data".

---

## 2. Current Architecture (OCA)

### 2.1 Insertion flow

```
PivotRenderer (Odoo pivot view)
  └─ onSpreadsheetButtonClicked()
       └─ opens spreadsheet_spreadsheet_import wizard
            ├─ default_can_be_dynamic: false  ← hardcoded UI gate
            └─ default_import_data: { mode, metaData, searchParams }

wizard → action_spreadsheet_oca client action
  └─ spreadsheet_action.esm.js :: importDataPivot()
       ├─ dispatch ADD_PIVOT          ← registers pivot data source
       ├─ ds.load()                   ← fetches data once, right now
       └─ dispatch INSERT_PIVOT_WITH_TABLE  ← writes static cells to sheet
```

### 2.2 What is stored

The spreadsheet JSON stores the **rendered table** (cell values). The pivot
query definition (`model`, `domain`, `measures`, `columns`, `rows`) is stored
as part of the pivot data source descriptor in the sheet JSON, but there is no
mechanism to use it after insertion.

### 2.3 What exists but is broken/unused

- `can_be_dynamic` and `dynamic` fields in the wizard — exist but only affect
  `dyn_number_of_rows` which is used for **list** inserts, not pivots.
- `REFRESH_ALL_DATA_SOURCES` command — dispatched by the "Refresh all data"
  menu item but **never registered** in OCA's command handlers. It's a no-op.
- `INSERT_PIVOT_WITH_TABLE` accepts `pivotMode: "dynamic"` or `"static"` — but
  this is only a UI hint for the re-insert cog menu options
  (`PivotTitleSectionInsertion.reinsertTable`), not a live-data signal.

### 2.4 Key source files

| File | Role |
|------|------|
| `static/src/spreadsheet/pivot_controller.esm.js` | Patches `PivotRenderer`; adds "Add to spreadsheet" button; hardcodes `default_can_be_dynamic: false` |
| `static/src/spreadsheet/bundle/spreadsheet_action.esm.js` | Handles `import_data` on client action load; `importDataPivot()` does the one-shot insert |
| `static/src/spreadsheet/bundle/filter_panel_datasources.esm.js` | Side panel for pivot/list; `reinsertTable()` calls `REFRESH_PIVOT` (unregistered); "Refresh all data" dispatches `REFRESH_ALL_DATA_SOURCES` (unregistered) |
| `wizards/spreadsheet_spreadsheet_import.py` | Wizard; `dynamic` flag exists but only wires `dyn_number_of_rows` for lists |

---

## 3. Enterprise Architecture (Reference)

Enterprise stores **pivot definitions, not cell values**. On open, the
spreadsheet fetches fresh data via RPC and renders live. On filter change, it
re-fetches automatically.

Key components Enterprise has that OCA lacks:

| Component | Enterprise location | OCA equivalent |
|-----------|--------------------|-|
| Python RPC endpoint | `spreadsheet_account` / `spreadsheet` — `get_pivot_data()` | ❌ None |
| `OdooPivotLoader` JS class | `spreadsheet/static/src/pivot/` | ❌ None |
| `PIVOT.VALUE()` formula function | `spreadsheet/static/src/pivot/pivot_functions.js` | ❌ None |
| `PIVOT.HEADER()` formula function | same | ❌ None |
| `REFRESH_PIVOT` command handler | pivot plugin | ❌ Unregistered |
| `REFRESH_ALL_DATA_SOURCES` handler | core plugin | ❌ Unregistered |
| Filter → pivot domain update plugin | `pivot_ui_global_filter_plugin.js` | ❌ None |

---

## 4. Implementation Plan

### Phase 1 — Python RPC Spike (validate approach, ~3 days)

**Goal:** Prove we can produce the same data the JS pivot view renders, from
Python, for arbitrary model/domain/groupby/measure combinations.

**Key unknown:** Odoo doesn't expose a public Python API for pivot
calculations. The web view's `PivotModel` does this entirely in JavaScript via
`read_group`. We need to replicate it.

**Spike tasks:**

1. **Understand `read_group` for pivot data**

   A pivot with `rows=["date:month"]`, `columns=["stage_id"]`,
   `measures=["amount_total"]` on `sale.order` maps to:

   ```python
   # Row headers: one read_group per row groupby level
   env["sale.order"].read_group(
       domain=domain,
       fields=["amount_total:sum"],
       groupby=["date:month"],
   )
   # Column headers:
   env["sale.order"].read_group(
       domain=domain,
       fields=["amount_total:sum"],
       groupby=["stage_id"],
   )
   # Cell values (intersection): read_group with both groupbys
   env["sale.order"].read_group(
       domain=domain,
       fields=["amount_total:sum"],
       groupby=["date:month", "stage_id"],
   )
   ```

   The spike is: write a Python function that does this for N row groupbys ×
   M column groupbys × K measures and returns a flat dict of
   `{(row_vals, col_vals): {measure: value}}` matching what the JS renders.

2. **Handle date granularity**

   `date:month`, `date:quarter`, `date:year`, `date:week` — each produces
   different `read_group` groupby keys. Map these to display labels correctly
   (e.g. `"2026-01"` → `"January 2026"`).

3. **Handle Many2one display names**

   When groupby field is a Many2one (e.g. `stage_id`), `read_group` returns
   `(id, name)` tuples. Need to normalise to `(id, display_name)`.

4. **Edge cases to test**

   - Empty domain (all records)
   - Domain with date filters
   - Many2many groupby fields (not supported in Enterprise either — skip)
   - Multiple measures
   - `count` pseudo-measure
   - `__count` vs explicit count field

**Deliverable:** `spreadsheet_oca/models/pivot_data.py` — a standalone
`_get_pivot_data(model, domain, context, rows, columns, measures)` function
that returns normalised pivot data. Tested with `odoo shell` against real data
before any JS work.

---

### Phase 2 — Register Missing Commands (~1 day)

Wire up the commands that exist in the UI but are no-ops:

1. **`REFRESH_ALL_DATA_SOURCES`** — iterate all pivot IDs, call
   `ds.load({ reload: true })` on each, then re-render. This gives "Refresh
   all data" menu item actual behaviour.

2. **`REFRESH_PIVOT`** — same but for a single pivot ID.

These are pure JS additions to the bundle, no Python changes. Gives immediate
visible value: user can click "Refresh all data" and the pivot updates.

**Files:** new `bundle/pivot_commands.esm.js` + register in `spreadsheet.xml`.

---

### Phase 3 — JS Loader connecting to Python RPC (~3 days)

Replace the one-shot `ds.load()` call at insert time with a persistent loader
that can re-fetch on demand.

1. **Controller endpoint**

   ```python
   # controllers/main.py
   @route("/spreadsheet/pivot/data", type="json", auth="user")
   def get_pivot_data(self, model, domain, context, rows, columns, measures):
       return request.env["spreadsheet.spreadsheet"]._get_pivot_data(
           model, domain, context, rows, columns, measures
       )
   ```

2. **`OcaPivotDataSource` JS class**

   ```js
   // static/src/spreadsheet/pivot_data_source.esm.js
   export class OcaPivotDataSource {
       constructor(orm, definition) { ... }
       async load({ reload = false } = {}) {
           if (!reload && this._data) return;
           this._data = await this.orm.call("spreadsheet.spreadsheet", "get_pivot_data", ...);
       }
       getValue(measure, rowPath, colPath) { ... }
       getHeaderLabel(dimension, value) { ... }
       getTableStructure() { ... }  // returns same shape as current static export
   }
   ```

3. **Wire into `importDataPivot`**

   Replace the current one-shot flow:
   ```js
   // Before:
   const ds = spreadsheet_model.getters.getPivot(pivotId);
   await ds.load();                    // one-shot
   const table = ds.getTableStructure();
   // ...INSERT_PIVOT_WITH_TABLE with static table

   // After:
   const ds = new OcaPivotDataSource(this.orm, pivot_info);
   await ds.load();
   const table = ds.getTableStructure();
   // register ds so REFRESH_PIVOT can reload it
   pivotDataSourceRegistry.set(pivotId, ds);
   // INSERT_PIVOT_WITH_TABLE with same static table (Phase 3 still table-based)
   ```

   At this stage the inserted table still looks the same — but "Refresh all
   data" now actually refetches and re-renders.

---

### Phase 4 — Formula Functions (`PIVOT.VALUE`, `PIVOT.HEADER`) (~3 days)

Instead of inserting a static table of values, insert formulas that call the
data source on demand. This is the feature that makes pivots truly live.

1. **Register functions**

   ```js
   // static/src/spreadsheet/pivot_functions.esm.js
   functionRegistry.add("PIVOT.VALUE", {
       description: _t("Get a value from an Odoo pivot"),
       args: [
           arg("pivot_id (string)", _t("Pivot ID")),
           arg("measure (string)", _t("Measure name")),
           arg("...domain_values (any, optional)", _t("Row/col header values")),
       ],
       compute(pivotId, measure, ...headerValues) {
           const ds = pivotDataSourceRegistry.get(pivotId.toString());
           if (!ds || !ds.isLoaded()) return null;
           const [rowPath, colPath] = ds.splitHeaderValues(measure, headerValues);
           return ds.getValue(measure, rowPath, colPath);
       },
       returns: ["NUMBER", "STRING"],
   });

   functionRegistry.add("PIVOT.HEADER", {
       // ...similar — returns display label for a header cell
   });
   ```

2. **Change `INSERT_PIVOT_WITH_TABLE` to formula mode**

   The existing `INSERT_PIVOT_WITH_TABLE` command writes cell values. Need an
   alternative (or a new flag) that writes `=PIVOT.VALUE(...)` formulas
   instead. When the data source reloads, formulas recalculate automatically.

   This is the biggest structural change — it requires understanding exactly
   what cell coordinates map to what `(rowPath, colPath)` combination, which
   the existing `getTableStructure().export()` already knows.

---

### Phase 5 — Auto-refresh on Filter Change (~2 days)

Wire up the global filter → pivot domain update → reload chain.

```js
// static/src/spreadsheet/bundle/pivot_filter_plugin.esm.js
class PivotGlobalFilterPlugin {
    handle(cmd) {
        switch (cmd.type) {
            case "UPDATE_GLOBAL_FILTER": {
                for (const pivotId of this.getters.getPivotIds()) {
                    const ds = pivotDataSourceRegistry.get(pivotId);
                    if (ds && this._filterAffectsPivot(cmd, pivotId)) {
                        ds.updateDomain(this._computeNewDomain(pivotId, cmd));
                        this.dispatch("REFRESH_PIVOT", { id: pivotId });
                    }
                }
                break;
            }
        }
    }
}
pluginRegistry.add("PivotGlobalFilterPlugin", PivotGlobalFilterPlugin);
```

This phase is optional for initial value delivery — manual "Refresh all data"
(Phase 2) covers most real-world use cases.

---

## 5. Delivery Sequence (by value)

| Phase | What user gets | Effort | Dependencies |
|-------|---------------|--------|-------------|
| 1 — Python RPC spike | Nothing visible; validates feasibility | 3 days | None |
| 2 — Register commands | "Refresh all data" actually works | 1 day | Phase 1 |
| 3 — JS Loader | Refresh pulls fresh data from server | 3 days | Phases 1+2 |
| 4 — Formulas | Pivot stays live; no re-insert needed | 3 days | Phase 3 |
| 5 — Auto-refresh | Filters drive pivot updates automatically | 2 days | Phases 3+4 |

**Total:** ~12 development days for full implementation.
**Minimum viable:** Phases 1–3 (~7 days) → "Refresh all data" works with real
server data.

---

## 6. Open Questions

1. **`read_group` for multi-level row groupbys** — does a single `read_group`
   call with `["date:month", "partner_id"]` give us all the data we need, or
   do we need one call per groupby level? Needs spiking.

2. **Pivot definition persistence** — when a spreadsheet is saved and
   re-opened, does the current JSON preserve enough to reconstruct the
   `OcaPivotDataSource`? Need to read how `ADD_PIVOT` serialises to JSON.

3. **`o-spreadsheet` version delta** — the `PIVOT.VALUE` function may already
   be partially defined in the bundled `o_spreadsheet.js`. Check before
   re-implementing.

4. **`ODOO_AGGREGATORS` import** — `filter_panel_datasources.esm.js` already
   imports `from "@spreadsheet/pivot/pivot_helpers"`. This suggests the base
   `spreadsheet` CE module exposes some pivot infrastructure. Audit what's
   available before writing new code.

---

## 7. Files to Create / Modify

| File | Action | Phase |
|------|--------|-------|
| `spreadsheet_oca/models/pivot_data.py` | Create — Python pivot RPC | 1 |
| `spreadsheet_oca/controllers/main.py` | Add `/spreadsheet/pivot/data` endpoint | 1 |
| `spreadsheet_oca/static/src/spreadsheet/bundle/pivot_commands.esm.js` | Create — register REFRESH_PIVOT / REFRESH_ALL_DATA_SOURCES | 2 |
| `spreadsheet_oca/static/src/spreadsheet/bundle/spreadsheet.xml` | Add new JS to assets | 2 |
| `spreadsheet_oca/static/src/spreadsheet/pivot_data_source.esm.js` | Create — OcaPivotDataSource class | 3 |
| `spreadsheet_oca/static/src/spreadsheet/bundle/spreadsheet_action.esm.js` | Modify — wire loader into importDataPivot | 3 |
| `spreadsheet_oca/static/src/spreadsheet/pivot_functions.esm.js` | Create — PIVOT.VALUE, PIVOT.HEADER | 4 |
| `spreadsheet_oca/static/src/spreadsheet/bundle/pivot_filter_plugin.esm.js` | Create — global filter → pivot refresh | 5 |
| `spreadsheet_oca/tests/test_pivot_data.py` | Create — unit tests for Python RPC | 1 |

---

## 8. What We Are NOT Doing

- **Pivot domain editor in side panel** — Enterprise has a Domain Selector
  widget in the pivot side panel. Not in scope; too much UI surface area.
- **`PIVOT.POSITION()` / legacy formula migration** — Enterprise has tooling to
  upgrade old pivot formulas. Out of scope.
- **`ODOO.LIST()` dynamic rows** — the `can_be_dynamic` flag for lists is a
  separate (and simpler) project. Not in scope here.
- **PR to OCA until value is confirmed** — this branch is research-only.
  No upstream PR until Phases 1–3 are working and tested against real data.
