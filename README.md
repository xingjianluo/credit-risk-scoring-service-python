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
| NON_FINITE_VALUE | 非有限数值（`NaN` / `Infinity` / `-Infinity`），不允许当普通低分放过 |
| FEATURE_OUT_OF_RANGE | 取值越界 |
| LOW_SCORE | 校验通过但分数低于复核阈值 |
| ALERT_LINKAGE | 结论被交易告警联动收紧（理由含具体告警 ID、类别、扣分与判定下限） |

## 版本与重放规则

- 打分是 `(model_version, features)` 的**纯函数**：同一特征快照 + 同一版本 ⇒ 完全一致的分数、类别与结论；快照哈希（`feature_snapshot_hash`）相同。
- 模型按版本注册（当前 `v1`、`v2`），未指定版本时使用最新版；未知版本返回 `UNKNOWN_MODEL_VERSION`。
- 每条申请记录保存原始特征快照与所用版本；`GET /applications/{id}/replay` 始终按**原版本**重放，模型升级不影响历史结论；重放结果与历史不一致时视为内部错误（`COMPUTE_FAILURE`）。

## 告警联动收紧（申请与交易闭环）

提交申请时，系统会认领该客户名下**状态为 `OPEN`（尚未关联任何决策）**的全部告警，把它们纳入最终判定。已经是 `LINKED_TO_DECISION` 的告警不会重复参与。

每类告警的收紧规则（`app/linkage.py`，纯函数）：

| 告警类别 | 评分扣减 | 判定下限 | 含义 |
|---|---:|---|---|
| DUPLICATE_TRANSACTION | 40 | REVIEW | 同窗口同商户同金额，疑似重复扣款/盗刷 |
| BURST | 80 | REVIEW | 短时间交易频次异常，疑似账户失控 |
| BEHAVIOR_DEVIATION | 150 | DECLINE | 单笔金额显著偏离历史水平，欺诈风险高 |

最终判定由两条收紧路径取更严者：

1. 基线分减去**所有被采用告警扣分之和**（分数钳制在 `[300, 900]`）后对应的判定；
2. 所有被采用告警的**判定下限**中最严的那个。

因此联动只可能收紧、不会放松：原本 `APPROVE` 可能变为 `REVIEW`/`DECLINE`。每条被采用告警都会在结论的 `reasons` 中产生一条 `ALERT_LINKAGE`，明确写出**告警 ID、类别、关联交易、扣了多少分、判定被抬到哪一级**；响应的 `linked_alerts` 字段同时给出冻结的联动明细。

关联关系的建立与双向追溯：

- 申请落库与告警认领（`OPEN → LINKED_TO_DECISION`、回填 `linked_application_id`）在存储层**同一把锁内原子完成**，杜绝漏关联、重复关联、挂到别的申请；
- 申请 → 告警：`GET /applications/{id}/alerts`（服务方法 `get_application_alerts`）；
- 告警 → 决策：`GET /alerts/{id}/decision`（服务方法 `get_alert_linked_application`），未关联返回 `linked: false`。

## 历史不可变与重放

- 每次决策会把当时采用的告警冻结成 `AlertLink` 快照（告警 ID、类别、交易、**扣分与判定下限的取值**、理由），随申请一并固定；
- 已出结论的申请，其判定与关联关系**不可变**：同一客户之后再冒出的新告警不会回头改写历史申请，也不会补挂；新告警保持 `OPEN`，只影响之后的新申请；
- `GET /applications/{id}/replay` 用**原模型版本**重算基线分，再叠加**当时冻结的联动快照**，与历史结论逐位一致（分数、判定、理由、`feature_snapshot_hash` 全相等）；即使之后调整 `app/linkage.py` 中的策略数值，历史结论也不漂移。重放与历史不一致会报 `COMPUTE_FAILURE`。

## 幂等、并发与错误归一化约定

- `application_id` 是幂等键：重复/并发提交只产生**一份最终结论和一条风险记录**（风险记录号在锁内、仅真正写入时分配，竞争失败不消耗序号、不重复计数，不产生重复额度占用）。
- 重复提交内容一致 ⇒ 返回既有结论；内容冲突（快照或版本不同）⇒ `CONCURRENT_CONFLICT`（HTTP 409），绝不返回互相矛盾的结论。
- 「创建申请 + 认领 OPEN 告警」原子串行化：同一申请并发重复提交返回同一对象；多个不同申请并发时，每条 OPEN 告警恰好被一个申请认领，不会重复或错挂。
- 告警按 `(customer_id, kind, window_start)` 去重：同一 600s 对齐窗口内同类告警只保留一条；告警与申请几乎同时产生时按存储锁线性化——先落库者先认领，未被认领的告警保持 `OPEN` 并在下次申请时被纳入，关联不丢。
- 底层异常一律归一化为 `RiskServiceError`（`UPSTREAM_FAILURE` / `COMPUTE_FAILURE` / `PERSISTENCE_FAILURE` / `CONCURRENT_CONFLICT` 等），原始异常仅保留在 `cause` 与日志中，不透出到对外响应。
- 非有限数值（`NaN`/`Infinity`）无论来自 int 还是 float 规格字段，都在类型与越界检查之前识别为 `NON_FINITE_VALUE`（随申请结论返回，HTTP 200 + `DECLINE`），不会被当成正常低分悄悄放过。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 31 个用例：联动升级/双向追溯/历史不可变与重放/并发关联一致/
                        # 非法数值边界 + 上一轮幂等、版本重放、特征异常、告警去重、错误归一化
uv run python main.py   # 启动服务（127.0.0.1:8000）
```

接口示例（制造告警 → 申请被收紧 → 双向追溯 → 重放）：

```bash
# 1) 同窗口两笔同商户同金额 -> 重复交易告警
curl -X POST localhost:8000/transactions -H 'content-type: application/json' -d '{
  "tx_id": "t1", "customer_id": "c1", "amount": 100.0, "timestamp": 0.0, "merchant": "m1"}'
curl -X POST localhost:8000/transactions -H 'content-type: application/json' -d '{
  "tx_id": "t2", "customer_id": "c1", "amount": 100.0, "timestamp": 10.0, "merchant": "m1"}'

# 2) 提交申请：基线 APPROVE 被联动收紧为 REVIEW，linked_alerts/reasons 给出明细
curl -X POST localhost:8000/applications -H 'content-type: application/json' -d '{
  "application_id": "a1", "customer_id": "c1",
  "features": {"age": 40, "annual_income": 500000, "debt_ratio": 0.1,
               "credit_history_months": 200, "delinquency_count": 0}}'

# 3) 双向追溯
curl localhost:8000/applications/a1/alerts
curl localhost:8000/alerts/<alert_id>/decision

# 4) 按原版本 + 冻结联动快照重放（逐位一致）
curl localhost:8000/applications/a1/replay
```

日志中每次申请都会打印申请标识、客户、模型版本、判定与分数、`linked_alerts`（本次用到的告警 ID 列表）以及判定理由码；归一化错误会以 ERROR 打印申请标识与失败阶段，方便定位且不外泄底层细节。
