/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class AdsetPerformanceView extends Component {
    static template = "urbenchat_sheet_connector.AdsetPerformanceView";
    static props = ["*"];

    setup() {
        this.orm    = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            loading: true, period: 'month', campaignFilter: '', rows: [],
        });
        onWillStart(() => this._load());
    }

    async _load() {
        try {
            const data = await this.orm.call("urbenchat.sheet.config", "get_dashboard_data", [], {});
            this.state.rows = data.adset_perf || [];
        } catch (e) {
            console.error(e);
            this.notification.add("Failed to load ad set performance.", { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    setPeriod(p) { this.state.period = p; }
    periodLabel() { return { today: 'Today', week: 'This Week', month: 'This Month', all: 'All Time' }[this.state.period]; }
    onCampaignFilter(ev) { this.state.campaignFilter = ev.target.value; }
    getCampaignList() { return [...new Set(this.state.rows.map(r => r.campaign))]; }
    getRows() {
        return this.state.campaignFilter
            ? this.state.rows.filter(r => r.campaign === this.state.campaignFilter)
            : this.state.rows;
    }
    getConvRate(d) { return (d && d.leads) ? Math.round((d.admissions / d.leads) * 100) : 0; }

    async viewLeads(adsetId) {
        try {
            const action = await this.orm.call(
                "urbenchat.sheet.config", "action_view_adset_leads",
                [adsetId, this.state.period], {}
            );
            this.action.doAction(action);
        } catch (e) {
            console.error(e);
            this.notification.add("Could not open leads for this ad set.", { type: "danger" });
        }
    }

    back() { this.action.doAction({ type: "ir.actions.client", tag: "urbenchat_dashboard", target: "current" }); }
}

registry.category("actions").add("urbenchat_adset_performance", AdsetPerformanceView);
