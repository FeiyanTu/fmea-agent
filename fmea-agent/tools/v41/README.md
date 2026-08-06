# v4.1 链式方法论工具集

本目录用于 FMEA 项目 v4.1 链式方法论（AIAG/VDA 2019）的数据生成、修复和验证。

## 文件清单

| 文件 | 用途 | 调用方式 |
|---|---|---|
| `rerun_step2_3_v41.py` | 重新跑 Step 2（功能分析）+ Step 3（失效分析） | `python rerun_step2_3_v41.py` |
| `gen_failure_v41.py` | 基于已有 02-function.json 程序化生成 03-failure.json（不依赖 LLM） | `python gen_failure_v41.py` |

## 链式方法论核心

```
上一较高级别  →  关注要素 (focus)  →  下一较高级别 (child)
   Pack总成         结构总成            上盖   (一行对应一个 child)
                                            下盖   (一行对应一个 child)
```

每条 `function` 和 `failure` 都必须带 `child_node_id`，与 (focus, child) 一对一展开。

## 模型扩展

v4.1 在 `models/function.py::FunctionDetail` 和 `models/failure.py::FailureMode`
中新增 `child_node_id: str = ""` 字段。

## 使用场景

- **LLM 工具调用稳定时**：跑 `rerun_step2_3_v41.py` 生成完整数据
- **LLM 工具调用不稳定时**：跑 `rerun_step2_3_v41.py` 拿到部分数据，再用 `gen_failure_v41.py`
  程序化补齐 03-failure.json（FM/FC 用模板，质量不如 LLM 但保证结构正确）
- **数据出现"伪 function"**：跑 `gen_failure_v41.py` 会自动清空无 child_node_id 的伪 function

## 注意事项

- `rerun_step2_3_v41.py` 会从 `.env` 读取 LLM 配置（API key），需要 .env 存在
- 跑完后需执行 `generate_report_tool(project_id)` 验证报告
- 报告 Excel/HTML 中 `next_lower_level` 列已是单个 child 名（不再用逗号拼接）
