# -*- coding: utf-8 -*-
"""
ModClientSystem - 哨戒机械臂客户端入口

职责:
1. 客户端实体生命周期（生成/销毁 create:sentry_mechanical_arm_model）
2. 齿轮旋转着色器 uniform 同步
3. 枪械模型渲染（BindItemToMinecraftModel 绑定到 item_locator 骨骼）
4. IK 瞄准角度（baseAngle 偏航 + headAngle 俯仰 → Molang query）
"""

import math
import time

from ...QuModLibs.Client import Listen, clientApi, playerId

# 导入交互模块（触发 @Listen("OnScriptTickClient") 准星检测注册）
from . import SentryArmInteraction as _sentryInteraction  # noqa: F401

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
SENTRY_ARM_ENTITY = "create:sentry_mechanical_arm_model"

_MAIN_PACK = "arrisCreateScripts"

compFactory = clientApi.GetEngineCompFactory()
levelId = clientApi.GetLevelId()

# 注册 HUD 代理（附属包的按钮面板已通过 hud_screen.json modifications 注入到 HUD）
NativeScreenManager = clientApi.GetNativeScreenManagerCls()
NativeScreenManager.instance().RegisterScreenProxy(
    "hud.hud_screen",
    "sentryMechanicalArmScripts.Content.Client.SentryArmHudProxy.SentryArmHudProxy",
)

# 客户端实体 ID 缓存: ecsEntityId -> clientEntityId
_clientEntityIds = {}
# RPM 缓存: ecsEntityId -> lastRpm
_lastRpm = {}
# 枪械渲染缓存
_lastWeapon = {}  # ecsEntityId -> lastWeaponItemName
_weaponModelIds = {}  # ecsEntityId -> bindModelId

# 枪械绑定参数（对齐主包 MechanicalArmItemDisplaySystem 的 BindItemToMinecraftModel 模式）
WEAPON_BONE = "item_locator"
WEAPON_OFFSET = (-0.2, 0, 0)
WEAPON_ROTATION = (-15, 60, -30)
WEAPON_SCALE = 0.15

_initDone = False


# ==================== Molang 查询注册 ====================


def _registerMolangQueries():
    # 变量名对齐资源包 animation: arm_* 前缀（per-entity, 不与主包冲突）
    queryComp = compFactory.CreateQueryVariable(levelId)
    queryComp.Register("query.mod.arm_base_angle", 0.0)
    queryComp.Register("query.mod.arm_lower_angle", 0.0)
    queryComp.Register("query.mod.arm_upper_angle", 0.0)
    queryComp.Register("query.mod.arm_claw_angle", 0.0)
    queryComp.Register("query.mod.arm_ceiling", 0.0)
    queryComp.Register("query.mod.arm_z_rotation", 0.0)
    queryComp.Register("query.mod.arm_claw_grip", 0.0)


_registerMolangQueries()


# ==================== 主包引用缓存 ====================


_ClientWorld = None
_RotationRenderSystem = None


def _getClientWorld():
    global _ClientWorld
    if _ClientWorld is not None:
        return _ClientWorld
    mod = clientApi.ImportModule(_MAIN_PACK + ".Content.Client.ClientWorld")
    if not mod:
        return None
    _ClientWorld = mod.ClientWorld()
    return _ClientWorld


def _getRotationRenderSystem():
    global _RotationRenderSystem
    if _RotationRenderSystem is not None:
        return _RotationRenderSystem
    mod = clientApi.ImportModule(_MAIN_PACK + ".Content.Client.Systems.RotationRenderSystem")
    if mod:
        _RotationRenderSystem = mod.RotationRenderSystem
    return _RotationRenderSystem


def _getLoaderSystem():
    from ...QuModLibs.Systems.Loader.Client import LoaderSystem

    return LoaderSystem.getSystem()


# ==================== 客户端实体生命周期 ====================


def _createClientEntity(entity):
    # type: (object) -> None
    pos = entity.blockPos
    if not pos:
        return
    spawnPos = (pos[0] + 0.5, pos[1], pos[2] + 0.5)
    clientSystem = _getLoaderSystem()
    if not clientSystem:
        return
    clientEid = clientSystem.CreateClientEntityByTypeStr(SENTRY_ARM_ENTITY, spawnPos, (0, 0))
    if not clientEid:
        return
    compFactory.CreateModel(clientEid).SetEntityShadowShow(False)
    _clientEntityIds[entity.id] = clientEid

    # 初始设置 ceiling + 齿轮偏移 + RPM + 枪械
    _refreshEntity(entity, clientEid, force=True)
    _refreshWeapon(entity, clientEid)


def _destroyClientEntity(ecsEntityId):
    # type: (str) -> None
    clientEid = _clientEntityIds.pop(ecsEntityId, None)
    _lastRpm.pop(ecsEntityId, None)

    # 清理枪械绑定
    _lastWeapon.pop(ecsEntityId, None)
    oldModelId = _weaponModelIds.pop(ecsEntityId, None)
    if oldModelId is not None and oldModelId >= 0 and clientEid:
        compFactory.CreateItem(levelId).SetBindBoneForBindItem(clientEid, oldModelId, "", True)

    # 清理角度 lerp 状态
    _renderTargets.pop(ecsEntityId, None)
    _currentBaseAngle.pop(ecsEntityId, None)
    _currentHeadAngle.pop(ecsEntityId, None)

    if clientEid:
        clientSystem = _getLoaderSystem()
        if clientSystem:
            clientSystem.DestroyClientEntity(clientEid)


# ==================== 齿轮旋转 + Molang 同步 ====================


def _refreshEntity(entity, clientEid, force=False):
    # type: (object, str, bool) -> None
    queryComp = compFactory.CreateQueryVariable(clientEid)

    # ceiling 模式（对齐动画 arm_ceiling）
    facingComp = entity.getComponent("SixFacingComponent")
    facing = facingComp.facing if facingComp else 1
    queryComp.Set("query.mod.arm_ceiling", 1.0 if facing == 0 else 0.0)

    # 22.5° 齿轮偏移（对齐动画 arm_z_rotation）
    RRS = _getRotationRenderSystem()
    if RRS:
        from ...QuModLibs.Client import clientApi as _cApi

        AxisMod = _cApi.ImportModule(_MAIN_PACK + ".Content.Shared.Base.Direction")
        if AxisMod:
            offset = RRS._rotationOffset(AxisMod.Axis.Y, entity.blockPos)
            queryComp.Set("query.mod.arm_z_rotation", offset)

    # RPM → 着色器 uniform
    rpmComp = entity.getComponent("RPMComponent")
    rpm = 0.0
    if rpmComp:
        rawRpm = rpmComp.rpm
        rpm = float(rawRpm) if rawRpm is not None else 0.0

    if not force and _lastRpm.get(entity.id) == rpm:
        return
    _lastRpm[entity.id] = rpm

    actorRenderComp = compFactory.CreateActorRender(clientEid)
    if actorRenderComp:
        rpmVal = -rpm
        actorRenderComp.SetEntityExtraUniforms(1, (1.0, 1.0, 1.0, 1.0))
        actorRenderComp.SetEntityExtraUniforms(2, (1.0, rpmVal + 1.0, 1.0, 0.0))


# ==================== 枪械模型渲染 ====================


def _refreshWeapon(entity, clientEid):
    # type: (object, str) -> None
    """
    检测武器变化，绑定/解绑枪械到客户端实体的 item_locator 骨骼。
    重绑条件：名字变化 OR (期望有武器但实际未绑定 — 重进 hydrate 时序修复)
    """
    comp = entity.getComponent("SentryArmComponent")
    weaponName = comp.weaponItemName if comp else ""

    lastWeapon = _lastWeapon.get(entity.id, "")
    boundModelId = _weaponModelIds.get(entity.id)
    # 名字一致且绑定状态也一致（有武器→已绑 / 无武器→未绑）→ 无需动作
    sameName = weaponName == lastWeapon
    needBind = weaponName and boundModelId is None
    if sameName and not needBind:
        return

    _lastWeapon[entity.id] = weaponName

    # 先解绑旧的（对齐主包: modelId=0 也是合法 ID）
    oldModelId = _weaponModelIds.pop(entity.id, None)
    if oldModelId is not None and oldModelId >= 0:
        compFactory.CreateItem(levelId).SetBindBoneForBindItem(clientEid, oldModelId, "", True)

    # 绑定新的
    if weaponName:
        itemDict = {
            "newItemName": weaponName,
            "newAuxValue": 0,
            "count": 1,
        }
        modelId = compFactory.CreateItem(levelId).BindItemToMinecraftModel(
            clientEid, itemDict, WEAPON_BONE, True, (-0.2, 0, 0), (-2.5, 60, -30), WEAPON_SCALE
        )
        # 对齐主包: modelId 可能为 0（合法 ID），不能用 if modelId 判断
        if modelId is not None and modelId != -1:
            _weaponModelIds[entity.id] = modelId


# ==================== Tick 驱动 ====================


@Listen("OnScriptTickClient")
def _onClientTick(args=None):
    global _initDone
    clientWorld = _getClientWorld()
    if not clientWorld:
        return

    if not _initDone:
        _initDone = True

    # 发现新的哨戒臂实体 → 生成客户端实体 + 同步 RPM + 枪械
    activeIds = set()
    for entity in clientWorld.getAllEntities():
        if entity.blockName != SENTRY_ARM_BLOCK:
            continue
        activeIds.add(entity.id)
        if entity.id not in _clientEntityIds:
            _createClientEntity(entity)
        else:
            clientEid = _clientEntityIds.get(entity.id)
            if clientEid:
                _refreshEntity(entity, clientEid, force=False)
                _refreshWeapon(entity, clientEid)
                _updateAimAngles(entity, clientEid)

    # 清理已移除的实体
    toRemove = [eid for eid in _clientEntityIds if eid not in activeIds]
    for eid in toRemove:
        _destroyClientEntity(eid)


# ==================== IK 瞄准角度 ====================
#
# 架构：
#   OnScriptTickClient (30Hz)  →  _updateAimAngles: 从 SentryArmComponent 读最新目标，
#                                  算出 targetBase/targetHead 存入 _renderTargets
#   GameRenderTickEvent (帧率) →  _onRenderTick: dt 驱动的 lerp，写 Molang query
#
# 这样角度 lerp 跑在渲染帧上，视觉平滑不受 30Hz tick 限制。

# 平滑插值缓存
_currentBaseAngle = {}  # ecsEntityId -> float（当前渲染角度）
_currentHeadAngle = {}  # ecsEntityId -> float
# 渲染目标缓存: ecsEntityId -> (targetBase, targetHead, rpm, clientEid)
_renderTargets = {}

# RPM → 每秒衰减率（一阶 lerp: angle += delta * (1 - exp(-rate * dt))）
# 原 per-frame@30Hz: RPM=256 时 lerp=0.25 → -ln(0.75)*30 ≈ 8.63 / 秒
LERP_RATE_MAX = 8.63  # 每秒指数衰减率，RPM=256 时取最大


def _getLerpRate(rpm):
    # type: (float) -> float
    """RPM → 每秒衰减率（rate 越大越快追上目标）"""
    return min(256.0, abs(rpm)) / 256.0 * LERP_RATE_MAX


def _updateAimAngles(entity, clientEid):
    # type: (object, str) -> None
    """
    从 SentryArmComponent.targetX/Y/Z 计算目标角度，存入 _renderTargets。
    不做 lerp（lerp 在 GameRenderTickEvent 中按 dt 驱动）。
    """
    comp = entity.getComponent("SentryArmComponent")
    if not comp or not comp.hasTarget:
        # 无目标 → 不更新（保持 _renderTargets 里的旧目标，避免多目标切换时跳变）
        return

    pos = entity.blockPos
    facingComp = entity.getComponent("SixFacingComponent")
    ceiling = facingComp and facingComp.facing == 0

    armX = pos[0] + 0.5
    armY = pos[1] + 0.5 + (0.0 if ceiling else 0.4)
    armZ = pos[2] + 0.5

    dx = comp.targetX - armX
    dy = comp.targetY - armY
    dz = comp.targetZ - armZ

    # 偏航角（Bedrock Y旋转: South=0, West=90, North=±180, East=-90）
    targetBase = -math.atan2(dx, dz) * (180.0 / math.pi)
    if ceiling:
        # 倒置时模型绕 X 轴翻转 180°，骨骼 +Z 对应世界 -Z：
        # base_angle 必须补偿 180° 才能指向世界正确方向
        dy = -dy
        targetBase = 180.0 - targetBase

    # 俯仰角（claw_base X 轴旋转：正值向下，取负让目标在上方时仰起）
    horizDist = math.sqrt(dx * dx + dz * dz)
    targetHead = -math.atan2(dy, horizDist) * (180.0 / math.pi) if horizDist > 0.01 else 0.0

    rpmComp = entity.getComponent("RPMComponent")
    rpm = abs(float(rpmComp.rpm)) if rpmComp and rpmComp.rpm else 0.0

    _renderTargets[entity.id] = (targetBase, targetHead, rpm, clientEid)


# ==================== 渲染帧 lerp ====================

_lastRenderTime = [None]  # list 作可变容器，python2 没有 nonlocal


@Listen("GameRenderTickEvent")
def _onRenderTick(args=None):
    """每渲染帧执行 dt 驱动的角度 lerp，保证肉眼顺滑（60+Hz）"""
    now = time.time()
    last = _lastRenderTime[0]
    _lastRenderTime[0] = now
    if last is None:
        return  # 首帧无 dt 基准
    dt = now - last
    if dt <= 0:
        return
    if dt > 0.1:
        dt = 0.1  # clamp：卡顿后避免角度暴冲

    for eid, (targetBase, targetHead, rpm, clientEid) in list(_renderTargets.items()):
        rate = _getLerpRate(rpm)
        if rate <= 0:
            continue  # RPM=0 不动
        factor = 1.0 - math.exp(-rate * dt)

        curBase = _currentBaseAngle.get(eid, targetBase)
        curHead = _currentHeadAngle.get(eid, targetHead)

        # 偏航角走最短路径（经 ±180 分界线不绕远路）
        baseDelta = (targetBase - curBase + 180.0) % 360.0 - 180.0
        curBase += baseDelta * factor
        if curBase > 180.0:
            curBase -= 360.0
        elif curBase < -180.0:
            curBase += 360.0

        curHead += (targetHead - curHead) * factor

        _currentBaseAngle[eid] = curBase
        _currentHeadAngle[eid] = curHead

        queryComp = compFactory.CreateQueryVariable(clientEid)
        queryComp.Set("query.mod.arm_base_angle", curBase)
        queryComp.Set("query.mod.arm_claw_angle", curHead)
