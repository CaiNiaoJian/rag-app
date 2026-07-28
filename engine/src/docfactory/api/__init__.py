"""HTTP 路由包（02 章 §2.1 端点表）。

每个 routes_*.py 暴露模块级 ``router: APIRouter``，由 app.py 统一挂载；
依赖（db/paths/settings/scheduler）一律从 ``request.app.state`` 取，不用全局单例，
以便测试里起多个互不干扰的应用实例。
"""
