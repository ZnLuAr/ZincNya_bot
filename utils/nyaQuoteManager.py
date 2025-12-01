"""
utils/nyaQuoteManager.py

用于管理 /nya 的语录存储与在 CLI 中展示用的数据模型。
模块组织与 whitelistManager.py 类似，分为：
  - 文件读写（内部函数）
  - 外部操作入口（userOperation）
  - UI 相关：collectQuoteViewModel、quoteUIRenderer、quoteMenuController
  - 编辑器辅助：edit_quote_via_editor（将 \n <-> 实际换行互转）

设计要点：
  - 存储为列表，每项为 dict: {"text": "...", "weight": float}
  - 展示时按 weight 从小到大排序（A 模式）
  - 编辑时在临时文件中将存储的 "\n" 展开为换行；保存时再转回 "\n"
"""




import os
import json
import tempfile
import shutil
import subprocess
from rich.table import Table
from rich.console import Console
from typing import List , Tuple , Optional

from config import QUOTES_DIR




def ensureQuoteFile():
    if QUOTES_DIR:
        os.makedirs(QUOTES_DIR , exist_ok=True)
    if not os.path.exists(QUOTES_DIR):
        saveQuoteFile([])


def loadQuoteFile() -> List[dict]:
    """
    返回列表形式的语录数据（items: {"text": str, "weight": float}）
    """

    ensureQuoteFile()

    try:
        with open(QUOTES_DIR , "r" , encoding="utf-8") as f:
            quoteData = json.load(f)
            if isinstance(quoteData , list):
                return quoteData
            return[]
    except Exception:
        return []
    

def saveQuoteFile(quotes: List[dict]):
    ensureQuoteFile()
    with open(QUOTES_DIR , "w" , encoding="utf-8") as f:
        json.dump(quotes , f , ensure_ascii=False , indent=2)




def userOperation(operation: str , index:Optional[int]=None , payload:Optional[dict]=None):
    """
    统一操作接口（面向 CLI / handlers 调用）
      - operation:
          "add"       : payload -> dict with keys "text"(str) and optional "weight"(float)
          "delete"    : index -> int
          "set"       : (index, payload) 修改指定条目（payload 可包含 text / weight）
          "list"      : 返回完整列表（不排序）
          "get"       : 返回 index 对应条目或 None
    返回：
      - 对于增删改 返回 True/False
      - "list" 返回 list
      - "get" 返回 dict | None
    """

    match operation:
        case "add":
            if not isinstance(payload , dict) or "text" not in payload:
                return False
            weight = float(payload)




def collectQuoteViewModel(selectedIndex: int=-1 , limitPreviewChars: int=15) -> Tuple[List[dict] , int]:
    """
    构建供渲染使用的 entries 列表，并按 weight 从小到大排序（A 模式）。
    返回 (sorted_quotes, new_selected_index)
      - sorted_quotes: 每项包含 {"text", "weight"}
      - new_selected_index: 传入 selected_index 在排序后的新下标（若传入非法则为 -1）
    注意：排序不会修改文件，仅在内存返回排序结果。
    """

    quotes = loadQuoteFile()
    sortedQuotes = sorted(quotes , key=lambda x:float(x.get("weight" , 1.0)))

    newIndex = -1
    if 0 <= selectedIndex < len(quotes):
        try:
            original = quotes[selectedIndex]
            newIndex = sortedQuotes.index(original)
        except ValueError:
            newIndex = -1

    entries = []
    for q in sortedQuotes:
        text = q.get("text" , "")
        preview = text[:limitPreviewChars] + ("...>>" if len(text) > limitPreviewChars else "")
        entries.append({
            "text": text,
            "preview": preview,
            "weight": float(q.get("weight" , 1.0))
        })

        return entries , newIndex
    

def quoteUIRenderer(entries: List[dict] , selectedIndex:int=-1):
    """
    使用 rich 渲染表格（序号、权重、文本预览），被选中的行高亮。

    返回字符串（方便 CLI 打印或测试）。
    """

    table = Table(title="ZincNya Quotes")
    table.add_column("No." , justify="right")
    table.add_column("Weight" , justify="right")
    table.add_column("Preview" , justify="left")

    for i , e in enumerate(entries , 1):
        isSelected = (i-1 == selectedIndex)
        weight = f"{e['weight']:.3g}"
        preview = e["preview"]

        if isSelected:
            table.add_row(
                f"[bold yellow]> {i} </bold yellow>",
                f"[bold yellow]{weight}[/]",
                f"[bold yellow]{preview}[/]"
            )
        else:
            table.add_row(str(i) , weight , preview)

    console = Console()
    console.print(table)
    return ""


def editQuoteViaEditor(initialTextEscaped: str) -> Optional[str]:
    """
    在临时文件中打开系统编辑器（$EDITOR 或 vi/nano），
    编辑器中显示 initial_text_escaped 的真实换行（把 "\\n" -> "\n"）。
    保存后把文件内容读取并把真实换行转换回 "\\n"（用于存储）。
    返回转码后的文本（字符串），或者 None 表示未保存/中断。
    """

    initialText = initialTextEscaped.replace("\\n" , "\n")

    editor = os.environ.get("EDITOR") or shutil.which("nano") or shutil.which("vi") or "vi"
    with tempfile.NamedTemporaryFile("w+" , delete=False , encoding="utf-8" , suffix=".tmp") as tf:
        tfName = tf.name
        tf.write(initialText)
        tf.flush()

        try:
            subprocess.run([editor , tfName])
            with open(tfName , "r" , encoding="utf-8") as f:
                newText = f.read()
            escaped = newText.replace("\r\n" , "\n").replace("\n" , "\\n")
            return escaped
        except Exception:
            return None
        finally:
            try:
                os.unlink(tfName)
            except Exception:
                pass