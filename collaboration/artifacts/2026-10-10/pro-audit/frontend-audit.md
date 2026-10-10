# SWE-bench Pro 前端专项审查

日期：2026-10-10。范围：选题、缓存、批次预览、求解结果、原地评测及跨题集比较。只审查，未修改产品源码。

使用 Impeccable scoped audit / Operate 标准；context 已运行一次。保留现有浅色风格。本报告不是完整双独立评委 critique，不宣称全站 WCAG 合规。

## 结论

主路径信息层次、长题号选取及 390px 布局可用，但发现 2 个需要优先修复的行为问题、3 个次要问题。机械 detector 对 index.html 返回空结果；该结果不能发现以下业务状态问题。

### P1 — 单题入口无法自动识别已缓存的 Pro 题

- 位置：`local_setup/webui/static/workbench.js:242`。
- 原因：仍使用旧式 `repo__repo-数字` 正则，Pro 的 `instance_...-vSHA` 被前端提前返回；SWE-rebench 的 `_interface` 后缀也同样受影响。
- 实证：复制正式缓存的一道 Pro 公开题及验证资源到隔离状态；直接调用 `/api/bench/cached` 返回 ready=true。界面填入同一完整 ID 并等待缓存检查后，没有发出缓存请求，loadedProblem=false，开始按钮禁用，提示仍让用户选择题目。
- 影响：每次从下拉选择、刷新草稿、复制历史到新任务后，已缓存题不能直接继续求解或评测，只能额外点击加载题目绕过；与承诺的缓存复用体验不符。
- 建议：根据当前基准验证完整题号，或将非空完整选择交给后端统一校验，保留关键词搜索期的友好提示。增加 Pro / rebench 完整 ID 的浏览器缓存识别检查。

### P1 — 原地评测异步响应会挂到后来打开的另一任务

- 位置：`local_setup/webui/static/evaluations.js:176`。
- 实证：在 A 的部分补丁结果点击评测（真实本机 API 读取），请求未完成时打开 B；Promise 完成后 selectedId=B，但 inlineEvaluationPlan.source_run_id=A、view_run_id=B，确认框可见，B 的已解决结果下方出现 A 的待评测补丁。
- 影响：用户可能在 B 页误以为正在评测 B。本次未点击最终提交，未产生评测或模型请求。长 ID 相近时更难分辨。
- 建议：请求开始时冻结页面 generation / selectedId；每次 await 后检查。切任务、关闭原地评测时使旧预览失效；不可在响应回来后才读取 view_run_id。
- 截图：`pro-inline-navigation-race-390.png`。屏幕上结果 ID 为 `...03095...`，下方评测确认 ID 为 `...00bec...`。

### P2 — 收起题目详情后，迟到的详情请求会重新展开

- 位置：`local_setup/webui/static/library.js:32`、`:75`。
- 实证：已有详情展开；开始读取另一个详情后立即点击收起。立即 hidden=true；真实 API 响应回来后 hidden=false，面板重开并滚动。
- 建议：收起时递增 libraryDetailEpoch；切页时也取消不再需要的详情更新。

### P2 — Pro 超时停止原因显示未翻译的内部名称

- 位置：`local_setup/webui/static/workbench.js:77`，实际发出原因的代码 `worker.py:125-126`、`:197-198`、`:208-209`。
- 实证：实际原因名是 TimeExceeded；前端映射仅有 TimeLimitExceeded，浏览器渲染“求解停止原因：TimeExceeded”。
- 建议：补齐实际原因映射，保留旧名称兼容。

### P2 — Pro 评测尚无阶段日志时误显示 1800 秒

- 位置：`local_setup/webui/static/workbench.js:164`。
- 实证：Pro 评测结果 fixture 无 evaluation_progress 时，页面显示“单题测试超时 1800 秒”。Pro 实际 verifier_timeout 由 worker.py:374 传入，官方缺省为 3000 秒（pro_harness.py:28）。
- 影响：初始等待/旧报告缺日志时，让用户以为 30 分钟已超时，实际官方验证允许 50 分钟。
- 建议：从冻结评测请求/基准能力取得时限；无法取得时显示未知，不硬编码旧题集时限。

## 已验证保持正常的行为

1. 四个题集选择器一致，默认 Verified；Pro 长 ID 用题目摘要为主、完整 ID 为辅。
2. 实例库默认每页 8 条；390px 无页面横向溢出；完整题号复制成功。
3. 方向键与 Enter 可从题目搜索选择完整 ID；切题集清除下载/自动执行授权。
4. 批次预览明确显示未授权缺失题目和镜像，最终按钮禁用；空选题错误在底部按钮旁可见且 role=alert。
5. Pro 的单题、批次、实验说明一致体现部分补丁与主动停止边界。
6. 部分补丁保留原 LimitsExceeded 原因，明确说明不等于模型成功提交；原地评测仍需单独确认。
7. 断网切换题集不允许选中旧题集残留行，可通过重新读取恢复。
8. 混合题集不显示合并解决率；Pro 比较限定到单一版本，窄窗口可读。
9. 无 JavaScript pageerror，未向外部发出浏览器请求，未创建模型任务/下载/评测任务。

## Impeccable 技术审查简评

|维度|分数|依据|
|---|---:|---|
|可访问性|3/4|检验到的搜索键盘、就地 alert、按钮焦点可用；未做全站对比度与读屏审查|
|性能|3/4|列表分页8条，未发现本次操作的持续抖动；未进行慢设备性能测量|
|主题一致性|4/4|保持当前浅色系统字体/蓝色操作层级，无新体系漂移；未要求深色模式|
|响应式|3/4|桌面与390px页面无溢出，长ID折行可读；未使用物理触摸设备|
|实现完整性|2/4|主路径正常，但两个缓存/异步身份问题影响连续操作|
|合计|15/20|优先 harden 状态与身份边界，再 clarify 文案|

Questions skipped: 本轮已有明确只读审查范围，问题和建议交由 root 汇总，不额外向用户发起设计方向选择。

## 证据与边界

浏览器脚本在 `.local/webui/pro-audit-20261010/frontend/audit_browser.cjs`。使用独立 headless Edge、独立 API 服务与合成结果 fixture；复制的真实公开缓存只验证读取，不含真实密钥。没有路由 mock、真实模型调用或镜像下载。JSON：`pro-browser-check.json`，截图与本报告同目录。隔离服务按生命周期接口关闭，正式后台未接管。

## 补充：root 发现的异常分类问题界面复核

根审查已在 results.py 发现有预测补丁时优先 classified=submitted。隔离 fixture 改为 status=failed、exit_status=InternalServerError、billing.unknown=true 后，只重启自有服务重读，做一次定点页面核对：结果列表仍显示“部分补丁待评测”，没有求解失败标志；费用正确为未知。进入任务详情则仍保留 error 红字与停止原因。因此问题范围是列表与汇总的分类呈现，不是原始错误记录丢失。证据 `partial-error-ui.json`。未发起模型、下载或评测。

隔离服务已通过自身 `/api/lifecycle` + ack 退出并确认停止；独立浏览器均关闭。正式后台未重启或接管。
