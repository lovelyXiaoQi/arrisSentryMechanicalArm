# -*- coding: utf-8 -*-
"""
SentryArmInteraction - 哨戒臂枪械装备交互（服务端）

玩家手持枪械右键哨戒臂 → 装备枪械
空手右键 → 取出枪械
手持匹配弹药（任意等级）→ 装填备弹
通过 Eplus 军械库 EpApiClient.IsGun 判断枪械（bind 变体枪走合并数据兜底）

EP+ 新版数据适配（详见 Shared/SentryArmEpCompat）:
- 枪械物品 userData（bullet_list 逐发等级 / bullet_priority / ep_skin）在装备时
  快照进 Component、取出/掉落时原样写回，弹药等级与皮肤随枪走不丢失
- bind 变体枪（so14 / holger26 / m4a1_ziptie 等）自身 JSON 无 type/useBullet，
  IsGun 与 GetGunInfo 原生路径拿不到，必须按 bind 合并语义兜底
"""

from ...QuModLibs.Server import AllowCall, InjectHttpPlayerId, regModLoadFinishHandler, serverApi
from ...QuModLibs.Server import System as GameSystem
from ..Shared import SentryArmEpCompat as EpCompat
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


def _getEpBullet():
    # type: () -> object | None
    """EP 子弹等级数据模块（server 侧统一缓存在 SentryArmTargeting）"""
    from . import SentryArmTargeting

    return SentryArmTargeting._getEpBullet()


def _isGun(itemName):
    # type: (str) -> bool
    """通过 Eplus 军械库 API 判断物品是否为枪械（含 bind 变体枪兜底）"""
    if not itemName:
        return False
    return EpCompat.isGunWithBind(_getEpApiInstance(), itemName)


def _resolveBulletType(itemName):
    # type: (str) -> str
    """
    获取枪械接受的弹药物品 ID（基于基础 useBullet — 配件不改此字段，已验证；
    bind 变体枪按合并数据兜底，否则 so14 等会拿到空串导致永远无法装填）
    """
    if not itemName:
        return ""
    info = EpCompat.getGunInfoWithBind(_getEpApiInstance(), itemName)
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

    # 3.5 手持本枪可用弹药（任意等级）→ 装填备弹
    #     等级/同名门禁统一在 RuntimePoint.insert 里，拒收时 accepted=0 不动手持
    if comp.weaponItemName and comp.bulletType and EpCompat.isBulletAccepted(
        _getEpBullet(), comp.bulletType, itemName
    ):
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
    comp.reserveBulletType = ""

    # userData 快照 + 弹匣逐发等级（EP bullet_list 数字串，规整到弹匣数长度）
    userData = carriedItem.get("userData") or {}
    comp.weaponUserData = EpCompat.dumpUserData(userData)
    rawBulletList = EpCompat.userDataValue(userData, "bullet_list", "") or ""
    comp.magazineBulletList = EpCompat.magListToStr(
        EpCompat.parseMagList(rawBulletList, comp.currentMagazine)
    )

    # 7. 清空玩家手持物品
    selectSlot = compFactory.CreateItem(playerId).GetSelectSlotId()
    itemComp.SetInvItemNum(selectSlot, 0)


def _buildWeaponItemDict(comp):
    # type: (object) -> dict
    """按 Component 状态构造返还/掉落的枪械物品字典。
    弹匣数写 extraId、逐发等级写回 userData['bullet_list']（对齐 EP 服务端
    ReloadBullet 的裸字符串写法），其余 userData 条目原样保留。"""
    mag = int(comp.currentMagazine or 0)
    weaponDict = {
        "newItemName": comp.weaponItemName,
        "count": 1,
        "newAuxValue": 0,
        "extraId": str(mag),
    }
    if comp.weaponCustomTips:
        weaponDict["customTips"] = comp.weaponCustomTips
    userData = EpCompat.loadUserData(getattr(comp, "weaponUserData", ""))
    magList = getattr(comp, "magazineBulletList", "") or ""
    if userData or magList:
        userData["bullet_list"] = EpCompat.magListToStr(EpCompat.parseMagList(magList, mag))
        weaponDict["userData"] = userData
    return weaponDict


def _buildReserveItemDict(comp):
    # type: (object) -> dict | None
    """库存弹药的返还/掉落字典。物品名 = 实际存放的等级，不降级。"""
    reserve = int(comp.ammoReserve or 0)
    if not comp.bulletType or reserve <= 0:
        return None
    return {
        "newItemName": getattr(comp, "reserveBulletType", "") or comp.bulletType,
        "count": reserve,
        "newAuxValue": 0,
    }


def _clearWeaponState(comp):
    # type: (object) -> None
    """清空武器与弹药相关的全部 Component 字段"""
    comp.weaponItemName = ""
    comp.weaponCustomTips = ""
    comp.weaponExtraId = ""
    comp.weaponUserData = ""
    comp.bulletType = ""
    comp.currentMagazine = 0
    comp.ammoReserve = 0
    comp.reserveBulletType = ""
    comp.magazineBulletList = ""


def _removeWeapon(comp, playerId, itemComp):
    # type: (object, str, object) -> None
    """从哨戒臂取出武器 + 库存弹药返还玩家背包；弹匣子弹数/等级写回枪械物品"""
    itemComp.SpawnItemToPlayerInv(_buildWeaponItemDict(comp), playerId)

    # 库存弹药（ammoReserve）单独返还
    ammoDict = _buildReserveItemDict(comp)
    if ammoDict:
        itemComp.SpawnItemToPlayerInv(ammoDict, playerId)

    _clearWeaponState(comp)


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

    # 掉落武器（弹匣子弹数/等级通过 extraId + userData 保留在枪上）
    if comp.weaponItemName:
        GameSystem.CreateEngineItemEntity(_buildWeaponItemDict(comp), dimensionId, dropPos)

    # 掉落库存弹药（ammoReserve 单独，按实际存放等级；弹匣已随枪一起）
    ammoDict = _buildReserveItemDict(comp)
    if ammoDict:
        GameSystem.CreateEngineItemEntity(ammoDict, dimensionId, dropPos)


# 通过主包 EventRegistry 订阅 BlockRemoveServerEvent 分发
# 主包 ModServerSystem.onBlockRemove 先 dispatch 再 removeBlockEntity，
# 此刻 Entity 仍存在，可读 weaponItemName
@regModLoadFinishHandler
def _registerBlockRemoveHandler():
    EventRegistry = serverApi.ImportModule(_MAIN_PACK + ".Content.Server.EventRegistry")
    if EventRegistry and hasattr(EventRegistry, "registerHandler"):
        EventRegistry.registerHandler("BlockRemoveServerEvent")(_onBlockRemove)
