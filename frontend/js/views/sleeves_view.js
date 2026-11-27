window.SleevesView = {
  props: ["rows"],
  emits: ["refresh"],
  data() {
    return { expanded: null, detail: null, loading: false };
  },
  methods: {
    async toggle(id) {
      if (this.expanded === id) { this.expanded = null; this.detail = null; return; }
      this.expanded = id;
      this.loading = true;
      try { this.detail = await BasisAPI.sleeve(id); }
      finally { this.loading = false; }
    },
    sideText(qty) { return qty > 0 ? "多头" : (qty < 0 ? "空头" : "平仓"); },
  },
  template: `
  <div>
    <div class="panel">
      <h3>策略纸面簿</h3>
      <table>
        <thead><tr><th>编码</th><th>名称</th><th>负责人</th><th>权益</th><th>敞口上限</th><th>状态</th><th>权益告警</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.code }}</td><td>{{ r.title }}</td><td>{{ r.owner }}</td>
            <td>{{ r.base_equity }}</td><td>{{ r.max_gross_exposure }}</td>
            <td><span class="tag" :class="r.status==='active'?'ok':(r.status==='frozen'?'warn':'')">{{ r.status }}</span></td>
            <td><span v-if="r.equity_alert" class="tag bad">支付方向反号 · 权益告警</span><span v-else class="tag ok">正常</span></td>
            <td><button class="btn secondary" @click="toggle(r.id)">{{ expanded===r.id ? '收起仓证' : '查看仓证/计提' }}</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel" v-if="expanded && detail">
      <h3>{{ detail.code }} 仓证边与正式计提联查</h3>
      <div class="tag bad" v-if="detail.equity_alert">权益栏告警：存在支付方向反号的正式计提</div>
      <p class="meta" v-if="loading">加载中…</p>
      <table v-else>
        <thead><tr><th>仓位#</th><th>标的</th><th>仓证边</th><th>带符号仓量</th><th>均价</th><th>已实现盈亏</th><th>计提反号告警</th></tr></thead>
        <tbody>
          <tr v-for="p in detail.positions" :key="p.id">
            <td>{{ p.id }}</td><td>{{ p.instrument_id }}</td><td>{{ sideText(p.qty) }}</td>
            <td>{{ p.qty }}</td><td>{{ p.avg_entry }}</td><td>{{ p.realized_pnl }}</td>
            <td>
              <div v-if="p.funding_alerts && p.funding_alerts.length">
                <div v-for="a in p.funding_alerts" :key="a.ledger_code" class="err">
                  <span class="tag bad">反号</span> {{ a.message }}
                </div>
              </div>
              <span v-else class="tag ok">无</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>`
};
