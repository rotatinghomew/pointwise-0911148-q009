# 复核供应商一致性台

本项目用于复核供应商一致性台的业务资料核对与分析留存。目前提供 `assignment_overlap` 覆盖核对和 `category_consensus` 类别共识与供应商可靠度分析，业务定义见 `docs/data-contract.md`。示例数据在本地生成，分析不访问远程业务系统。

续约讨论请使用 `category_consensus`：可靠度只以专家结论为锚，按资料层和派单情境分层；没有锚点或没有交叠的分组不给供应商排名；每条建议都带来源与不确定性。共识概率是相对支持度，不是无条件准确率。

## 运行环境
Python 3.11；数值库固定在 `requirements.txt`。Dockerfile 通过不可变镜像摘要锁定已验证的 Python 3.11.14，代码兼容控制台 Python 3.11.2。依赖安装属于环境准备，不属于服务启动；已经准备过的环境不再联网下载。

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 -m app demo
python3 -m app consensus --summary --request-id consensus-2026-11
python3 -m app serve
```

服务默认监听本机 8080，`GET /health` 检查存活，`GET /v1/catalog` 查看当前分析类型。`POST /v1/analyses` 接收 `request_id`、`operation`、`as_of`、可选 `options` 对象和可选 `supersedes`，返回保存后的分析；`GET /v1/analyses/{request_id}` 打开历史记录，`GET /v1/analyses/{request_id}/lineage` 查看版本链。数据文件由 `--source` 指定，SQLite 由 `--database` 指定，参数放在子命令前。`POST /v1/actions` 调用领域操作，已支持的动作见领域模块；没有配置动作的项目会明确拒绝。

## 续约分析用法

先执行一次并留存，例如 `--request-id renewal-v1`。后续补了复核或专家结论时，用新的编号和 `supersedes` 重算：`--request-id renewal-v2 --supersedes renewal-v1`。旧版本内容不会改变，只标记 `superseded_by`，因此已经用于续约讨论的版本仍可原样回看。

`consensus --summary` 按专家补复核优先级输出关注资料，并给出供应商可靠度、分组可比较性、专家名额队列、类别映射核对项和校准诊断；完整逐资料明细保存在同一次分析记录中，每个条目的 `sources` 可追到具体派单、复核和专家版本。

样例数据中，`s1` 的标签与专家结论呈稳定的整体类别映射错位（映射后一致率 98%，原样一致率 2%），因此被标为 `mapping_review_required`，不进入加权或排名，需先核对类别口径；`s2` 所在的 30 条资料分组没有任何专家锚点，也不参与供应商排名。这两类问题分别属于“口径待核对”和“证据不足”，都不能直接读成供应商质量高低。

## 目录职责
`app/domain.py` 保存当前业务核对与动作，`app/consensus.py` 实现类别共识与供应商可靠度分析，`app/contracts.py` 处理可见版本，`app/repository.py` 维护输入、分析记录与版本链，`app/service.py` 协调操作和留存，`app/httpd.py` 提供本地接口。`tests` 覆盖业务口径、共识边界、版本链、重复请求、并发回放及真实 HTTP 往返。

## 快速查看
`python3 -m app demo` 从本地数据执行覆盖核对并打印类别共识摘要，一般数秒内完成。清理 `var` 可从干净状态重新运行，历史正式记录应另外留存。该命令展示的是当前已具备的核对能力，不代表已完成其他分析。

## 容器与离线复现
首次在运行前执行 `docker build -t local-analysis .` 准备锁定依赖。
之后 `docker run --rm --network none local-analysis python3 -m unittest discover -s tests -v` 检查领域契约和真实 HTTP 工作流，`docker run --rm --network none local-analysis` 从全新容器输出当前业务核对。
容器不访问业务网络，不需要外部数据库或第二个服务；`requirements.lock` 与正式依赖入口 `requirements.txt` 保留相同精确版本。
当前测试和演示的执行上限各为80秒；新增分析应提供90秒以内可实际展示完整结果的入口，依赖准备在演示前完成。
