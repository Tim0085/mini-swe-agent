# GitHub 自动检查修复与接续说明

## 背景与目标

首次上传提交 `54a5194` 触发了原项目的自动检查。文档检查遇到 Slack 邀请链接 403、旧博客地址 404；定时检查另有 vLLM 文档 429。Ubuntu / Python 3.11 的默认 pytest 扫描同时收集了 Windows 工作台和第三方评测源码，产生 53 项失败、1 项收集错误。

目标是让官方代码与本地工作台各自在适合的环境中验证，保留有效的失败检查，不影响本机正式工作台。

## 修改范围

- `.github/workflows/pytest.yaml`：官方测试明确运行 `tests`；新增 Windows 2025 / Python 3.12 的独立工作台任务，串行运行原有隔离测试。
- Windows 任务安装已有依赖清单与 `swebench==5.0.2`，明确定位 Git Bash，先创建 runner 自己的 `.local` 备份父目录，再应用已有的 Windows UTF-8/LF 修复脚本。测试环境启用 UTF-8 和 Hugging Face 离线模式。
- Codecov 仅在配置了令牌时运行，仓库名使用当前仓库，不向原项目上传本仓库的覆盖率。
- `.github/mlc_config.json`：跳过 SWE-bench 的 Slack 邀请链接，以及已核实可访问、但 GitHub runner 持续收到 429 的 vLLM 文档首页这一精确地址；其他 429 按工具规则重试两次，缺少有效 Retry-After 时等待 10 秒。没有把 403、404、429 全局当成成功。
- `docs/models/quickstart.md`：将旧博客地址改为已核实的官方新地址。
- `.gitattributes`：固定评测器源码保留原始字节，防止 Windows Git 换行转换破坏清单校验。
- `test_workbench.py`：评测边界测试使用隔离状态中的确定性数据，保留真实评测器版本和换行校验，并检查缺少评测字段时拒绝输入；不再依赖本机缓存的 Django 题目。

本轮没有修改工作台前后端运行代码、启动脚本、正式配置、密钥、历史记录或已安装的 Python 包。

## 关键规则

1. 两个 pytest 任务显式指定目录；第三方 `vendor` 源码仍由专门的来源与协议测试验证，不作为应用测试自动收集。
2. 工作台测试沿用独立状态和配置根，不继承本机正式状态，不从 GitHub Secrets 配置模型密钥，不启用真实付费求解。
3. 干净机器没有 `.local` 中的 Pro 题目及资源缓存。34 项依赖这些缓存的测试沿用原有跳过规则，不自动下载题目或资源；本机完整测试继续验证它们。
4. 固定源码的清单校验不放宽；用 Git 属性保留原始字节，不重写清单中的哈希。
5. 运行代码未变，不需要重启或刷新正式工作台。

## 实现与验证方法

- 修改前使用现有 `.venv` / Python 3.12.4 运行完整工作台测试，记录保护文件与 Python 包版本。
- 用不含 `.local` 的干净副本复现 GitHub 环境；在副本中启用 `core.autocrlf=true` 并执行 Git checkout，实际验证固定源码在 Windows 检出后的字节。
- 使用与失败任务相同版本的 `markdown-link-check` 检查 Markdown，用官方 `actionlint` 检查 Actions 语法。
- 工具及原始测试日志放在 `.local/webui/ci-fixes-20261011/`，脱敏汇总放在 `collaboration/artifacts/2026-10-11/github-ci/`。

## 验收结果

- 修改前完整工作台回归：213 项通过，保留 3 个已有弃用提示。
- 本轮修改后的完整工作台回归：213 项通过，保留 3 个已有弃用提示。
- 不含本地状态与资料缓存的干净副本：179 项通过、34 项按原规则跳过，保留同样的 3 个已有弃用提示。启用 Windows Git 换行转换后，42 个固定评测源码文件的字节校验全部通过。
- 共 90 份 Markdown（包含新增接续说明）的链接检查通过，工具退出码为 0。
- 修改后的三个相关 Actions 文件通过 actionlint 1.7.12 检查。
- 保护基线中的 591 个文件逐字节未变，118 个 Python 包及版本未变；运行源码指纹保持 `ea85e026be468168`。脱敏汇总见 `collaboration/artifacts/2026-10-11/github-ci/validation-summary.json`。

## GitHub 提交与云端核实

- 用户已明确授权提交和推送；保留用户在 Desktop 创建的 `f217914`，通过 SSH 成功推送到 `origin/main`。
- 本仓库 SSH 命令固定使用已安装的 Git SSH 和现有私钥的绝对路径。随后实际复现：默认 Windows SSH 报 `Permission denied (publickey)`，显式指定已有密钥则认证成功。Desktop 的已安装代码在启用系统 SSH 时会用环境变量覆盖仓库 SSH 命令，因此还为本机 SSH 配置追加仅匹配 `github.com` 的密钥选择项，保留原文件字节并在忽略目录备份原件。未更换、上传或打印密钥，未修改账号登录状态或打开的 Desktop 窗口。
- 默认 Windows SSH 认证成功；Desktop 自带 Git 使用通常路径和显式系统 SSH 路径的推送预检均通过。Desktop 原日志为空，无法从通用提示确定当时失败的唯一原因；Desktop 设置和界面未接管、未复测。
- 首轮 [Pytest](https://github.com/Tim0085/mini-swe-agent/actions/runs/38075815729)：Ubuntu 官方测试 595 项通过、24 项跳过、1 个已有提示；Windows 在测试前因 `.local` 父目录不存在退出。格式检查与文档构建通过。
- 首轮 [链接检查](https://github.com/Tim0085/mini-swe-agent/actions/runs/38075815720)：原 Slack 403 和旧博客 404 已消失；vLLM 首页重试后仍为 429，另行只读核实官方页面可访问。
- 追加 CI 修复只涉及精确的 vLLM 首页例外及 Windows CI 的目录创建。实际复制未修复的评测包，在全新临时目录执行原脚本，验证首次修复、原件备份和重复执行均正常；90 份 Markdown 链接及 Actions 语法复查通过。
- 修复提交 `1384f0b` 已通过全部四个云端工作流：[Pytest](https://github.com/Tim0085/mini-swe-agent/actions/runs/38076149806)、[链接检查](https://github.com/Tim0085/mini-swe-agent/actions/runs/38076149789)、[格式检查](https://github.com/Tim0085/mini-swe-agent/actions/runs/38076149780)、[文档构建](https://github.com/Tim0085/mini-swe-agent/actions/runs/38076149785)。Windows 工作台实际为 179 项通过、34 项按原资料缓存规则跳过、4 个依赖弃用提示；Ubuntu 官方测试为 606 项通过、13 项跳过、1 个已有提示。提示未作为失败隐藏。

## 验证边界

- 干净副本跳过的缓存资料测试必须与本机完整回归一起解读，不能将离线合成检查作为模型或整个基准的成绩。
- 外部网站以后仍可能出现新的暂时性限流；保留通用重试和其他坏链接检查。
