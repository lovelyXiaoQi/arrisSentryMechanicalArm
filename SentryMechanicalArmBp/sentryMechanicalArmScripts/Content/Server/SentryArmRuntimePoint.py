# -*- coding: utf-8 -*-
"""
SentryArmRuntimePoint - 哨戒臂的动力臂交互点

让普通动力臂 (create:mechanical_arm) 能与哨戒臂 (create:sentry_arm) 交互:
- insert: 向哨戒臂输送弹药（必须匹配 comp.bulletType）
- extract: 从哨戒臂取出备用弹药（不取已装膛的 currentMagazine）

对齐 Java SentryArmInteraction.SentryPoint:
    交互位置 = 方块上方 1.5 格
"""

from ...QuModLibs.Server import serverApi

_MAIN_PACK = "arrisCreateScripts"

# 弹药库存容量 = magazine × RESERVE_MULT（5 个弹匣量）
RESERVE_MULT = 5


def _getSentryComp(blockPos, dimensionId):
    # type: (tuple, int) -> object | None
    """读取哨戒臂 ECS Component"""
    SW = serverApi.ImportModule(_MAIN_PACK + ".Content.Server.ServerWorld")
    if not SW:
        return None
    world = SW.ServerWorld()
    entity = world.getEntityByPos(blockPos, dimensionId)
    if not entity:
        return None
    return entity.getComponent("SentryArmComponent")


def _getReserveCap(comp):
    # type: (object) -> int
    """
    弹药库存容量上限。
    优先从 SentryArmTargeting._gunInfoCache 读当前枪的 magazine × RESERVE_MULT；
    缓存未建立时（首次交互）兜底 30 × RESERVE_MULT。
    """
    try:
        from . import SentryArmTargeting
        # 逐一查缓存，匹配 bulletType 的枪信息即可（同枪械同弹药）
        for info in SentryArmTargeting._gunInfoCache.values():
            if info and info.get("useBullet") == comp.bulletType:
                return int(info.get("magazine", 30)) * RESERVE_MULT
    except Exception:
        pass
    return 30 * RESERVE_MULT


class SentryArmRuntimePoint(object):
    """哨戒臂的动力臂交互点"""

    def getInteractionPos(self, blockPos):
        # type: (tuple) -> tuple
        """IK 目标点: 方块上方 1.5 格（对齐 Java）"""
        return (blockPos[0] + 0.5, blockPos[1] + 1.5, blockPos[2] + 0.5)

    def getInteractionFacing(self, blockPos, dimensionId):
        # type: (tuple, int) -> int
        """IK 爪子方向: UP=1"""
        return 1

    def extract(self, blockPos, dimensionId, maxAmount, simulate=False):
        # type: (tuple, int, int, bool) -> dict | None
        """
        从哨戒臂库存取出弹药（不取已装膛的 currentMagazine，那是子弹上膛状态）。
        动力臂可用此回收弹药，例如玩家想换枪时先清空库存。
        """
        comp = _getSentryComp(blockPos, dimensionId)
        if not comp or not comp.bulletType:
            return None
        reserve = int(comp.ammoReserve or 0)
        if reserve <= 0:
            return None
        count = min(int(maxAmount), reserve)
        if count <= 0:
            return None
        if not simulate:
            comp.ammoReserve = reserve - count
        return {
            "newItemName": comp.bulletType,
            "newAuxValue": 0,
            "count": count,
        }

    def insert(self, blockPos, dimensionId, itemDict, simulate=False):
        # type: (tuple, int, dict, bool) -> int
        """
        向哨戒臂库存放入弹药。
        只接受 itemDict.newItemName == comp.bulletType 的物品，不符返回 0 让动力臂跳过。
        """
        comp = _getSentryComp(blockPos, dimensionId)
        if not comp or not comp.bulletType:
            return 0  # 哨戒臂无枪或未配置
        if not itemDict or itemDict.get("newItemName") != comp.bulletType:
            return 0  # 类型不符
        count = int(itemDict.get("count", 1))
        if count <= 0:
            return 0

        reserveCap = _getReserveCap(comp)
        capacityLeft = reserveCap - int(comp.ammoReserve or 0)
        if capacityLeft <= 0:
            return 0  # 库存已满
        accepted = min(count, capacityLeft)
        if not simulate:
            comp.ammoReserve = int(comp.ammoReserve or 0) + accepted
        return accepted

    def isDepositOnly(self):
        # type: () -> bool
        return False
