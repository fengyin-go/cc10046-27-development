"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource
from app.routers import proxy
from app.routers import chains

# 代理池专用路由是固定前缀，必须在通配的资源路由之前注册。
ROUTERS = [proxy, resource, chains]
