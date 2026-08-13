# -*- coding: utf-8 -*-
"""
SentryArmTargeting - 哨戒臂目标扫描 + 瞄准 + 射击（服务端）

状态机：IDLE → SCANNING → AIMING → LOCKED → 射击 → COOLDOWN → SCANNING → ...

通过 EpApiServer.Shoot() 执行射击，支持枪械全属性（伤害/射速/霰弹/暴击/音效）。
"""

import math

from ...QuModLibs.Server import AllowCall, Call, serverApi
from ..Shared import SentryArmEpCompat as EpCompat
from ..Shared import SentryTargetMatcher as TargetMatcher
from .SentryArmHopperIntake import tickHopperIntake
from .SentryArmPlacement import consumePendingOwner

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

_AttrType = serverApi.GetMinecraftEnum().AttrType


# 敌对生物 type_family 过滤集（行为包字段 minecraft:type_family）。
# 用 set & set 相交判定，比 GetEngineType 位掩码更友好——
# GetEngineType 对自定义/模组实体有限制（默认归类 Mob），但 type_family 由实体 JSON 显式声明。
# 与某实体 family 列表有交集即视为敌对。
#
# 故意不收 piglin / piglin_brute / hoglin 等中立怪——它们不主动攻击玩家，
# 哨戒臂也不应主动锁定。
_HOSTILE_FAMILIES = frozenset(
    [
        "monster",  # 通用敌对（绝大多数原版 + 模组敌对生物）
        "undead",  # 亡灵系（僵尸/骷髅/凋灵/幻翼/僵尸猪灵 等）
        "zombie",  # 僵尸 / 僵尸村民 / 尸壳 / 溺尸 / 僵尸疣猪兽
        "skeleton",  # 骷髅 / 流浪者 / 凋灵骷髅 / 沼骸
        "arthropod",  # 蜘蛛 / 蠹虫 / 末影螨
        "creeper",
        "spider",
        "enderman",
        "ghast",
        "blaze",
        "slime",
        "magma_cube",
        "guardian",
        "elder_guardian",
        "shulker",
        "vex",
        "pillager",
        "illager",
        "vindicator",
        "evocation_illager",
        "witch",
        "ravager",
        "warden",
        "breeze",
        "wither",
        "wither_boss",
        "dragon",
    ]
)

# 敌对豁免名单(优先级高于 _HOSTILE_FAMILIES):
# 即便实体匹配上敌对 family,只要带这些标签也跳过——
#   - "epitem"     : EP 军工里的弹药/特效/掉落物之类的非生物体
#   - "inanimate"  : 模组约定的"无生命/装饰物/雕像"标签,通常不应被攻击
_NON_HOSTILE_FAMILIES = frozenset(["epitem", "inanimate"])

_RayFilterType = serverApi.GetMinecraftEnum().RayFilterType

# 射线穿透白名单：植物 / 树苗 / 珊瑚 / 液体 / 藤蔓 / 泡泡柱 / 细雪等非实体阻挡方块。
# EpApiServer.Shoot 已改为 OnlyEntities 过滤（不再判定方块命中），阻挡判定完全由此处 LOS 兜底。
# 即：只要目标和枪口之间没有"非此列表的方块"，哨戒臂就会开火并稳定造成伤害。
_RAY_PASSTHROUGH_BLOCKS = frozenset(
    [
        "minecraft:fern",
        "minecraft:large_fern",
        "minecraft:short_grass",
        "minecraft:tall_grass",
        "minecraft:short_dry_grass",
        "minecraft:tall_dry_grass",
        "minecraft:bush",
        "minecraft:nether_sprouts",
        "minecraft:fire_coral",
        "minecraft:brain_coral",
        "minecraft:bubble_coral",
        "minecraft:tube_coral",
        "minecraft:horn_coral",
        "minecraft:dead_fire_coral",
        "minecraft:dead_brain_coral",
        "minecraft:dead_bubble_coral",
        "minecraft:dead_tube_coral",
        "minecraft:dead_horn_coral",
        "minecraft:coral_fan",
        "minecraft:coral_fan_dead",
        "minecraft:crimson_roots",
        "minecraft:warped_roots",
        "minecraft:yellow_flower",
        "minecraft:red_flower",
        "minecraft:double_plant",
        "minecraft:pitcher_plant",
        "minecraft:pink_petals",
        "minecraft:wildflowers",
        "minecraft:wither_rose",
        "minecraft:torchflower",
        "minecraft:cactus_flower",
        "minecraft:closed_eyeblossom",
        "minecraft:open_eyeblossom",
        "minecraft:vine",
        "minecraft:weeping_vines",
        "minecraft:twisting_vines",
        "minecraft:seagrass",
        "minecraft:flowing_water",
        "minecraft:water",
        "minecraft:flowing_lava",
        "minecraft:lava",
        "minecraft:bubble_column",
        "minecraft:powder_snow",
        # 树苗
        "minecraft:oak_sapling",
        "minecraft:spruce_sapling",
        "minecraft:birch_sapling",
        "minecraft:jungle_sapling",
        "minecraft:acacia_sapling",
        "minecraft:dark_oak_sapling",
        "minecraft:mangrove_propagule",
        "minecraft:cherry_sapling",
        "minecraft:pale_oak_sapling",
    ]
)

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
_epBullet = None
_epBulletChecked = False
_epEntityArmor = None
_epEntityArmorChecked = False
# 旧存档武器数据（bulletType / magazineSize）自愈只补试一次的实体集合
_weaponHealTried = set()


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


def _getEpBullet():
    # type: () -> object | None
    """EP 子弹等级数据模块（纯数据，EP 双端都会加载；服务端各模块共用此缓存）"""
    global _epBullet, _epBulletChecked
    if not _epBulletChecked:
        _epBullet = serverApi.ImportModule(_EP_PACK + ".modCommon.epBullet")
        _epBulletChecked = True
    return _epBullet


def _getEpEntityArmor():
    # type: () -> object | None
    """EP 实体护甲数据模块（ZOMBIE_ARMOR 枪械护甲表，纯数据）"""
    global _epEntityArmor, _epEntityArmorChecked
    if not _epEntityArmorChecked:
        _epEntityArmor = serverApi.ImportModule(_EP_PACK + ".modCommon.entityArmor")
        _epEntityArmorChecked = True
    return _epEntityArmor


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

    # 放置时暂存的主人信息在实体首个 tick 落盘（放置事件那一刻 ECS 实体尚未创建）
    if not getattr(comp, "ownerId", "") and not getattr(comp, "ownerName", ""):
        owner = consumePendingOwner(entity.blockPos, entity.dimensionId)
        if owner:
            comp.ownerId = owner[0]
            comp.ownerName = owner[1]

    # 原版漏斗供弹（内部按 0.4s 节流；放在武器前置检查之前——
    # 红石锁定 / 停转时也应能补弹，与动力臂交互点行为一致）
    tickHopperIntake(entity, comp)

    # 旧版数据自愈：bind 变体枪（so14 等）曾解析不出 useBullet；旧存档没有
    # magazineSize（容量判定会漂移 → 动力臂吞子弹）。缺任一项都补一次，
    # 每实体每会话只试一次。
    if (
        hasWeapon
        and entity.id not in _weaponHealTried
        and (not comp.bulletType or int(getattr(comp, "magazineSize", 0) or 0) <= 0)
    ):
        _weaponHealTried.add(entity.id)
        from .SentryArmInteraction import _getEpApiInstance

        info = EpCompat.getGunInfoWithBind(_getEpApiInstance(), comp.weaponItemName)
        if info:
            if not comp.bulletType:
                comp.bulletType = info.get("useBullet", "") or ""
            if int(getattr(comp, "magazineSize", 0) or 0) <= 0:
                comp.magazineSize = int(info.get("magazine", 0) or 0)

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
    gunInfo = _getOrCacheGunInfo(entity, comp)
    if not gunInfo:
        return

    # 弹药前置：currentMagazine == 0 时尝试从库存补满（初次装枪 / 换枪后）
    if int(comp.currentMagazine or 0) <= 0:
        if _refillMagazine(comp, gunInfo) > 0:
            # 装填耗时 + 播放上弹音效
            reloadSec = float(gunInfo.get("reloadEmptyTick", 2.0))
            _fireCooldowns[entity.id] = max(1, int(reloadSec * 30.0))
            comp.state = COOLDOWN
            _playReloadSound(entity, gunInfo)
        else:
            # 无弹药可用 → 等补给
            comp.state = WAITING_AMMO
            comp.hasTarget = False
        return

    # 执行射击
    api = _getEpApiServer()
    if not api:
        return

    # 射击前重验:目标被某 mod 转成"尸体"(SPEED=0 / markVariant=999 /
    # 加上 epitem/inanimate family)、或已豁免（锁定期间切创造 / 主人）
    # → 停止射击,放弃目标重回 SCANNING
    targetId = _trackedTargets.get(entity.id)
    if targetId and (not _isStillAttackable(targetId) or _isExemptTarget(comp, targetId)):
        comp.state = SCANNING
        comp.hasTarget = False
        _trackedTargets.pop(entity.id, None)
        _fireCooldowns.pop(entity.id, None)
        return

    # 射线检查：目标被实体方块遮挡 → 放弃当前目标重回 SCANNING
    if not _hasLineOfSight(entity, targetPos):
        comp.state = SCANNING
        comp.hasTarget = False
        _trackedTargets.pop(entity.id, None)
        _fireCooldowns.pop(entity.id, None)
        return

    # 当前发弹种 = 弹匣逐发等级记录的末位（对齐 EP gunFire.GetEpBulletData：
    # 降序数字串从末尾消耗，高级弹优先打出）。
    # 伤害 = 基础伤害 × 等级平射倍率(danger) × 对甲衰减系数——
    # 等级差异主要体现在后者（EP 各等级 danger 几乎相同，armor_penetration 才拉开差距）
    mag = int(comp.currentMagazine or 0)
    magList = EpCompat.parseMagList(getattr(comp, "magazineBulletList", "") or "", mag)
    epBulletMod = _getEpBullet()
    shotIndex = magList[mag - 1] if mag > 0 else 0
    shotBullet = EpCompat.bulletAtIndex(epBulletMod, comp.bulletType, shotIndex)
    damageMult = EpCompat.bulletDamageMultiplier(epBulletMod, shotBullet)
    # 对锁定目标按 EP 枪械护甲 + 枪/子弹穿甲预折算（Shoot 内部不区分目标；
    # 霰弹误中他人时沿用锁定目标的甲伤系数，属可接受近似）
    damageMult *= EpCompat.armorDamageFactor(
        epBulletMod,
        _getEpEntityArmor(),
        shotBullet,
        gunInfo,
        compFactory.CreateEngineType(targetId).GetEngineTypeStr(),
    )
    shootInfo = gunInfo
    if damageMult != 1.0:
        shootInfo = dict(gunInfo)
        shootInfo["damage"] = gunInfo.get("damage", 0) * damageMult

    shooterPos = _computeMuzzle(entity)
    api.Shoot(
        shooterPos=shooterPos,
        targetPos=targetPos,
        gunInfo=shootInfo,
        dimensionId=entity.dimensionId,
        shooterEntityId=None,
        playSound=True,
    )

    # 扣减弹药（persistent 字段 comp.currentMagazine + 逐发等级记录同步弹出）
    comp.currentMagazine = mag - 1 if mag > 0 else 0
    comp.magazineBulletList = EpCompat.magListToStr(magList[: comp.currentMagazine])
    ammo = comp.currentMagazine

    # 计算冷却（对齐 Eplus 玩家射击 fireSpeed/boltSpeed/reload 完整周期）
    # fireSpeed 双语义：<1 = 秒 / >=1 = tick（EP 新版自动枪普遍是秒值，
    # 直接 int() 会截成 0 → 射速失控），统一换算成 tick
    fireSpeed = EpCompat.fireSpeedToTicks(gunInfo.get("fireSpeed", 4))
    boltSpeed = int(float(gunInfo.get("boltSpeed", 0) or 0))
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
        gunInfo = _getOrCacheGunInfo(entity, comp)
        # 补不进 = reserve 冷却期内被玩家用动力臂取空 → 转 WAITING
        if _refillMagazine(comp, gunInfo) <= 0:
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
    if int(comp.ammoReserve or 0) <= 0:
        return
    # 有货 → 先从库存补弹匣，再进入换弹冷却（不立刻 SCANNING，保持上弹动画/音效感）
    gunInfo = _getOrCacheGunInfo(entity, comp)
    if _refillMagazine(comp, gunInfo) <= 0:
        return
    # 换弹冷却 + 音效
    if gunInfo:
        reloadSec = float(gunInfo.get("reloadEmptyTick", 2.0))
        _fireCooldowns[entity.id] = max(1, int(reloadSec * 30.0))
        _playReloadSound(entity, gunInfo)
        comp.state = COOLDOWN
    else:
        comp.state = SCANNING


# ==================== 弹药装填 ====================


def _refillMagazine(comp, gunInfo):
    # type: (object, dict | None) -> int
    """
    从库存（ammoReserve）向弹匣转移弹药，维护逐发等级记录（magazineBulletList）。

    转移的一批全部是库存当前存放的等级（reserveBulletType），与弹匣残留
    子弹按 EP 规范降序合并。返回实际转移发数；库存清空时同步清
    reserveBulletType（允许动力臂换存其他等级）。gunInfo 缺失时容量兜底 30。
    """
    reserve = int(comp.ammoReserve or 0)
    if reserve <= 0:
        return 0
    # 弹匣容量优先读装枪时持久化的 magazineSize（与库存容量判定同源），
    # 旧存档兜底 gunInfo
    magCap = int(getattr(comp, "magazineSize", 0) or 0)
    if magCap <= 0:
        magCap = max(1, int(gunInfo.get("magazine", 30))) if gunInfo else 30
    mag = int(comp.currentMagazine or 0)
    transfer = min(max(0, magCap - mag), reserve)
    if transfer <= 0:
        return 0
    epBulletMod = _getEpBullet()
    reserveBullet = getattr(comp, "reserveBulletType", "") or comp.bulletType
    addIndex = EpCompat.bulletIndexInSequence(epBulletMod, comp.bulletType, reserveBullet)
    magList = EpCompat.parseMagList(getattr(comp, "magazineBulletList", "") or "", mag)
    magList = EpCompat.mergeMagList(magList, addIndex, transfer)
    comp.currentMagazine = mag + transfer
    comp.magazineBulletList = EpCompat.magListToStr(magList)
    comp.ammoReserve = reserve - transfer
    if reserve - transfer <= 0:
        comp.reserveBulletType = ""
    return transfer


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
    """
    换弹音效：老枪播 reloadSound 字段值；EP 3.5x 新枪该字段为空（音效改由
    动画关键帧驱动的分段定义），走 EpCompat.reloadSoundCandidates 生成候选名单，
    客户端逐个试播、命中即停（PlayCustomMusic 对不存在的名字无副作用）。

    这些名字是 sound_definitions 注册名，服务端 /playsound 对部分自定义名
    找不到，所以走 QuMod 的 server→client Call 广播 → 客户端播放。
    """
    if not gunInfo:
        return
    candidates = EpCompat.reloadSoundCandidates(gunInfo)
    if not candidates:
        return
    pos = entity.blockPos
    dimensionId = entity.dimensionId
    cx, cy, cz = pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5
    Call("*", "sentryArmPlayReloadSound", candidates, cx, cy, cz, dimensionId)


_gunInfoRequestTimes = {}  # entityId -> last broadcast timestamp（避免每 tick 狂发请求）
_GUN_INFO_REQUEST_COOLDOWN = 1.0  # 秒


def _cacheGunInfo(entityId, comp):
    # type: (str, object) -> None
    """
    向所有客户端广播请求：本地 EP 客户端系统算出含配件加成的完整枪械数据，
    再通过 sentryArmReportGunInfo RPC 回报到服务端。落入 _gunInfoCache[entityId]。

    异步：首次调用只发请求；实际 cache 会在 1~2 tick 内就位。
    tick 路径对 cache 未命中做 early-return，等到就位再继续。
    """
    weaponName = comp.weaponItemName
    if not weaponName:
        return

    import time

    now = time.time()
    last = _gunInfoRequestTimes.get(entityId, 0.0)
    if now - last < _GUN_INFO_REQUEST_COOLDOWN:
        return
    _gunInfoRequestTimes[entityId] = now

    try:
        from ...QuModLibs.Server import Call

        Call(
            "*",
            "sentryArmFetchGunInfo",
            entityId,
            weaponName,
            comp.weaponCustomTips or "",
            str(comp.weaponExtraId or ""),
        )
    except Exception:
        pass


@AllowCall
def sentryArmReportGunInfo(entityId, gunInfo):
    # type: (str, dict) -> None
    """客户端回报枪械数据 → 写入服务端 cache。"""
    if not isinstance(gunInfo, dict) or not gunInfo.get("name"):
        return
    _gunInfoCache[entityId] = gunInfo


def _getOrCacheGunInfo(entity, comp):
    # type: (object, object) -> dict | None
    """
    取 _gunInfoCache，缺失或武器名变了就当场补填。
    tick 路径统一用这个拿 gunInfo，避免 cache 未命中时静默丢失 reloadSound 等字段。
    """
    gunInfo = _gunInfoCache.get(entity.id)
    if not gunInfo or gunInfo.get("name") != comp.weaponItemName:
        _cacheGunInfo(entity.id, comp)
        gunInfo = _gunInfoCache.get(entity.id)
    return gunInfo


def _isInRange(entity, comp, targetPos):
    # type: (object, object, tuple) -> bool
    pos = entity.blockPos
    armCenter = (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)
    dx = targetPos[0] - armCenter[0]
    dy = targetPos[1] - armCenter[1]
    dz = targetPos[2] - armCenter[2]
    maxDist = comp.scanRange + 2
    return dx * dx + dy * dy + dz * dz <= maxDist * maxDist


def _computeMuzzle(entity):
    # type: (object) -> tuple
    """枪口世界坐标（与 api.Shoot / 客户端 _updateAimAngles 一致：ceiling 时枪口在方块下方）"""
    pos = entity.blockPos
    facingComp = entity.getComponent("SixFacingComponent")
    ceiling = facingComp and facingComp.facing == 0
    armY = pos[1] + 0.5 + (-1.0 if ceiling else 1.0)
    return (pos[0] + 0.5, armY, pos[2] + 0.5)


def _hasLineOfSight(entity, targetPos):
    # type: (object, tuple) -> bool
    """
    枪口 → 目标的射线检测。
    用 serverApi.getEntitiesOrBlockFromRay(OnlyBlocks, isThrough=True) 取沿途全部方块，
    跳过自身哨戒臂方块 + _RAY_PASSTHROUGH_BLOCKS 中的植物/液体，首个"实心"阻挡判距离。
    障碍物在目标之后 → 视为通路。
    """
    muzzle = _computeMuzzle(entity)
    dx = targetPos[0] - muzzle[0]
    dy = targetPos[1] - muzzle[1]
    dz = targetPos[2] - muzzle[2]
    distSq = dx * dx + dy * dy + dz * dz
    if distSq < 0.25:  # 距离 < 0.5
        return True
    dist = math.sqrt(distSq)
    direction = (dx / dist, dy / dist, dz / dist)
    rayLen = int(math.ceil(dist)) + 1

    hits = serverApi.getEntitiesOrBlockFromRay(
        int(entity.dimensionId),
        muzzle,
        direction,
        rayLen,
        True,  # isThrough: 拿到全部 hits，才能跳过自身/透明再判剩下的
        _RayFilterType.OnlyBlocks,
    )
    if not hits:
        return True

    selfBlockPos = tuple(entity.blockPos)
    for hit in hits:
        blockPos = hit.get("pos")
        if blockPos and tuple(blockPos) == selfBlockPos:
            continue
        name = hit.get("identifier", "")
        if name in _RAY_PASSTHROUGH_BLOCKS:
            continue
        hitPos = hit.get("hitPos")
        if hitPos:
            hx = hitPos[0] - muzzle[0]
            hy = hitPos[1] - muzzle[1]
            hz = hitPos[2] - muzzle[2]
            if hx * hx + hy * hy + hz * hz >= distSq:
                return True  # 首个实心障碍在目标之后 → 视线通畅
        return False
    return True


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

    # 索敌模式判定（一次读取 SentryArmComponent 给后续循环复用）
    sentryComp = entity.getComponent("SentryArmComponent")
    mode = int(getattr(sentryComp, "targetMode", 0) or 0) if sentryComp else 0
    # 自定义模式：customTargets 预编译成 (正向, 取反) 匹配条目，
    # 支持 * 通配 / ! 取反（语义见 Shared/SentryTargetMatcher）
    customCompiled = None
    if mode == 1:
        customRaw = getattr(sentryComp, "customTargets", "") or ""
        customCompiled = TargetMatcher.compileTargets(customRaw)
        if not customCompiled[0] and not customCompiled[1]:
            # 自定义模式但列表空 → 不索敌（行为同 IDLE）
            return None

    # 主人豁免键（一次读取给循环复用）
    ownerId = (getattr(sentryComp, "ownerId", "") or "") if sentryComp else ""
    ownerName = (getattr(sentryComp, "ownerName", "") or "") if sentryComp else ""

    armCenter = (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)
    candidates = []  # [(distSq, eid)]

    for eid in entityIds:
        if ownerId and str(eid) == ownerId:
            continue  # 主人豁免（运行时 id，同会话内可靠）
        attrComp = compFactory.CreateAttr(eid)
        if not attrComp:
            continue
        # 敌对判定按模式分流
        if mode == 1:
            # CUSTOM: 匹配键只用两个接口——GetEngineTypeStr(实体ID) +
            # GetName(玩家名/命名牌名，所有实体统一取)，
            # 精确条目 / * 通配 / ! 取反统一走 TargetMatcher
            typeStr = compFactory.CreateEngineType(eid).GetEngineTypeStr()
            if not typeStr:
                continue
            try:
                entityName = compFactory.CreateName(eid).GetName() or ""
            except Exception:
                entityName = ""
            if typeStr == "minecraft:player":
                # 主人（跨会话按名字）与创造模式玩家无条件豁免，
                # 优先级高于自定义规则（包括纯取反的"打一切"列表）
                if ownerName and entityName == ownerName:
                    continue
                if _isCreativePlayer(eid):
                    continue
            if not TargetMatcher.matchTarget(customCompiled, typeStr, entityName):
                continue
        else:
            # DEFAULT: 实体 type_family 与 _HOSTILE_FAMILIES 有交集即敌对。
            # type_family 由实体行为包 JSON 显式声明，对自定义 / 模组实体也可靠
            # （比 GetEngineType 位掩码更稳，自定义实体常被引擎默认归类为 Mob）。
            families = attrComp.GetTypeFamily()
            if not families:
                continue
            familySet = set(families)
            # 豁免名单 (epitem / inanimate 等) 优先级高于敌对 family
            if familySet & _NON_HOSTILE_FAMILIES:
                continue
            if not (familySet & _HOSTILE_FAMILIES):
                continue
        # 健康 > 0
        health = attrComp.GetAttrValue(_AttrType.HEALTH)
        if health is None or health <= 0:
            continue
        # SPEED == 0 通常是 mod 的"尸体"残留（免伤打不死），跳过
        speed = attrComp.GetAttrValue(_AttrType.SPEED)
        if speed is None or speed <= 0:
            continue
        # mark_variant == 999 是部分 mod 用于标记"尸体"实体的约定值，免伤无法击杀，跳过
        markVariant = compFactory.CreateEntityDefinitions(eid).GetMarkVariant() or 0
        if markVariant == 999:
            continue
        ePos = compFactory.CreatePos(eid).GetFootPos()
        if not ePos:
            continue
        dx = ePos[0] - armCenter[0]
        dy = ePos[1] - armCenter[1]
        dz = ePos[2] - armCenter[2]
        candidates.append((dx * dx + dy * dy + dz * dz, eid))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    # 由近到远 LOS 检查，第一个可见的即为目标；全被遮挡则放弃本轮扫描
    for _, eid in candidates:
        targetCenter = _getEntityCenter(eid)
        if not targetCenter:
            continue
        if _hasLineOfSight(entity, targetCenter):
            return eid
    return None


def _isEntityAlive(entityId):
    # type: (str) -> bool
    attrComp = compFactory.CreateAttr(entityId)
    if not attrComp:
        return False
    health = attrComp.GetAttrValue(_AttrType.HEALTH)
    return health is not None and health > 0


def _isCreativePlayer(playerId):
    # type: (str) -> bool
    """创造模式玩家判定（GetPlayerGameType: 1 = 创造），只对玩家 id 调用"""
    return gameComp.GetPlayerGameType(playerId) == 1


def _isExemptTarget(comp, entityId):
    # type: (object, str) -> bool
    """
    主人 / 创造模式玩家 豁免复检（扫描阶段已过滤，这里覆盖锁定期间
    才出现的变化：目标切创造、旧存档目标恰为主人等）。
    """
    ownerId = getattr(comp, "ownerId", "") or ""
    if ownerId and str(entityId) == ownerId:
        return True
    typeStr = compFactory.CreateEngineType(entityId).GetEngineTypeStr()
    if typeStr != "minecraft:player":
        return False
    ownerName = getattr(comp, "ownerName", "") or ""
    if ownerName:
        try:
            name = compFactory.CreateName(entityId).GetName() or ""
        except Exception:
            name = ""
        if name == ownerName:
            return True
    return _isCreativePlayer(entityId)


def resetTargeting(entityId, comp):
    # type: (str, object) -> None
    """
    索敌配置被外部改动（登记板应用/清空）后立刻重置状态机：
    丢弃当前目标、射击冷却与扫描间隔，下一 tick 按新配置重新扫描。
    瞄准角度跟踪（_serverAimBase/Head）保留——臂从当前朝向自然转向新目标。
    """
    _trackedTargets.pop(entityId, None)
    _fireCooldowns.pop(entityId, None)
    _scanCooldowns.pop(entityId, None)
    comp.state = IDLE
    comp.hasTarget = False


def _isStillAttackable(entityId):
    # type: (str) -> bool
    """
    锁定后 / 射击前的"仍可攻击"复检。
    返回 False → 应停止攻击,放弃目标重回 SCANNING。

    检查项 (与扫描时的 _findNearestHostile 过滤对齐):
    - SPEED > 0      —— 部分模组把"尸体"实体的 SPEED 设为 0
    - markVariant != 999 —— 部分模组用 999 标记免伤"尸体"
    - type_family 不含 _NON_HOSTILE_FAMILIES (epitem / inanimate 等)
    HEALTH 由 _isEntityAlive 单独检 (这里不重复)。
    """
    attrComp = compFactory.CreateAttr(entityId)
    if not attrComp:
        return False
    # SPEED
    speed = attrComp.GetAttrValue(_AttrType.SPEED)
    if speed is None or speed <= 0:
        return False
    # markVariant
    try:
        markVariant = compFactory.CreateEntityDefinitions(entityId).GetMarkVariant()
    except Exception:
        markVariant = -1
    if markVariant == 999:
        return False
    # type_family 豁免标签
    families = attrComp.GetTypeFamily()
    if families and (set(families) & _NON_HOSTILE_FAMILIES):
        return False
    return True


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
