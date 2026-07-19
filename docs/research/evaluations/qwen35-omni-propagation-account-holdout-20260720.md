# Qwen3.5-Omni 传播事实特征账号隔离验证

- 日期：2026-07-20
- 目标：G1 已切短片筛选排名 / G3 本地与公网模型协同
- 状态：`research_only / keep_v2_4`

## 1. 目的

本轮承接 10 条 schema pilot，验证完整音视频事实特征是否能在跨账号条件下稳定补充 `research_ranker_v2_4`。Omni 只抽取可观察的音画、时序和叙事事实；请求不含账号、标题、标签、互动数或既有策略结果，也不要求模型预测流量。

冻结产物：

- 样本 manifest：`benchmarks/dso-omni-propagation-account-holdout-20260720-r1.json`，文件 SHA-256 `8eeb114bda5f7343e0f038c1cd8c066f52af83e8cb3581a0fcceb87f166ca2a1`。
- 受限恢复 manifest：`benchmarks/dso-omni-propagation-account-holdout-20260720-r1-recovery1800.json`，文件 SHA-256 `22c29db390e9fdeebb0b46c56c96c38d90cbe1c1956966c1becd64a397661464`。
- 冻结评测：`benchmarks/dso-omni-propagation-account-holdout-result-20260720-r1.json`，文件 SHA-256 `21fb2055029f05111a90eb917b8fb4ceec147d75c957c793d3bc770ffc2b0fcc`。

## 2. 数据与防泄漏

共 30 组 high/low 配对、60 条唯一短片、8 个研究账号。高低样本必须同账号、同发布时间年龄桶、同时长桶、同 `content_category`，并满足：

- 校正互动分差至少 55，实际最小值 `66.3158`。
- 可见互动代理比至少 1.8，实际最小值 `1.9438`。
- 媒体时长差最多 5 秒，实际最大值 `4.895s`。4 秒上限经真实媒体校验后只有 29 对，因此预先记录后放宽 1 秒以保持 30 对目标。
- 平台 item、稳定标题和媒体 SHA 均不可重复；60 个媒体 SHA 全部唯一。
- 60/60 有视频流和音轨。248 条候选中 241 条通过媒体探测；2 条无音轨、5 条数据库/媒体时长不一致被排除。

与此前完整视频 Omni 清单重叠为 0。26 条曾进入代表帧向量 benchmark，已在 manifest 披露；因此本轮可以检验“完整音视频事实特征”的新增价值，但不能称为完全未见的平台结果盲测。

账号配对分布为：`duanduanzhengzheng / raoxianyin / tianci` 各 7 对，`taotao_daxiaojie` 4 对，`yuhuan` 2 对，其余 3 个账号各 1 对。评测除总体指标外，额外使用样本充足账号宏平均和账号胜负门禁，避免大账号支配结论。

## 3. 执行与成本

零网络预检生成 60/60 个完整时域 H.264/AAC 代理，代理总量约 173 MB，最坏预留 `7.900778 CNY`。主批 60 次请求中 59 次通过严格 schema；一条 58.354 秒样本达到 1200 输出 Token 上限，被 `finish_reason` 门禁拒绝。随后只对该样本使用独立 1800 Token 恢复 manifest，实际输出 1097 Token 并通过。

最终结果：

| 指标 | 结果 |
| --- | ---: |
| 严格成功覆盖 | 60/60 |
| 网络请求 | 61 |
| usage 估算 | `6.654878 CNY` |
| schema / 时间线 / 音频 / 视觉证据覆盖 | 100% / 100% / 100% / 100% |
| 成功请求延迟中位数 / P95 | `23.296s / 29.061s` |
| 重试策略 | 单请求 0 次；仅 1 条独立受限恢复 |

日常 EnvironmentFile 没有持久开放 `full_media`。模型输出、缓存和恢复结果不写生产排序、人工 Gold、审核、导出或发布状态。

## 4. 账号隔离结果

每个 fold 留出一个完整账号。Omni 类别对数优势和 v2.4 历史证据都只能读取其他账号；固定融合为 `85% v2.4 + 15% Omni`，没有搜索权重。

| 策略 | 30 对命中 | Top15 高互动命中 | Top15 lift | NDCG@15 |
| --- | ---: | ---: | ---: | ---: |
| 账号隔离 v2.4 | 66.67% | 73.33% | 1.4667 | 0.7952 |
| Omni 事实特征 | 63.33% | 60.00% | 1.2000 | 0.6704 |
| 固定 85/15 融合 | 73.33% | 73.33% | 1.4667 | 0.7984 |

融合的成对命中增加 `6.67pp`，样本充足账号宏平均增加 `7.14pp`，但 Top15 高互动命中和 lift 完全不变，NDCG@15 只增加 `0.0032`。

融合只改变 2/30 个 pair，均由错误改为正确、无新增误导；两条都来自 `duanduanzhengzheng`。精确双侧符号检验 `p=0.5`，账号簇 bootstrap 95% 区间为 `[0, 18.18pp]`，包含 0。4 个样本充足账号中只有 1 个改善，未达到至少 3 个账号的门禁。

## 5. 特征诊断

全量探索性富集里，`novelty=low` 在 low 侧出现率为 `56.67%`、high 侧为 `20%`；`novelty=medium` 在 high 侧为 `53.33%`、low 侧为 `30%`。30 个配对中有 10 对表现为 `medium > low`。`payoff_present=true > false` 出现 8 对，`emotional_intensity=high > medium` 出现 7 对。

这些方向没有形成稳定账号泛化。音频能量、观众反应、叙事弧和 payoff 同时存在反向配对；纯 Omni 在 `duanduanzhengzheng` 为 100%，但在 `raoxianyin` 为 42.86%、`sixuweilive` 和 `yule_xiaoe_yu` 为 0%。这说明当前事实标签更适合作为分歧解释和低分差候选的研究证据，不适合作为统一全局排序权重。

## 6. 结果字段边界

| 目标 | 覆盖 | 结论 |
| --- | ---: | --- |
| 可见互动热度代理 | 60/60 | 可用于本轮成对研究 |
| 分享率 | 0/60 | 缺少播放或曝光分母 |
| 关注转化率 | 0/60 | 缺少分母和新增关注 |
| 观看质量 | 0/60 | 缺少 5 秒留存、平均观看比例和完播率 |

因此本轮没有验证真实流量、分享机制、关注转化或观看质量，也不能解释因果。

## 7. Gate 与决策

通过项：60/60 覆盖、8 个账号、总体增量超过 5pp、样本充足账号宏平均增量超过 5pp、无样本充足账号回退。未通过项：只有 1 个样本充足账号改善，低于要求的 3 个；统计区间包含 0，Top15 命中没有提升，纯 Omni 弱于 v2.4。

```text
promotion_gate=failed
decision=keep_v2_4
production_promotion=false
production_weight_changed=false
writes_manual_gold=false
```

下一步不在同一 60 条上继续搜索权重。Omni 可保留为低分差分歧诊断；若要验证排序价值，需要冻结新的未见 pair，并让至少 3 个账号各具备足够配对。更优先的产品工作仍是接入授权曝光、观看、分享和关注窗口指标，把研究目标从可见互动代理升级为真实多目标结果。

## 8. 复现

```bash
PYTHONPATH=src .venv/bin/python scripts/run_propagation_feature_validation.py merge \
  --manifest benchmarks/dso-omni-propagation-account-holdout-20260720-r1.json \
  --feature-report <primary-feature-report.json> \
  --feature-report <recovery1800-feature-report.json> \
  --output <merged-feature-report.json>

PYTHONPATH=src .venv/bin/python scripts/run_propagation_feature_validation.py evaluate \
  --manifest benchmarks/dso-omni-propagation-account-holdout-20260720-r1.json \
  --feature-report <merged-feature-report.json> \
  --db-path data/db/dso.sqlite3 \
  --omni-weight 0.15 --top-k 15 \
  --output <account-holdout-evaluation.json>
```
