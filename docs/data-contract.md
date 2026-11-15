# 业务记录约定

items 的 stratum 是分派时已知资料层，categories 是合法类别集合。assignments 记录 supplier、batch、item_id 及派单概率，未派单不等于弃判。
reviews 以 item_id、supplier、round 标识一次复核，label=abstain 表示明确弃判；revision 更新同一次复核，不增加独立票数。expert_versions 只有部分资料有，expert_label 也是带时间版本的专业结论，不能覆盖整批作为真值。
现有 assignment_overlap 给出供应商计数、交叠资料数与弃判情况。合成记录包含不连通交叠组；供应商重复接到同一资料可能沿用上次判断。

## 共同版本语义
所有示例记录均为本地合成，不包含真实客户资料。received_at 不晚于观察截止的记录才可见；缺省该字段的配置为静态配置。revision 表示同一业务身份的订正次数，不是新样本。时间不带时区时按 UTC 解读。
分析请求会保存可见数据的内容摘要及完整内容；重复 request_id 与原请求完全相同时返回原结果，即使源文件后来改变。其他请求内容复用该编号返回冲突。新观察须使用新编号。


items.categories、reviews.label 与 expert_versions.expert_label 使用相同的 `c0`、`c1`、`c2` 类别标识；abstain 仅用于 reviews，不是类别。


## 快照样例字段补充

以上字段说明对应当前初始快照中的本地合成记录；示例不存在的历史字段不视为隐藏接口要求。


## 已有能力与字段边界

类别统一使用 c0、c1、c2，abstain 是弃判。reviews 的修订身份是 item_id、supplier、round，expert_versions 以 item_id 修订。当前核对只报告去重后的复核和交叠数，没有可靠度估计、共识学习或主动复核排序。

## 类别共识与供应商可靠度（category_consensus）

新增分析 `category_consensus`（精简视图 `category_consensus_summary`）。它不改变上面任何记录语义，只在其上做统计：

- **可靠度只由专家锚点估计**：每个供应商的准确率只用 `expert_versions` 最新版本作锚，按 `items.stratum`（资料层）与“分派供应商数｜派单概率”的派单情境分层。多数票从不作为真值，也不会反过来给供应商打分。
- **弃判与未派单不同**：`abstain` 是明确判断，不计入准确率分子或分母，也不折算成任何类别；未派单只是覆盖缺口。弃判次数和弃判率单独报告。
- **修订不是新票**：`reviews` 以 `(item_id, supplier, round)` 去重，`revision` 取组内最新；`expert_versions` 以 `item_id` 取最新。同一资料同一供应商的多个 `round` 只取最新一轮，`repeat_rounds` 会标出这些配对，旧轮次不当作独立样本。新复核请用新的 `request_id` 重算。
- **分派群体差异**：供应商拿到的资料层构成可能不同。跨供应商比较只在同一连通分组内做直接标准化，同时控制两个维度：资料层构成（`items.stratum`）和专家结论的类别构成（`expert_label`）。缺少锚点的单元不进入加权，不补齐、不外推；只有至少两家供应商在两个维度上都能标准化时才给出排名。
- **不可识别不排名**：没有专家锚点、没有交叠、只有一家可标准化供应商、或缺少派单/复核的分组，`components[].ranking` 为空，状态为 `unidentified_no_anchor`、`no_overlap`、`insufficient_comparable_suppliers` 或 `no_assignment_or_review`。这些供应商不参与高低排序。
- **系统性映射先核对**：若某供应商的错误可由一个整体类别映射解释（映射后一致率明显高于原样一致率），标记为 `mapping_review_required`，不进入加权或排名，系统不会静默反推其标签。
- **共识概率不是准确率**：逐资料的 `leading_probability` / `posterior` 是“在给定锚定可靠度与本次票数下的相对支持度”。区间来自锚点准确率的参数自助法，只覆盖可靠度不确定性；`calibration` 仅是锚点上的留一诊断，不是准确率承诺。
- **每条建议可追溯**：每个 `items[]` 带 `sources`（派单、去重后的复核、专家版本历史）与 `trace_digest`；`differences[]` 逐类别说明该差异是有证据、被更强证据压倒，还是仍需补证据。
- **专家名额分配**：`expert_queue` 按证据缺口排序：先隔离类别非法的复核，再补完全无锚点的分组，然后才是已有锚点但未解决的争议、全弃判、低置信度和单源结论；名额有限时优先覆盖尚未有锚点的分组。分数只决定补复核顺序，不代表资料真实类别。
- **版本留存**：新分析用新的 `request_id`，并可用 `supersedes` 指向前一版。旧版本内容不变，只记录 `superseded_by`；`GET /v1/analyses/{id}/lineage` 返回祖先与后继链。已经用于续约讨论的版本不会被覆盖。

### 选项

`options` 支持 `min_anchors`（默认 5，每层最少锚点数）、`expert_slots`（默认 12，专家名额）、`consensus_threshold`（默认 0.8，后验阈值）、`lower_confidence_floor`（默认 0.65，区间下界要求）。未知选项会被拒绝。

### 输出状态

`single_category`（只有唯一合法类别，结论由定义决定）、`no_review`、`invalid_review_records`（复核类别非法，已隔离）、`abstained`、`unverified`（无可用锚定权重）、`low_confidence`（点估计高但区间不足，不给 recommendation）、`contested`（未达阈值或留一翻盘）、`supported_single_source`（仅一个供应商进入加权，置信度为 medium）、`supported`（多个有信息供应商加权支持）。
