# Mini 工作台 · 协作入口

本目录保存我们协作产生的方案、使用说明和可分享产物；上游 docs/ 仍用于官方文档。

- [前端方案与接口约定](docs/frontend-plan.md)
- [当前环境与验证状态](docs/current-state.md)
- [操作与故障处理](docs/runbook.md)
- [使用完整性审计：16 组问题与修复顺序](docs/workbench-audit-2026-09-15.md)
- [1.1 全量修复与逐项验收](docs/workbench-fixes-2026-09-15.md)
- [对话展示与补丁选择改进](docs/conversation-patches-2026-09-15.md)
- [1.1.1 全面复审与优化清单](docs/review-1.1.1-2026-09-15.md)
- [1.2.0 复审问题修复与验收](docs/review-fixes-1.2.0.md)
- [实例库与顺序队列方案及接口](docs/instance-library-plan.md)
- [多模型配置与一体化评测中心](docs/model-evaluation-plan.md)
- [模型验证状态与失败恢复](docs/model-probe-recovery.md)
- [整体复盘与评测界面改进](docs/workbench-review-2026-09-20.md)
- [模型选择去重与布局整理](docs/model-picker-2026-09-21.md)
- [整体审查修复与单题验收](docs/comprehensive-fixes-2026-09-22.md)
- [SWE-rebench 经典榜单接入](docs/swe-rebench-integration.md)
- [SWE-bench Pro V2 接入、操作与验收](docs/swebench-pro-integration.md)
- [Pro 工作流 10 项修复与验收（1.6.1）](docs/pro-workflow-fixes-2026-10-10.md)
- [2026-10-09 整体复审与优化建议](docs/workbench-review-2026-10-09.md)
- [1.5.1 整体修复与验收](docs/workbench-fixes-2026-10-09.md)

## 资料管理

- 新方案和说明放入 docs/，更新现有主题，避免复制多份“最终版”。
- 截图、设计稿和脱敏检查结果放入 artifacts/YYYY-MM-DD/主题/。
- 密钥、原始日志、缓存及任务记录留在 .local/，不复制到本目录。
- 原有运行路径不迁移，只建立索引；不保存完整聊天记录。
- 变更后同步当前状态及操作说明，未执行的验证明确标注。

当前阶段：1.6.1 已完成 Pro 工作流全部 10 项修复，213 项全量工作台测试、桌面/390px 浏览器验收及独立 review 通过。获准后正式后台已重启，GPT-5.5 缓存 Pro 单题完整实测成功：修复 1/1、回归 9/9，9 次调用、估算 $0.214217（上限 $3）。除重启必需的服务身份更新外，432 份受保护旧文件未变；没有新镜像、环境或依赖。此前 Luna 成功与首次断连记录都保留，少量单题结果不代表全集成绩。详情见当前状态及 10 项修复说明。
