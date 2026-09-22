/** @odoo-module **/

import { Component, useState, useRef, onWillStart, onMounted, onPatched, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const QUALITY_COLORS = {
    'New':'#6366f1','First Attempt':'#3b82f6','Hot':'#ef4444','Warm':'#f59e0b',
    'Cold':'#64748b','Not Responding':'#94a3b8','Follow Up':'#8b5cf6',
    'Admission':'#10b981','Language Barrier':'#f97316','Crash Lead':'#dc2626',
};

class UrbenchatDashboard extends Component {
    static template = "urbenchat_sheet_connector.Dashboard";
    static props = ["*"];

    setup() {
        this.orm          = useService("orm");
        this.notification = useService("notification");
        this.action       = useService("action");
        this.state = useState({
            loading: true, syncing: false,
            period: 'month', campaignFilter: '', adSearch: '',
            refreshInterval: 0, data: {},
        });
        this._refreshTimer = null;
        this.rootRef = useRef("uc_root");
        this._onResize = () => this._fixScroll();
        onWillStart(() => this._loadData());
        onMounted(() => {
            this._renderCharts();
            this._startAutoRefresh();
            this._fixScroll();
            window.addEventListener("resize", this._onResize);
            // Chart.js loads async from CDN and reflows the page afterward;
            // a single check at mount time is too early, and Odoo's own JS
            // can re-apply inline styles to .o_content after we run (e.g.
            // when the breadcrumb finishes mounting) so we recheck a few
            // times as things settle, then watch continuously.
            [50, 200, 500, 1000, 2000].forEach(ms => setTimeout(() => this._fixScroll(), ms));
            if (window.ResizeObserver && this.rootRef.el) {
                this._resizeObserver = new ResizeObserver(() => this._fixScroll());
                this._resizeObserver.observe(this.rootRef.el);
                this._resizeObserver.observe(document.body);
            }
        });
        onPatched(() => { if (!this.state.loading) { this._renderCharts(); this._fixScroll(); } });
        onWillUnmount(() => {
            this._stopAutoRefresh();
            window.removeEventListener("resize", this._onResize);
            if (this._resizeObserver) this._resizeObserver.disconnect();
        });
    }

    // ── Force real, guaranteed-working scrolling ─────────────────────────
    // Root cause: Odoo's DESKTOP layout sets overflow:hidden on its own
    // .o_content wrapper (each client action is expected to manage its own
    // internal scroll region). Odoo's MOBILE breakpoint does NOT apply that
    // restriction, which is exactly why this dashboard scrolls fine on
    // mobile but not desktop. Trying to make our own div into its own
    // scroll container (via height:100%/overflow-y:auto on uc_dashboard)
    // depends on that div having a bounded height, which is unreliable
    // across Odoo views/themes. Instead we go straight to the actual
    // ancestor that's clipping content and force IT open via inline style
    // (max specificity — no CSS/SCSS override can out-rank an inline
    // style, so this can't lose a specificity fight), and let our own div
    // flow naturally so the browser's native scroll (same mechanism mobile
    // already uses) just works.
    _fixScroll() {
        const el = this.rootRef.el;
        if (!el) return;
        el.style.height = "auto";
        el.style.maxHeight = "none";
        el.style.overflow = "visible";
        const content = el.closest(".o_content");
        if (content) {
            content.style.overflowY = "auto";
            content.style.overflowX = "hidden";
            content.style.height = "100%";
        }
        const actionManager = el.closest(".o_action_manager");
        if (actionManager) {
            actionManager.style.overflowY = "auto";
            actionManager.style.height = "100%";
        }
    }

    // ── Data ──────────────────────────────────────────────────────────────
    async _loadData() {
        try {
            this.state.data = await this.orm.call("urbenchat.sheet.config","get_dashboard_data",[],{});
        } catch(e) {
            console.error(e);
            this.notification.add("Failed to load dashboard data.", { type: "danger" });
        } finally { this.state.loading = false; }
    }

    // ── Auto refresh ──────────────────────────────────────────────────────
    async _startAutoRefresh() {
        this._stopAutoRefresh();
        try {
            const secs = await this.orm.call("urbenchat.sheet.config","get_sync_interval",[],{});
            const ms = Math.max((secs||300),60)*1000;
            this.state.refreshInterval = Math.round(ms/60000);
            this._refreshTimer = setInterval(() => this._loadData(), ms);
        } catch(e) {
            this._refreshTimer = setInterval(() => this._loadData(), 5*60*1000);
        }
    }
    _stopAutoRefresh() { if (this._refreshTimer) { clearInterval(this._refreshTimer); this._refreshTimer = null; } }

    // ── Period ────────────────────────────────────────────────────────────
    setPeriod(p) { this.state.period = p; }

    openAdsetPerformance() {
        this.action.doAction({ type: "ir.actions.client", tag: "urbenchat_adset_performance", target: "current" });
    }
    openPerAdPerformance() {
        this.action.doAction({ type: "ir.actions.client", tag: "urbenchat_per_ad_performance", target: "current" });
    }

    periodLabel() { return {today:'Today',week:'This Week',month:'This Month',all:'All Time'}[this.state.period]; }

    // ── KPI helpers ───────────────────────────────────────────────────────
    getPeriodLeads() {
        const kpi = this.state.data.kpi || {};
        return {today: kpi.today, week: kpi.week, month: kpi.month, all: kpi.total}[this.state.period] || 0;
    }
    getPeriodAdmissions() {
        return (this.state.data.adset_perf||[]).reduce((s,r)=>s+(r[this.state.period]?.admissions||0),0);
    }

    // ── Best adset ────────────────────────────────────────────────────────
    getBestAdsetData(metric) {
        const p=this.state.period, rows=this.state.data.adset_perf||[];
        const f=rows.filter(r=>r[p]&&r[p][metric]>0);
        return f.length ? f.reduce((b,r)=>r[p][metric]>b[p][metric]?r:b) : null;
    }
    getBestAdset(metric) {
        const b=this.getBestAdsetData(metric); if(!b) return '—';
        return `${b.adset} (${b[this.state.period][metric]})`;
    }

    // ── Conversion rate ───────────────────────────────────────────────────
    getConvRate(d) { return (d&&d.leads) ? Math.round((d.admissions/d.leads)*100) : 0; }

    // ── Campaign ──────────────────────────────────────────────────────────
    getCampaignPerf() {
        return (this.state.data.campaign_perf||[]).filter(c=>c[this.state.period]&&c[this.state.period].leads>0);
    }
    getCampaignList() { return [...new Set((this.state.data.adset_perf||[]).map(r=>r.campaign))]; }
    getCampShare(val,isAdm=false) {
        const rows=this.getCampaignPerf();
        const max=Math.max(...rows.map(r=>isAdm?r[this.state.period].admissions:r[this.state.period].leads),1);
        return Math.round((val/max)*100);
    }

    // ── Adset ─────────────────────────────────────────────────────────────
    onCampaignFilter(ev) { this.state.campaignFilter=ev.target.value; }
    getFilteredAdsets() {
        const p=this.state.period;
        return (this.state.data.adset_perf||[])
            .filter(r=>r[p]&&r[p].leads>0)
            .filter(r=>!this.state.campaignFilter||r.campaign===this.state.campaignFilter)
            .sort((a,b)=>b[p].leads-a[p].leads);
    }
    getAdsetShare(val,isAdm=false) {
        const rows=this.getFilteredAdsets(),p=this.state.period;
        const max=Math.max(...rows.map(r=>isAdm?r[p].admissions:r[p].leads),1);
        return Math.round((val/max)*100);
    }

    // ── Per Ad ────────────────────────────────────────────────────────────
    onAdSearch(ev) { this.state.adSearch = ev.target.value.toLowerCase(); }

    // ── Drill-down buttons ───────────────────────────────────────────────
    async viewAdsetLeads(adsetId) {
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

    async viewAdLeads(adId) {
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

    getFilteredAds() {
        const q=this.state.adSearch, p=this.state.period;
        return (this.state.data.ad_perf||[])
            .filter(a=>!q||(a.ad_id||'').toLowerCase().includes(q)||(a.source_camp||'').toLowerCase().includes(q)||(a.campaign||'').toLowerCase().includes(q))
            .sort((a,b)=>(b[p]||0)-(a[p]||0));
    }

    // ── Sync ──────────────────────────────────────────────────────────────
    async syncNow() {
        this.state.syncing = true;
        try {
            const cfgs = await this.orm.searchRead("urbenchat.sheet.config",[["active","=",true]],["id","name"]);
            if (!cfgs.length) { this.notification.add("No active sheet configs.", {type:"warning"}); return; }
            for (const c of cfgs) await this.orm.call("urbenchat.sheet.config","action_sync_now",[c.id],{});
            this.notification.add("Sync complete! Refreshing…",{type:"success"});
            this.state.loading=true; await this._loadData();
        } catch(e) { this.notification.add("Sync failed: "+(e.message||e),{type:"danger"}); }
        finally { this.state.syncing=false; }
    }

    // ── Charts ────────────────────────────────────────────────────────────
    _renderCharts() {
        if (this.state.loading) return;
        if (!window.Chart) {
            const s=document.createElement("script");
            s.src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js";
            s.onload=()=>this._buildAllCharts(); document.head.appendChild(s);
        } else { this._buildAllCharts(); }
    }
    _destroy(id) { const el=document.getElementById(id); if(el&&el._chartInstance){el._chartInstance.destroy();el._chartInstance=null;} }
    _buildAllCharts() { this._buildDailyChart(); this._buildQualityChart(); }

    _buildDailyChart() {
        this._destroy("uc_daily_chart");
        const el=document.getElementById("uc_daily_chart"); if(!el) return;
        const daily=this.state.data.daily_data||[];
        el._chartInstance=new window.Chart(el,{
            type:"line",
            data:{
                labels:daily.map(d=>{const dt=new Date(d.date);return dt.toLocaleDateString("en-IN",{day:"numeric",month:"short"});}),
                datasets:[
                    {label:"Prospects",data:daily.map(d=>d.leads),fill:true,backgroundColor:"rgba(99,102,241,0.12)",borderColor:"#6366f1",tension:0.4,pointRadius:4,pointBackgroundColor:"#6366f1"},
                    {label:"Admissions",data:daily.map(d=>d.admissions),fill:false,borderColor:"#10b981",backgroundColor:"#10b981",tension:0.4,pointRadius:4,borderDash:[4,3]},
                ],
            },
            options:{responsive:true,plugins:{legend:{position:"top"},tooltip:{mode:"index"}},scales:{y:{beginAtZero:true,ticks:{stepSize:1}},x:{grid:{display:false}}}},
        });
    }

    _buildQualityChart() {
        this._destroy("uc_quality_chart");
        const el=document.getElementById("uc_quality_chart"); if(!el) return;
        const entries=Object.entries(this.state.data.quality_data||{}).filter(([,v])=>v>0);
        if(!entries.length) return;
        el._chartInstance=new window.Chart(el,{
            type:"doughnut",
            data:{
                labels:entries.map(([k])=>k),
                datasets:[{data:entries.map(([,v])=>v),backgroundColor:entries.map(([k])=>QUALITY_COLORS[k]||"#94a3b8"),borderWidth:2,borderColor:"#fff"}],
            },
            options:{responsive:true,cutout:"60%",plugins:{legend:{position:"right",labels:{boxWidth:12,font:{size:11}}}}},
        });
    }
}

registry.category("actions").add("urbenchat_dashboard", UrbenchatDashboard);
