# Mini 工作台：方案与实现约定

确认日期：2026-09-15。验证事实见 [current-state.md](current-state.md)，操作步骤见 [runbook.md](runbook.md)。

2026-09-15 增补：按 [16 组修复清单](workbench-fixes-2026-09-15.md) 实施 1.1 生命周期与恢复加固，保持原架构、浅色方向及环境复用。

2026-10-10 增补：按 [Pro 接入说明](swebench-pro-integration.md) 扩展现有四处题集选择。Impeccable 辅助评估后采用最小界面变化，不新增侧栏：长题号以摘要为首行、完整 ID 为副行；Pro 自动评测明确说明部分补丁规则，保留原授权复选框；题集限制放在展开说明中。原生前端、浅色风格与默认 8 条分页不变。桌面与 390px 已完成隔离浏览器检查。

## 背景与目标

2026-10-10 后续修复：按 [Pro 10 项修复说明](pro-workflow-fixes-2026-10-10.md) 完成单题缓存识别、异步预览/详情失效、失败部分补丁、有效预算及冻结时限提示。保持现有页面层级和浅色设计；错误保留明确恢复动作，不以通用题号提示覆盖缓存损坏原因。

现有 mini-swe-agent 后端已配置，但日常操作依赖 PowerShell、mini.cmd 和手动编辑配置。新增本机网页工作台，让 Tim 通过桌面快捷方式完成任务、配置和 SWE-bench 单题流程。

## 已确认决策

| 决策 | 采用方式 | 原因 |
| --- | --- | --- |
| 范围 | 编程、模型设置、历史查看、单题求解及评测 | 覆盖当前工作，不做批量平台 |
| 前端 | 原生 HTML/CSS/JavaScript | 无 Node 构建、Electron 或在线资源 |
| 服务 | FastAPI＋Uvicorn，复用项目 Python 环境 | 独立于终端，新增依赖少 |
| 视觉 | 固定浅色，参考 VS Code Light 2026 | 白色主区、浅灰侧栏、深灰文字、蓝色按钮 |
| 控制 | 默认每批命令确认，自动执行仅对本次显式开启 | 用户可见、可拒绝、可停止 |
| 并发 | 全工作台同时一项活动任务 | 避免冲突和重复费用 |
| 资料 | collaboration/ 管文档，local_setup/ 管代码，.local/ 管私有数据 | 保留上游结构及现有路径 |

第一版不含：深色模式、嵌入式 IDE、任意终端、多用户、远程公开访问、批量队列、云同步、自动清理镜像。

## 文件分层

```text
collaboration/
  README.md                    导航与资料规则
  docs/frontend-plan.md        方案、决策、接口、验收
  docs/current-state.md        环境与验证事实
  docs/runbook.md              操作与故障处理
  artifacts/YYYY-MM-DD/主题/   截图与脱敏验证结果
local_setup/webui/             服务、页面、启动器、测试、维护脚本
.local/webui/                 服务状态、配置备份、新任务、测试记录
.local/archive/install/       原始安装日志及归档校验清单
```

原有 .venv、配置、DPAPI 密钥、缓存、轨迹和评测不迁移。官方 src/、tests/、docs/ 及 README.md 保持上游职责。LOCAL_SETUP.md 留作导航。原始聊天和私密日志不进入 collaboration/。

## 页面与交互

- 左侧导航和最近任务，主区按类型显示表单；窄窗口收紧导航但保留功能。
- 编程任务可输入或浏览本机文件夹。目录选择不构成文件系统沙箱。
- 开始前显示模型、步数和费用估算上限；保存配置不调用模型。
- 确认展示命令及工作目录，可批准、拒绝并反馈或停止。
- 对话、日志、提交与评测分开显示。输出使用文本节点，不执行 HTML。
- 刷新恢复任务和待确认票据，关闭页面不停止任务。
- 提供退出／重启入口和资源清理状态；任务草稿保护、历史搜索分页、明确清理预览及复制参数入口。
- 设置只提供必要字段，已有密钥不回显；替换密钥用 Windows DPAPI 加密。
- SWE-bench 先加载题目，再显式求解或评测；优先缓存，下载需勾选。
- 既有轨迹／预测直接评测，避免重新生成。提交成功不等于测试通过。

## 实现与关键规则

启动器用已有 pythonw.exe 启动隐藏服务，通过锁和健康检查复用单实例；默认 8765，遇其他服务占用时选择 8765–8775 的空闲端口，不结束占用进程。

仅绑定 127.0.0.1。启动 URL fragment 交换 HttpOnly、SameSite=Strict Cookie 后被移除；API 校验会话，Host、Origin、Fetch Metadata 阻止跨站执行。私有 service.json 不可分享。

每次保存 request.json 配置快照，独立 worker 通过结构化 stdout 事件和 stdin 确认消息与服务交互。服务写 meta.json、events.jsonl，SSE 支持事件编号恢复。请求 ID 去重，确认票据使用后失效。不模拟终端输入。

Windows Job Object 只包含该任务及后代，停止时关闭 Job。Docker 容器采用随机任务编号并记录所属名称，禁止全局清理。重启后未结束任务标为中断，不自动恢复付费调用；停止不撤销文件修改。

网页代理继承 DefaultAgent。本地任务复用 Windows 环境，SWE-bench 使用官方题目 Docker 镜像及独立配置。求解只传题目和必要环境，标准补丁／评测脚本不进入模型上下文。评测检查 SWE-bench 5.0.2 与 Windows LF 修复，每次使用新运行 ID。

设置保存保留未知字段、注释、换行与 BOM；先备份，用指纹拒绝过期覆盖，再通过事务记录和文件级原子写入处理故障恢复。新设置只影响后续任务。私有执行输入保持原文，展示脱敏单独处理，疑似凭据的导出明确阻止。备份和完整记录留在 Git 忽略目录。

## 接口索引

除 health、session 和静态页外均要求本机会话。校验错误不回传请求中的密钥。

| 接口 | 用途 |
| --- | --- |
| GET /health；POST /session | 启动检测、建立会话 |
| GET/PUT /api/config | 读取；保存 settings＋version |
| PUT /api/key | key＋version，加密保存，不返回明文 |
| GET /api/environment | 解释器、项目目录、Git Bash、活动任务 |
| GET /api/directories?path=… | 浏览本机子目录，不接管文件管理器 |
| POST /api/bench/preview | 兼容只读缓存检查；下载改用 prepare 类型任务 |
| GET /api/bench/cached | 按 dataset＋instance_id 核对已缓存题目，刷新恢复，不下载、不创建任务 |
| GET /api/library；GET /api/library/{dataset}/{instance}；POST /api/library/sync | 实例目录筛选、详情、可取消同步 |
| POST /api/queues/preview；GET/POST /api/queues；GET /api/queues/{id}；POST /api/queues/{id}/control | 顺序批次预览、确认、恢复与控制；完整约定见 instance-library-plan.md |
| GET /api/patches；POST /api/patches/inspect | 按题号、补丁内容及来源模型选取；手动路径只读检查 |
| GET/POST /api/runs | 查询／创建；创建含 kind、request_id、settings_version |
| GET /api/runs/{id} | 状态、待确认票据、统计及结果 |
| GET /api/runs/{id}/events | SSE，支持 after 和 Last-Event-ID |
| GET /api/runs/{id}/timeline | before 为事件编号；每页最多 50 条，返回最后事件编号供 SSE 无重复接续 |
| GET /api/runs/{id}/conversation-source | 匹配历史生成对话；before 为消息索引，每页最多 50 条，长消息节选 |
| POST /api/runs/{id}/approve | ticket、approve、feedback |
| POST /api/runs/{id}/cancel | 停止所属任务 |
| GET /api/runs/{id}/artifact/{name} | 白名单轨迹／预测下载 |
| GET /api/legacy；POST /api/legacy/read | 原位置历史索引与读取，禁止任意文件下载 |
| GET /api/history | search、offset、limit、archived；分页索引 |
| GET /api/requests/{request_id} | 丢失响应后按原请求编号核对，删除记录保留去重标记 |
| POST /api/lifecycle；/ack；/cancel | 退出／重启、确认关闭、取消待执行退出；支持明确保留待清理清单 |
| POST /api/lifecycle/repair-link | 从已认证页面恢复损坏的启动定位文件 |
| GET /api/config/backups；POST /api/config/restore | 查看备份清单、明确恢复配置 |
| POST /api/runs/{id}/cleanup；/archive | 重试所属资源清理、切换归档状态 |
| GET /api/runs/{id}/reuse | 原样复制允许的参数，一次性授权清零，不恢复旧代理上下文 |
| GET /api/storage；POST /api/storage/preview；DELETE /api/storage | 占用、精确路径预览、确认后按令牌清理 |

kind：local、offline、prepare、cache、download、catalog_sync、swebench、evaluate。状态：preparing、running、awaiting_confirmation、evaluating、stopping、cleaning、cleanup_required、completed、failed、cancelled、interrupted。completed 表示任务及正常清理结束，评测是否通过另看 evaluation.verdict；cleanup_required 明确保留未确认清理资源。

1.2.0：evaluation.category 区分 passed、test_failed、infrastructure、ambiguous、incomplete；原始计数和 failure_reasons 保留。evaluation_progress 提供阶段、最后阶段日志时间和单题测试超时。设置草稿保留编辑基线，冲突字段明确选值后再保存。changes 基于安全前后文本快照提供新增／修改／删除预览，限制与未覆盖原因见 runbook。

## 验收与接续

### 1.4.0 多模型与评测扩展

完整方案见 [model-evaluation-plan.md](model-evaluation-plan.md)。此版本新增服务连接/模型档案，原 `/api/config`、`/api/key` 仍只操作“原配置”，网页默认档案独立保存。

| 接口 | 新行为 |
| --- | --- |
| GET/POST /api/profiles；PUT /api/profiles/{id}；PUT /api/profiles/default | 模型档案、网页默认；列表 execution_fingerprint 用于确认本次所见参数 |
| POST /api/connections；PUT /api/connections/{id}；PUT /api/connections/{id}/key；GET /api/connections/{id}/models | 连接、独立密钥、主动获取目录 |
| POST /api/profiles/{id}/test/preview；POST /api/profiles/{id}/test | 确认冻结快照后最多两次无害工具往返；token 与 request_id 幂等，退出期间不得中断 |
| POST /api/runs | 可选 profile_id、profile_fingerprint、auto_evaluate；旧调用省略保持兼容。自动评测使用唯一队列并返回 workflow_id |
| GET /api/runs/{id} | 同时提供 workflow、result_summary；子阶段仍使用原历史和事件接口 |
| GET/POST /api/experiments；POST /api/experiments/preview；GET /api/experiments/{id}；POST /api/experiments/{id}/control | 固定题集、多模型首轮实验，复用队列；限五模型、五十求解单元 |
| GET /api/results；/compare；/export；/{attempt_id} | 只读聚合、共同题集/首轮对比、按dataset/group_ids或experiment_id导出；实验筛选优先 |

新网页求解默认自动评测，旧任务不自动获得新授权。资料、镜像和自动执行命令分别确认。模型配置变更暂停；仅换密钥可确认新凭据版本继续，改变执行条件另建实验。来源不明的旧记录不虚构求解关联；重复评测不增加分母，重试不替换实验首轮。

以下为原有通用验收标准，实际本轮证据以 current-state.md 为准。

1. 桌面入口无需终端，重复打开不重复服务，mini.cmd 仍可用。
2. 真实离线代理与 Bash 验证中文、多行、拒绝、提交、失败、超时。
3. 重复请求拦截，刷新不重复执行，停止不影响其他任务进程。
4. 配置保留未知项与注释，冲突拒绝覆盖；DPAPI 测试不覆盖真实密钥。
5. 预测校验与正式报告解析区分解决、未解决和基础设施错误。
6. 桌面及窄窗口无横向溢出，无页面脚本错误。
7. 原依赖版本、受保护文件不变；记录新增依赖和体积。
8. 未获准不做付费模型联调，Docker 不可用时不声称容器评测通过。

后续从 current-state.md 的未验证项继续。行为变更同步本方案与操作说明，新决策记录日期及原因，避免保留多份相互冲突的最终版。

## 1.5.0 SWE-rebench 经典榜单

2026-10-08 新增固定题集 `rebench-34d5a588`，官方 `test` 切分 860 题。统一 `GET /api/benchmarks` 驱动新任务、实例库、结果筛选和实验选择器；原 Verified/Lite 接口和路径继续兼容。

`RunRequest`、目录与队列接口接受已注册题集；新任务和预测带 `benchmark_snapshot`、`case_key`。`GET /api/patches` 与 `POST /api/patches/inspect` 增加可选 `dataset`，旧请求保持兼容，新基准要求明确来源。不同基准／固定版本的综合查询保留计数，解决率不混算，模型对比要求先选定题集。

数据加载固定提交与文件校验和；评测通过附带的官方 fork 在独立子进程运行，不覆盖现有 SWE-bench 包。下载授权、队列预算、暂停、清理和不自动重跑继续生效。详细范围、验收与未验证项见 [SWE-rebench 接入](swe-rebench-integration.md)。

## 模型验证恢复补丁

新增只读鉴权接口 `GET /api/profiles/{profile_id}/test/{request_id}`，供刷新及连接中断时恢复状态。模型列表读取原配置与自建模型的持久化验证记录；预览返回实际模型 ID、协议、请求地址及参数。测试授权与错误提示在面板内显示，重新验证必须重新预览和确认。实现规则及验收见 model-probe-recovery.md。

## 1.5.1 整体复审修复

`GET /api/storage` 保留原字段并增加 scanning/scanned_at/stale/error，首次返回统计状态，后台单次扫描完成后缓存 60 秒。`refresh=true` 手动更新；前端仅在统计中轮询，保留勾选和精确清理预览；删除仍实时校验并使缓存失效。

结果条目与导出增加 evaluated_at/evaluation_time_source，历史按最近有效评测完成时间聚合，实验首轮口径不变。conditions.pricing 仅提供价格标量白名单供条件差异摘要。对话接口优先明确求解/尝试及输入来源，歧义不猜测。

前端保留推理参数、隔离题集旧响应与队列控制迟到响应；长表单提供就地错误和字段定位；基准名称、阶段重试、尝试结果和折叠条件说明统一。启动恢复与超长日志处理同时加固。具体边界和验证见 [修复与验收](workbench-fixes-2026-10-09.md)。
