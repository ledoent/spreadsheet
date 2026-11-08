import * as spreadsheet from "@odoo/o-spreadsheet";

import {Component, onWillStart, t, useProps} from "@odoo/owl";
import {ImageFileStore} from "./image_file_store.esm";
import {OdooDataProvider} from "@spreadsheet/data_sources/odoo_data_provider";
import {SpreadsheetComponent} from "@spreadsheet/actions/spreadsheet_component";
import {SpreadsheetTransportService} from "./spreadsheet_transport_service.esm";
import {_t} from "@web/core/l10n/translation";
import {loadBundle} from "@web/core/assets";
import {useService} from "@web/core/utils/hooks";
import {useSetupAction} from "@web/search/action_hook";
import {useSubEnv} from "@web/owl2/utils";
import {user} from "@web/core/user";
import {waitForDataLoaded} from "@spreadsheet/helpers/model";

const {Model, load} = spreadsheet;

const {useStoreProvider, ModelStore} = spreadsheet.stores;
const {UuidGenerator} = spreadsheet.helpers;

export class SpreadsheetRenderer extends Component {
    static template = "spreadsheet_oca.SpreadsheetRenderer";
    static components = {SpreadsheetComponent};
    props = useProps({
        record: t.object(),
        res_id: t.number(),
        model: t.string(),
        importData: t.function(),
    });

    createDefaultCurrency(currency) {
        if (!currency) {
            return undefined;
        }
        return {
            symbol: currency.symbol,
            position: currency.position,
            decimalPlaces: currency.decimal_places,
        };
    }
    getLocales() {
        const orm = useService("orm");
        return async () => {
            return orm.call("res.lang", "get_locales_for_spreadsheet", []);
        };
    }
    getCurrencies() {
        const orm = useService("orm");
        return async () => {
            const odooCurrencies = await orm.searchRead(
                "res.currency",
                [],
                ["symbol", "full_name", "position", "name", "decimal_places"],
                {
                    order: "active DESC, full_name ASC",
                    context: {active_test: false},
                }
            );
            return odooCurrencies.map((currency) => {
                return {
                    code: currency.name,
                    symbol: currency.symbol,
                    position: currency.position || "after",
                    name: currency.full_name || _t("Currency"),
                    decimalPlaces: currency.decimal_places || 2,
                };
            });
        };
    }
    /**
     * Connected clients, us included. getConnectedClients() is a Set on the
     * session but arrives through the getters as an Array, so accept either.
     *
     * @returns {Number}
     */
    getConnectedClientCount() {
        const clients = this.spreadsheet_model?.getters.getConnectedClients();
        if (!clients) {
            return 2;
        }
        return clients.size ?? clients.length ?? 2;
    }
    setup() {
        this.orm = useService("orm");
        this.http = useService("http");
        this.bus_service = this.env.services.bus_service;
        this.ui = useService("ui");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notifications = useService("notification");
        const odooDataProvider = new OdooDataProvider(this.env);
        this.loadCurrencies = this.getCurrencies();
        this.loadLocales = this.getLocales();
        const defaultCurrency = this.props.record.default_currency;
        this.fileStore = new ImageFileStore(
            this.props.model,
            this.props.res_id,
            this.http,
            this.orm
        );
        this.stores = useStoreProvider();
        // The o-spreadsheet Model handles currency formatting internally
        this.spreadsheet_model = new Model(
            load(this.props.record.spreadsheet_raw),
            {
                custom: {env: this.env, orm: this.orm, odooDataProvider},
                defaultCurrency: this.createDefaultCurrency(defaultCurrency),
                external: {
                    loadCurrencies: this.loadCurrencies,
                    loadLocales: this.loadLocales,
                    fileStore: this.fileStore,
                    geoJsonService: this.env.services.geo_json_service,
                },
                transportService: new SpreadsheetTransportService(
                    this.orm,
                    this.bus_service,
                    this.props.model,
                    this.props.res_id,
                    {getClientCount: () => this.getConnectedClientCount()}
                ),
                client: {
                    id: UuidGenerator.uuidv4(),
                    name: user.name,
                    userId: user.userId,
                },
                mode: this.props.record.mode,
            },
            this.props.record.revisions
        );
        useSubEnv({
            saveSpreadsheet: this.onSpreadsheetSaved.bind(this),
            downloadAsXLXS: this.downloadAsXLXS.bind(this),
        });
        onWillStart(async () => {
            await loadBundle("spreadsheet.o_spreadsheet");
            await waitForDataLoaded(this.spreadsheet_model);
            // No joinSession(): the Model constructor already joined, and a
            // second join clears our own position.
            await this.env.importData(this.spreadsheet_model);
            this.stores.inject(ModelStore, this.spreadsheet_model);
        });
        useSetupAction({
            beforeLeave: this.onSpreadsheetSaved.bind(this),
        });
        odooDataProvider.addEventListener("data-source-updated", () => {
            const sheetId = this.spreadsheet_model.getters.getActiveSheetId();
            this.spreadsheet_model.dispatch("EVALUATE_CELLS", {sheetId});
        });
    }
    async onSpreadsheetSaved() {
        const data = this.spreadsheet_model.exportData();
        await this.env.saveRecord({spreadsheet_raw: data});
        await this.spreadsheet_model.leaveSession();
        this.spreadsheet_model.off("update", this);
    }
    async downloadAsXLXS() {
        this.ui.block();
        await waitForDataLoaded(this.spreadsheet_model);
        await this.action.doAction({
            type: "ir.actions.client",
            tag: "action_download_spreadsheet",
            params: {
                name: this.props.record.name,
                xlsxData: await this.spreadsheet_model.exportXLSX(),
            },
        });
        this.ui.unblock();
    }
}
