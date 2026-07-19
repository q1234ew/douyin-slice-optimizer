# V1 Beta-D-12A：百炼缓存信号归因与融合校准

- 日期：2026-07-19
- 目标：G3 本地/公网模型协同
- 状态：`research_only / keep_v2_4`

## 1. 问题与冻结输入

D12-A 用于判断现有云信号的增量来自 Text/Fusion Embedding、Rerank 还是与 v2.4 的保守融合。实验不重抽样，继续使用冻结 benchmark `dso-multimodal-vector-value-20260719-r1`：

- Manifest SHA-256：`f32a6699ffc2cc7554ea6e2fa9a0550afc7e92a91cd2c46585beb130544a1510`
- v2.4 冻结侧车 SHA-256：`a2dd4087a688515fb6dadf5a43c0c91668d7f0b6e9ef092e7083996ffb5a54ec`
- 云向量覆盖：Text 110/240、Fusion 110/240、双向量 110/240
- 客观标签：账号内归一化的抓取时点可见互动代理，不含播放量、曝光或关注转化
- 可比子集：现有云缓存覆盖的 40 个完整 pair

全量 60 对上的 v2.4 平衡命中为 `72.85%`，仅作背景；所有 D12-A 增量都使用同一 40 对子集上的 v2.4 `69.80%` 比较，避免覆盖差异。

## 2. 消融矩阵

Runner 固定比较：

1. Text/Fusion cosine，高低互动参考各半。
2. 平衡参考池总量 10/20/40；当前 40 配置因只有 20 条平衡缓存而跳过。
3. 每侧近邻 Top-3/5/10。
4. 当前 `rerank-latest.json` 缓存。
5. Embedding 与 Rerank 的 25%/50%/75% 融合。
6. v2.4 与排名前五的云配置按 5%–30% 云权重搜索。

Pair delta 在融合前按 median absolute pair delta 归一化。每项输出结果侧 macro-average、原始命中、低互动避让、分层 bootstrap 95% 区间、账号/类别/互动差距切片。运行只读现有文件和数据库记录，没有构造 Provider Runtime。

## 3. 结果

| 配置 | 平衡命中 | 同子集 v2.4 | 增量 | 原始命中 | Bootstrap 95% |
| --- | ---: | ---: | ---: | ---: | ---: |
| 最佳纯云：Text cosine + 25% cached Rerank | 75.64% | 69.80% | +5.84pp | 72.50% | -15.24–28.77pp |
| 最佳观察融合：85% v2.4 + 15% cloud | 81.34% | 69.80% | +11.54pp | 77.50% | 0–23.08pp |

最佳观察融合的低互动避让率为 `77.50%`，同子集 v2.4 为 `70.00%`。类别层面 `performance_clip` 和 `commentary` 两个样本量充足组改善；账号层面只有 `dk_voice_teacher` 与 `tianci` 两个样本量充足账号改善，未达到要求的 3 个账号。

## 4. Gate 与限制

扩量 Gate 条件：40 对覆盖、相对 v2.4 至少 `+2pp`、低互动避让不下降、至少 3 个样本量充足账号改善、至少 2 个类别改善、零网络调用。本轮只有账号条件未通过，因此结果为：

```text
status=ready
expansion_gate=keep_v2_4
network_request_count=0
effective_cost_cny=0
production_weight_changed=false
```

配置和融合权重在同一 40 对上搜索，存在明确选择偏差；bootstrap 下界为 0 也说明稳定性证据不足。因此 `81.34%` 是研究候选观察值，不是独立泛化成绩，不冻结为生产权重，也不恢复 Judge 扩量。

## 5. 复现

```bash
dso bailian-vector-ablation \
  --benchmark-id dso-multimodal-vector-value-20260719-r1
```

JSON 报告位于：

```text
outputs/bailian_vector_chain/dso-multimodal-vector-value-20260719-r1/ablation-latest.json
```

下一次只有在形成独立跨账号冻结扩展集后，才使用建议的单批 `5.00 CNY` 上限验证 85/15 融合；当前 50 元配置仍只是系统硬上限，不是实验消费目标。
