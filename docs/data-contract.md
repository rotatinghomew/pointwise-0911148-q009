# 业务记录约定

items 的 stratum 是分派时已知资料层，categories 是合法类别集合。assignments 记录 supplier、batch、item_id 及派单概率，未派单不等于弃判。
reviews 以 item_id、supplier、round 标识一次复核，label=abstain 表示明确弃判；revision 更新同一次复核，不增加独立票数。expert_versions 只有部分资料有，expert_label 也是带时间版本的专业结论，不能覆盖整批作为真值。
现有 assignment_overlap 给出供应商交叠图、各层覆盖与弃判情况。合成记录包含不连通交叠组；供应商重复接到同一资料可能沿用上次判断。

## 共同版本语义
所有示例记录均为本地合成，不包含真实客户资料。received_at 不晚于观察截止的记录才可见；缺省该字段的配置为静态配置。revision 表示同一业务身份的订正次数，不是新样本。时间不带时区时按 UTC 解读。
分析请求会保存可见数据的内容摘要及完整内容；重复 request_id 与原请求完全相同时返回原结果，即使源文件后来改变。其他请求内容复用该编号返回冲突。新观察须使用新编号。


items.categories、reviews.label 与 expert_versions.expert_label 使用相同的 `c0`、`c1`、`c2` 类别标识；abstain 仅用于 reviews，不是类别。


## 快照样例字段补充

以上字段说明对应当前初始快照中的本地合成记录；示例不存在的历史字段不视为隐藏接口要求。
