"""部门邻接树的缓存键 / 编解码 / 失效——auth 域内的纯基础设施叶子模块。

刻意从 ``auth.service`` 拆出:数据域(``admin.departments``)的写侧只需在 mutation 后
调一下 :func:`invalidate_department_tree`,不该为此 import 整个 ``AuthService``
(那是业务服务,会把 admin→auth 的依赖拉到服务层、并埋下循环 import 隐患)。本模块
只依赖 ``core.cache`` + ``json``,是无业务逻辑的叶子,谁都能安全 import。

缓存内容是部门邻接 ``(id, parent_id)``:在每个受数据域约束的请求里被读,但只在部门
CRUD 时变更(极少)。短 TTL 兜跨副本陈旧;写副本在 commit 后主动失效。可安全缓存
(与权限码不同):陈旧的树至多在 ≤TTL 内放宽/收窄组织架构内的可见范围,而数据域校验
本身是叠加在「每请求查库、从不缓存」的权限集之上的纵深防御。

**一致性说明(cache-aside 固有竞态,M1 显式接受)**:写副本「commit→失效」无法消除
一类竞态——某读副本在写提交前 miss 并查到旧树,却在写副本失效**之后**才把旧值种回
Redis,于是旧树最多存活一个 TTL(30s)。鉴于上述安全姿态(数据域是纵深防御、权限永不
缓存,且部门变更极罕见),该窗口可接受。跨副本即时失效(pub/sub)留到 M2 真正出现网关
热路径消费者时再做。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from src.core import cache

# (id, parent_id) pair; parent_id is null for roots.
DeptPair = tuple[int, int | None]

CACHE_KEY = "auth:dept-tree:adjacency"
TTL_SECONDS = 30


def encode(pairs: Sequence[DeptPair]) -> str:
    return json.dumps([[i, p] for i, p in pairs])


def decode(raw: str) -> list[DeptPair]:
    data = json.loads(raw)
    return [(int(i), None if p is None else int(p)) for i, p in data]


async def invalidate_department_tree() -> None:
    """Drop the cached department adjacency (call after any department mutation)."""
    await cache.invalidate(CACHE_KEY)
