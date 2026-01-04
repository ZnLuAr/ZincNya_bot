import os
from prompt_toolkit.application import Application
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.layout import Layout
from prompt_toolkit.key_binding import KeyBindings




async def editFile(filePath: str) -> bool:

    if os.path.exists(filePath):
        with open(filePath , "r" , encoding="utf-8") as f:
            initialText = f.read()

    else:
        initialText = ""

    editor = TextArea(
        text=initialText,
        multiline=True,
        scrollbar=True,
        wrap_lines=True,
        focus_on_click=True,
    )

    kb = KeyBindings()


    @kb.add("c-s")
    @kb.add("escape" , "enter")

    def _(event):
        text = editor.text.replace("\r\n" , "\n")
        with open(filePath , "w" , encoding="utf-8") as f:
            f.write(text)
        event.app.exit(result=True)

    @kb.add("c-c")
    def _(event):
        event.app.exit(result=False)

    @kb.add("escape")
    def _(event):
        pass


    app = Application(
        layout=Layout(editor),
        key_bindings=kb,
        full_screen=True,
    )

    result = await app.run_async()
    return result