# -*- coding: utf-8 -*-
"""
SentryArmHopperIntake - 原版漏斗向哨戒臂供弹

引擎不把脚本自定义方块当容器（HopperTryPull* 事件只对真容器触发、且仅是
一次性的开关参数），原版漏斗永远不会主动往哨戒臂里推东西——所以反过来：
哨戒臂按原版漏斗的搬运节流（8 游戏刻 ≈ 0.4 秒 ≈ 12 个 30Hz 服务端 tick）
主动从"出口指向自己"的相邻漏斗抽取弹药。

支持的摆放：正上方漏斗（出口朝下）+ 四侧漏斗（出口指向哨戒臂）。
红石充能（toggle_bit=true）的漏斗按原版规则暂停。
入库门禁与动力臂 / 机械动力漏斗完全一致（复用 RuntimePoint.insert：
本枪弹药序列任意等级、库存单一等级、容量 magazineSize × 5）。
"""

from ...QuModLibs.Server import serverApi
from .SentryArmRuntimePoint import SentryArmRuntimePoint

_HOPPER_BLOCK = "minecraft:hopper"
# 原版漏斗搬运节流：8 游戏刻(20Hz) = 0.4s ≈ 12 个服务端 tick(30Hz)
_PULL_INTERVAL_TICKS = 12
_HOPPER_SLOTS = 5

compFactory = serverApi.GetEngineCompFactory()
levelId = serverApi.GetLevelId()

# (相对哨戒臂的偏移, 该位置漏斗必须的 facing_direction) —— 出口指向哨戒臂才收
# facing_direction: 0=下, 2=北(z-), 3=南(z+), 4=西(x-), 5=东(x+)
_INTAKE_OFFSETS = (
    ((0, 1, 0), 0),   # 正上方，出口朝下
    ((0, 0, -1), 3),  # 北侧邻居，出口朝南
    ((0, 0, 1), 2),   # 南侧邻居，出口朝北
    ((-1, 0, 0), 5),  # 西侧邻居，出口朝东
    ((1, 0, 0), 4),   # 东侧邻居，出口朝西
)

_pullCooldowns = {}  # ecsEntityId -> remainingTicks
_point = SentryArmRuntimePoint()


def tickHopperIntake(entity, comp):
    # type: (object, object) -> None
    """每 tick 由 SentryArmTargeting._tickSentryArm 调用；内部按原版漏斗节流限频。
    装填不受红石锁定 / RPM=0 影响（与动力臂交互点一致，停机也能补弹）。"""
    eid = entity.id
    cd = _pullCooldowns.get(eid, 0) - 1
    if cd > 0:
        _pullCooldowns[eid] = cd
        return
    _pullCooldowns[eid] = _PULL_INTERVAL_TICKS

    if not comp.bulletType:
        return  # 未装枪，不收弹

    pos = entity.blockPos
    dimensionId = entity.dimensionId
    blockInfoComp = compFactory.CreateBlockInfo(levelId)
    blockStateComp = compFactory.CreateBlockState(levelId)
    itemComp = compFactory.CreateItem(levelId)

    for offset, requiredFacing in _INTAKE_OFFSETS:
        hopperPos = (pos[0] + offset[0], pos[1] + offset[1], pos[2] + offset[2])
        blockDict = blockInfoComp.GetBlockNew(hopperPos, dimensionId)
        if not blockDict or blockDict.get("name") != _HOPPER_BLOCK:
            continue
        states = blockStateComp.GetBlockStates(hopperPos, dimensionId) or {}
        if states.get("facing_direction", 0) != requiredFacing:
            continue
        if states.get("toggle_bit"):
            continue  # 红石充能，漏斗暂停（对齐原版）
        if _pullOneBullet(itemComp, hopperPos, pos, dimensionId):
            return  # 每个节流周期最多转移 1 发（对齐原版漏斗速率）


def _pullOneBullet(itemComp, hopperPos, sentryPos, dimensionId):
    # type: (object, tuple, tuple, int) -> bool
    """从漏斗 5 个槽位里找首个可入库的子弹，转移 1 发。返回是否成功。

    先入库再扣漏斗（写回失败最多多 1 发，不会凭空消失——与主包漏斗系统
    的容器写回同风险等级）。
    """
    for slot in range(_HOPPER_SLOTS):
        slotItem = itemComp.GetContainerItem(hopperPos, slot, dimensionId, True)
        if not slotItem or slotItem.get("count", 0) <= 0:
            continue
        piece = {
            "newItemName": slotItem.get("newItemName", ""),
            "newAuxValue": slotItem.get("newAuxValue", 0),
            "count": 1,
        }
        accepted = _point.insert(sentryPos, dimensionId, piece, simulate=False)
        if accepted <= 0:
            continue  # 非本枪弹药 / 等级不符 / 库存满 → 试下一槽
        remaining = int(slotItem.get("count", 0)) - 1
        if remaining <= 0:
            itemComp.SpawnItemToContainer({}, slot, hopperPos, dimensionId)
        else:
            slotItem["count"] = remaining
            itemComp.SpawnItemToContainer(slotItem, slot, hopperPos, dimensionId)
        return True
    return False
