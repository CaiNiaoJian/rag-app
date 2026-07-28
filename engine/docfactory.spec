# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 出包配置（08 章 §2「打包与部署」）。

产物：``dist/engine/engine.exe`` —— electron-builder 的 extraResources 把整个
``dist/engine`` 目录复制到安装包的 ``resources/engine\\``，主进程按
``process.resourcesPath/engine/engine.exe`` 拉起（见 app/src/main/engine-supervisor.ts）。

三个非改不可的取舍：

- **onedir 而非 onefile**：onefile 每次启动都要把整包解压到临时目录，冷启动预算只有 5s
  （01 章 NFR），且 .kmod 的逐文件哈希校验需要文件在磁盘上真实可寻址。
- **console=True 必需**：引擎的启动握手是往 stdout 打一行 ``READY {...}``，windowed 模式
  下 Windows 不给 stdout，握手直接失效。主进程 spawn 时已带 ``windowsHide: true``，
  所以不会闪控制台窗口。
- **hiddenimports 用 collect_submodules 全收**：本工程大量使用延迟 import——路由表
  （app._ROUTE_MODULES）、任务 runner（scheduler.RUNNERS）、解析器（parsers._PARSERS）
  都是运行时按字符串 import 的，PyInstaller 的静态扫描一个都看不见。逐个列举等于
  给自己埋雷：新增一个 routes_*.py 就会在打包产物里静默消失。整包收进来最省心，
  代价只是几百 KB。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# 延迟 import 的模块全部显式收进来（见上方说明）
hiddenimports = collect_submodules("docfactory") + collect_submodules("uvicorn")
hiddenimports += ["sse_starlette"]

# 引擎**自己**的包内数据：SQLite 迁移脚本。db._load_migrations 走
# ``importlib.resources.files("docfactory") / "migrations"`` 读它，而 PyInstaller 只收 .py，
# 包里的非代码文件一个都不带。漏了它连启动都到不了——_bootstrap 第一步就是 db.migrate()，
# 直接 FileNotFoundError 退 2，主进程侧表现为「引擎在握手前退出」。
# 用 glob 而非逐个列举：以后加 0002_*.sql 不必回来改这里。
datas = [("src/docfactory/migrations/*.sql", "docfactory/migrations")]

# 带数据文件的第三方库：python-docx/python-pptx 的默认模板 xml、pdfminer 的 CMap 表。
# 少了它们，程序能起来但一解析就报错——属于只有真机跑一遍才发现的那类问题。
datas += (
    collect_data_files("docx")
    + collect_data_files("pptx")
    + collect_data_files("pdfminer")
)

a = Analysis(
    ["src/docfactory/main.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 引擎不做 GUI，也不跑测试：把这些大件排除掉，省体积也少一批杀软误报面
    excludes=["tkinter", "test", "unittest", "pytest", "reportlab", "PIL.ImageQt"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # 不加壳：08 章风险 #9，UPX 是杀软误报的头号来源
    console=True,       # READY 握手依赖 stdout，不能改
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="engine",
)
