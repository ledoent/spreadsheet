import {Component, t, usePlugin, useProps} from "@odoo/owl";
import {ActionPlugin} from "@web/webclient/actions/action_plugin";
import {FileUploader} from "@web/views/fields/file_handler";
import {ListController} from "@web/views/list/list_controller";
import {_t} from "@web/core/l10n/translation";
import {listView} from "@web/views/list/list_view";
import {registry} from "@web/core/registry";
import {standardWidgetProps} from "@web/views/widgets/standard_widget_props";
import {useService} from "@web/core/utils/hooks";

class SpreadsheetFileUploader extends Component {
    props = useProps({
        ...standardWidgetProps,
        acceptedFileExtensions: t
            .string()
            .optional(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        record: t.object().optional(),
        togglerTemplate: t.string().optional(),
        slots: t.object().optional(),
        linkText: t.string().optional(_t("Upload a Spreadsheet")),
    });

    setup() {
        this.orm = useService("orm");
        this.attachmentIdsToProcess = [];
        this.action = usePlugin(ActionPlugin);
    }
    async onFileUploaded(file) {
        const att_data = {
            name: file.name,
            mimetype: file.type,
            datas: file.data,
        };
        const att_id = await this.orm.create("ir.attachment", [att_data], {
            context: this.env.searchModel.context,
        });
        this.attachmentIdsToProcess.push(att_id[0]);
    }
    async onUploadComplete() {
        let action = {};
        try {
            action = await this.orm.call(
                "spreadsheet.spreadsheet",
                "create_document_from_attachment",
                ["", this.attachmentIdsToProcess],
                {context: this.env.searchModel.context}
            );
        } finally {
            // Ensures attachments are cleared on success as well as on error
            this.attachmentIdsToProcess = [];
        }
        if (action.context && action.context.notifications) {
            for (const [file, msg] of Object.entries(action.context.notifications)) {
                this.notification.add(msg, {
                    title: file,
                    type: "info",
                    sticky: true,
                });
            }
            delete action.context.notifications;
        }
        this.action.doAction(action);
    }
}
SpreadsheetFileUploader.components = {
    FileUploader,
};
SpreadsheetFileUploader.template = "spreadsheet_oca.SpreadsheetFileUploader";
export class SpreadsheetListController extends ListController {}
SpreadsheetListController.components = {
    ...ListController.components,
    SpreadsheetFileUploader,
};
export const SpreadsheetListView = {
    ...listView,
    Controller: SpreadsheetListController,
    buttonTemplate: "spreadsheet_oca.SpreadsheetTreeView.Buttons",
};

registry.category("views").add("spreadsheet_tree", SpreadsheetListView);
