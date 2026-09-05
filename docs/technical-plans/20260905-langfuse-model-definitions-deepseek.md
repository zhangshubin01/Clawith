# Langfuse 自定义模型定义：DeepSeek V4（#7 落地清单）

> 目的：让 Langfuse 对 DeepSeek 的 `cost_usd` 推断从「0 / 错价」变为准确，从而在
> dashboard 里看到每 trace / agent / tenant 的真实美元成本。
> 状态：纯 UI 操作，零代码、零线上行为变更；按本清单在 Langfuse `Project Settings
> > Models` 添加即可。

## 背景事实（本次核实）

- Clawith 实际在用的模型（`llm_models` 表现行数据）只有两个：
  `deepseek-v4-pro`、`deepseek-v4-flash`，`base_url` 均为 `https://api.deepseek.com`。
- DeepSeek 官方定价（`api-docs.deepseek.com/quick_start/pricing`，per **1M** tokens，
  峰时 = 谷时 ×2；峰时 = UTC 01:00–04:00 & 06:00–10:00 工作日 = **北京时间 09:00–12:00 &
  14:00–18:00**）：

| 模型 | 输入 cache hit（谷/峰） | 输入 cache miss（谷/峰） | 输出（谷/峰） |
| --- | --- | --- | --- |
| deepseek-v4-flash | $0.007 / $0.014 | $0.22 / $0.44 | $0.66 / $1.32 |
| deepseek-v4-pro   | $0.022 / $0.044 | $0.66 / $1.32 | $1.98 / $3.96 |

- DeepSeek **reasoning 与普通输出同价**（价目表只有「1M OUTPUT TOKENS」一栏，无独立
  reasoning 价）；**无 cache write 计费**（前缀缓存只有 hit 折扣，无 write 溢价）。
- Langfuse 的 usage bucket 键名（`input` / `input_cache_read` / `output` /
  `output_reasoning_tokens`）与 Clawith `_map_usage` 输出的键名一致；`total` 由
  Langfuse 自动按桶求和，无需填。

## 换算成 per-token USD（Langfuse 填的是「每 token 美元」）

| 模型 | input（cache miss） | input_cache_read（hit） | output（含 reasoning） |
| --- | --- | --- | --- |
| deepseek-v4-flash 谷价 | 2.2e-7 | 7e-9 | 6.6e-7 |
| deepseek-v4-flash 峰价 | 4.4e-7 | 1.4e-8 | 1.32e-6 |
| deepseek-v4-pro 谷价   | 6.6e-7 | 2.2e-8 | 1.98e-6 |
| deepseek-v4-pro 峰价   | 1.32e-6 | 4.4e-8 | 3.96e-6 |

## 操作步骤（Project Settings > Models）

1. `Project Settings > Models` → 点 `+`（或 "Add model definition"）。
2. 每个模型一条，字段如下（`unit` 选 **TOKENS**）：

**deepseek-v4-pro**

| 字段 | 值 |
| --- | --- |
| Model name | `deepseek-v4-pro` |
| Match pattern | `(?i)^deepseek-v4-pro$` |
| Unit | `TOKENS` |
| `input` (cache miss) | `0.00000066`（谷）或 `0.00000132`（峰） |
| `input_cache_read` (cache hit) | `0.000000022`（谷）或 `0.000000044`（峰） |
| `output` | `0.00000198`（谷）或 `0.00000396`（峰） |
| `output_reasoning_tokens` | 与 `output` 同价 |

**deepseek-v4-flash**

| 字段 | 值 |
| --- | --- |
| Model name | `deepseek-v4-flash` |
| Match pattern | `(?i)^deepseek-v4-flash$` |
| Unit | `TOKENS` |
| `input` (cache miss) | `0.00000022`（谷）或 `0.00000044`（峰） |
| `input_cache_read` (cache hit) | `0.000000007`（谷）或 `0.000000014`（峰） |
| `output` | `0.00000066`（谷）或 `0.00000132`（峰） |
| `output_reasoning_tokens` | 与 `output` 同价 |

> 不用填 `input_cache_creation`（DeepSeek 无 cache write 计费，Clawith 对 DeepSeek
> 也不会输出该桶）。

## 峰 / 谷 选哪个（重要，需你拍板）

Langfuse 的 pricing tier **只能按 usage / model 参数 / metadata 匹配，无法按时段**，
所以峰谷只能二选一：

- 峰价 = 北京时间 **09:00–12:00、14:00–18:00**（工作日）。若你的租户用量集中在
  **白天工作时间**，用**峰价**更准（且偏保守，dashboard 不会低估成本）。
- 谷价覆盖其余 ~76% 的时钟小时；若用量全天均匀，用谷价。

**推荐：先按峰价填**（成本敏感时宁可高估不可低估，且你的主流量大概率落在北京时间白天
= DeepSeek 峰时）。若之后发现明显虚高，再改回谷价。

## 备选：Models API 精确写入（需 Langfuse API key，可脚本化）

`POST /api/public/models`，body（以 deepseek-v4-pro 峰价为例）：

```json
{
  "modelName": "deepseek-v4-pro",
  "matchPattern": "(?i)^deepseek-v4-pro$",
  "unit": "TOKENS",
  "pricingTiers": [
    {
      "name": "Standard",
      "isDefault": true,
      "priority": 0,
      "conditions": [],
      "prices": {
        "input": 1.32e-6,
        "input_cache_read": 4.4e-8,
        "output": 3.96e-6,
        "output_reasoning_tokens": 3.96e-6
      }
    }
  ]
}
```

> `matchPattern` 用正则匹配 generation 的 `model` 字段；`(?i)^...$` 为精确匹配、
> 忽略大小写。用户自定义模型定义优先于 Langfuse 内置定价表。

## 验证方式

配置后：任意一条新 run 的 generation 详情里，`cost` 应出现非零 USD；dashboard 的
成本聚合（按 model / tenant / tag）不再显示 0。旧 trace 不会追溯重算，只影响新 trace。
