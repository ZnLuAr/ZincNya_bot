# AFC 工具管理脚本

> 落地时间：2026-07-17
>
> 这份文档记载的是 `scripts/afc_tools.py` —— AFC 工具的装卸与开关管理脚本——的设计与用法：九个命令各负责什么、检查为什么全程不 import 工具代码、安装时脚本都做了什么，以及怎么把自己写的工具发到 GitHub 给别人装。
>
> Written by ZincNya~ ❤

---

## 概述

AFC 模块从被设计出来的那天起，就被定位成一个「可扩展框架」——工具是可以一个个自由引入、也可以整个拎走的小单元，框架核心不认识任何一个具体工具（见 [docs/afc.md](afc.md) 的设计决策）。但「可以自由装卸」这句话，光在文档里说说是不算数的：如果自己加一个新工具尚要想东想西、手工建六七个文件又记住一堆约定的话，想装别人写的工具，就更是难于上青天了……

于是就有了这套脚本——`scripts/afc_tools.py`，AFC 工具的专属管家。它负责的事情包括：

- **总览**：有哪些工具、谁启用谁禁用、触发词长什么样（`list` / `show`）
- **细查**：工具结构完不完整、触发正则编不编得过（`check`）
- **开关**：临时关掉某个工具而不删文件（`enable` / `disable`）
- **新建**：脚手架一键搭好新工具的骨架（`new`）
- **装卸**：从 GitHub 装第三方工具、把不想要的请出去（`install` / `uninstall`）
- **更新**：把第三方工具升级到新版本（`update`）

一共九个子命令：

```bash
python scripts/afc_tools.py list [-a|--all] [-e|--enabled] [-d|--disabled] [--full]
python scripts/afc_tools.py show <tool>
python scripts/afc_tools.py check
python scripts/afc_tools.py enable <tool>
python scripts/afc_tools.py disable <tool>
python scripts/afc_tools.py new <tool>
python scripts/afc_tools.py install <github_url> [--branch <branch>] [-f|--force]
python scripts/afc_tools.py update <tool> [--check] [-y|--yes]
python scripts/afc_tools.py update --all (--check | --yes)
python scripts/afc_tools.py uninstall <tool> [-f|--force]
```

应该可以看见，这个工具的定位和 `scripts/module.py` 有重合的地方。那么为什么工具还要单开一套呢？

这其实是粒度不同—— `module.py` 管的是「模块」这种大块头（LLM、todos、stickers 这个级别），而 AFC 工具是 afc 模块内部的小单元，weather、calc 这种。要是每加一个工具都去惊动模块注册表，粒度就太粗了；给工具单开一套轻量的管理，装拆都在 afc 模块内部完成，就刚刚好的说。

## 目录

- [工具的目录结构约定](#工具的目录结构约定)
- [三个状态文件](#三个状态文件)
- [工具的三个来源](#工具的三个来源)
- [afcTool.json 清单规范](#afctooljson-清单规范)
- [九个命令](#九个命令)
- [安全边界](#安全边界)
- [把工具发到 GitHub 给别人装](#把工具发到-github-给别人装)
- [日志](#日志)
- [已知局限](#已知局限)
- [参考](#参考)

---

## 工具的目录结构约定

先讲清楚「一个工具长什么样」，后面的命令才有意义。

一个标准的工具目录差不多是这样：

```
utils/afc/tools/mytool/
├── __init__.py      # 薄壳：async 入口函数定义在这里，转调 core.py
├── core.py          # 核心实现（复杂的逻辑都往这里塞）
├── triggers.py      # KEYWORDS / PATTERNS 触发配置
├── config.py        # 工具自己的配置（环境变量从这里读）
├── README.md        # 工具自述
└── afcTool.json     # 工具清单（manifest，详见后文）
```

测试文件则放在另一边，扁平命名：

```
tests/utils/afc/tools/test_mytool.py
```

### 为什么入口必须是「薄壳」而不是纯 re-export

这是这套约定里最容易踩到的一条，有必要展开说说。

工具的 async 入口函数应该**定义**在 `__init__.py` 顶层，函数体里转调 `core.py` 的实现：

```python
# utils/afc/tools/mytool/__init__.py
from . import core


async def mytool() -> str:
    """
    函数简介（会进入 schema 给 LLM 看）。

    返回：
        结果字符串
    """
    return await core.mytool()


__all__ = ["mytool"]
```

而不是图省事，只写一句 `from .core import mytool` 纯转发就了事。

这是因为 `check` 命令全程只做 AST 静态分析（原因见「安全边界」一节），它要在 `__init__.py` 的语法树里**亲眼看到**一个 `async def`。相比之下，纯 re-export 在 AST 里只是一个 `ImportFrom` 节点而非函数定义——运行时，registry 确实能顺着 import 找到函数，但 check 是看不到的，这个时候就会报「未在顶层定义任何 async 入口函数」。那么，与其让检查器去猜 import 链，不如统一约定：入口显式写在 `__init__.py` 里，对谁都可读。

> calc 以前就是纯 re-export 的写法（`from .calculator import calculate`），这次已经改成薄壳了。
> 
> 现在三个内置工具的入口都**定义**在各自 `__init__.py` 顶层，check 都能在语法树里亲眼看到 `async def`：calc / datetime 的函数体只做一行转调（分别转给 `calculator.py` / `core.py`）；weather 的壳稍厚一点——编排逻辑（参数校验、缓存、API 调用、错误分支）直接写在 `__init__.py` 里，缓存/HTTP/解析/格式化拆在 `cache.py` / `client.py` / `dateParser.py` / `formatter.py`。
> 
> 这样，三者的形态不完全一样，但「入口显式定义在 `__init__.py`」这条 check 真正在意的约定是一致的。

### 为什么不能随手在 __init__.py 写裸帮助函数

必须注意的是，不应在 `__init__.py` 顶层定义不带 `_` 前缀的普通帮助函数。

原因是 registry 扫描工具时，用的是 `inspect.getmembers(toolModule, inspect.isfunction)`，只按「函数定义在不在本工具包内」过滤——**定义**在 `__init__.py` 里的帮助函数，模块名恰好以工具包名开头，会顺利通过过滤，被误登记成工具 schema 递给 LLM。到时候 LLM 看到一个名字莫名其妙的「工具」，场面会很尴尬……

所以帮助函数只有两个去处：要么放进 `core.py`（registry 只扫 `__init__` 模块的成员，够不着它），要么加 `_` 前缀（registry 明确跳过下划线开头的名字）。

---

## 三个状态文件

工具的运行时状态收在 `data/` 下的三个 JSON 里：

| 文件 | 作用 | 是否进入 git |
| --- | --- | --- |
| `data/afcTools.json` | 每个工具的 `enabled` 开关（`enable`/`disable` 的落点） | 不进入 |
| `data/afcToolsBuiltin.json` | 内置工具（weather/calc/datetime）的 manifest | **进入**（仓库提供） |
| `data/afcToolsCustom.json` | 第三方工具的 manifest（`install` 时生成） | 不进入 |

对于 `afcTools.json`，它的格式很简单：

```json
{
  "weather": {"enabled": false}
}
```

值得说道的是它的宽松语义：**找不到条目就当启用**。一个工具第一次出现（刚 new 出来、刚 install 完），配置文件里根本没有它的名字，这时默认就是启用状态，不用先手动配置才能用。文件整个不存在或损坏时也一样，降级为「全部默认启用」。可以说，这个文件只记「谁被关掉了」，不记「谁开着」——这样新增工具的默认路径就是零配置直接可用。

而为什么 `afcTools.json` 和 `afcToolsCustom.json` 需要 gitignore，builtin 那个却进仓库？道理和模块系统的 `modules.json` 一样：启用开关和装了哪些第三方工具，是体现了每个部署实例自己偏好的数据文件，跟仓库本身无关，塞进 git 只会制造无意义的合并冲突；而内置工具的 manifest 是仓库内容的一部分，得跟着代码走。

另外，是一点实现上的细节：这些 JSON 的写入都是原子写（先写 `.tmp` 再 `os.replace`），不会留写一半的损坏文件；`isAfcToolEnabled()` 带模块级缓存，扫描热路径里不会每个工具各读一次盘，所以可以放心地安装、更新，而不用太多考虑残留文件和性能的问题。

---

## 工具的三个来源

`list` 输出里每个工具后面都跟着一个 `[来源]` 标记，一共三种：

- **builtin**：内置工具。manifest 由仓库里的 `data/afcToolsBuiltin.json` 提供，随代码一起分发。像 calc、datetime 等都是这类。
- **custom**：第三方工具。通过 `install` 从 GitHub 装进来的，manifest 存在 `data/afcToolsCustom.json`，里面还记着来源（`github:<url>@<branch>`）和安装时间。
- **local-draft**：本地草稿。目录确实存在于 `utils/afc/tools/` 下，但既没登记进 builtin 也没登记进 custom——比如像是刚用 `new` 搭好骨架、还没决定归属，或者干脆手工建了个目录在捣鼓的这种情况。

local-draft，可以理解为是一种「合法但未完成」的状态：bot 照常会加载它（目录在就会被扫描），`check` 会对它报一条 Warning 提醒「未登记」，`uninstall -f` 会整个目录删掉。等它长大了，再给它登记一个正式身份就好。

---

## afcTool.json 清单规范

每个工具带一份 `afcTool.json` 清单，描述自身的概况。内置工具的清单汇总在 `data/afcToolsBuiltin.json`；第三方工具的清单放在它自己仓库的根目录，install 时先抓它再抓文件。

**必需字段**（只要缺一个，install 就拒装）：

- `id`：工具 id，必须匹配 `^[a-z0-9_]+$`（小写字母、数字、下划线）
- `name`：显示名（`list`/`show` 里给人看的那个）
- `version`：版本号
- `files`：文件列表，项目相对路径，且必须落在 `utils/afc/tools/<id>/` 之内

**可选字段**：

- `author`：作者
- `description`：描述
- `testFile`：测试文件路径，扁平的 `tests/utils/afc/tools/test_<id>.py`（注意不在 `<id>/` 子目录里）
- `dependencies`：pip 依赖列表。install 和 check 时会拿它跟 `requirements.txt` 做模糊比对，找不到的包报出来提醒

### 为什么缺了这四个就拒装

四个字段缺一不可的设定，其实有其安全考量——

- **`id` 是路径的锚**。工具目录 `utils/afc/tools/<id>/`、测试文件 `test_<id>.py`、各个 json 里的键名，全都是从它拼出来的；路径校验也以它为基准目录（`_assertSafeToolPath(toolId, ...)`）
  * 说起这点，`id` 中塞 `..` 或者斜杠也是不允许的。要是没有 `^[a-z0-9_]+$` 的限制，可能的恶意工具就真的如冯虚御风，路径逃逸不知其所止了……
- **`name` 是给人看的**。管理脚本有一条设计主线是「不加载工具代码也能展示工具」，`list` / `show` 的显示名全靠 manifest 里这一个字段。缺了它，清单里就只剩一个目录名在裸奔——那是 local-draft 的待遇，一个有正式户口的 AFC 工具不应该这么懒。
- **`version` 是给更新定调的**。`update` 拿它告诉你「v1.0.0 → v1.1.0」、识别降级警告；字节比对固然能兜底裁决，但人总要先有个一眼可读的版本印象。install 这天就强制它，是为了让每个装进来的工具从第一天起就能被 update 好好对待。
- **`files` 是装卸的清单**。install 照它下载、uninstall 照它删除、update 照它比对——没有它，脚本连「这个工具有哪些文件」都不知道，总不能把整个仓库拖回家。它同时还是路径校验的作用对象：每条路径都得落在 `utils/afc/tools/<id>/` 之内，这是防恶意 manifest 的核心闸门。

拿一份完整的例子（取自内置的 weather）：

```json
{
  "id": "weather",
  "name": "天气查询",
  "version": "1.0.0",
  "author": "ZincPhos",
  "description": "查询指定城市的天气信息（当天/明天/后天）",
  "files": [
    "utils/afc/tools/weather/__init__.py",
    "utils/afc/tools/weather/triggers.py",
    "utils/afc/tools/weather/config.py",
    "utils/afc/tools/weather/cache.py",
    "utils/afc/tools/weather/client.py",
    "utils/afc/tools/weather/dateParser.py",
    "utils/afc/tools/weather/formatter.py",
    "utils/afc/tools/weather/README.md"
  ],
  "testFile": "tests/utils/afc/tools/test_weather.py",
  "source": "builtin"
}
```

有个设计上的取舍：**工具的名称和描述以 manifest 为准，不写在代码常量里**。这样 `list`/`show` 展示信息时只需要读 JSON，根本不用加载工具代码。

---

## 九个命令

这是本文档的主菜。逐个过一遍——

### list —— 看看有哪些工具

```bash
python scripts/afc_tools.py list            # 全部工具（默认）
python scripts/afc_tools.py list -e         # 只看已启用
python scripts/afc_tools.py list -d         # 只看已禁用
python scripts/afc_tools.py list --full     # 附带触发词预览和文件数
```

实际输出长这样：

```
已启用工具（3 个）：
  [+] weather         天气查询 (v1.0.0) [builtin]
  [+] calc            计算器 (v1.0.0) [builtin]
  [+] datetime        日期时间 (v1.0.0) [builtin]

提示：使用 show <tool> 查看详细信息，list --full 查看触发词
```

每行依次是：状态标记（`[+]` 启用 / `[-]` 禁用）、工具 id、显示名、版本、来源。local-draft 工具没有 manifest，显示名退化为目录名，也没有版本号。

加 `--full` 会多展开三行明细（触发词只预览前 3 个，免得刷屏）：

```
已启用工具（3 个）：
  [+] weather         天气查询 (v1.0.0) [builtin]
      关键词：天气, 气温, 温度 ... (共 8 个)
      正则：.*天气.*怎么样, 会下雨吗, 明天.*冷 ... (共 4 个)
      文件数：8 个
  [+] calc            计算器 (v1.0.0) [builtin]
      关键词：算, 计算, 等于 ... (共 9 个)
      正则：.*算.*一下, .*等于多少, .*计算.*结果 ... (共 4 个)
      文件数：7 个
  [+] datetime        日期时间 (v1.0.0) [builtin]
      关键词：现在几点, 几点了, 当前时间 ... (共 20 个)
      正则： (共 0 个)
      文件数：5 个
```

注意这些触发词是用 AST 从 `triggers.py` 里**静态**扒出来的，不是 import 之后读的——这个区别为什么是底线，留到「安全边界」讲。

### show —— 看一个工具的底细

```bash
python scripts/afc_tools.py show weather
```

```
工具：天气查询 (weather)
版本：1.0.0
作者：ZincPhos
状态：已启用
来源：builtin

描述：
  查询指定城市的天气信息（当天/明天/后天）

文件：
  - utils/afc/tools/weather/__init__.py
  - utils/afc/tools/weather/triggers.py
  - utils/afc/tools/weather/config.py
  - utils/afc/tools/weather/cache.py
  - utils/afc/tools/weather/client.py
  - utils/afc/tools/weather/dateParser.py
  - utils/afc/tools/weather/formatter.py
  - utils/afc/tools/weather/README.md

测试：
  - tests/utils/afc/tools/test_weather.py

触发词（关键词）：
  - 天气, 气温, 温度, 下雨, 晴, 阴, 冷不冷, 热不热

触发词（正则）：
  - .*天气.*怎么样, 会下雨吗, 明天.*冷, 今天.*热
```

展示的信息全部来自 manifest + AST 静态提取。对 local-draft 工具，末尾会多一行提示：「此为 local-draft 工具（未登记到 builtin/custom manifest）」。

### check —— 体检，全程 AST 静态分析

```bash
python scripts/afc_tools.py check
```

这是在开发时最常用的一个命令——写完工具、装完工具、或者单纯心里不踏实的时候，跑一遍：

```
正在检查工具完整性（AST 静态分析，不 import 工具代码）...

[OK] weather
  ✓ triggers.py 可解析，KEYWORDS(8) + PATTERNS(4)

[OK] calc
  ✓ triggers.py 可解析，KEYWORDS(9) + PATTERNS(4)

[OK] datetime
  ✓ triggers.py 可解析，KEYWORDS(20) + PATTERNS(0)

[OK] 所有工具检查通过
```

检查项有分两档：

**硬性（Error）**，过了才算是个能跑的工具：

- `__init__.py` 能被 AST 解析（缺失或语法错误直接 Error）
- `__init__.py` 顶层至少定义一个 async 函数（薄壳约定的那条）
- `triggers.py` 能被 AST 解析
- `KEYWORDS` / `PATTERNS` 至少一个非空（都没有的话工具永远不会被触发）
- `PATTERNS` 里每条正则都能 `re.compile()` 通过

**建议（Warning）**，不阻断，但最好收拾一下：

- `README.md` / `config.py` 缺失
- 测试文件缺失（查 manifest 的 `testFile`，没登记则按惯例查 `tests/utils/afc/tools/test_<id>.py`）
- 正则含嵌套量词（ReDoS 启发式，详见安全边界）
- local-draft 未登记到 builtin/custom manifest
- custom 工具 manifest 里的 `dependencies` 在 `requirements.txt` 中找不到（仅 custom 工具参与这项检测）

来看一份带 Warning 的真实输出（临时造了个 draft 工具演示，正则里故意埋了个嵌套量词）：

```
[!] draftdemo
  ✓ triggers.py 可解析，KEYWORDS(1) + PATTERNS(1)
  ⚠ 未登记到 builtin/custom manifest（local-draft）
  ⚠ ReDoS 风险：正则 "(x+)+$" 含嵌套量词
  ⚠ README.md 缺失（建议项）
  ⚠ config.py 缺失（建议项）
  ⚠ 测试文件缺失（建议项）：tests/utils/afc/tools/test_draftdemo.py

发现 1 处 Warning
```

汇总时只要有 Error，就会往日志里记一条（Warning 不记，详见「日志」一节）。

### enable / disable —— 开关，不删文件

```bash
python scripts/afc_tools.py disable weather
python scripts/afc_tools.py enable weather
```

```
[OK] 工具 weather 已禁用

提示：重启 bot 后生效
```

开关落在 `data/afcTools.json`，只改状态不动文件。生效时机是**重启 bot 之后**——因为触发词索引和工具 schema 都是启动时扫描构建的，运行中不会重扫。想临时让某个工具闭嘴（比如它的外部 API 挂了），用 disable 就比 uninstall 温和得多。

### new —— 脚手架生成新工具

使用 `new` 子命令一口气把目录结构约定里的全套骨架都搭好：

```bash
python scripts/afc_tools.py new mytool
```

执行的结果大概是：

```
正在创建工具 mytool...

  [OK] utils/afc/tools/mytool/__init__.py
  [OK] utils/afc/tools/mytool/triggers.py
  [OK] utils/afc/tools/mytool/config.py
  [OK] utils/afc/tools/mytool/core.py
  [OK] utils/afc/tools/mytool/README.md
  [OK] utils/afc/tools/mytool/afcTool.json
  [OK] tests/utils/afc/tools/test_mytool.py

[OK] 工具 mytool 创建成功

下一步：
  1. 编辑 triggers.py 补充触发词和正则
  2. 编辑 config.py 补充工具配置（如需环境变量）
  3. 实现 core.py 的 mytool（及拆分的内部逻辑）
  4. 补充 tests/utils/afc/tools/test_mytool.py 的真实断言
  5. 编辑 afcTool.json，补充 name/description/author，更新 files（如有新增文件）
  6. 选择工具归属：
     - 作为内置工具：把 manifest 加入 data/afcToolsBuiltin.json
     - 作为第三方工具：发布到 GitHub，供 install 流程管理
  7. 运行 python scripts/afc_tools.py check 验证
```

其中，`__init__.py` 中已经写好用于转调的薄壳，`core.py` 里留着 `NotImplementedError` 待填，测试文件也是能直接跑的空壳。工具名只允许小写字母、数字、下划线；若目录已经存在，则会拒绝生成工具的请求。

注意，刚 new 出来的工具还是 local-draft ——而上面的第 6 步就是在给它上户口、决定它的归属。

### install —— 从 GitHub 安装第三方工具

```bash
python scripts/afc_tools.py install https://github.com/<user>/<repo>
python scripts/afc_tools.py install <url> --branch dev
python scripts/afc_tools.py install <url> -f      # 覆盖已存在的同名工具
```

安装成功时会有输出：

```
正在从 https://github.com/<user>/<repo> (分支: main) 下载 6 个文件...
  [OK] utils/afc/tools/mytool/__init__.py
  [OK] utils/afc/tools/mytool/triggers.py
  ...
安装文件...
  [OK] utils/afc/tools/mytool/__init__.py
  ...

[OK] 工具 mytool 安装成功

提示：重启 bot 后生效
```

整条流程是：解析 URL → 按分支抓 `afcTool.json`（`--branch` 指定 > `main` > `master`）→ 校验 manifest 必需字段与 id 格式 → **先校验所有文件路径再下载** → 检查依赖 → 下载到与目标同盘的临时目录 → AST 校验 `triggers.py` → 原子落盘 → 最后才登记 `data/afcToolsCustom.json`。

任何一步失败都会触发回滚清理，不留半成品。

应该记住，

- **内置工具禁止覆盖**：`id` 撞上 `afcToolsBuiltin.json` 里的条目直接中止。想魔改内置工具应该走别的路子，install 是不会帮你顶包的……
- **目标已存在要加 `-f`**：不加会提示「确认要覆盖请加 -f/--force」。
- **依赖缺失会问一句**：manifest 的 `dependencies` 里有 `requirements.txt` 找不到的包时，会列出来并询问「是否继续安装？(y/n)」。
- 测试文件（`testFile`）是可选的：下载失败只警告不中断，工具本体照样装好。

下载与校验的层层限额留到「安全边界」一起讲。

### update —— 把第三方工具升级到新版本

```bash
python scripts/afc_tools.py update mytool            # 检查并更新（会问一句）
python scripts/afc_tools.py update mytool --check    # 只检查，不落盘
python scripts/afc_tools.py update mytool -y         # 跳过确认
python scripts/afc_tools.py update --all --check     # 批量检查全部第三方工具
python scripts/afc_tools.py update --all -y          # 批量更新
```

有更新时的输出（真实跑出来的样子）：

```
工具 mytool：v1.0.0 → v1.1.0
  变更（2）：
    - utils/afc/tools/mytool/__init__.py
    - utils/afc/tools/mytool/triggers.py
  未变（1）
[!] 本地对这些文件的修改将被覆盖
更新文件...
  [OK] utils/afc/tools/mytool/__init__.py
  [OK] utils/afc/tools/mytool/triggers.py

[OK] 工具 mytool 已更新（新增 0 / 变更 2 / 恢复 0 / 删除 0）

提示：重启 bot 后生效
```

没有检查到更新时，只会有一句提示：

```
[OK] mytool 已是最新 (v1.0.0，安装于 2026-07-01T00:00:00)
```

**检测更新并非靠版本号，而是字节比对。** 
- 流程是：从 `afcToolsCustom.json` 里记录的来源抓远端 `afcTool.json` → 比较版本号（只定调：升了标「有版本更新」，降了会警告「仓库可能已回滚」，比不了就当没这回事）→ 把远端全部文件下载到暂存目录，和本地逐文件比字节——新增 / 变更 / 本地缺失 / 删除四分类列出清单。版本号只是引子，字节才是裁决：作者忘了 bump 版本（懒作者到处都有……比如咱认识的某位）也瞒不过去。

要记住的几条：

- **`enabled` 状态和 `installedAt` 都保留**，只换 manifest 并记一个 `updatedAt`——禁用中的工具也可以放心更新，更新完它还是禁用。
- **落盘是原子的（工具级）**：写文件前先把「将被覆盖/删除」的本地文件备份到 `data/.afc_install/backup_<tool>/`，落盘、删除、改 json 任何一步失败都会从备份还原——工具要么全新，要么全旧，没有半新半旧的中间态。备份成功后即删；万一回滚都没能全救回来，备份会留在原地并打出路径，留给你人工抢救。——当然，只换 manifest 的「仅清单更新」不涉文件，没有可备份的东西，json 本身靠原子写兜底。
- **删除集也走安全校验**：新 manifest 不再声明的文件会被删掉，但删之前每个路径重新过一遍 `_assertSafeToolPath`（不信任存储的旧 manifest——和 uninstall 一个心眼）。
- **本地魔改会被覆盖**：字节比对分不清「远端改了」和「你改了」——确认前那句「本地对这些文件的修改将被覆盖」就是说这个。想保住本地改动，先自己备份。
- **builtin 和 local-draft 不归它管**：内置工具随仓库 `git pull` 更新；local-draft 没有来源，无从比起。
- **`--all` 必须配 `--check` 或 `--yes`**——批量场景不该一个个按 y，这样对脚本和 cron 也友好。

#### remoteSha 快车道（加速器，不是裁决）

每次 install / update 成功后，脚本会顺手记一下远端分支 HEAD 的 commit sha（写进 `afcToolsCustom.json` 的 `remoteSha` 字段，抓不到就算了）。下次 update 先拿这个 sha 问一句远端：没变就直接宣布「已是最新」，把「下载全部文件逐字节比对」这一步整个省掉—— N+1 次请求就这样变成 1 次了。

```
[OK] mytool 已是最新（远端提交未变，安装于 2026-07-01T00:00:00）
```

要注意它的定位：sha 只是加速器，**字节比对才是裁决**。sha 对不上、抓不到、超了 GitHub API 未认证配额（60 次/小时，`--all` 时每工具 1 次），都会静默退化回完整比对。另外，`--check` 是永远走完整比对的。

### uninstall —— 把工具从设备中卸载

```bash
python scripts/afc_tools.py uninstall mytool        # custom 工具直接卸
python scripts/afc_tools.py uninstall weather       # builtin 会被拦下来
python scripts/afc_tools.py uninstall weather -f    # 执意要删的话
```

对内置工具，默认是劝阻的（和 module.py 相同）：

```
[!] weather 是内置工具

  disable       只禁用，不删除文件（推荐）
  uninstall -f  强制删除文件（不可逆，需重新拉取代码才能恢复）
```

对 local-draft 工具也类似：不加 `-f` 只提示，加了就整个目录连带对应测试文件一起删。

对 custom 工具，按 manifest 里登记的文件逐个删除：

```
正在卸载工具 mytool...
  [OK] 删除 utils/afc/tools/mytool/__init__.py
  [OK] 删除 utils/afc/tools/mytool/triggers.py
  ...

[OK] 工具 mytool 已卸载
  删除 7 个文件
```

删除时有两点细节：一是每个路径在删之前都会**重新**做安全校验，不信任存储的 manifest 原文（理由见安全边界），校验不过的会跳过并警告；二是工具目录里残留的 `afcTool.json` 会被顺手清掉——它不在 manifest 的 `files` 清单里，但要是不删，空目录里剩个清单文件，工具会被当成 local-draft「复活」，就很灵异了……

---

## 安全边界

`install` / `check` 处理的是「尚未被信任」的代码——第三方仓库拉下来的东西，在用户审阅之前，它和一封陌生邮件的附件没有区别。这一节讲管道上扎的几道笼子。

### 为什么全程 AST，绝不 import

这是整套脚本的第一原则：**管理管道（install / check / list）绝不 import 任何工具代码**。

> 搞这么大阵仗，是因为 import 即执行——Python 模块被 import 时，顶层代码会原样跑一遍。一个恶意工具只要在 `__init__.py` 顶层写一行，就能在 `check` 这种「我只是想检查一下」的命令里完成攻击。所以管道上对工具代码的所有了解——入口函数存不存在、KEYWORDS/PATTERNS 是什么——全部通过 AST 静态分析拿到：把文件解析成语法树，在树里找 `AsyncFunctionDef` 节点、找顶层赋值的字符串列表，一个字节都不执行。
> 
> 这里会有个**重要的区分**：bot 启动时，`_scanTriggers` / `_scanTools` 是会 import 已安装工具的 `triggers` 和 `__init__`（连带 `core`）的——那是「工具已经被信任、正式上岗」之后的正常加载，在信任边界以内。「绝不 import」专指管理脚本管道在「尚未信任」的阶段。这是两码事，各管各的。

### 路径校验：realpath + commonpath

manifest 里的每个文件路径，写盘前都要过 `_assertSafeToolPath`：

- 先拒绝 `..` 段和绝对路径
- 再用 `os.path.realpath` + `os.path.commonpath` 判定目标是否真的落在基准目录内——而**不是**用 `startswith` 做子串匹配。子串匹配会被绝对路径吞并和 `..` 穿越绕过。~~快快去检查你们自己的仓库有没有这样用的……~~
- 工具代码文件要求落在 `utils/afc/tools/<id>/` 子目录内；`testFile` 因为是扁平的 `test_<id>.py`（不在 `<id>/` 子目录），只约束在 `tests/utils/afc/tools` 基准内。

顺序上也有讲究：**先校验完所有路径，再开始下载**，不给逃逸路径任何写盘的机会。

### 下载限额与原子落盘

从网络抓文件时的笼子（install 和 update 共用同一套）：

- 单次请求 `timeout=15` 秒
- 单个工具文件 ≤ 2MB，超限中止并删掉半成品
- 单工具文件数 ≤ 50
- `afcTool.json` 本身 ≤ 1MB
- 下载先进临时目录 `data/.afc_install`——刻意选在**与目标同盘**的位置，因为 `os.replace` 只有同盘才原子。落盘时每文件先挪到 `<target>.tmp` 再 `os.replace` 到最终路径，任何异常路径都会在 finally 里把临时目录和没完成的 `.tmp` 清干净。

> install 和 update 在这条之上还各加了一道**工具级原子**：落盘前把「将被覆盖/删除」的本地文件复制备份到 `data/.afc_install/backup_<tool>/`（install 用 `backup_install_<tool>/`，copy2 复制），落盘、删除、写 json 任一步失败就用备份还原（os.replace 移回）——要么全新要么全旧，不留半成品。备份成功后即删；回滚都没救全时备份留在原地并打出路径。备份成本随变更文件量线性增长，这是原子性的合理价格。

### 卸载与更新时的「不信任存储」

`uninstall` 按 manifest 删文件，但 `data/afcToolsCustom.json` 是落盘的 JSON，理论上可能被篡改——要是里面被塞进一个 `../../important.py`，照单全收地删就出事了。所以删除前每个路径都重新跑一遍 realpath+commonpath 校验，逃逸的直接 skip + 警告日志。存储的 manifest 只用来「列清单」，不用来「授信任」。`update` 删除「新 manifest 不再声明」的旧文件时，走的也是同一道重校；工具名本身同样要过 id 格式校验才敢拼进备份目录路径——json 的键名也是存储，一样不可信。

### 顺手收紧了 module.py

同一批安全思路也回填到了模块管理那边：`scripts/module.py` 的 `installFromGitHub` / `cmdUninstall` 改用同款 `_assertSafeModulePath`（realpath+commonpath，并保留原有的 `handlers/` 或 `utils/` 前缀要求），不再用 startswith 子串判断。

### ReDoS 只是启发式提醒

`check` 和 `install` 会用一条简单的正则去嗅探嵌套量词（`(x+)+` 这类形态），命中报 Warning 而不是 Error。这是因为启发式有假阳性——有些嵌套量词实际是无害的——所以只提醒开发者自查，不替人下结论。触发词正则会在每条用户消息上跑，真塞一个灾难性回溯进去，bot 会被自己的工具卡脖子，所以还是值得多看一眼的说。

---

## 把工具发到 GitHub 给别人装

自己写的工具想分享出去，让别人一条 `install` 就装上，仓库要按约定摆：

```
afc-mytool/                  # GitHub 仓库
├── afcTool.json             # 清单，放仓库根（install 第一个抓的就是它）
├── utils/afc/tools/mytool/  # 工具文件按项目相对路径原样摆放
│   ├── __init__.py
│   ├── core.py
│   ├── triggers.py
│   ├── config.py
│   └── README.md
└── tests/utils/afc/tools/
    └── test_mytool.py       # 可选，对应 manifest 的 testFile
```

要点就三条：

1. **`afcTool.json` 必须在仓库根**。install 按 `raw.githubusercontent.com/<user>/<repo>/<branch>/afcTool.json` 去抓它，抓不到就当这个分支不存在。
2. **工具文件按 `files` 里声明的项目相对路径摆放**。install 是照着 manifest 逐文件抓的，仓库目录结构得和路径对得上。
3. **分支默认依次尝试 `main` → `master`**，用别的分支就让安装方加 `--branch`。

反过来说，`afcTool.json` 本身**不会**被下载进项目——它是清单不是内容，install 抓它只是为了读，读完后信息登记进 `data/afcToolsCustom.json` 就结束了。所以 `files` 里不需要（也不应该）列出 `afcTool.json` 自己。

---

## 日志

安全敏感的操作必须留痕，这套脚本走 `utils/core/logger.py` 的 `logSystemEvent`，事件名统一带 **「AFC 工具」** 前缀：

| 事件 | 时机 |
| --- | --- |
| AFC 工具安装开始 / 成功 / 失败 | install 全链路，失败原因也记下 |
| AFC 工具覆盖安装 | install `-f` 覆盖已有目录时（WARNING） |
| AFC 工具依赖缺失 | install / update 时 dependencies 比对落空（WARNING） |
| AFC 工具更新开始 / 成功 / 失败 | update 全链路；`--check` 不记开始/成功（失败仍记），已是最新与用户取消不记结果日志 |
| AFC 工具更新路径逃逸 | update 删除旧文件时路径校验被 SKIP（WARNING） |
| AFC 工具卸载成功 / 卸载路径逃逸 | uninstall 全链路，逃逸的路径单独记（WARNING） |
| AFC 工具检查异常 | check 发现 Error 级问题时（ERROR；只 Warning 不记） |

而 `enable` / `disable` / `new` 这三个是本地小操作，不记日志——启用状态本身就写在 `afcTools.json` 里，脚手架产物也都在明面上，没有审计价值的事情就不吵了。日志失败不会阻断命令本身。

---

## 已知局限

有些不足是这一版有意先放着的……写下来，也算给以后的自己留个明白账。

1. **版本号比较是简易规则，不是严格 semver**。`update` 的版本比较只认「数字段 + 后缀」；semver 的复杂玩法（构建元数据、预发行通道排序）一律当不可比，退化为纯字节比对——反正字节才是裁决，影响其实有限。
2. **没有手动 rollback 命令**。update 失败会自动回滚（备份-还原），但成功后备份即删——想退回旧版只能 uninstall 再装旧版。备份是单槽的：上次回滚失败留下的救援副本，会被下次 update 覆盖。
3. **不支持本地路径安装**。`install` 只认 GitHub URL（`https://github.com/<user>/<repo>`），还没有 `--local <path>`。本地捣鼓请直接用 `new` 或者手工摆目录走 local-draft 路线。
4. **触发词冲突无优先级**。两个工具的触发词重叠时，意图检测会把候选都返回，交给 LLM 自己选——没有「谁优先」的机制（这也是 afc.md 里就承认的局限）。
5. **ReDoS 检测只是启发式**。嵌套量词嗅探有假阳性也有漏网之鱼，只报 Warning，最终的判断还是在人。
6. **enable/disable 要重启才生效**。触发词索引和工具 schema 都是启动时扫一次建好的，运行中不会热重载。update 同理——更新完也提示重启。

---

## 参考

- `scripts/afc_tools.py` — 本文档的主角，九个命令的实现
- `utils/afc/toolManager.py` — 三个状态文件的读写与启用判定
- [docs/afc.md](afc.md) — AFC 架构文档：意图检测、registry、工具的运行时加载
- [docs/module-management.md](module-management.md) — 模块管理系统：粗粒度那一层，设计思路同源