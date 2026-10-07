# 信用评分与异常交易检测服务

可复现的信用评分与异常交易检测服务：同一份客户与交易数据在不同请求、并发与模型版本下得到一致且可解释的风险结论。

## 评分类别与阈值语义

分数区间 `[300, 900]`，基准分 600，按模型版本的特征权重线性加减（见 `app/model_registry.py`）。

| 类别 | 分数区间 | 判定 |
|---|---|---|
| EXCELLENT | ≥ 750 | APPROVE |
| GOOD | [650, 750) | APPROVE |
| FAIR | [500, 650) | REVIEW（人工复核） |
| POOR | < 500 | DECLINE |

判定阈值：`score >= 650` 通过；`[500, 650)` 复核；`< 500` 拒绝并附 `LOW_SCORE` 原因（含每条规则的贡献分明细）。

特征校验**严格失败**，不用默认值静默通过，拒绝原因可区分：

| 原因码 | 含义 |
|---|---|
| MISSING_FEATURE | 关键特征缺失或为 null |
| INVALID_FEATURE_TYPE | 类型异常（含 bool 冒充数值） |
| FEATURE_OUT_OF_RANGE | 取值越界 |
| NON_FINITE_NUMERIC | NaN / ±Infinity 等非有限数值 |
| LOW_SCORE | 校验通过但分数低于复核阈值 |
| ALERT_TIGHTENED | 被未关联交易告警联动收紧（逐条 + 汇总理由） |

## 交易告警 ↔ 申请决策联动（link-v1）

提交申请时，系统会在**同一把锁内原子地**抓取该客户当前所有尚未关联过决策（状态 `OPEN`）的交易告警，纳入判定并收紧；被采用的告警状态随即变为 `LINKED_TO_DECISION` 并回写关联申请号。

收紧策略（`app/linkage.py`，带版本号 `link-v1`）对“分数侧”与“判定阶梯侧”**取更严**：

| 告警类别 | 严重度 | 扣分 | 判定阶梯升级 |
|---|---|---|---|
| DUPLICATE_TRANSACTION | LOW | 80 | 1 级（APPROVE→REVIEW） |
| BURST | MEDIUM | 160 | 2 级（APPROVE→DECLINE） |
| BEHAVIOR_DEVIATION | HIGH | 250 | 3 级 |

- 分数侧：基础分减去命中告警扣分（钳制在 `[300,900]`），按固定阈值重新归类。
- 阶梯侧：按严重度累加抬升判定下限（`APPROVE → REVIEW → DECLINE`），保证“原本能放过”的申请在有未关联告警时**必然至少进入人工复核或拒绝**，即使扣完分仍高于通过线。
- 结论同时返回 `base_score/base_decision`（收紧前）与 `score/decision`（收紧后），`reasons` 中为**每一条**被采用告警给出一条 `ALERT_TIGHTENED`（含告警号、类别、严重度、扣分、阶梯级数、窗口、关联交易笔数），并附一条汇总理由，说明从什么判定收紧到什么判定。

关联关系的建立与保持：

- 建立：申请落库与告警状态翻转在存储层同一临界区内完成（“查重 → 抢占 OPEN 告警 → 构造收紧结论 → 分配风险记录号 → 落库”），构造失败会回滚告警预占。因此重复/并发提交、告警与申请几乎同时到达时，不会漏关联、重复关联或把告警挂到别的申请；每条告警最多归属一次决策。
- 保持：关联是决策当时的**不可变事实**。申请记录固化 `base_result`、所用模型版本、联动策略版本、逐条 `AlertImpact` 与告警快照 `AlertSnapshot`。之后同客户再冒出新告警，只可能被之后的新申请采用，**不会**回头改写历史结论或关联。
- 双向追溯：
  - `GET /applications/{id}`：返回该申请固定结论及其 `linked_alerts`（逐条影响）；
  - `GET /alerts/{alert_id}/decision`：返回该告警状态及其 `linked_application_id`（未关联时为 null）。
- 边界：特征校验失败（缺失/类型/越界/非有限值）的畸形申请只落一条拒绝结论，**不抢占、不关联**任何 OPEN 告警——它不构成“采用了交易告警”的风险决策，告警仍留给后续正常申请。

## 非法数值输入

上游偶尔传来的非有限数值（JSON 的 `NaN`/`Infinity`/`-Infinity`）在提交入口即被识别为独立的 `INVALID_INPUT` 错误（HTTP 422），**不会**进入打分被当成正常低分悄悄放过，也不落库、不占用风险记录号、不吞告警。字符串形态的 `"NaN"` 等仍按 `INVALID_FEATURE_TYPE` 区分。

## 版本与重放规则

- 打分是 `(model_version, features)` 的**纯函数**：同一特征快照 + 同一版本 ⇒ 完全一致的分数、类别与结论；快照哈希（`feature_snapshot_hash`）相同。
- 模型按版本注册（当前 `v1`、`v2`），未指定版本时使用最新版；未知版本返回 `UNKNOWN_MODEL_VERSION`。
- 告警联动策略同样带版本（`link_policy_version`，当前 `link-v1`），并随申请固化；策略升级不影响历史申请。
- 每条申请记录保存原始特征快照、所用模型版本、联动策略版本、收紧前 `base_result` 与当时告警快照；`GET /applications/{id}/replay` 始终按**原模型版本 + 原告警快照**重放，模型或联动策略升级、客户后续新增告警都不影响历史结论；重放结果（含逐条联动理由）与历史不一致时视为内部错误（`COMPUTE_FAILURE`）。

## 幂等与告警状态一致性约定

- `application_id` 是幂等键：重复/并发提交只产生**一份最终结论和一条风险记录**（风险记录号在锁内、仅真正写入时分配，竞争失败不消耗序号、不重复计数）。
- 重复提交内容一致 ⇒ 返回既有结论（含其固定关联）；内容冲突（快照或版本不同）⇒ `CONCURRENT_CONFLICT`（HTTP 409），绝不返回互相矛盾的结论。
- 申请与告警的关联在同一临界区原子完成：告警状态机为 `OPEN → LINKED_TO_DECISION`，一条告警最多关联一次，不会漏关联、重复关联或挂错申请。
- 告警按 `(customer_id, kind, window_start)` 去重：同一 600s 对齐窗口内的重复/突发/偏离告警只保留一条。
- 底层异常一律归一化为 `RiskServiceError`（`INVALID_INPUT` / `UPSTREAM_FAILURE` / `COMPUTE_FAILURE` / `PERSISTENCE_FAILURE` 等），原始异常仅保留在 `cause` 与日志中，不透出到对外响应。

## 兼容性边界

- 本轮在既有链路上**增量**接入联动：不联动的申请（提交时无 OPEN 告警）其结论、字段与重放行为与上一版本完全一致。
- 评分阈值、特征校验、告警去重与窗口对齐规则保持不变；新增字段（`link_policy_version`、`base_*`、`linked_alerts`、`linked_application_id`）均为附加信息，不改变原有字段语义。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 联动升级、双向追溯、历史不可变与重放一致、并发关联一致性、非有限数值边界、错误归一化
uv run ruff check app tests
uv run python main.py   # 启动服务（127.0.0.1:8000）
```

跑测试时 `risk.service` logger 会输出申请标识、模型版本、收紧前后判定与分数、判定理由、本次用到的每条告警（alert 号/类别/严重度/扣分/阶梯）以及归一化后的错误，便于定位。

接口示例：

```bash
# 交易侧先产生一条重复交易告警（同窗口同商户同金额两笔）
curl -X POST localhost:8000/transactions -H 'content-type: application/json' -d '{"tx_id":"t1","customer_id":"c1","amount":100.0,"timestamp":0.0,"merchant":"m1"}'
curl -X POST localhost:8000/transactions -H 'content-type: application/json' -d '{"tx_id":"t2","customer_id":"c1","amount":100.0,"timestamp":10.0,"merchant":"m1"}'

# 再提交申请：原本 APPROVE 会因未关联告警收紧为 REVIEW，结论里含逐条 ALERT_TIGHTENED 理由
curl -X POST localhost:8000/applications -H 'content-type: application/json' -d '{
  "application_id": "a1", "customer_id": "c1", "model_version": "v1",
  "features": {"age": 40, "annual_income": 500000, "debt_ratio": 0.1,
               "credit_history_months": 200, "delinquency_count": 0}}'

curl localhost:8000/applications/a1            # 正向：申请关联了哪些告警
curl localhost:8000/alerts/<alert_id>/decision # 反向：告警关联了哪次决策
curl localhost:8000/applications/a1/replay      # 按原模型版本+原告警快照逐位重放
```

非法数值（422 `INVALID_INPUT`，不落库、不吞告警）：

```bash
curl -X POST localhost:8000/applications -H 'content-type: application/json' \
  -d '{"application_id":"a2","customer_id":"c1","features":{"age":40,"annual_income":500000,"debt_ratio":NaN,"credit_history_months":200,"delinquency_count":0}}'
```
