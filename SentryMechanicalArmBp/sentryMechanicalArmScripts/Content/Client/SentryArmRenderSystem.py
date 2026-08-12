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
    onEntityAdded        → CreateClientEntityByTypeStr 生成本地视觉实体
    onEntityRemoved      → DestroyClientEntity + 清自愈重试记账
    onDimensionChanged   → 引擎已销毁全部客户端实体,清空映射与记账
    update(dt)           → 重激活重推 + 自愈补建 + 每 tick 同步 RPM / 武器 / 瞄准角度
    updateFrame()        → 渲染帧(60+ Hz) dt-lerp 写入 Molang query

客户端实体自愈(对齐主包 MechanicalArmRenderSystem + ClientVisualRescue):
    onEntityAdded 一次性建实体有三个静默失败出口(blockPos 未就位 / LoaderSystem
    未就绪 / 引擎返回 None),任一命中该实体就永远没有 visual。补建统一走主包
    ClientVisualRescue.rescueMissingVisuals —— 节流(20 tick 一扫)、限额(单次 2 个)、
    连败放弃(10 次),替代旧版"每 tick 无限重试"的裸循环。
"""

import math
import time

from ...QuModLibs.Client import clientApi, levelId
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


# ==================== 事件驱动注册 ====================
# 监听 ClientExtensionApiReady 事件,收到 facade 后通过 @ext.registerSystem 装饰类。
# 主包 _publishAndFreezeClientExtensionApi 在广播 ready 事件之后会再调一次
# `_clientWorld.initRegisteredSystems()`,我们 handler 内 append 的 _pendingSystems
# 条目会被那一次 init 拾起实例化。无需手动 addSystem。

_registered = False


def _doRegister(ext):
    # type: (object) -> bool
    """向主包 ClientWorld 注册 SentryArmRenderSystem。成功返回 True。

    Args:
        ext: ExtensionApiFacade 实例 (含 System / registerSystem 属性)
    """
    global _registered
    if _registered:
        return True
    if ext is None:
        return False
    System = ext.System
    registerSystem = ext.registerSystem
    if System is None or registerSystem is None:
        return False

    # 主包客户端实体补建共用体(节流 / 限额 / 连败放弃的重试记账都在主包侧,
    # 与大水车/动力臂等主包渲染系统共享同一套语义)。internal 路径,但注册
    # 集中在本函数内,主包重构时只改这一处。
    rescueMod = clientApi.ImportModule(_MAIN_PACK + ".Content.Client.ClientVisualRescue")

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
            # 无条件清理:没有 visual 时 destroy 是 no-op;forget 必须执行,
            # 否则原位重放的方块(实体 id 由坐标派生,跨重建相同)会继承上一轮
            # 失败计数,甚至直接落在放弃名单里再也不补建
            self._destroyClientEntity(entity.id)
            if rescueMod is not None:
                rescueMod.forgetVisualRescue(self, entity.id)

        def onDimensionChanged(self, toDimensionId):
            # 维度切换时引擎已销毁本进程所有 CreateClientEntityByTypeStr 实体,
            # 映射与缓存全部作废 —— 只清表,不要对死实体调 Destroy/解绑。
            # 不清的话回到原维度后 _clientEntityIds 里全是死 id,
            # update 以为 visual 还在,模型永远不重建
            if rescueMod is not None:
                rescueMod.resetVisualRescue(self)
            self._clientEntityIds.clear()
            self._lastRpm.clear()
            self._lastWeapon.clear()
            self._weaponModelIds.clear()
            self._currentBaseAngle.clear()
            self._currentHeadAngle.clear()
            self._renderTargets.clear()

        # ---- 每 tick 同步 ----

        def update(self, dt=0):
            # 重激活(维度回切 / 走远后重回模拟距离):visual 可能已被引擎销毁,
            # 缺失的补建,存活的强制重推一次渲染参数(RPM uniform 等不在
            # Molang query 里的状态不会自动恢复)
            for entityId in self._world._reactivatedEntityIds:
                if entityId not in self._matchedEntityIds:
                    continue
                entity = self._world.getEntity(entityId)
                if not entity or entity.blockName != SENTRY_ARM_BLOCK:
                    continue
                clientEid = self._clientEntityIds.get(entityId)
                if not clientEid:
                    self._createClientEntity(entity)
                else:
                    self._refreshEntity(entity, clientEid, force=True)
                    self._refreshWeapon(entity, clientEid)

            # 自愈补建:onEntityAdded 那一刻 blockPos / LoaderSystem / 引擎未就绪
            # 导致的首建失败,由主包共用体按节流+限额+放弃上限统一重试。
            # _createClientEntity 成功时内部自带 force 刷新 + 武器绑定,
            # 无需像主包动力臂那样对返回列表再补推一次
            if rescueMod is not None:
                rescueMod.rescueMissingVisuals(
                    self,
                    self._clientEntityIds,
                    self._createClientEntity,
                    predicate=lambda e: e.blockName == SENTRY_ARM_BLOCK,
                )

            for entity in self.getEntities():
                if entity.blockName != SENTRY_ARM_BLOCK:
                    continue
                clientEid = self._clientEntityIds.get(entity.id)
                if not clientEid:
                    continue  # 等自愈补建,不在这里裸重试
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
            """幂等：先销毁可能残留的旧 visual，再创建新的 —— 防止
            ModBlockEntityLoadedClientEvent 多次触发（chunk reload / 玩家离开后重入加载范围）
            导致 onEntityAdded 反复调用而堆积多个客户端实体。"""
            self._destroyClientEntity(entity.id)

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


# ==================== 事件订阅入口 ====================


def _onArrisCreateReady(args):
    ext = args.get("extension") if isinstance(args, dict) else None
    if ext is None:
        return
    _doRegister(ext)


def _setupRegistration():
    arrisMod = clientApi.ImportModule(_MAIN_PACK + ".Api.ExtensionApi")
    if arrisMod is None:
        return  # 主包未安装,优雅降级

    if not hasattr(arrisMod, "getClientExtensionApi"):
        return  # 旧版主包没有 facade,这条渲染系统注册路径只支持新版

    ext = arrisMod.getClientExtensionApi()
    eventName = getattr(arrisMod, "CLIENT_EXTENSION_API_READY_EVENT", None)
    if eventName:
        namespace = arrisMod.EXTENSION_API_NAMESPACE
        systemName = arrisMod.EXTENSION_API_SYSTEM_NAME
        loader = LoaderSystem.getSystem()
        if loader is not None:
            # NetEase ListenForEvent 通过 getattr(parent, func.__name__) 查回调,
            # 模块级函数必须先挂到 parent(loader) 上。QuMod 的 _allocMethodWithOUTFunction
            # 用随机名做 setattr 并返回包装方法,避免重名冲突。
            wrappedFunc = loader._allocMethodWithOUTFunction(_onArrisCreateReady)
            loader.ListenForEvent(namespace, systemName, eventName, loader, wrappedFunc)

    # 兜底:订阅来得晚,主包已 freeze → 立即跑一次
    if ext.isFrozen():
        _doRegister(ext)


_setupRegistration()
