#!/usr/bin/env python3
import sys
print(f"Python 路径: {sys.executable}")
print(f"Python 版本: {sys.version}")

try:
    import nonebot
    print("✓ NoneBot 导入成功")
    print(f"NoneBot 版本: {nonebot.__version__}")
except ImportError as e:
    print(f"✗ NoneBot 导入失败: {e}")

try:
    from nonebot.adapters.onebot.v11 import Adapter
    print("✓ OneBot V11 适配器导入成功")
except ImportError as e:
    print(f"✗ OneBot V11 适配器导入失败: {e}")

try:
    import websockets
    print("✓ WebSockets 导入成功")
except ImportError as e:
    print(f"✗ WebSockets 导入失败: {e}")
