# -*- coding: utf-8 -*-
"""
SentryArmTargeting - 哨戒臂目标扫描 + 瞄准 + 射击（服务端）

状态机：IDLE → SCANNING → AIMING → LOCKED → 射击 → COOLDOWN → SCANNING → ...

通过 EpApiServer.Shoot() 执行射击，支持枪械全属性（伤害/射速/霰弹/暴击/音效）。
"""

import math

from ...QuModLibs.Server import serverApi

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
_MAIN_PACK = "arrisCreateScripts"
_EP_PACK = "EpJxkScript"

compFactory = serverApi.GetEngineCompFactory()
levelId = serverApi.GetLevelId()
gameComp = compFactory.CreateGame(levelId)

# 状态常量
IDLE = 0
SCANNING = 1
AIMING = 2
LOCKED = 3
COOLDOWN = 4
WAITING_AMMO = 5  # 弹匣空 + 库存空，等动力臂补弹

# 扫描间隔（tick）
SCAN_INTERVAL = 10

# 敌对实体分类（EntityType 位掩码 OR 组合，覆盖原版 + 模组怪物）
# 用 GetEngineType() & mask == mask 判断实体是否属于某类
_ETEnum = serverApi.GetMinecraftEnum().EntityType


def _collectHostileMasks():
    """收集可用的 EntityType 位掩码。部分位在旧 SDK 可能不存在，用 getattr 兜底"""
    names = ("Monster", "Hostile", "Undead", "Zombie", "Skeleton", "Arthropod")
    masks = []
    for name in names:
        mask = getattr(_ETEnum, name, None)
        if mask is not None:
            masks.append(mask)
    return tuple(masks)


_HOSTILE_MASKS = _collectHostileMasks()

# 缓存
_scanCooldowns = {}  # ecsEntityId -> remainingTicks
_trackedTargets = {}  # ecsEntityId -> targetEntityId
_fireCooldowns = {}  # ecsEntityId -> remainingTicks（射击冷却）
_gunInfoCache = {}  # ecsEntityId -> gunInfo dict（缓存枪械属性）
_serverAimBase = {}  # ecsEntityId -> 服务端跟踪的当前偏航角（和客户端 lerp 同步）
_serverAimHead = {}  # ecsEntityId -> 服务端跟踪的当前俯仰角
# 注：弹药状态改用 SentryArmComponent.currentMagazine / ammoReserve（persistent 持久化）

# 瞄准对齐阈值（角度偏差小于此值才允许射击）
AIM_THRESHOLD_DEG = 5.0
# 服务端 lerp 速度（对齐客户端: min(256, |rpm|) / 1024）
SERVER_LERP_BASE = 1.0 / 1024.0

_serverWorld = None
_epApi = None
_epApiChecked = False


def _getServerWorld():
    global _serverWorld
    if _serverWorld is not None:
        return _serverWorld
    SW = serverApi.ImportModule(_MAIN_PACK + ".Content.Server.ServerWorld")
    if SW:
        _serverWorld = SW.ServerWorld()
    return _serverWorld


def _getEpApiServer():
    global _epApi, _epApiChecked
    if not _epApiChecked:
        mod = serverApi.ImportModule(_EP_PACK + ".Api.EpApiServer")
        if mod:
            _epApi = getattr(mod, "epApiServer", None)
        _epApiChecked = True
    return _epApi


def _onServerTick(args=None):
    world = _getServerWorld()
    if not world:
        return
    for entity in world.getAllEntities():
        if entity.blockName != SENTRY_ARM_BLOCK:
            continue
        _tickSentryArm(entity)


def _tickSentryArm(entity):
    # type: (object) -> None
    comp = entity.getComponent("SentryArmComponent")
    netComp = entity.getComponent("NetworkComponent")
    if not comp or not netComp:
        return

    rpm = netComp.theoreticalSpeed if netComp else 0.0
    hasWeapon = bool(comp.weaponItemName)

    # 前置条件（注：currentMagazine/ammoReserve 是 persistent 字段，由装卸枪路径管理，
    # 这里临时失效不清，恢复后继续用原弹药）
    if not hasWeapon or comp.redstoneLocked or rpm == 0 or netComp.overStressed:
        if comp.state != IDLE:
            comp.state = IDLE
            comp.hasTarget = False
            _trackedTargets.pop(entity.id, None)
            _fireCooldowns.pop(entity.id, None)
            _gunInfoCache.pop(entity.id, None)
        return

    state = comp.state

    # AIMING/LOCKED/COOLDOWN 时验证目标有效（WAITING_AMMO 不需要目标）
    if state in (AIMING, LOCKED, COOLDOWN):
        trackedId = _trackedTargets.get(entity.id)
        if not trackedId or not _isEntityAlive(trackedId):
            comp.state = IDLE
            comp.hasTarget = False
            _trackedTargets.pop(entity.id, None)
            _fireCooldowns.pop(entity.id, None)
            state = IDLE

    if state in (IDLE, SCANNING):
        _tickScanning(entity, comp)
    elif state == AIMING:
        _tickAiming(entity, comp)
    elif state == LOCKED:
        _tickShooting(entity, comp)
    elif state == COOLDOWN:
        _tickCooldown(entity, comp)
    elif state == WAITING_AMMO:
        _tickWaitingAmmo(entity, comp)


# ==================== 扫描 ====================


def _tickScanning(entity, comp):
    # type: (object, object) -> None
    eid = entity.id
    cd = _scanCooldowns.get(eid, 0) - 1
    if cd > 0:
        _scanCooldowns[eid] = cd
        return
    _scanCooldowns[eid] = SCAN_INTERVAL

    targetId = _findNearestHostile(entity, comp.scanRange)
    if targetId:
        targetPos = _getEntityCenter(targetId)
        if targetPos:
            comp.targetX = float(targetPos[0])
            comp.targetY = float(targetPos[1])
            comp.targetZ = float(targetPos[2])
            comp.hasTarget = True
            comp.state = AIMING
            _trackedTargets[entity.id] = targetId
            # 缓存枪械信息（首次瞄准时查一次）
            _cacheGunInfo(entity.id, comp)
            return

    comp.state = IDLE
    comp.hasTarget = False
    _trackedTargets.pop(entity.id, None)


# ==================== 瞄准（追踪目标 + 等待锁定）====================


def _tickAiming(entity, comp):
    # type: (object, object) -> None
    """追踪目标位置，几 tick 后锁定（模拟瞄准时间）"""
    targetId = _trackedTargets.get(entity.id)
    if not targetId:
        comp.state = SCANNING
        return

    targetPos = _getEntityCenter(targetId)
    if not targetPos or not _isInRange(entity, comp, targetPos):
        comp.state = SCANNING
        comp.hasTarget = False
        _trackedTargets.pop(entity.id, None)
        return

    comp.targetX = float(targetPos[0])
    comp.targetY = float(targetPos[1])
    comp.targetZ = float(targetPos[2])

    # 直接进入 LOCKED（瞄准速度由客户端 lerp 控制视觉，服务端不延迟）
    comp.state = LOCKED


# ==================== 射击 ====================


def _tickShooting(entity, comp):
    # type: (object, object) -> None
    """LOCKED 状态：执行射击 → 进入冷却"""
    targetId = _trackedTargets.get(entity.id)
    if not targetId:
        comp.state = SCANNING
        return

    # 实时更新目标位置
    targetPos = _getEntityCenter(targetId)
    if not targetPos or not _isInRange(entity, comp, targetPos):
        comp.state = SCANNING
        comp.hasTarget = False
        _trackedTargets.pop(entity.id, None)
        return

    comp.targetX = float(targetPos[0])
    comp.targetY = float(targetPos[1])
    comp.targetZ = float(targetPos[2])

    # 检查瞄准是否对齐目标（角度偏差小于阈值才开火）
    if not _isAimAligned(entity, targetPos):
        return  # 还没瞄准到位，等下一 tick

    # 获取枪械信息（武器名变化时强制刷新，支持运行时换枪）
    gunInfo = _gunInfoCache.get(entity.id)
    if not gunInfo or gunInfo.get("name") != comp.weaponItemName:
        _cacheGunInfo(entity.id, comp)
        gunInfo = _gunInfoCache.get(entity.id)
    if not gunInfo:
        return

    # 弹药前置：currentMagazine == 0 时尝试从库存补满（初次装枪 / 换枪后）
    if int(comp.currentMagazine or 0) <= 0:
        reserve = int(comp.ammoReserve or 0)
        if reserve > 0:
            magCap = max(1, int(gunInfo.get("magazine", 30)))
            transfer = min(magCap, reserve)
            comp.currentMagazine = transfer
            comp.ammoReserve = reserve - transfer
            # 装填耗时 + 播放上弹音效
            reloadSec = float(gunInfo.get("reloadEmptyTick", 2.0))
            _fireCooldowns[entity.id] = max(1, int(reloadSec * 30.0))
            comp.state = COOLDOWN
            _playReloadSound(entity, gunInfo)
            return
        else:
            # 无弹药可用 → 等补给
            comp.state = WAITING_AMMO
            comp.hasTarget = False
            return

    # 执行射击
    api = _getEpApiServer()
    if not api:
        return

    # 枪口位置（对齐客户端 _updateAimAngles：ceiling 时枪口在方块底部附近）
    pos = entity.blockPos
    facingComp = entity.getComponent("SixFacingComponent")
    ceiling = facingComp and facingComp.facing == 0
    armY = pos[1] + 0.5 + (-1.0 if ceiling else 1.0)
    shooterPos = (pos[0] + 0.5, armY, pos[2] + 0.5)
    api.Shoot(
        shooterPos=shooterPos,
        targetPos=targetPos,
        gunInfo=gunInfo,
        dimensionId=entity.dimensionId,
        shooterEntityId=None,
        playSound=True,
    )

    # 扣减弹药（persistent 字段 comp.currentMagazine）
    comp.currentMagazine = int(comp.currentMagazine or 0) - 1
    ammo = comp.currentMagazine
    magazine = max(1, int(gunInfo.get("magazine", 30)))

    # 计算冷却（对齐 Eplus 玩家射击 fireSpeed/boltSpeed/reload 完整周期）
    fireSpeed = int(gunInfo.get("fireSpeed", 4))
    boltSpeed = int(gunInfo.get("boltSpeed", 0))
    fireType = gunInfo.get("fireType", 0)
    # 每发之间的基础冷却（fireType 决定栓动/点射/自动）
    if fireType == 1:
        shotCooldown = fireSpeed + boltSpeed  # 栓动：击发 + 栓动
    elif fireType == 2:
        shotCooldown = fireSpeed * max(1, int(gunInfo.get("shootCount", 1)))  # 点射
    else:
        shotCooldown = fireSpeed  # 自动

    if ammo <= 0:
        reserve = int(comp.ammoReserve or 0)
        if reserve > 0:
            # 弹匣空 + 库存有货 → 换弹冷却（reloadEmptyTick 秒 × 30 tick/秒），冷却结束弹匣回满
            reloadSec = float(gunInfo.get("reloadEmptyTick", 2.0))
            cooldown = shotCooldown + int(reloadSec * 30.0)
            # 冷却结束的弹匣回填在 _tickCooldown 里处理
            _playReloadSound(entity, gunInfo)  # 上弹音效
        else:
            # 库存也空 → 转 WAITING_AMMO 等动力臂补给，不进入 COOLDOWN
            _fireCooldowns.pop(entity.id, None)
            comp.state = WAITING_AMMO
            comp.hasTarget = False
            return
    else:
        cooldown = shotCooldown

    _fireCooldowns[entity.id] = max(1, cooldown)
    comp.state = COOLDOWN


# ==================== 冷却 ====================


def _tickCooldown(entity, comp):
    # type: (object, object) -> None
    """射击冷却：等待 fireSpeed tick 后回到 LOCKED（继续射击）或 SCANNING"""
    eid = entity.id
    cd = _fireCooldowns.get(eid, 0) - 1
    if cd > 0:
        _fireCooldowns[eid] = cd
        # 冷却期间仍更新目标位置（保持瞄准追踪）
        targetId = _trackedTargets.get(eid)
        if targetId:
            targetPos = _getEntityCenter(targetId)
            if targetPos:
                comp.targetX = float(targetPos[0])
                comp.targetY = float(targetPos[1])
                comp.targetZ = float(targetPos[2])
        return

    _fireCooldowns.pop(eid, None)

    # 弹匣空 → 尝试从库存补弹（换弹冷却刚结束）
    if int(comp.currentMagazine or 0) <= 0:
        reserve = int(comp.ammoReserve or 0)
        if reserve > 0:
            gunInfo = _gunInfoCache.get(eid)
            magCap = max(1, int(gunInfo.get("magazine", 30))) if gunInfo else 30
            transfer = min(magCap, reserve)
            comp.currentMagazine = transfer
            comp.ammoReserve = reserve - transfer
        # 若 reserve 冷却期内被玩家用动力臂取空 → 转 WAITING
        else:
            comp.state = WAITING_AMMO
            comp.hasTarget = False
            _trackedTargets.pop(eid, None)
            return

    # 冷却结束 → 目标还活着就继续射击，否则重新扫描
    targetId = _trackedTargets.get(eid)
    if targetId and _isEntityAlive(targetId):
        comp.state = LOCKED  # 下一 tick 继续射击
    else:
        comp.state = SCANNING
        comp.hasTarget = False
        _trackedTargets.pop(eid, None)


# ==================== 等待弹药 ====================


def _tickWaitingAmmo(entity, comp):
    # type: (object, object) -> None
    """弹匣空 + 库存空：不动，等动力臂补弹。有货即转 SCANNING（带换弹冷却 + 音效）"""
    if int(comp.ammoReserve or 0) > 0:
        # 有货 → 先从库存补弹匣，再进入换弹冷却（不立刻 SCANNING，保持上弹动画/音效感）
        gunInfo = _gunInfoCache.get(entity.id)
        magCap = max(1, int(gunInfo.get("magazine", 30))) if gunInfo else 30
        transfer = min(magCap, int(comp.ammoReserve))
        comp.currentMagazine = transfer
        comp.ammoReserve = int(comp.ammoReserve) - transfer
        # 换弹冷却 + 音效
        if gunInfo:
            reloadSec = float(gunInfo.get("reloadEmptyTick", 2.0))
            _fireCooldowns[entity.id] = max(1, int(reloadSec * 30.0))
            _playReloadSound(entity, gunInfo)
            comp.state = COOLDOWN
        else:
            comp.state = SCANNING


# ==================== 瞄准对齐检测 ====================


def _isAimAligned(entity, targetPos):
    # type: (object, tuple) -> bool
    """
    检查服务端跟踪的瞄准角度是否已对齐目标。
    用和客户端相同的 lerp 公式跟踪角度，偏差小于阈值才返回 True。
    """
    pos = entity.blockPos
    facingComp = entity.getComponent("SixFacingComponent")
    ceiling = facingComp and facingComp.facing == 0
    armX = pos[0] + 0.5
    armY = pos[1] + 0.5 + (-1.0 if ceiling else 1.0)
    armZ = pos[2] + 0.5

    dx = targetPos[0] - armX
    dy = targetPos[1] - armY
    dz = targetPos[2] - armZ

    # 目标角度（和客户端公式一致）
    targetBase = -math.atan2(dx, dz) * (180.0 / math.pi)
    if ceiling:
        dy = -dy
        targetBase = -targetBase
    horizDist = math.sqrt(dx * dx + dz * dz)
    targetHead = -math.atan2(dy, horizDist) * (180.0 / math.pi) if horizDist > 0.01 else 0.0

    # 服务端 lerp 跟踪（和客户端同步）
    netComp = entity.getComponent("NetworkComponent")
    rpm = abs(netComp.theoreticalSpeed) if netComp else 0.0
    lerpSpeed = min(256.0, rpm) * SERVER_LERP_BASE

    eid = entity.id
    curBase = _serverAimBase.get(eid, targetBase)
    curHead = _serverAimHead.get(eid, targetHead)
    # 偏航角走最短路径（经 ±180 分界线不绕远路）
    baseDelta = (targetBase - curBase + 180.0) % 360.0 - 180.0
    curBase += baseDelta * lerpSpeed
    if curBase > 180.0:
        curBase -= 360.0
    elif curBase < -180.0:
        curBase += 360.0
    curHead += (targetHead - curHead) * lerpSpeed
    _serverAimBase[eid] = curBase
    _serverAimHead[eid] = curHead

    # 偏差检测（用最短环绕距离）
    baseDiff = abs((targetBase - curBase + 180.0) % 360.0 - 180.0)
    headDiff = abs(targetHead - curHead)

    return baseDiff < AIM_THRESHOLD_DEG and headDiff < AIM_THRESHOLD_DEG


# ==================== 工具函数 ====================


def _playReloadSound(entity, gunInfo):
    # type: (object, dict) -> None
    """换弹音效：播放 reloadSound[1]（空弹换弹），fallback reloadSound[0]"""
    if not gunInfo:
        return
    sounds = gunInfo.get("reloadSound", [])
    if not sounds:
        return
    soundName = sounds[1] if len(sounds) > 1 and sounds[1] else (sounds[0] if sounds[0] else "")
    if not soundName:
        return
    pos = entity.blockPos
    try:
        compFactory.CreateCommand(levelId).SetCommand(
            "playsound {} @a {} {} {} 1.0 1.0 32".format(
                soundName, pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5
            )
        )
    except Exception:
        pass


def _cacheGunInfo(entityId, comp):
    # type: (str, object) -> None
    """
    缓存枪械信息。
    通过 Eplus 客户端系统的 GetEplisItemData 获取配件加成后的完整数据
    （包含正确的 shootSound/damage/spread 等）。
    """
    weaponName = comp.weaponItemName
    if not weaponName:
        return

    # 构造 itemDict（对齐 GetEplisItemData 的输入格式）
    itemDict = {
        "newItemName": weaponName,
        "customTips": comp.weaponCustomTips or "",
        "extraId": comp.weaponExtraId or "",
    }

    # 通过 Eplus 客户端系统获取完整枪械数据（含配件加成）
    import mod.client.extraClientApi as clientApi

    epSystem = clientApi.GetSystem(_EP_PACK, "EpJxkScriptClientSystem")
    if not epSystem:
        # fallback: 用 EpApiClient.GetGunInfo（无配件加成）
        mod = serverApi.ImportModule(_EP_PACK + ".Api.EpApiClient")
        if mod:
            instance = getattr(mod, "epApiClient", None)
            if instance:
                info = instance.GetGunInfo(weaponName)
                if info:
                    _gunInfoCache[entityId] = info
        return

    allData = epSystem.GetEplisItemData(itemDict)
    if not allData or "data" not in allData:
        return

    d = allData["data"]
    shootSound = d.get("shootSound", [])
    shootSoundX = d.get("shootSoundX", [])
    hasShootX = d.get("shootX", False)
    # 使用消音版音效（如果有）
    soundList = shootSoundX if hasShootX and shootSoundX else shootSound

    _gunInfoCache[entityId] = {
        "name": weaponName,
        "damage": d.get("danger", 0),
        "fireSpeed": d.get("fireSpeed", 4),
        "boltSpeed": d.get("boltSpeed", 0),
        "shootCount": d.get("shootCount", 1),
        "fireType": d.get("fireType", 0),
        "magazine": d.get("magazine", 30),
        "reloadEmptyTick": d.get("reloadEmptyTick", 2.0),  # 秒
        "reloadTacticalTick": d.get("reloadTacticalTick", 2.0),  # 秒
        "dangerType": d.get("dangerType", "projectile"),  # 配件特殊弹种（如 fire）
        "reloadSound": d.get("reloadSound", []),  # [tactical, empty] 换弹音效
        "bulletSpeed": d.get("bulletSpeed", 100),
        "useBullet": d.get("useBullet", ""),
        "count": d.get("count", 1),
        "spread": d.get("spread", 0),
        "distance": d.get("distance", 100),
        "crit": d.get("crit", 0),
        "critDamage": d.get("critDabger", 1.5),
        "shootSound": soundList,
        "fireFlash": d.get("fire_flash", ""),
        "hitPartic": d.get("hitPartic", ""),  # 命中特效（爆炸弹/龙息弹等，配件加成后值）
        "fireParts": d.get("fireParts", ""),  # 弹道特效（龙息弹等）
    }


def _isInRange(entity, comp, targetPos):
    # type: (object, object, tuple) -> bool
    pos = entity.blockPos
    armCenter = (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)
    dx = targetPos[0] - armCenter[0]
    dy = targetPos[1] - armCenter[1]
    dz = targetPos[2] - armCenter[2]
    maxDist = comp.scanRange + 2
    return dx * dx + dy * dy + dz * dz <= maxDist * maxDist


def _findNearestHostile(entity, scanRange):
    # type: (object, int) -> str | None
    pos = entity.blockPos
    dimId = entity.dimensionId
    r = int(scanRange)
    startPos = (pos[0] - r, pos[1] - r, pos[2] - r)
    endPos = (pos[0] + r + 1, pos[1] + r + 1, pos[2] + r + 1)

    entityIds = gameComp.GetEntitiesInSquareArea(None, startPos, endPos, dimId)
    if not entityIds:
        return None

    armCenter = (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)
    bestEntity = None
    bestDistSq = float("inf")

    for eid in entityIds:
        typeComp = compFactory.CreateEngineType(eid)
        if not typeComp:
            continue
        engType = typeComp.GetEngineType()
        if engType is None:
            continue
        # 位掩码 OR 组合：Monster/Hostile/Undead/Zombie/Skeleton/Arthropod 任一命中
        if not any((engType & m) == m for m in _HOSTILE_MASKS):
            continue
        if not _isEntityAlive(eid):
            continue
        ePos = compFactory.CreatePos(eid).GetFootPos()
        if not ePos:
            continue
        dx = ePos[0] - armCenter[0]
        dy = ePos[1] - armCenter[1]
        dz = ePos[2] - armCenter[2]
        distSq = dx * dx + dy * dy + dz * dz
        if distSq < bestDistSq:
            bestDistSq = distSq
            bestEntity = eid

    return bestEntity


def _isEntityAlive(entityId):
    # type: (str) -> bool
    attrComp = compFactory.CreateAttr(entityId)
    if not attrComp:
        return False
    health = attrComp.GetAttrValue(0)
    return health is not None and health > 0


def _getEntityCenter(entityId):
    # type: (str) -> tuple | None
    """获取实体瞄准位置（脚底 + 碰撞盒高度/2 = 身体中点）"""
    posComp = compFactory.CreatePos(entityId)
    if not posComp:
        return None
    footPos = posComp.GetFootPos()
    if not footPos:
        return None
    # 获取碰撞盒高度，瞄准中点
    collComp = compFactory.CreateCollisionBox(entityId)
    halfHeight = 0.5  # 默认回退
    if collComp:
        size = collComp.GetSize()
        if size and len(size) >= 2:
            halfHeight = size[1] / 2.0  # size = (width, height)
    return (footPos[0], footPos[1] + halfHeight, footPos[2])
