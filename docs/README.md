# ZincNya Bot 文档索引

本目录包含 Bot 各子系统的设计文档和使用指南。

## LLM 系统

### Memory（长期记忆）
- **[llm-memory.md](llm-memory.md)** — Memory 系统主文档：架构、核心概念、使用指南、FAQ
- **[llm-memory-hybrid.md](llm-memory-hybrid.md)** — Hybrid 混合检索详细文档：算法原理、实验数据、上线指南、故障排查
- **[../SMOKE_TEST.md](../SMOKE_TEST.md)** — Memory 冒烟测试：自动化、部署、灰度与回滚检查

### 其他 LLM 模块
- **[llm-handler.md](llm-handler.md)** — LLM Handler 架构：从收到消息到发出回复的完整流水线
- **[llm-context-assembly.md](llm-context-assembly.md)** — Prompt 组装：Memory、Knowledge、History、URL 如何组装进 prompt
- **[llm-knowledge.md](llm-knowledge.md)** — Knowledge 知识库系统
- **[llm-knowledge-authoring.md](llm-knowledge-authoring.md)** — Knowledge 写作指南

### AFC（AI Function Calling）
- **[afc.md](afc.md)** — AFC 架构总览
- **[afc-error-handling.md](afc-error-handling.md)** — AFC 错误处理
- **[afc-tool-management.md](afc-tool-management.md)** — AFC 工具管理

## 用户界面

- **[chatScreen.md](chatScreen.md)** — ChatScreen（聊天屏）系统
- **[tui.md](tui.md)** — TUI（终端界面）架构

## 基础设施

- **[telegram-handlers-group.md](telegram-handlers-group.md)** — Telegram Handlers 组织结构
- **[bot-data-push-layer.md](bot-data-push-layer.md)** — Bot Data 推送层
- **[module-management.md](module-management.md)** — 模块管理
- **[constant-guidelines.md](constant-guidelines.md)** — 常量命名与管理规范

## 内部文档

特定批次的实验材料已归档；当前验收入口见 [Memory 冒烟测试](../SMOKE_TEST.md)。

---

## 文档维护

- 主要功能的设计文档放在 `docs/` 根目录
- 归档文档放在 `docs/archive/`（如有）
- 内部研究文档放在 `docs/internal/`（如有）
- 临时数据和实验结果放在 `tmp/`

**文档更新原则：** 代码变更后及时更新对应文档，保持文档与代码一致。
