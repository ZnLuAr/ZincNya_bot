"""
utils/core/tui/paradigms/textEditor.py

TextEditorApp —— 全屏文本编辑器（fullScreen 范式的编辑器特化）。

屏幕模型：单个 TextArea 全屏接管（pt full_screen=True，自管备用屏幕）。
交互：c-s 或 esc+enter 保存退出（返回 True）；c-c 放弃（返回 False）；escape 吞掉（不退出）。
生命周期：单发——mainLoop 跑一轮 runOnce 即退。

editFile(filePath) 是保名薄壳——quote/memory 的测试 patch 点 'editFile' 依赖此函数名稳定。
"""

import os

from prompt_toolkit.widgets import TextArea

from utils.core.tui.paradigms.fullScreen import FullScreenTUIApp




class TextEditorApp(FullScreenTUIApp):
    """全屏文本编辑器。构造时读文件 + 建 TextArea，runSession 跑一轮即退。"""

    def __init__(self, filePath: str):
        self._filePath = filePath

        # 读文件内容（不存在则空串）+ 建 TextArea（buildLayout 要用）
        if os.path.exists(filePath):
            with open(filePath, "r", encoding="utf-8") as f:
                initialText = f.read()
        else:
            initialText = ""

        self._editor = TextArea(
            text=initialText,
            multiline=True,
            scrollbar=True,
            wrap_lines=True,
            focus_on_click=True,
        )

        # super 链中 FullScreenTUIApp.__init__ 会调 createApplication → buildLayout/setupKeyBindings
        # 此时 _editor 已就位
        super().__init__()


    def buildLayout(self):
        return self._editor


    def setupKeyBindings(self, kb):
        @kb.add("c-s")
        @kb.add("escape", "enter")
        def _save(event):
            text = self._editor.text.replace("\r\n", "\n")
            with open(self._filePath, "w", encoding="utf-8") as f:
                f.write(text)
            event.app.exit(result=True)

        @kb.add("c-c")
        def _cancel(event):
            event.app.exit(result=False)

        @kb.add("escape")
        def _swallow(event):
            # 单独的 escape 吞掉（不与 esc+enter 冲突）
            pass


    async def mainLoop(self):
        # 单发编辑器：一轮即退
        return await self.runOnce()




async def editFile(filePath: str) -> bool:
    """全屏文本编辑器薄壳（保名：quote/memory 的 patch 点 'editFile' 不变）。

    走 runSession（不是 run）——TextEditorApp 无 run 方法，调 .run() 会 AttributeError；
    runSession 走 onEnter → mainLoop(=runOnce) → 收尾 的完整单发生命周期。
    """
    return await TextEditorApp(filePath).runSession()
