/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class PerAdPerformanceView extends Component {
    static template = "urbenchat_sheet_connector.PerAdPerformanceView";
    static props = ["*"];

    setup() {
        this.orm    = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            loading: true, period: 'month', adSearch: '', rows: [],
        });
        onWillStart(() => this._load());
    }

    async _load() {
        try {
            const data = await this.orm.call("urbenchat.sheet.config", "get_dashboard_data", [], {});
            this.state.rows = data.ad_perf || [];
        } catch (e) {
            console.error(e);
            this.notification.add("Failed to load per-ad performance.", { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    setPeriod(p) { this.state.period = p; }
    periodLabel() { return { today: 'Today', week: 'This Week', month: 'This Month', all: 'All Time' }[this.state.period]; }
    onAdSearch(ev) { this.state.adSearch = ev.target.value.toLowerCase(); }
    getRows() {
        const q = this.state.adSearch;
        if (!q) return this.state.rows;
        return this.state.rows.filter(
            a => (a.ad_id || '').toLowerCase().includes(q) || (a.campaign || '').toLowerCase().includes(q)
        );
    }

    async viewLeads(adId) {
        try {
            const action = await this.orm.call(
                "urbenchat.sheet.config", "action_view_ad_leads",
                [adId, this.state.period], {}
            );
            this.action.doAction(action);
        } catch (e) {
            console.error(e);
            this.notification.add("Could not open leads for this ad.", { type: "danger" });
        }
    }

    back() { this.action.doAction({ type: "ir.actions.client", tag: "urbenchat_dashboard", target: "current" }); }
}

registry.category("actions").add("urbenchat_per_ad_performance", PerAdPerformanceView);
