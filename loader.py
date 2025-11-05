import importlib
import pkgutil
from telegram.ext import Application


# 用于读取已注册的功能
def loadHandlers(app: Application):
    package = "handlers"
    for _ , modeName , _ in pkgutil.iter_modules([package]):
        module = importlib.import_module(f"{package}.{modeName}")
        if hasattr(module , "register"):
            for handler in module.register():
                app.add_handler(handler)
                print(f"    · 加载了给咱注册的功能喵： {module.__name__}")
    print ("\n")