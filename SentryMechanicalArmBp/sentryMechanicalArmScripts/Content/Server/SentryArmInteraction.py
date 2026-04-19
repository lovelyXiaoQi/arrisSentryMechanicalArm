# -*- coding: utf-8 -*-
"""
SentryArmInteraction - 哨戒臂枪械装备交互（服务端）

玩家手持枪械右键哨戒臂 → 装备枪械
空手右键 → 取出枪械
通过 Eplus 军械库 EpApiClient.IsGun 判断枪械
"""

from ...QuModLibs.Server import AllowCall, InjectHttpPlayerId, regModLoadFinishHandler, serverApi
from ...QuModLibs.Server import System as GameSystem
from .SentryArmRuntimePoint import SentryArmRuntimePoint

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
_MAIN_PACK = "arrisCreateScripts"
_EP_PACK = "EpJxkScript"

compFactory = serverApi.GetEngineCompFactory()
levelId = serverApi.GetLevelId()

# 缓存 EpApiClient 模块引用（避免每次 ImportModule）
_epApi = None
_epApiChecked = False


def _getEpApiInstance():
    # type: () -> object | None
    """获取 EpApiClient 的全局实例（不是模块，是类实例）"""
    global _epApi, _epApiChecked
    if not _epApiChecked:
        mod = serverApi.ImportModule(_EP_PACK + ".Api.EpApiClient")
        if mod:
            _epApi = getattr(mod, "epApiClient", None)
        _epApiChecked = True
    return _epApi


def _isGun(itemName):
    # type: (str) -> bool
    """通过 Eplus 军械库 API 判断物品是否为枪械"""
    if not itemName:
        return False
    instance = _getEpApiInstance()
    if instance and hasattr(instance, "IsGun"):
        return instance.IsGun(itemName)
    return False


def _resolveBulletType(itemName):
    # type: (str) -> str
    """
    获取枪械接受的弹药物品 ID（基于基础 useBullet — 配件不改此字段，已验证）
    """
    if not itemName:
        return ""
    instance = _getEpApiInstance()
    if not instance or not hasattr(instance, "GetGunInfo"):
        return ""
    info = instance.GetGunInfo(itemName)
    if not info:
        return ""
    return info.get("useBullet", "") or ""


def _getServerWorld():
    SW = serverApi.ImportModule(_MAIN_PACK + ".Content.Server.ServerWorld")
    return SW.ServerWorld() if SW else None


@AllowCall
@InjectHttpPlayerId
def equipGunToSentry(playerId, data):
    # type: (str, dict) -> None
    """
    RPC: 客户端请求装备/取出枪械

    data: {"posX": int, "posY": int, "posZ": int, "dimensionId": int}
    """
    blockPos = (int(data["posX"]), int(data["posY"]), int(data["posZ"]))
    dimensionId = int(data.get("dimensionId", 0))
    # 1. 验证方块
    blockInfoComp = compFactory.CreateBlockInfo(levelId)
    blockDict = blockInfoComp.GetBlockNew(blockPos, dimensionId)
    if not blockDict or blockDict.get("name") != SENTRY_ARM_BLOCK:
        return

    # 2. 获取 ECS 实体
    world = _getServerWorld()
    if not world:
        return
    entity = world.getEntityByPos(blockPos, dimensionId)
    if not entity:
        return
    comp = entity.getComponent("SentryArmComponent")
    if not comp:
        return

    # 3. 获取玩家手持物品
    itemComp = compFactory.CreateItem(playerId)
    carriedItem = itemComp.GetPlayerItem(serverApi.GetMinecraftEnum().ItemPosType.CARRIED, 0, True)
    itemName = carriedItem.get("newItemName", "") if carriedItem else ""

    if not itemName:
        # 空手 → 取出枪械
        if comp.weaponItemName:
            _removeWeapon(comp, playerId, itemComp)
        return

    # 3.5 手持子弹匹配已装备枪械的 bulletType → 装填备弹
    if comp.weaponItemName and comp.bulletType and itemName == comp.bulletType:
        heldCount = int(carriedItem.get("count", 0) or 0)
        if heldCount <= 0:
            return
        itemDict = {"newItemName": itemName, "newAuxValue": 0, "count": heldCount}
        accepted = SentryArmRuntimePoint().insert(blockPos, dimensionId, itemDict, simulate=False)
        if accepted > 0:
            selectSlot = itemComp.GetSelectSlotId()
            itemComp.SetInvItemNum(selectSlot, heldCount - accepted)
        return

    # 4. 验证是枪械（服务端权威校验，防客户端篡改）
    if not _isGun(itemName):
        return

    # 5. 如果已有武器，先返还
    if comp.weaponItemName:
        _removeWeapon(comp, playerId, itemComp)

    # 6. 装备枪械
    comp.weaponItemName = itemName
    comp.weaponCustomTips = carriedItem.get("customTips", "")
    comp.weaponExtraId = carriedItem.get("extraId", "")

    # 弹药状态：bulletType 同步；currentMagazine 读枪械物品 extraId（已装填子弹数）
    comp.bulletType = _resolveBulletType(itemName)
    rawExtra = carriedItem.get("extraId", 0)
    try:
        comp.currentMagazine = int(rawExtra) if rawExtra else 0
    except (TypeError, ValueError):
        comp.currentMagazine = 0
    comp.ammoReserve = 0

    # 7. 清空玩家手持物品
    selectSlot = compFactory.CreateItem(playerId).GetSelectSlotId()
    itemComp.SetInvItemNum(selectSlot, 0)


def _removeWeapon(comp, playerId, itemComp):
    # type: (object, str, object) -> None
    """从哨戒臂取出武器 + 库存弹药返还玩家背包；弹匣内子弹写回枪械 extraId"""
    # 弹匣内子弹（currentMagazine）写回枪械物品的 extraId — 玩家拿回的枪保留弹量
    mag = int(comp.currentMagazine or 0)
    weaponDict = {
        "newItemName": comp.weaponItemName,
        "count": 1,
        "newAuxValue": 0,
        "extraId": str(mag),
    }
    if comp.weaponCustomTips:
        weaponDict["customTips"] = comp.weaponCustomTips

    itemComp.SpawnItemToPlayerInv(weaponDict, playerId)

    # 库存弹药（ammoReserve）单独返还
    reserve = int(comp.ammoReserve or 0)
    if comp.bulletType and reserve > 0:
        ammoDict = {
            "newItemName": comp.bulletType,
            "count": reserve,
            "newAuxValue": 0,
        }
        itemComp.SpawnItemToPlayerInv(ammoDict, playerId)

    comp.weaponItemName = ""
    comp.weaponCustomTips = ""
    comp.weaponExtraId = ""
    comp.bulletType = ""
    comp.currentMagazine = 0
    comp.ammoReserve = 0


# ==================== 方块破坏时掉落武器 ====================


def _onBlockRemove(args):
    # type: (dict) -> None
    """方块破坏时掉落装备的武器 + 所有弹药（对齐主包 MillstoneInteraction._onBlockRemove）"""
    if args.get("fullName") != SENTRY_ARM_BLOCK:
        return

    blockPos = (args["x"], args["y"], args["z"])
    dimensionId = args.get("dimension")

    world = _getServerWorld()
    if not world:
        return
    entity = world.getEntityByPos(blockPos, dimensionId)
    if not entity:
        return
    comp = entity.getComponent("SentryArmComponent")
    if not comp:
        return

    dropPos = (blockPos[0] + 0.5, blockPos[1] + 0.5, blockPos[2] + 0.5)

    # 掉落武器（弹匣内子弹通过 extraId 保留在枪上）
    if comp.weaponItemName:
        mag = int(comp.currentMagazine or 0)
        weaponDict = {
            "newItemName": comp.weaponItemName,
            "count": 1,
            "newAuxValue": 0,
            "extraId": str(mag),
        }
        if comp.weaponCustomTips:
            weaponDict["customTips"] = comp.weaponCustomTips
        GameSystem.CreateEngineItemEntity(weaponDict, dimensionId, dropPos)

    # 掉落库存弹药（ammoReserve 单独，弹匣已随枪一起）
    reserve = int(comp.ammoReserve or 0)
    if comp.bulletType and reserve > 0:
        ammoDict = {
            "newItemName": comp.bulletType,
            "count": reserve,
            "newAuxValue": 0,
        }
        GameSystem.CreateEngineItemEntity(ammoDict, dimensionId, dropPos)


# 通过主包 EventRegistry 订阅 BlockRemoveServerEvent 分发
# 主包 ModServerSystem.onBlockRemove 先 dispatch 再 removeBlockEntity，
# 此刻 Entity 仍存在，可读 weaponItemName
@regModLoadFinishHandler
def _registerBlockRemoveHandler():
    EventRegistry = serverApi.ImportModule(_MAIN_PACK + ".Content.Server.EventRegistry")
    if EventRegistry and hasattr(EventRegistry, "registerHandler"):
        EventRegistry.registerHandler("BlockRemoveServerEvent")(_onBlockRemove)
