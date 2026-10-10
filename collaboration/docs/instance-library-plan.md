# 实例库与顺序队列：实施方案及接口约定

## 背景与目标

原工作台要求手工查找实例 ID，并分别操作题目、镜像、求解和评测。用户希望直接浏览题目库，选择是否下载，并支持多题顺序处理。

沿用 Verified、Lite、现有 Python 环境及浅色界面，不引入新框架、数据库、环境或依赖。默认每页 8 题，可选 8／16／25／50 题并保留浏览器偏好；每批最多 50 题，同时只有一个未完成批次。

## 已确认的流程

- 目录支持题号／仓库／关键词搜索，题目缓存、环境状态、补丁与最近评测筛选。详情显示描述、已有补丁和历史任务。
- 批次操作分为缓存题目、下载环境、求解、评测补丁。数据下载与镜像下载分别授权，默认关闭。求解可选“成功提交后自动评测”，默认关闭。
- 执行异常暂停；正常完成但测试未通过继续。暂停允许本题及其已授权自动评测结束；停止立即请求停止当前步骤并等待清理。
- 浏览器关闭不停止后台。后台退出或重启停止派发，恢复后必须手动继续；失败／中断题必须明确重试或跳过。
- 默认逐批确认求解命令。自动执行授权只对当前批次有效。模型或密钥变化后，重新确认剩余任务参数。

## 实现与数据流

1. `library.py` 从官方 Dataset Viewer 分页读取目录，不硬编码题目总数。只持久化白名单题目信息，不保存标准补丁和测试脚本到目录；全部校验后原子替换，取消或失败保留上一份完整目录。
2. 完整题目资料仅在明确授权后保存，优先用目录行位置读取并核对题号，不匹配则重新搜索。完整资料仍保留原缓存路径约定。
3. `queues.py` 为批次预览生成短期票据，确认后保存有序条目、稳定请求编号、阶段及子任务关联。复用原 Manager、worker、Job Object 和清理机制，共用一个执行槽。
4. 评测输入在批次确认时保存为快照，开始评测前再次核对 hash。自动评测只取本次求解生成的补丁。重试生成新请求编号和任务记录。
5. 队列保存调用估算下限和最终统计，按累计费用限制后续请求；保留每题步数和费用限制，未知计价阻止继续。队列模型请求关闭隐式网络重试，避免未知费用下连续请求。

## 接口

| 接口 | 约定 |
| --- | --- |
| GET /api/library | dataset、search、cached、environment_state、patch、result、offset、limit；limit 默认 8，可选 8／16／25／50 |
| GET /api/instance-options | dataset、search；读取本机公开目录，最多返回 8 项 instance_id／repo／title，以及 total、synced；供单题可搜索下拉使用，不触发 Docker 或网络请求 |
| GET /api/library/{dataset}/{instance} | 公开描述、缓存／环境状态、已有补丁、最近任务 |
| POST /api/library/sync | dataset、request_id、allow_network；创建可取消的目录同步任务 |
| POST /api/queues/preview | BatchRequest：有序题号、操作、补丁选择、独立下载授权、自动评测／执行选项、总预算和配置版本 |
| POST /api/queues | 预览 token＋request_id；相同编号返回原批次，不重复创建 |
| GET /api/queues | 批次摘要与稳定请求编号，用于回看及丢失响应后的核对 |
| GET /api/queues/{id} | 条目、尝试记录、当前步骤、预算及配置变化 |
| POST /api/queues/{id}/control | pause、stop、resume、retry、skip、cancel_item、finish；配置变化时 resume 需 accepted_settings_version |

RunRequest 新增 cache、download、catalog_sync 类型及独立的数据／镜像授权字段，原 allow_download 和原单题接口继续兼容。退出后台同时暂停队列派发，不把队列后续题目当作已授权重启后自动运行。

## 文件与保护边界

- `.local/webui/catalog/`：公开目录索引。
- `.local/webui/queues/<id>/`：私有批次状态和所选补丁快照，批次补丁快照合计限 20 MB。
- `.local/webui/runs/`：继续保存独立子任务及产物；未结束队列引用的任务不进入可删除列表。
- 原配置、凭据、缓存、轨迹、预测及旧评测不迁移。工作台磁盘可用空间不足 10 MB 时暂停；该检查不代表 Docker 所在磁盘空间保证。
- 镜像由 Docker 管理，共享层意味着标称体积不等于新增占用。Docker 无法访问时状态为“无法检查”。本版不自动清理镜像或批次历史。

## 验收与限制

使用独立实例、离线确定性模型、真实进程和浏览器检查顺序、授权、预算、暂停、重启、重试及防重复。网络验收仅同步目录并下载一题完整资料；真实评测复用现有补丁和缓存镜像，不调用付费模型、不下载新镜像。

对应结果与当前生效版本见 `current-state.md` 和 `collaboration/artifacts/2026-09-16/instance-library/`。未知或中断请求可能产生服务商费用，不能把本机估算当成账单；无法确认费用的批次禁止进一步求解，需核对账单并结束批次后重新安排。

数据接口依据：[Hugging Face Dataset Viewer rows 文档](https://huggingface.co/docs/dataset-viewer/rows)。测试专用 diagnostics 只允许隔离状态与配置根、MINI_WEB_TESTING=1，必须覆盖整批题目，禁止混入真实模型请求；生产接口拒绝这一模式。
