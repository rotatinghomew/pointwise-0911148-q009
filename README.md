# 复核供应商一致性台

本项目用于复核供应商一致性台的业务资料核对与分析留存。目前提供 `assignment_overlap` 覆盖核对，业务定义见 `docs/data-contract.md`。示例数据在本地生成，分析不访问远程业务系统。

## 运行环境
Python 3.11.14；数值库固定在 `requirements.lock`。依赖安装属于环境准备，不属于服务启动；已经准备过的环境不再联网下载。

```sh
python3 -m pip install -r requirements.lock
python3 -m unittest discover -s tests -v
python3 -m app demo
python3 -m app serve
```

服务默认监听本机 8080，`GET /health` 检查存活，`GET /v1/catalog` 查看当前分析类型。`POST /v1/analyses` 接收 `request_id`、`operation`、`as_of` 和可选 `options` 对象，返回保存后的分析；`GET /v1/analyses/{request_id}` 打开历史记录。数据文件由 `--source` 指定，SQLite 由 `--database` 指定，参数放在子命令前。`POST /v1/actions` 调用领域操作，已支持的动作见领域模块；没有配置动作的项目会明确拒绝。

## 目录职责
`app/domain.py` 保存当前业务核对与动作，`app/contracts.py` 处理可见版本，`app/repository.py` 维护输入与分析记录，`app/service.py` 协调操作和留存，`app/httpd.py` 提供本地接口。`tests` 覆盖业务口径、重复请求、并发回放及真实 HTTP 往返。

## 快速查看
`python3 -m app demo` 从本地数据执行当前覆盖核对并完整打印结果，一般数秒内完成。清理 `var` 可从干净状态重新运行，历史正式记录应另外留存。该命令展示的是当前已具备的核对能力，不代表已完成其他分析。
