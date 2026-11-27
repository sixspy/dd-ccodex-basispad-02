# 费率计提案 · basispad-02 —— 争用段（费率计提互斥）

> 印记：**费率计提案** · 枪号 **basispad-02**
> 本文与取舍段 `docs/funding_accrue_decision.md` **物理分开**：取舍段讲甲乙口径，
> 本篇只讲双写者争「费率计提互斥」时的并发纪律。禁止把两篇合并成一段。

## 争用模型

- 互斥资源：`funding:{snapshot_id}`，作用于同一快照的正式计提口。
- 两路（或多路）写口并发进入时，只允许一路抢到计提权；
  **未抢到的一方只产生争用记录，正式台账零增、快照不动、仓位已实现零改。**
- 禁止无锁双成：不得出现两份正式 ledger、两次 `realized_pnl` 累加、快照被翻两次。

## 实现纪律（甲路径下的锁册展开）

1. 每个请求以独立连接执行 `BEGIN IMMEDIATE`，先拿 SQLite 写库级保留锁，
   再在事务内做条件抢占：
   `UPDATE funding_snapshots SET status='accrued' WHERE id=? AND status='draft'`。
   游标 `rowcount` 为 0 即代表未抢到（已被他路升正 / 快照非 draft）。
2. 未抢到者：在**同一事务**插入一条争用记录后 `COMMIT`（争用记录是允许的唯一写入），
   接口返回 409。争用记录落 `audit_logs`，`action='funding_accrue_contention'`，
   `entity='funding_snapshot'`，`detail` 含资源名与快照 code，便于交班联查。
3. 抢到者：同事务写正式 ledger、改 `realized_pnl`、翻快照、写成功审计后 `COMMIT`。
4. 连接设置 `busy_timeout`，把「库锁正忙」转成有序等待而不是裸 500；
   等待后仍抢不到 draft 条件的，按第 2 条走争用记录。
5. 对打压测脚本点名 `race_funding_accrue`（`tests/feat_fund_race.py`）：
   两路同票并发，结果必须恰为一路 200、一路 409；正式行数等于持仓数，不翻倍。

## 与乙闸专文的边界

- 仓证方向与计提行成对落库、同票联查、反号告警的口径见
  `docs/perp_gate_b_pairing.md`（永续乙闸专文），本篇不重复、不邻题照抄。
- 强平成交枪的叙事与本文无关，禁止把本枪永续事故句粘到强平成交枪。
