# G1/G2 跨入口冻结基准

日期：2026-07-18
状态：`baseline_frozen_with_known_gaps`

## 冻结对象

- Benchmark ID：`dso-v1-cross-entry-20260718-r2`
- Manifest：`benchmarks/dso-v1-cross-entry-20260718-r2.json`
- 内容 SHA-256：`a4df0cbac32689a475a6abbacdd5eff81a86163900184b1d5a8ce642cbf66386`
- Git 父检查点：`79e356c86e81c4a2df666a23d5d4cc381b8108de`
- 候选 contract：`standard_candidate.v1`
- 生产排序 contract：`production_ranking_policy.v1`

快照包含 10,984 条可比较历史互动代理样本、25 个账号、60 条 confirmed Material Gold、623 条 Omni Shadow cache、1 个完整节目源和 30 条已评分 G2 候选。30 条候选均为 `standard_candidate.v1`，无 contract mismatch。

r1 在冻结后因并行 ASR 工作新增无关的全局版本常量而严格报 `source_code` 漂移；数据、候选和生产 policy 均未漂移。r2 保留所有实际影响跨入口排序行为的源码指纹，并移除无关全局版本文件，是当前唯一比较基准。

正式数据库在冻结时没有 G1 precut 批次，因此 G1 只冻结了已发布历史短片的互动代理排序集；这不是实际 G1 发布反馈。G2 也尚无人工片段召回 Gold，因此当前不能报告 Recall@K。

## 参考回测

时间切分报告：`bt_93c6ad2ad31d4639`，验证样本 2,157 条，`K=30`。

| 策略 | NDCG@30 | Top-K lift | 高互动命中 | 低互动避让 |
| --- | ---: | ---: | ---: | ---: |
| `current_rules` | 0.4641 | 1.4905 | 0.4333 | 0.9000 |
| `semantic_baseline_v2` | 0.4537 | 1.4856 | 0.5667 | 0.9333 |
| `research_ranker_v2_2` | 0.6048 | 1.5941 | 0.7000 | 0.9333 |
| `research_ranker_v2_4` | 0.5275 | 1.4305 | 0.5667 | 0.9000 |
| `research_ranker_v2_8/v2_9` | 0.5374 | 1.4356 | 0.5667 | 0.9000 |

这些指标基于可见互动代理，不是播放量预测或线上流量承诺。

## 排序与门禁结论

默认生产排序固定为 `current_rules/final_score`。`ranker_score`、Omni `hybrid_score`、embedding 与后续模型分只在显式 research scope 中使用。

`research_ranker_v2_4` 未通过 promotion gate：

- 未达到 lift `1.85`、高互动命中 `0.90`、低互动避让 `0.95` 的绝对门槛。
- lift 相对最强基线为 `-0.0600`，要求至少 `+0.0300`。
- 低互动避让相对最强基线为 `-0.0333`，不允许回退。
- 即使未来 gate 通过，也只标记 `eligible_for_promotion`；不会自动改写生产 policy。

## 复验命令

```bash
PYTHONPATH=src .venv/bin/python -m dso.cli benchmark-verify \
  --benchmark-id dso-v1-cross-entry-20260718-r2

PYTHONPATH=src .venv/bin/python -m dso.cli benchmark-run \
  --benchmark-id dso-v1-cross-entry-20260718-r2
```

冻结 manifest 不得原地编辑。样本、候选、缓存、Gold、评分代码或 policy 变化后必须创建新的 benchmark ID。
