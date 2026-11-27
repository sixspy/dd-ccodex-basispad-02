window.FundingView = {
  props: ["snapshots", "ledgers", "recon", "contention", "links", "instruments"],
  emits: ["refresh"],
  data() {
    return { form: { code: "", instrument_id: "", rate: "0.0001", mark_price: "", period_start: "", period_end: "" }, err: "" };
  },
  computed: {
    violations() { return (this.recon && this.recon.violations) || []; },
  },
  methods: {
    async create() {
      this.err = "";
      try {
        const now = Date.now() / 1000;
        await BasisAPI.createFunding({
          code: this.form.code,
          instrument_id: Number(this.form.instrument_id),
          rate: Number(this.form.rate),
          mark_price: this.form.mark_price === "" ? null : Number(this.form.mark_price),
          period_start: Number(this.form.period_start || now - 28800),
          period_end: Number(this.form.period_end || now),
        });
        this.$emit("refresh");
      } catch (e) { this.err = e.message; }
    },
    async accrue(id) {
      this.err = "";
      try { await BasisAPI.accrueFunding(id); this.$emit("refresh"); }
      catch (e) { this.err = e.message; this.$emit("refresh"); }
    },
    sideText(qty) { return qty > 0 ? "多头" : (qty < 0 ? "空头" : "平仓"); },
    statusClass(s) { return s === "accrued" || s === "settled" ? "ok" : (s === "void" ? "bad" : "warn"); },
    typeText(t) {
      return {
        bypass_ledger_on_non_accrued: "旁路落账：非正式快照挂正式台账",
        shadow_row_under_formal_snapshot: "正式快照挂影子行",
        direction_reversed: "支付方向反号",
      }[t] || t;
    },
  },
  template: `
  <div>
    <div class="panel">
      <h3>新建资金费率快照</h3>
      <div class="form-grid">
        <div><label>编码</label><input v-model="form.code" /></div>
        <div><label>标的</label>
          <select v-model="form.instrument_id"><option value="">选择</option>
            <option v-for="i in instruments" :value="i.id">{{ i.symbol }}</option></select></div>
        <div><label>费率</label><input v-model="form.rate" /></div>
        <div><label>标记价(可空)</label><input v-model="form.mark_price" /></div>
      </div>
      <div class="row"><button class="btn" @click="create">创建草稿快照</button></div>
      <div class="err" v-if="err">{{ err }}</div>
    </div>

    <div class="panel">
      <h3>快照状态 × 台账双写联检（跨表对账）</h3>
      <div class="tag ok" v-if="recon && recon.ok === true">对账一致：仅 accrued 后台账有行</div>
      <div class="tag bad" v-else-if="recon && recon.ok === false">对账发现 {{ violations.length }} 处违规</div>
      <table v-if="violations.length">
        <thead><tr><th>类型</th><th>快照</th><th>计提行号</th><th>簿</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="(v,i) in violations" :key="i">
            <td><span class="tag bad">{{ typeText(v.type) }}</span></td>
            <td>#{{ v.snapshot_id }} {{ v.snapshot_code }}</td>
            <td>{{ v.ledger_code }}</td><td>{{ v.sleeve_id }}</td><td>{{ v.detail }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>费率快照（仅 draft 可正式计提，甲路径整笔原子）</h3>
      <table>
        <thead><tr><th>编码</th><th>标的</th><th>费率</th><th>标记价</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in snapshots" :key="r.id">
            <td>{{ r.code }}</td><td>{{ r.instrument_id }}</td><td>{{ r.rate }}</td><td>{{ r.mark_price }}</td>
            <td><span class="tag" :class="statusClass(r.status)">{{ r.status }}</span></td>
            <td><button class="btn secondary" v-if="r.status==='draft'" @click="accrue(r.id)">正式计提</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>费率台账（影子行标灰，禁止假绿）</h3>
      <table>
        <thead><tr><th>编码</th><th>快照</th><th>簿</th><th>仓位</th><th>支付</th><th>状态</th><th>正式</th></tr></thead>
        <tbody>
          <tr v-for="r in ledgers" :key="r.id">
            <td>{{ r.code }}</td><td>{{ r.snapshot_id }}</td><td>{{ r.sleeve_id }}</td>
            <td>{{ r.position_qty }}</td><td>{{ r.payment }}</td>
            <td>{{ r.status }}</td>
            <td><span class="tag" :class="r.status==='accrued'||r.status==='settled' ? 'ok' : 'warn'">
              {{ r.status==='accrued'||r.status==='settled' ? '正式' : '未升正' }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>同票联查：计提行号 ↔ 快照 ↔ 仓证边</h3>
      <table>
        <thead><tr><th>计提行号</th><th>快照</th><th>仓证边</th><th>带符号仓量</th><th>支付</th><th>正式</th><th>方向告警</th></tr></thead>
        <tbody>
          <tr v-for="r in links" :key="r.ledger_code">
            <td>{{ r.ledger_code }}</td>
            <td>#{{ r.snapshot_id }} {{ r.snapshot_code }}（{{ r.snapshot_status }}）</td>
            <td>仓位#{{ r.position_id }} {{ sideText(r.position_qty) }}</td>
            <td>{{ r.position_qty }}</td><td>{{ r.payment }}</td>
            <td><span class="tag" :class="r.formal ? 'ok' : 'warn'">{{ r.formal ? '正式' : '影子' }}</span></td>
            <td><span v-if="r.direction_alert" class="tag bad">反号告警</span><span v-else>-</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>费率计提互斥 · 争用败方记录</h3>
      <table v-if="contention.length">
        <thead><tr><th>时间</th><th>败方账号</th><th>互斥资源</th><th>快照</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="c in contention" :key="c.id">
            <td>{{ c.created_at }}</td><td>{{ c.actor }}</td><td>{{ c.resource }}</td>
            <td>#{{ c.snapshot_id }} {{ c.snapshot_code }}</td><td>{{ c.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p class="meta" v-else>暂无争用记录。</p>
    </div>
  </div>`
};
