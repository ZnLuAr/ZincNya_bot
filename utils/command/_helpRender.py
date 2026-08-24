"""
utils/command/_helpRender.py

控制台命令子命令帮助的统一渲染：速查提示（case _ / 无参分支）与子命令表共用单一数据源，
加子命令时只改表——表与 match 分支同文件同屏，漏更新会在同屏暴露。
"""




def renderSubcommands(header: str, table: dict[str, str]) -> str:
    """
    把子命令用法渲染为两列速查文本（表）。

    列宽按表内最长命令自适应；返回的字符串已含首尾换行约定（尾部单 \\n，print 补行兜底空行）
    """
    width = max(len(k) for k in table)
    lines = [f"{header}："]
    for sub, desc in table.items():
        lines.append(f"  {sub:<{width}}  {desc}")

    return "\n".join(lines) + "\n"
