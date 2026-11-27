# 永续乙闸专文 · basispad-02 —— 计提台账与仓证方向成对落库

> 印记：**费率计提案** · 枪号 **basispad-02** · 测试器名：**永续乙闸**（pytest 标记 `perp_gate_b`）
> 本篇是乙闸自己的文案，围绕本枪资金费率计提事故写；不邻题照抄，
> 不粘贴结算封批枪、强平成交枪的句子，本枪叙事也不得挪到邻题费率枪。

## 现场记录

夜班对账发现：永续仓证方向（`positions.qty` 的带符号仓量）与资金费支付方向
（`funding_ledgers.payment` 的正负号）在纸面簿对不上。计提写口与仓位边若不同票，
事后无法联查是哪一笔计提改了哪个仓的已实现盈亏。

## 成对落库口径

1. 正式计提事务内，每条正式台账行都必须能同票联查到仓证边：
   `ledger.code`（计提行号）↔ `positions.id`（仓证）↔ `funding_snapshots.id`（快照号），
   经 `(sleeve_id, instrument_id)` 成对绑定，同生同灭同事务。
2. 盯市接口 `/api/marks/mtm` 的每行带 `funding_rows`：逐项给出
   `ledger_code / snapshot_id / snapshot_status / position_id / position_side /
   position_qty / payment / formal / direction_alert`，写口与仓位边同票可点回。
3. 纸面簿接口 `/api/sleeves/{id}` 的每个仓位带 `funding_alerts`，
   告警条目点名仓证边（多头/空头 + 仓位 id）与计提行号；
   簿级 `equity_alert` 同步翻红，禁止只在费率页角标闪点。
4. 仓位 `realized_pnl` 的增量只允许来自正式计提事务，且与台账支付**同额同源**；
   影子/旁路行不得推动已实现盈亏（由红测乙守）。

## 方向反号告警

- 期望方向（`policies.funding_payment` 口径）：
  多头（qty>0）在正费率下支付为负，空头（qty<0）在正费率下收取为正，
  即 `expected = -qty * mark * rate`。
- 实落台账行的 `payment` 与期望方向不一致（同为非零且符号相反）即判反号，
  `direction_alert=True`；反号时该仓位所属策略簿 `equity_alert=True`，权益栏同步告警。
- 交班要点名：仓证边（哪个 sleeve 的哪个 position、多/空、带符号仓量）
  与计提行号（ledger code），两者都在告警文案里。

## 页面浏览次序（夜班复核）

策略纸面簿页 → 标记价盯市页 → 时间线页 → 总览页 → 资金费率页，五页依次过：
纸面簿看权益告警与仓证边、盯市页看正式/影子分色与联查字段、
时间线看计提与争用事件、总览看草稿计数、资金费率页看状态与对账结果。
