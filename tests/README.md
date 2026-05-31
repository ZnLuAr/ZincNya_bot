## 测试体系总览

ZincNya Bot 的单元测试目录，使用 pytest + pytest-asyncio。

---

### 快速开始

```bash
# 安装依赖
pip install -r requirements-dev.txt

# 运行所有测试
python scripts/test.py
# 或直接 pytest
pytest tests/

# 运行特定模块
python scripts/test.py -m llm
pytest tests/utils/llm/ -v

# 生成覆盖率报告
python scripts/test.py --cov
# 查看 htmlcov/index.html

# 跳过慢速测试
python scripts/test.py --fast
```

---

### 目录结构

```
tests/
├── conftest.py              # 全局 fixture（数据库、Telegram mock、时间冻结）
├── test_module_system.py    # 模块系统测试（注册表、配置管理、CLI）
├── handlers/                # Telegram handler 层测试
│   ├── test_reaction.py     # reaction handler 测试
│   └── test_shutdown.py     # shutdown handler 测试
├── utils/
│   ├── core/                # 基础设施（logger/errorHandler/database/resourceManager）
│   ├── llm/                 # LLM 模块（tokenizer/loader/urlReader/contextBuilder/config）
│   ├── todos/               # 待办事项（database/reminder/tgRender/utils）
│   ├── nyaQuoteManager/     # 语录管理
│   ├── whitelistManager/    # 白名单管理
│   ├── test_bookSearchAPI.py
│   ├── test_chatUI.py
│   ├── test_downloader.py
│   └── test_newsAPI.py
└── integration/             # 集成测试（预留，暂无测试）
```

---

### 测试统计

总计 **550+ 个测试**，整体行覆盖率 **40%**。

| 模块组 | 测试数 | 平均覆盖率 |
|--------|--------|-----------|
| Core 基础设施 | 102 | 94% |
| LLM 模块 | 90+ | 60-100% |
| 业务模块（todos/nya/whitelist/book） | 200+ | 80-100% |
| Handler 层（reaction/shutdown） | 50+ | 100% |
| 工具模块（chatUI/downloader/news） | 100+ | 85-99% |
| 模块系统（registry/manager） | 20+ | 85% |

各模块目录下若有 `*README.md`，记录该模块特有的测试策略与陷阱。无文档的模块按通用约定即可。

---

### 通用约定

#### Fixture（驼峰命名，定义在 `conftest.py`）

**数据库**
- `inMemoryDb` — in-memory SQLite 连接
- `tempDbPath` — 临时数据库文件路径
- `tempDataDir` — 临时 data/ 目录（已 monkeypatch 路径常量）
- `tempJsonFile` — 临时 JSON 文件路径

**Telegram Mock**
- `mockUser` / `mockChat` / `mockMessage` / `mockUpdate` / `mockContext`

**时间与网络**
- `frozenTime` — 冻结到 `2026-05-26 12:00:00`（freezegun）
- `mockAiohttp` — aiohttp 响应 mock（aioresponses）

#### 测试常量（从 `tests.conftest` 导入）

- `DB_BUSY_TIMEOUT = 5000` — SQLite busy_timeout（毫秒）
- `TEST_USER_ID = 123456789`
- `TEST_CHAT_ID = 987654321`
- `TEST_FROZEN_TIME = "2026-05-26 12:00:00"`

#### 命名规则

- 测试函数：`test_<function>_<scenario>()`
- Fixture：驼峰命名（与项目代码风格一致）
- 断言：使用 `assert`，不使用 `unittest.TestCase`

#### 隔离策略

| 资源 | 隔离手段 |
|------|----------|
| SQLite | in-memory DB |
| 文件系统 | `tmp_path` + monkeypatch |
| 网络请求 | aioresponses |
| LLM SDK | AsyncMock |
| Telegram | MagicMock |
| 时间 | freezegun |

#### Marker

- `@pytest.mark.slow` — 慢速测试（>1s）
- `@pytest.mark.integration` — 集成测试（自动应用于 `tests/integration/`，目前该目录为空）
- `@pytest.mark.network` — 需要网络访问
- `@pytest.mark.unit` — 单元测试（默认）

---

### 关键测试技术

#### 异步测试

```python
@pytest.mark.asyncio
async def test_something():
    result = await someAsyncFunction()
    assert result == expected
```

#### 参数化（多场景一次写完）

```python
@pytest.mark.parametrize("input,expected", [
    (None, "System"),
    ("user", "user"),
    ({"username": "u1"}, "u1"),
])
def test_extract_user_name(input, expected):
    assert extractUserName(input) == expected
```

#### Mock UI/CLI 分支

```python
# CLI 模式
with patch("utils.core.stateManager.getStateManager") as mockState:
    mockState.return_value.getConsoleOutputCallback.return_value = None
    await logger.log(...)

# UI 模式
mockCallback = MagicMock()
with patch("utils.core.stateManager.getStateManager") as mockState:
    mockState.return_value.getConsoleOutputCallback.return_value = mockCallback
    await logger.log(...)
```

#### 全局状态重置（autouse）

```python
@pytest.fixture(autouse=True)
def resetGlobalState():
    from utils.core import someModule
    someModule._instance = None
    yield
    someModule._instance = None
```

---

### 覆盖率目标

- 第一版：40%（已达成）
- 短期目标：60%（补 handler 层）
- 长期目标：80%

UI 渲染、终端绘制、初始化分支可降低优先级。

---

### 测试性能优化

**默认禁用覆盖率收集**（提升 30-50% 速度）

本地开发时，pytest 默认不收集覆盖率以加快测试速度。如需覆盖率报告：

```bash
# 使用 test.py 脚本
python scripts/test.py --cov

# 或直接使用 pytest
pytest --cov=utils --cov=handlers --cov=config --cov=loader --cov=modulesRegistry --cov-report=term-missing --cov-report=html:htmlcov
```

覆盖率报告生成在 `htmlcov/index.html`。

---

### 已知局限

- `handlers/llm.py`、`handlers/todos.py`、`handlers/book.py` 等 handler 层暂未测试
- LLM client（anthropic/gemini/openaiCompat）只测试 router 与 guardrails，SDK 调用层 mock 较薄
- chatUI/terminalUI 的终端绘制逻辑覆盖较低（依赖真实终端）
- `integration/` 目录预留但暂无集成测试（单元测试已覆盖主要流程）