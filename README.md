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
| LOW_SCORE | 校验通过但分数低于复核阈值 |

## 版本与重放规则

- 打分是 `(model_version, features)` 的**纯函数**：同一特征快照 + 同一版本 ⇒ 完全一致的分数、类别与结论；快照哈希（`feature_snapshot_hash`）相同。
- 模型按版本注册（当前 `v1`、`v2`），未指定版本时使用最新版；未知版本返回 `UNKNOWN_MODEL_VERSION`。
- 每条申请记录保存原始特征快照与所用版本；`GET /applications/{id}/replay` 始终按**原版本**重放，模型升级不影响历史结论；重放结果与历史不一致时视为内部错误（`COMPUTE_FAILURE`）。

## 幂等与告警状态一致性约定

- `application_id` 是幂等键：重复/并发提交只产生**一份最终结论和一条风险记录**（风险记录号在锁内、仅真正写入时分配，竞争失败不消耗序号、不重复计数）。
- 重复提交内容一致 ⇒ 返回既有结论；内容冲突（快照或版本不同）⇒ `CONCURRENT_CONFLICT`（HTTP 409），绝不返回互相矛盾的结论。
- 告警按 `(customer_id, kind, window_start)` 去重：同一 600s 对齐窗口内的重复/突发/偏离告警只保留一条；告警状态 `OPEN → LINKED_TO_DECISION` 与评分结论联动。
- 底层异常一律归一化为 `RiskServiceError`（`UPSTREAM_FAILURE` / `COMPUTE_FAILURE` / `PERSISTENCE_FAILURE`），原始异常仅保留在 `cause` 与日志中，不透出到对外响应。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行测试（并发重复提交、版本重放、特征异常、告警去重、错误归一化）
uv run python main.py   # 启动服务（127.0.0.1:8000）
```

接口示例：

```bash
curl -X POST localhost:8000/applications -H 'content-type: application/json' -d '{
  "application_id": "a1", "customer_id": "c1",
  "features": {"age": 40, "annual_income": 500000, "debt_ratio": 0.1,
               "credit_history_months": 200, "delinquency_count": 0}}'
curl localhost:8000/applications/a1/replay
curl -X POST localhost:8000/transactions -H 'content-type: application/json' -d '{
  "tx_id": "t1", "customer_id": "c1", "amount": 100.0, "timestamp": 0.0, "merchant": "m1"}'
```
