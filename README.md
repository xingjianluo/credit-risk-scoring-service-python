# 信用评分与异常交易检测服务

基于 FastAPI 的可复现信用评分与异常交易检测服务：同一份客户与交易数据，
在不同请求、并发与模型版本下得到一致且可解释的风险结论。

## 评分类别与阈值语义

评分为 `score ∈ [0, 1]` 的逻辑回归输出（确定性线性组合 + sigmoid），按两个阈值分三档：

| 类别 | 条件 | 语义 |
| --- | --- | --- |
| `APPROVE` | `score >= approve_threshold` | 通过 |
| `REVIEW` | `review_threshold <= score < approve_threshold` | 人工复核 |
| `REJECT` | `score < review_threshold` | 拒绝 |

注册模型时强制 `0 <= review_threshold <= approve_threshold <= 1`。
每个结论都附带 `reasons`（判定依据）：对得分产生负向贡献的特征及其贡献值，
按固定顺序输出，保证可解释且可复现。

### 特征校验（异常不得被默认值静默通过）

评分前校验全部关键特征（`app/features.py` 的 `FEATURE_SPECS`），失败原因可区分：

| 错误码 | 含义 |
| --- | --- |
| `FEATURE_MISSING` | 关键特征缺失 |
| `FEATURE_TYPE_INVALID` | 类型异常（含 `bool` 冒充数值、`NaN`/`Inf`、`None`） |
| `FEATURE_OUT_OF_RANGE` | 取值越界（带上下限说明） |

## 模型版本与重放规则

- 模型版本**不可变**：同一版本号禁止以不同参数重复注册（同参数重复注册幂等允许）。
- 同一特征快照 + 同一模型版本 => 任何请求/并发下结论**完全一致**
  （权重按固定顺序求和，浮点结果确定）。
- 模型升级通过注册新版本完成（内置 `v1`、`v2` 并存），旧版本永久保留。
- 历史结论重放：`POST /applications/{id}/replay` 按风险记录中保存的
  **原模型版本**重新评分，结论与历史完全一致。
- 未知版本返回 `MODEL_VERSION_UNKNOWN`。

## 幂等与告警状态一致性约定

### 申请提交

- 同一 `application_id` 只产生**一份最终结论和一条风险记录**
  （存储层 `insert_if_absent` 在单锁内完成"检查+写入"）。
- 重复/并发提交（同模型版本）：返回已存结论，不重复计算、不重复计数。
- 同一 `application_id` 换模型版本提交：返回 `409 APPLICATION_CONFLICT`，
  绝不返回互相矛盾的结论，已有记录不发生状态漂移。
- 特征校验失败**不落库、不计数**，修正后重试可成功。

### 异常交易告警

检测规则（`app/anomaly.py`）：

| 规则 | 含义 |
| --- | --- |
| `DUPLICATE_TRANSACTION` | 同一 `transaction_id` 在窗口内重复出现 |
| `BURST_TRANSACTIONS` | 同一客户窗口内交易数达到阈值 |
| `BEHAVIOR_DEVIATION` | 单笔金额 >= 历史均值 × 偏离倍数 |

- 去重：同一 `(客户, 规则, 时间窗口)` 只告警一次；跨窗口重新评估。
- 状态联动：评分结论为 `REJECT`/`REVIEW` 时，该客户未处理的告警
  由 `OPEN` 置为 `LINKED`，告警状态与评分结论保持一致；`APPROVE` 不联动。

### 错误归一化

底层异常不直接透出，统一归一化为可区分错误码：
`FEATURE_*`、`MODEL_VERSION_UNKNOWN`、`APPLICATION_CONFLICT`、
`UPSTREAM_FAILURE`（上游数据非法）、`COMPUTE_FAILURE`、
`PERSISTENCE_FAILURE`、`INTERNAL_ERROR`。`cause` 仅记录在服务日志中。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/applications` | 幂等提交申请并评分 |
| POST | `/applications/{id}/replay` | 按原模型版本重放历史结论 |
| GET | `/risk-records/{id}` | 查询风险记录 |
| POST | `/transactions` | 摄入交易，返回新产生的告警 |
| GET | `/alerts` | 查询全部告警 |

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（并发重复提交 / 版本重放 / 特征异常 / 告警去重）
uv run pytest -q -s     # 查看单测日志：申请标识、模型版本、判定依据、归一化错误
uv run python main.py   # 启动服务（127.0.0.1:8000，文档见 /docs）
```
