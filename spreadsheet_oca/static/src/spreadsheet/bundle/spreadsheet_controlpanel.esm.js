import {ActionPlugin} from "@web/webclient/actions/action_plugin";
import {ControlPanel} from "@web/search/control_panel/control_panel";

const {Component, proxy, t, useProps, usePlugin} = owl;

export class SpreadsheetName extends Component {
    props = useProps({
        name: t.string(),
        isReadonly: t.boolean(),
        onChanged: t.function().optional(),
    });

    setup() {
        this.state = proxy({
            name: this.props.name,
        });
    }
    _onNameChanged(ev) {
        if (this.props.isReadonly) {
            return;
        }
        if (ev.target.value) {
            this.env.saveRecord({name: ev.target.value});
        }
        this.state.name = ev.target.value;
        if (this.props.onChanged) {
            this.props.onChanged(ev);
        }
    }
}
SpreadsheetName.template = "spreadsheet_oca.SpreadsheetName";

export class SpreadsheetControlPanel extends ControlPanel {
    props = useProps({
        display: t.object().optional(),
        record: t.object(),
    });

    setup() {
        super.setup();
        this.actionService = usePlugin(ActionPlugin);
    }

    onBreadcrumbClicked(jsId) {
        this.actionService.restore(jsId);
    }
}
SpreadsheetControlPanel.template = "spreadsheet_oca.SpreadsheetControlPanel";
SpreadsheetControlPanel.components = {
    ...ControlPanel.components,
    SpreadsheetName,
};
