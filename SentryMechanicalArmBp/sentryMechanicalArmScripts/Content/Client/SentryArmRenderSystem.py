# -*- coding: utf-8 -*-
"""
SentryArmRenderSystem - 哨戒动力臂客户端渲染系统

对齐主包 MechanicalArmRenderSystem 的 ECS 模式：
    @registerSystem("ClientWorld", priority=2)
    class SentryArmRenderSystem(System):
        requiredComponents = ["SentryArmComponent"]

主包 ECS 在实体 hydrate 时会主动调 onEntityAdded(实体一被同步到本地客户端就触发),
对所有玩家可靠 —— 替代之前 OnScriptTickClient 轮询 getAllEntities() 的脆弱模式
(后者在远端客户端首次同步时可能漏掉实体导致看不到动力臂模型)。

生命周期:
    onEntityAdded   → CreateClientEntityByTypeStr 生成本地视觉实体
    onEntityRemoved → DestroyClientEntity
    update(dt)      → 每 tick 同步 RPM / 武器 / 瞄准目标角度
    updateFrame()   → 渲染帧(60+ Hz) dt-lerp 写入 Molang query
"""

import math
import time

from ...QuModLibs.Client import clientApi, levelId, regModLoadFinishHandler
from ...QuModLibs.Systems.Loader.Client import LoaderSystem

SENTRY_ARM_BLOCK = "create:sentry_mechanical_arm"
SENTRY_ARM_ENTITY = "create:sentry_mechanical_arm_model"
_MAIN_PACK = "arrisCreateScripts"

compFactory = clientApi.GetEngineCompFactory()

# 枪械绑定参数(对齐主包 MechanicalArmItemDisplaySystem)
WEAPON_BONE = "item_locator"
WEAPON_OFFSET = (-0.2, 0, 0)
WEAPON_ROTATION = (-2.5, 60, -30)
WEAPON_SCALE = 0.15

# 角度 lerp 速率: RPM=256 时 -ln(0.75)*30 ≈ 8.63 / 秒
LERP_RATE_MAX = 8.63


def _getClientLoader():
    return LoaderSystem.getSystem()


# ==================== 双保险注册 ====================
# 主包加载顺序不确定: 模块 import 时尝试一次,失败则注册到
# regModLoadFinishHandler 在所有 mod 加载完毕后再试。

_registered = False


def _doRegister():
    # type: () -> bool
    """向主包 ClientWorld 注册 SentryArmRenderSystem。成功返回 True。"""
    global _registered
    if _registered:
        return True
    sysMod = clientApi.ImportModule(_MAIN_PACK + ".Content.Shared.System")
    worldMod = clientApi.ImportModule(_MAIN_PACK + ".Content.Shared.World")
    if not sysMod or not worldMod:
        return False
    System = sysMod.System
    registerSystem = worldMod.registerSystem

    @registerSystem("ClientWorld", priority=2)
    class SentryArmRenderSystem(System):
        requiredComponents = ["SentryArmComponent"]

        def __init__(self, world):
            System.__init__(self, world)
            self._priority = 2
            # ECS-id → client-entity-id
            self._clientEntityIds = {}
            # tick 缓存
            self._lastRpm = {}
            self._lastWeapon = {}
            self._weaponModelIds = {}
            # IK 角度 lerp 状态
            self._currentBaseAngle = {}
            self._currentHeadAngle = {}
            self._renderTargets = {}
            self._lastRenderTime = None
            # 主包工具引用 (lazy)
            self._RotationRenderSystem = None
            self._AxisMod = None

        # ---- ECS 生命周期回调 ----

        def onEntityAdded(self, entity):
            System.onEntityAdded(self, entity)
            if entity.blockName != SENTRY_ARM_BLOCK:
                return
            self._createClientEntity(entity)

        def onEntityRemoved(self, entity):
            System.onEntityRemoved(self, entity)
            if entity.blockName != SENTRY_ARM_BLOCK:
                return
            self._destroyClientEntity(entity.id)

        # ---- 每 tick 同步 ----

        def update(self, dt=0):
            for entity in self.getEntities():
                if entity.blockName != SENTRY_ARM_BLOCK:
                    continue
                clientEid = self._clientEntityIds.get(entity.id)
                if not clientEid:
                    # hydrate 时 blockPos 还没就绪导致初次 _createClientEntity 失败
                    # → 这一 tick 重试
                    self._createClientEntity(entity)
                    clientEid = self._clientEntityIds.get(entity.id)
                if not clientEid:
                    continue
                self._refreshEntity(entity, clientEid, force=False)
                self._refreshWeapon(entity, clientEid)
                self._updateAimAngles(entity, clientEid)

        # ---- 渲染帧 lerp ----

        def updateFrame(self):
            now = time.time()
            last = self._lastRenderTime
            self._lastRenderTime = now
            if last is None:
                return
            dt = now - last
            if dt <= 0:
                return
            if dt > 0.1:
                dt = 0.1

            for eid in list(self._renderTargets):
                targetBase, targetHead, rpm, clientEid = self._renderTargets[eid]
                rate = min(256.0, abs(rpm)) / 256.0 * LERP_RATE_MAX
                if rate <= 0:
                    continue
                factor = 1.0 - math.exp(-rate * dt)
                curBase = self._currentBaseAngle.get(eid, targetBase)
                curHead = self._currentHeadAngle.get(eid, targetHead)
                # 偏航角走最短路径(经 ±180 分界线不绕远路)
                baseDelta = (targetBase - curBase + 180.0) % 360.0 - 180.0
                curBase += baseDelta * factor
                if curBase > 180.0:
                    curBase -= 360.0
                elif curBase < -180.0:
                    curBase += 360.0
                curHead += (targetHead - curHead) * factor
                self._currentBaseAngle[eid] = curBase
                self._currentHeadAngle[eid] = curHead
                queryComp = compFactory.CreateQueryVariable(clientEid)
                queryComp.Set("query.mod.arm_base_angle", curBase)
                queryComp.Set("query.mod.arm_claw_angle", curHead)

        # ---- 私有：生命周期 ----

        def _createClientEntity(self, entity):
            pos = entity.blockPos
            if not pos:
                return
            spawnPos = (pos[0] + 0.5, pos[1], pos[2] + 0.5)
            clientSystem = _getClientLoader()
            if not clientSystem:
                return
            clientEid = clientSystem.CreateClientEntityByTypeStr(SENTRY_ARM_ENTITY, spawnPos, (0, 0))
            if not clientEid:
                return
            try:
                compFactory.CreateModel(clientEid).SetEntityShadowShow(False)
            except Exception:
                pass
            self._clientEntityIds[entity.id] = clientEid
            # 初始姿态 + 武器
            self._refreshEntity(entity, clientEid, force=True)
            self._refreshWeapon(entity, clientEid)

        def _destroyClientEntity(self, ecsId):
            clientEid = self._clientEntityIds.pop(ecsId, None)
            self._lastRpm.pop(ecsId, None)
            self._lastWeapon.pop(ecsId, None)
            self._renderTargets.pop(ecsId, None)
            self._currentBaseAngle.pop(ecsId, None)
            self._currentHeadAngle.pop(ecsId, None)

            oldModelId = self._weaponModelIds.pop(ecsId, None)
            if oldModelId is not None and oldModelId >= 0 and clientEid:
                try:
                    compFactory.CreateItem(levelId).SetBindBoneForBindItem(clientEid, oldModelId, "", True)
                except Exception:
                    pass
            if clientEid:
                clientSystem = _getClientLoader()
                if clientSystem:
                    try:
                        clientSystem.DestroyClientEntity(clientEid)
                    except Exception:
                        pass

        # ---- 私有：每 tick 状态同步 ----

        def _refreshEntity(self, entity, clientEid, force=False):
            queryComp = compFactory.CreateQueryVariable(clientEid)
            facingComp = entity.getComponent("SixFacingComponent")
            facing = facingComp.facing if facingComp else 1
            queryComp.Set("query.mod.arm_ceiling", 1.0 if facing == 0 else 0.0)

            # 22.5° 齿轮偏移(对齐动画 arm_z_rotation)
            if self._RotationRenderSystem is None:
                rrsMod = clientApi.ImportModule(_MAIN_PACK + ".Content.Client.Systems.RotationRenderSystem")
                if rrsMod:
                    self._RotationRenderSystem = rrsMod.RotationRenderSystem
            if self._AxisMod is None:
                self._AxisMod = clientApi.ImportModule(_MAIN_PACK + ".Content.Shared.Base.Direction")
            if self._RotationRenderSystem and self._AxisMod:
                offset = self._RotationRenderSystem._rotationOffset(self._AxisMod.Axis.Y, entity.blockPos)
                queryComp.Set("query.mod.arm_z_rotation", offset)

            # RPM → 着色器 uniform
            rpmComp = entity.getComponent("RPMComponent")
            rpm = 0.0
            if rpmComp:
                rawRpm = rpmComp.rpm
                rpm = float(rawRpm) if rawRpm is not None else 0.0

            if not force and self._lastRpm.get(entity.id) == rpm:
                return
            self._lastRpm[entity.id] = rpm

            actorRenderComp = compFactory.CreateActorRender(clientEid)
            if actorRenderComp:
                rpmVal = -rpm
                actorRenderComp.SetEntityExtraUniforms(1, (1.0, 1.0, 1.0, 1.0))
                actorRenderComp.SetEntityExtraUniforms(2, (1.0, rpmVal + 1.0, 1.0, 0.0))

        def _refreshWeapon(self, entity, clientEid):
            comp = entity.getComponent("SentryArmComponent")
            weaponName = comp.weaponItemName if comp else ""

            lastWeapon = self._lastWeapon.get(entity.id, "")
            boundModelId = self._weaponModelIds.get(entity.id)
            sameName = weaponName == lastWeapon
            needBind = weaponName and boundModelId is None
            if sameName and not needBind:
                return
            self._lastWeapon[entity.id] = weaponName

            # 解绑旧的(注意 modelId=0 也是合法)
            oldModelId = self._weaponModelIds.pop(entity.id, None)
            if oldModelId is not None and oldModelId >= 0:
                try:
                    compFactory.CreateItem(levelId).SetBindBoneForBindItem(clientEid, oldModelId, "", True)
                except Exception:
                    pass
            if weaponName:
                itemDict = {"newItemName": weaponName, "newAuxValue": 0, "count": 1}
                modelId = compFactory.CreateItem(levelId).BindItemToMinecraftModel(
                    clientEid,
                    itemDict,
                    WEAPON_BONE,
                    True,
                    WEAPON_OFFSET,
                    WEAPON_ROTATION,
                    WEAPON_SCALE,
                )
                if modelId is not None and modelId != -1:
                    self._weaponModelIds[entity.id] = modelId

        def _updateAimAngles(self, entity, clientEid):
            comp = entity.getComponent("SentryArmComponent")
            if not comp or not comp.hasTarget:
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
            targetBase = -math.atan2(dx, dz) * (180.0 / math.pi)
            if ceiling:
                # 倒置时模型绕 X 轴翻转 180°,需要补偿 180° 让骨骼指向正确世界方向
                dy = -dy
                targetBase = 180.0 - targetBase
            horizDist = math.sqrt(dx * dx + dz * dz)
            targetHead = -math.atan2(dy, horizDist) * (180.0 / math.pi) if horizDist > 0.01 else 0.0
            rpmComp = entity.getComponent("RPMComponent")
            rpm = abs(float(rpmComp.rpm)) if rpmComp and rpmComp.rpm else 0.0
            self._renderTargets[entity.id] = (targetBase, targetHead, rpm, clientEid)

    _registered = True
    return True


# 尝试 1: 模块加载时(主包可能已就绪)
_doRegister()


# 尝试 2: 全部 mod 加载完毕后兜底
@regModLoadFinishHandler
def _onAllModsLoaded():
    _doRegister()
