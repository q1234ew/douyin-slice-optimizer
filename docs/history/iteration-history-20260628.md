# 2026-06-28 迭代复盘与后续计划

生成日期：2026-06-28
定位：历史记录、后续迭代入口
范围：Dashboard 体验、抖音采集、数据质量、账号分表、算法准备。

## 1. 本轮阶段结论

本轮工作已经从“单纯优化工具界面”推进到“建立抖音切片数据闭环”。后续迭代应围绕以下主线展开：

```text
采集账号作品 -> 清洗去重 -> 账号级保存 -> 历史样本化 -> 账号洞察 -> 候选评分增强 -> Dashboard 展示
```

优先级判断：

- 当前不要优先训练复杂模型。
- 当前不要优先重做大架构或再次大改 Dashboard。
- 当前优先把已采集数据变成系统可复用的历史样本库和账号基线。

## 2. 已完成资产

### 2.1 数据采集

三个关注账号已通过 Chrome Apple Events 页面上下文 API 完成 300+ 条作品采集。脚本不读取 Cookie、localStorage、sessionStorage，不执行点赞、关注、评论、发布等状态变更操作。

| 账号 | 去重作品数 | raw 目录 | clean 目录 |
| --- | ---: | --- | --- |
| 天赐的声音 | 330 | `data/douyin_capture/tianci/raw_20260628T202500_appleevents_api` | `data/douyin_capture/tianci/clean_20260628T202500_appleevents_api` |
| 歌手2026 | 326 | `data/douyin_capture/geshou2026/raw_20260628T203600_appleevents_api` | `data/douyin_capture/geshou2026/clean_20260628T203600_appleevents_api` |
| 思绪未live | 327 | `data/douyin_capture/sixuweilive/raw_20260628T203600_appleevents_api` | `data/douyin_capture/sixuweilive/clean_20260628T203600_appleevents_api` |

合计：983 条去重作品。

### 2.2 数据表输出

后续采集量增大时，必须继续按账号分开保存。总表只作为汇总索引，不作为唯一数据源。

账号独立表：

- `outputs/douyin_three_accounts_20260628/accounts/tianci/tianci_douyin_collection_latest.xlsx`
- `outputs/douyin_three_accounts_20260628/accounts/geshou2026/geshou2026_douyin_collection_latest.xlsx`
- `outputs/douyin_three_accounts_20260628/accounts/sixuweilive/sixuweilive_douyin_collection_latest.xlsx`

汇总索引表：

- `outputs/douyin_three_accounts_20260628/three_accounts_douyin_collection_latest.xlsx`

采集进度记录：

- `outputs/douyin_three_accounts_20260628/quality_progress.md`

## 3. 本轮复盘

### 3.1 Dashboard / UE

前期问题集中在使用流程不够清楚，而不是某个单点 UI。工作台、节目管理、推荐模拟、数据反馈、账号采集没有形成统一路径。后续 Dashboard 应围绕运营动作重新组织：

1. 导入素材。
2. 生成候选。
3. 评分与推荐模拟。
4. 发布或人工反馈。
5. 账号历史学习与复盘。

### 3.2 前后端技术方向

前端继续使用 Vue 3 + Vite + TypeScript 更合适，Python 后端继续承担本地媒体处理、清洗、评分、API 和文件管理。短期不需要为了采集和数据分析改成全 JS 后端。

### 3.3 抖音数据采集

旧路径问题：

- Chrome 扩展和 DOM 滚屏对深度作品网格不稳定。
- 推荐、热门、同类节目内容可能混入 DOM，不能当作账号作品到底信号。

新路径结论：

- Apple Events 页面上下文 API 更稳定。
- 使用 API 作者字段或账号上下文过滤非目标账号内容。
- 推荐内容混入只作为过滤风险，不作为停止条件。

### 3.4 数据质量

已形成的约定：

- 原始数据不可变保存。
- clean 数据按账号分目录。
- Excel 避免 CSV 编码问题。
- `aweme_id` 在 Excel 中按文本写入，避免科学计数和精度损失。
- 质量报告记录重复率、去重数、采集页数、作者不匹配剔除数。

### 3.5 算法准备

当前 983 条作品足够做：

- 账号内表现分位数。
- 弱监督高/中/低表现标签。
- 内容类别、hook、艺人、标签、时长、发布时间的账号基线。
- 历史相似召回。
- 主题聚类和机会发现。

当前还不适合直接做生产级复杂模型，原因：

- 账号数只有 3 个。
- 当前 `play_count` 多数为 0。
- 指标主要依赖点赞、评论、分享、收藏、页面可见计数。
- 容易学到账号体量或艺人热度偏差，而不是切片质量。

## 4. 主要短板

1. 采集数据仍主要停留在文件和 Excel 层，尚未进入系统级历史样本库。
2. Dashboard 还没有“账号数据资产”视图。
3. 算法模块已有 memory bank、interest clock、backtest 雏形，但还没有消费这批新采集数据。
4. 还没有完成一次端到端闭环验收：采集 -> 清洗 -> 样本化 -> 账号洞察 -> 候选评分增强 -> UI 展示。

## 5. 后续迭代主线

下一阶段命名建议：`V0.6 数据资产化 + 账号洞察`。

目标：把当前三账号 983 条作品转成可复用历史样本库，并让候选评分与 Dashboard 能读到账号历史规律。

## 6. 三轮迭代计划

### 第一轮：历史样本库与弱标签

目标：让系统稳定读取账号级 clean JSON，生成可复用历史样本。

任务：

- 增加采集作品到历史样本的导入入口。
- 每条作品生成 `reward_proxy`。
- 按账号内分位数生成表现标签：`high | mid | low`。
- 保留来源账号、采集批次、raw/clean 路径、质量等级、字段版本。
- 输出 JSON/CSV 或写入现有训练样本表，优先保持可回溯。

建议 `reward_proxy` 初版：

```text
reward_proxy =
  log1p(api_digg_count or visible_count)
  + 2.0 * log1p(api_comment_count)
  + 2.5 * log1p(api_share_count)
  + 2.0 * log1p(api_collect_count)
```

验收：

- 三账号合计导入 983 条历史样本。
- 每个账号都有高/中/低表现标签。
- 重跑导入不会重复写入同一 `aweme_id`。
- 输出样本数、跳过数、低质量行数。

### 第二轮：账号基线与洞察报告

目标：回答每个账号“什么内容更值得剪”。

任务：

- 统计每个账号的内容类别、hook、切片结构、艺人、歌曲、标签、时长段、发布时间表现。
- 输出账号基线 JSON。
- 输出账号洞察 Markdown。
- 标注样本数不足、不确定性高、指标缺失等风险。

验收：

- 每个账号至少输出 Top 内容类别、Top hook、Top 艺人/标签、低表现主题。
- 每个洞察项包含样本数、表现均值/分位数、置信度。
- Dashboard 可直接消费该 JSON。

### 第三轮：历史相似召回与 Dashboard 接入

目标：让推荐候选真正用上历史数据。

任务：

- 用标题、标签、艺人、节目、hook、内容类别构建文本特征。
- 对新候选切片召回相似历史高表现和低表现作品。
- 输出：
  - `similar_high_perf_score`
  - `similar_low_perf_risk`
  - `history_uncertainty`
  - `matched_history_examples`
- 接入候选评分解释。
- Dashboard 增加账号数据资产和历史相似解释区。

验收：

- 新候选能展示相似历史作品。
- 推荐解释中能说明“为什么适合这个账号”。
- 对样本不足账号显示低置信度，不输出过度确定建议。

## 7. 暂缓事项

以下事项暂缓，避免过早复杂化：

- 训练生产级 LightGBM / XGBoost / 深度模型。
- 大改数据库 schema。
- 再次重构整个 Dashboard 信息架构。
- 继续扩大采集账号数量但不样本化。
- 使用总表替代账号级数据源。

当满足以下条件后，再考虑轻量模型：

- 至少 10 个账号。
- 每账号 300+ 作品。
- 有 6h / 24h / 7d / 30d 窗口指标。
- 指标来源和权限边界明确。
- 已有离线回测报告。

## 8. 下一步 P0

下一步优先做：

```text
账号级 clean JSON -> historical samples -> reward_proxy -> 账号内分位标签 -> 账号基线
```

建议第一批落地文件或接口：

- `src/dso/learning/historical_samples.py`
- `dso import-douyin-history --input-dir data/douyin_capture/<account>/clean_<run_id> --account <account>`
- `outputs/douyin_three_accounts_20260628/history_samples_latest.json`
- `outputs/douyin_three_accounts_20260628/account_baselines_latest.json`

## 9. V0.6 完成记录

完成日期：2026-06-28
版本定位：`V0.6 数据资产化 + 账号洞察`

### 9.1 代码实现

已在现有工程内完成以下能力：

- `historical_capture_samples` 扩展为可承载采集历史样本、弱标签、奖励分和质量信息的样本资产表。
- 新增 `DOUYIN_HISTORY_VERSION = "douyin_history.v1"`。
- 新增 `import_douyin_history()`：支持从账号级 clean JSON 自动配对 raw API JSON，写入历史样本。
- 新增 `douyin_history_baselines()`：按账号、内容类别、hook、切片结构、节目、艺人、标签、时长段、发布时间聚合账号基线。
- 新增 `export_douyin_history_assets()`：输出历史样本 JSON、账号基线 JSON 和账号洞察 Markdown。
- 新增 CLI：
  - `douyin-history-import`
  - `douyin-history-baselines`
- 新增 API：
  - `POST /learning/douyin-history/import`
  - `GET /learning/douyin-history/baselines`
  - `POST /learning/douyin-history/export`
- 兼容旧入口：`POST /learning/historical-samples/import` 传 `source_type=douyin_clean` 时走新导入流程。

### 9.2 实际数据入库

已基于当前三账号 clean 数据完成导入：

| 账号 | 数据集 | 样本数 | high | mid | low |
| --- | --- | ---: | ---: | ---: | ---: |
| 天赐的声音 | `tianci_20260628` | 330 | 66 | 198 | 66 |
| 歌手2026 | `geshou2026_20260628` | 326 | 66 | 194 | 66 |
| 思绪未live | `sixuweilive_20260628` | 327 | 66 | 195 | 66 |
| 合计 | `all` | 983 | 198 | 587 | 198 |

账号原型库已用本轮历史样本重建：

| 账号 | 原型数量 | 样本来源 |
| --- | ---: | --- |
| 天赐的声音 | 6 | `visible_capture` / `tianci_20260628` |
| 歌手2026 | 6 | `visible_capture` / `geshou2026_20260628` |
| 思绪未live | 5 | `visible_capture` / `sixuweilive_20260628` |

### 9.3 输出资产

V0.6 输出目录：

- `outputs/v0.6_douyin_history/history_samples_latest.json`
- `outputs/v0.6_douyin_history/account_baselines_latest.json`
- `outputs/v0.6_douyin_history/account_insights_latest.md`
- `outputs/v0.6_douyin_history/douyin_history_samples_tianci.json`
- `outputs/v0.6_douyin_history/douyin_history_samples_geshou2026.json`
- `outputs/v0.6_douyin_history/douyin_history_samples_sixuweilive.json`
- `outputs/v0.6_douyin_history/douyin_history_samples_all.json`

### 9.4 验证结果

新增测试覆盖：

- clean JSON 导入。
- high/mid/low 弱标签生成。
- 账号基线查询。
- 历史样本喂给 prototype bank。
- Web API 通过 `source_type=douyin_clean` 导入。

全量验证：

```text
pytest -q
78 passed, 4 warnings
```

### 9.5 下一步

V0.6 已经完成“数据资产化 + 账号洞察”的后端基础。下一轮建议进入：

```text
历史相似召回 -> 候选评分解释增强 -> Dashboard 账号数据资产视图
```

## 10. 参考文档

- `docs/guides/douyin-collection-standard.md`
- `docs/history/douyin-visible-collection-flow.md`
- `docs/research/strategy/algorithm-study.md`
- `docs/research/strategy/paper-architecture-review.md`
- `docs/history/agent-next-iteration-plan.md`
