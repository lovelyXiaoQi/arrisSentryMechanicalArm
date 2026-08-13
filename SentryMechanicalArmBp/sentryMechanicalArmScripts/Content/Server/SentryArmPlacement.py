# -*- coding: utf-8 -*-
"""
SentryArmPlacement - 哨戒机械臂放置规则

对齐 Java SentryArmBlock.getStateForPlacement():
- 点击方块下表面 → ceiling 模式
- 其他面 → floor 模式

通过 ModServerSystem._doRegister 手动注入到主包
PlacementRulesMeta._registry，而非元类自动注册。

另负责暂存放置者（主人）信息：ServerEntityTryPlaceBlockEvent 触发时
ECS 实体尚未创建，先按 (维度, 坐标) 暂存，SentryArmTargeting 在该实体
首个 tick 调 consumePendingOwner 消费并写入 ownerId / ownerName。
"""

import time

from ...QuModLibs.Server import serverApi

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
_MAIN_PACK = "arrisCreateScripts"

compFactory = serverApi.GetEngineCompFactory()

# (dimensionId, (x, y, z)) -> (ownerId, ownerName, 记录时间)
_pendingOwners = {}
# 放置被取消 / 实体未创建时残留条目的过期时长
_PENDING_TTL_SEC = 30.0


def consumePendingOwner(blockPos, dimensionId):
    # type: (tuple, int) -> tuple | None
    """取走该位置的待写入主人信息 (ownerId, ownerName)；无记录/已过期返回 None"""
    if not _pendingOwners:
        return None
    now = time.time()
    # 顺带清理过期残留
    for key in list(_pendingOwners.keys()):
        if now - _pendingOwners[key][2] > _PENDING_TTL_SEC:
            del _pendingOwners[key]
    key = (int(dimensionId), (int(blockPos[0]), int(blockPos[1]), int(blockPos[2])))
    entry = _pendingOwners.pop(key, None)
    if entry is None:
        return None
    return entry[0], entry[1]


class SentryArmPlacementHandler(object):
    """哨戒臂放置规则处理器"""

    BLOCK_ID = SENTRY_ARM_BLOCK

    def onPlace(self, args):
        # type: (dict) -> None
        fullName = args.get("fullName", "")
        if fullName != SENTRY_ARM_BLOCK:
            return

        # 对齐 Java SentryArmBlock: 点击下表面 (face=0=DOWN) → ceiling
        clickedFace = args.get("face", 1)
        isCeiling = clickedFace == 0

        BlockStateApi = serverApi.ImportModule(_MAIN_PACK + ".Content.Server.BlockStateApi")
        if BlockStateApi:
            auxData = BlockStateApi.getAuxFromBlockStates(
                SENTRY_ARM_BLOCK,
                {"create:ceiling": isCeiling, "create:mode": "block"},
            )
            if auxData is not None:
                args["auxData"] = auxData

        # 暂存放置者为主人（索敌永远绕过主人；实体建好后由 Targeting 落盘）
        placerId = args.get("entityId")
        if placerId:
            ownerName = compFactory.CreateName(placerId).GetName() or ""
            key = (
                int(args.get("dimensionId", 0)),
                (int(args.get("x", 0)), int(args.get("y", 0)), int(args.get("z", 0))),
            )
            _pendingOwners[key] = (str(placerId), ownerName, time.time())
