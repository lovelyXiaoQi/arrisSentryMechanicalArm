# -*- coding: utf-8 -*-
"""
SentryArmRuntimePoint - 哨戒臂的动力臂交互点

让普通动力臂 (create:mechanical_arm) 能与哨戒臂 (create:sentry_arm) 交互:
- insert: 向哨戒臂输送弹药（接受该枪弹药序列内任意等级子弹，库存同时只存一种）
- extract: 从哨戒臂取出备用弹药（按实际存放的等级返还，不取已装膛的 currentMagazine）

对齐 Java SentryArmInteraction.SentryPoint:
    交互位置 = 方块上方 1.5 格

⚠ 主包 MechanicalArmSystem 会无守卫直调 extractDistributable()（哨戒臂被配成
输入点时逐 tick）与 popContainerItem()（每次 insert 成功后），本类必须与主包
Helpers.MechanicalArmRuntimePoint.RuntimePoint 基类保持全接口同形——缺一个方法
就是 AttributeError 掀掉整个服务端 ECS tick。
"""

from ...QuModLibs.Server import serverApi
from ..Shared import SentryArmEpCompat as EpCompat

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
    弹药库存容量上限 = 弹匣容量 × RESERVE_MULT。

    以装枪时持久化的 comp.magazineSize 为准——容量判定必须在动力臂整个
    搬运周期内恒定（collect 的 simulate 预算和 deposit 的真实入库读到
    不同容量时，差额会永久滞留在动力臂爪子里，玩家视角就是"吞子弹"）。
    旧存档未记录 magazineSize 时走 gunInfo 缓存兜底（同样的漂移风险仍在，
    但服务端 tick 的武器自愈会在首次 tick 补写 magazineSize，窗口极短）。
    """
    magSize = int(getattr(comp, "magazineSize", 0) or 0)
    if magSize > 0:
        return magSize * RESERVE_MULT
    try:
        from . import SentryArmTargeting
        # 逐一查缓存，匹配 bulletType 的枪信息即可（同枪械同弹药）
        for info in SentryArmTargeting._gunInfoCache.values():
            if info and info.get("useBullet") == comp.bulletType:
                return int(info.get("magazine", 30)) * RESERVE_MULT
    except Exception:
        pass
    return 30 * RESERVE_MULT


def _getEpBullet():
    # type: () -> object | None
    """EP 子弹等级数据模块（server 侧统一缓存在 SentryArmTargeting）"""
    from . import SentryArmTargeting

    return SentryArmTargeting._getEpBullet()


def _reserveStoredType(comp):
    # type: (object) -> str
    """库存当前存放的子弹物品名。旧存档/旧字段为空时按基础弹处理。"""
    return getattr(comp, "reserveBulletType", "") or comp.bulletType


class SentryArmRuntimePoint(object):
    """哨戒臂的动力臂交互点（与主包 RuntimePoint 基类全接口同形）"""

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
        返还物品名 = 库存实际存放的等级（reserveBulletType），不降级。
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
        storedType = _reserveStoredType(comp)
        if not simulate:
            comp.ammoReserve = reserve - count
            if reserve - count <= 0:
                comp.reserveBulletType = ""  # 清空后允许换存其他等级
        return {
            "newItemName": storedType,
            "newAuxValue": 0,
            "count": count,
        }

    def extractDistributable(self, blockPos, dimensionId, maxAmount, canExtract, simulate=False):
        # type: (tuple, int, int, any, bool) -> dict | None
        """主包机械臂搜索/取料入口（无守卫直调）。
        哨戒臂库存是单物品候选，语义对齐主包 RuntimePoint 基类默认实现：
        先模拟取 → canExtract 过滤 → 再真取。"""
        item = self.extract(blockPos, dimensionId, maxAmount, simulate=True)
        if not item or item.get("count", 0) <= 0:
            return None
        if not canExtract(item):
            return None
        if simulate:
            return item
        return self.extract(blockPos, dimensionId, maxAmount, simulate=False)

    def insert(self, blockPos, dimensionId, itemDict, simulate=False):
        # type: (tuple, int, dict, bool) -> int
        """
        向哨戒臂库存放入弹药。
        接受当前枪弹药序列内的任意等级子弹（EP_BULLET_SEQUENCE[useBullet]）；
        库存同时只存一种等级——已有存货时只收同名弹，不符返回 0 让动力臂跳过。
        """
        comp = _getSentryComp(blockPos, dimensionId)
        if not comp or not comp.bulletType:
            return 0  # 哨戒臂无枪或未配置
        itemName = itemDict.get("newItemName", "") if itemDict else ""
        if not EpCompat.isBulletAccepted(_getEpBullet(), comp.bulletType, itemName):
            return 0  # 非本枪可用弹种
        reserve = int(comp.ammoReserve or 0)
        if reserve > 0 and itemName != _reserveStoredType(comp):
            return 0  # 库存已有其他等级，先取空再换
        count = int(itemDict.get("count", 1))
        if count <= 0:
            return 0

        reserveCap = _getReserveCap(comp)
        capacityLeft = reserveCap - reserve
        if capacityLeft <= 0:
            return 0  # 库存已满
        accepted = min(count, capacityLeft)
        if not simulate:
            comp.ammoReserve = reserve + accepted
            comp.reserveBulletType = itemName
        return accepted

    def popContainerItem(self):
        # type: () -> dict | None
        """主包 deposit 在每次 insert 成功后无守卫直调（岩浆桶→空桶那类容器
        返还语义）。子弹无容器返还，恒 None——对齐主包基类默认实现。"""
        return None

    def isDepositOnly(self):
        # type: () -> bool
        return False
