"""
utils/core/tui/paradigms —— 开放范式库。

每种交互范式一个模块：listMenu（列表 + 方向键）、fullScreen（全屏 pt 接管）、
textEditor（全屏文本编辑器）。新范式 = 这里加一个文件 + tui/__init__.py 导出。

零副作用约束（贯穿所有范式模块）：import 时不得有副作用——不启动 task、不动 interactiveMode、
不注册回调、不写全局状态、不在 import 时构造 Application。所有副作用只在 runSession() 生命周期
内发生。chatScreen 因 patch-target 约束例外保留实例级（__init__ 内）构造，是有据的例外。
"""