# -*- coding: utf-8 -*-
"""
SentryArmEpCompat - EP+ 军械库新版数据兼容层（双端共享，纯数据逻辑）

EP+ 3.5x 起引入三类新数据形态，本模块负责适配：

1. 子弹等级（EpJxkScript.modCommon.epBullet）
   - EP_BULLET_SEQUENCE = {基础弹名(gun data.useBullet): [子弹物品名, ...]}，
     序列固定"高等级 → 低等级"排序，基础弹在末位
   - BULLET_DATA[子弹物品名] = {"danger": 伤害倍率, "armor_strike"/"armor_penetration"...}
   - 弹匣按 EP 规范用"数字串"逐发记录：每位 = 序列下标（0 = 最高级），
     降序排列，开火从末尾消耗 → 高级弹优先打出
     （对齐 epGun.ReturnHasBulletOrBulletBox 与 gunFire.GetEpBulletData）

2. bind 变体枪（EP_JG_DATA JSON 无顶层 type/useBullet，靠 "bind" 指向本体枪）
   - EpApiClient.GetGunInfo 对它们返回 None、IsGun 可能 False、
     GetEplisItemData 直接 KeyError('type')，必须按 EP 的合并语义
     （bind 本体为底、自身字段覆盖）自行还原有效数据

3. fireSpeed 双语义（对齐 gunFire._startFireInterval）
   - < 1  → 秒
   - >= 1 → 30Hz tick 数

本模块不 import 引擎跳板，所有外部依赖由调用方注入
（epBullet 模块对象 / GetGunData 回调），服务端、客户端共用同一份逻辑。
"""

import json

# 对齐 gunFire._startFireInterval 的兜底射击间隔（秒）
_MIN_FIRE_INTERVAL_SEC = 0.05


# ==================== fireSpeed 双语义 ====================


def fireSpeedToTicks(raw):
    # type: (float) -> int
    """fireSpeed 统一换算为 30Hz tick：< 1 视为秒（×30），>= 1 视为 tick。"""
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return 4
    if raw <= 0:
        raw = _MIN_FIRE_INTERVAL_SEC
    if raw < 1.0:
        return max(1, int(round(raw * 30.0)))
    return max(1, int(round(raw)))


# ==================== bind 变体枪 ====================


def resolveGunData(getJgData, itemName, maxDepth=3):
    # type: (callable, str, int) -> dict | None
    """
    读取 EP_JG_DATA 并按 EP 的 bind 语义合并出"有效枪械数据"。

    Args:
        getJgData: itemName -> 原始 JSON dict | None
            （调用方包一层 EpApiClient 实例的 GetGunData）
        itemName: 枪械物品名

    Returns:
        合并后的顶层 dict（含 type / data），非枪械或查无数据返回 None。
    """
    if not itemName or not callable(getJgData):
        return None
    data = getJgData(itemName)
    if not isinstance(data, dict):
        return None
    seen = set([itemName])
    depth = 0
    # bind 变体枪自身 JSON 无顶层 type，逐级并入本体数据
    while "type" not in data and depth < maxDepth:
        bindItem = data.get("bind")
        if not bindItem or bindItem in seen:
            break
        bindData = getJgData(bindItem)
        if not isinstance(bindData, dict):
            break
        data = _mergeBindData(data, bindData)
        seen.add(bindItem)
        depth += 1
    if data.get("type") != 0:
        return None
    return data


def _mergeBindData(ownData, bindData):
    # type: (dict, dict) -> dict
    """对齐 EpJxkScriptClientSystem.GetEplisItemData 的 bind 合并：
    以 bind 本体为底，自身字段覆盖；dict 字段逐键覆盖（浅合并）。"""
    merged = json.loads(json.dumps(bindData))  # 深拷贝，避免污染 EP 侧缓存
    for key, value in ownData.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(value, dict) and isinstance(merged[key], dict):
            for subKey, subValue in value.items():
                merged[key][subKey] = subValue
        else:
            merged[key] = value
    return merged


def buildGunInfo(itemName, mergedData):
    # type: (str, dict) -> dict
    """从合并后的 EP_JG_DATA 顶层 dict 构造扁平枪械属性。
    字段集合对齐 EpApiClient.GetGunInfo（bind 枪的兜底数据源，无配件加成）。"""
    d = mergedData.get("data") or {}
    return {
        "name": itemName,
        "chinaName": mergedData.get("name", ""),
        "damage": d.get("danger", 0),
        "fireSpeed": d.get("fireSpeed", 4),
        "boltSpeed": d.get("boltSpeed", 0),
        "shootCount": d.get("shootCount", 1),
        "fireType": d.get("fireType", 0),
        "magazine": d.get("magazine", 30),
        "reloadEmptyTick": d.get("reloadEmptyTick", 2.0),
        "reloadTacticalTick": d.get("reloadTacticalTick", 2.0),
        "dangerType": d.get("dangerType", "projectile"),
        "bulletSpeed": d.get("bulletSpeed", 100),
        "useBullet": d.get("useBullet", ""),
        "count": d.get("count", 1),
        "spread": d.get("spread", 0),
        "distance": d.get("distance", 100),
        "crit": d.get("crit", 0),
        "critDamage": d.get("critDabger", 1.5),
        "shootSound": d.get("shootSound", []),
        "reloadSound": d.get("reloadSound", []),
        "hitPartic": d.get("hitPartic", ""),
        "fireParts": d.get("fireParts", ""),
        "percentArmorPenetration": d.get("PercentArmorPenetration", 0),
        "flatArmorPenetration": d.get("FlatArmorPenetration", 0),
        # bind 变体枪的本体名（音效档案挂在本体前缀下，换弹音候选生成用）
        "bindName": mergedData.get("bind", "") or "",
    }


def getGunInfoWithBind(epApiInstance, itemName):
    # type: (object, str) -> dict | None
    """GetGunInfo 的 bind 兜底版：标准枪直接走 EpApiClient.GetGunInfo；
    bind 变体枪（so14 / holger26 / m4a1_ziptie 等）按合并数据构造同形 dict。"""
    if epApiInstance is None or not itemName:
        return None
    if hasattr(epApiInstance, "GetGunInfo"):
        info = epApiInstance.GetGunInfo(itemName)
        if info:
            return info
    if not hasattr(epApiInstance, "GetGunData"):
        return None
    merged = resolveGunData(epApiInstance.GetGunData, itemName)
    if not merged:
        return None
    return buildGunInfo(itemName, merged)


def isGunWithBind(epApiInstance, itemName):
    # type: (object, str) -> bool
    """IsGun 的 bind 兜底版：注册表/JSON type 命中即 True；
    否则按 bind 合并数据判 type == 0（holger26 / m4a1_ziptie 不在
    composed_table 且自身 JSON 无 type，原生 IsGun 会误判 False）。"""
    if epApiInstance is None or not itemName:
        return False
    if hasattr(epApiInstance, "IsGun") and epApiInstance.IsGun(itemName):
        return True
    if not hasattr(epApiInstance, "GetGunData"):
        return False
    return resolveGunData(epApiInstance.GetGunData, itemName) is not None


# ==================== 子弹等级序列 ====================


def getBulletSequence(epBulletMod, useBullet):
    # type: (object, str) -> list
    """基础弹名 → 可用子弹物品名列表（高→低等级）。
    epBullet 缺失 / 无序列（如 bullet_338、xygm_*）时退化为 [useBullet]。"""
    if not useBullet:
        return []
    sequences = getattr(epBulletMod, "EP_BULLET_SEQUENCE", None) if epBulletMod else None
    if isinstance(sequences, dict):
        variants = sequences.get(useBullet)
        if variants:
            return list(variants)
    return [useBullet]


def isBulletAccepted(epBulletMod, useBullet, itemName):
    # type: (object, str, str) -> bool
    """itemName 是否为该枪可用的子弹（基础弹或任意等级变体）。"""
    if not itemName:
        return False
    return itemName in getBulletSequence(epBulletMod, useBullet)


def bulletIndexInSequence(epBulletMod, useBullet, itemName):
    # type: (object, str, str) -> int
    """子弹物品名 → 序列下标。未知子弹回退 0（对齐 gunFire.GetEpBulletData）。"""
    sequence = getBulletSequence(epBulletMod, useBullet)
    if itemName in sequence:
        return sequence.index(itemName)
    return 0


def bulletAtIndex(epBulletMod, useBullet, index):
    # type: (object, str, int) -> str
    """序列下标 → 子弹物品名。越界回退序列首位（对齐 gunFire.GetEpBulletData）。"""
    sequence = getBulletSequence(epBulletMod, useBullet)
    if not sequence:
        return useBullet or ""
    if 0 <= index < len(sequence):
        return sequence[index]
    return sequence[0]


def bulletDamageMultiplier(epBulletMod, bulletName):
    # type: (object, str) -> float
    """该子弹的平射伤害倍率（对齐 gunFire: danger *= bulletData['danger']）。
    BULLET_DATA 缺失时回退 1.0（EP 对未知子弹兜底 level_five，danger 同为 1.0）。"""
    if not epBulletMod or not bulletName:
        return 1.0
    bulletData = getattr(epBulletMod, "BULLET_DATA", {}).get(bulletName)
    if not isinstance(bulletData, dict):
        return 1.0
    try:
        return float(bulletData.get("danger", 1.0))
    except (TypeError, ValueError):
        return 1.0


def armorDamageFactor(epBulletMod, entityArmorMod, bulletName, gunInfo, identifier):
    # type: (object, object, str, dict, str) -> float
    """
    对甲伤害衰减系数（子弹等级差异的主战场——平射 danger 倍率各等级几乎相同）。

    对齐 gunFire 玩家远程命中的甲伤公式:
        有效护甲 = 枪械护甲(ZOMBIE_ARMOR[实体][1])
                   × (1 − 枪 PercentArmorPenetration)
                   × (1 − 子弹 armor_penetration)
                   − 枪 FlatArmorPenetration
        系数 = 1 − 有效护甲 / 200

    未收录护甲的实体返回 1.0。ARMOR_LEVEL 部位甲的逐部位耐久与
    HAS_ARMOR_ENTITY 的破甲视觉状态是 EP 内部（客户端 molang / 服务端
    实例缓存）状态，此处不模拟——对这类实体按整甲值折算。
    """
    if not entityArmorMod or not identifier:
        return 1.0
    entry = getattr(entityArmorMod, "ZOMBIE_ARMOR", {}).get(identifier)
    if not entry or len(entry) < 2 or not entry[1]:
        return 1.0
    try:
        effectiveArmor = float(entry[1])
    except (TypeError, ValueError):
        return 1.0

    bulletPen = 0.0
    bulletData = getattr(epBulletMod, "BULLET_DATA", {}).get(bulletName) if epBulletMod else None
    if isinstance(bulletData, dict):
        try:
            bulletPen = max(0.0, min(float(bulletData.get("armor_penetration", 0.0)), 1.0))
        except (TypeError, ValueError):
            bulletPen = 0.0
    try:
        pctPen = float(gunInfo.get("percentArmorPenetration", 0) or 0) if gunInfo else 0.0
        flatPen = float(gunInfo.get("flatArmorPenetration", 0) or 0) if gunInfo else 0.0
    except (TypeError, ValueError):
        pctPen, flatPen = 0.0, 0.0

    effectiveArmor = max(effectiveArmor * (1.0 - pctPen) * (1.0 - bulletPen) - flatPen, 0.0)
    return max(0.0, 1.0 - effectiveArmor / 200.0)


def bulletLevel(epBulletMod, bulletName):
    # type: (object, str) -> int | None
    """该子弹的等级（1-7），非等级弹/未知返回 None。供 HUD 显示。"""
    if not epBulletMod or not bulletName:
        return None
    bulletData = getattr(epBulletMod, "BULLET_DATA", {}).get(bulletName)
    if not isinstance(bulletData, dict):
        return None
    return bulletData.get("level")


# ==================== 换弹音效候选 ====================

# 分段命名无规律可循的枪：直接补录代表音（换弹最具辨识度的一声）。
# EP 新枪音效档案的内部代号可能与物品短名完全无关（holger→dm56、basp_tf→basp、
# bind 枪挂在本体前缀下）——通用模式猜不中的在此补录；
# EP 出新枪后若哨戒臂换弹无声，往这张表加一行即可（key = 物品短名）。
_RELOAD_SOUND_OVERRIDES = {
    "basp_tf": "basp.basp_reload_empty_magout",
    "holger": "holger.dm56_reload_empty_magout",
    "holger26": "holger.holger26_reload_empty_charge",
    "m4a1_twos": "m4a1_twos.m4_reload_empty_magout",
    "m4a1_ziptie": "m4a1_twos.m4_reload_empty_magout",
    "so14": "ebr14.ebr14_reload_empty_magout",
}

# 分段命名的已知代际模式（{0} = 枪短名）
_RELOAD_SOUND_PATTERNS = (
    "{0}.reload_empty",                # 老式命名（个别枪字段空但定义仍在）
    "{0}.{0}_reload_01",               # aek973 代：逐段编号
    "{0}.{0}_reload_empty_magout",     # ebr14/m13b 代：逐动作命名
    "{0}.{0}_reload_magout",
    "{0}.{0}_reload_empty_fast_arm",   # hdr 代：fast 系逐动作
)


def reloadSoundCandidates(gunInfo):
    # type: (dict) -> list
    """
    换弹音效候选名列表（按优先级，调用方逐个试播、命中即停）。

    EP 3.5x 重制的新枪（hdr / aek973 / basp_tf / m4a1_twos 等）把换弹音改成
    动画关键帧驱动的分段定义，JSON 的 reloadSound 字段留空——老枪字段仍有效。
    PlayCustomMusic 对不存在的名字返回空 id 且无副作用，逐候选尝试即可。
    bind 变体枪的音效挂在本体前缀下，gunInfo["bindName"]（若有）也参与生成。
    """
    candidates = []
    sounds = (gunInfo or {}).get("reloadSound") or []
    # 空弹匣换弹音优先（哨戒臂都是打空弹匣才换弹）
    for idx in (1, 0):
        if len(sounds) > idx and sounds[idx]:
            candidates.append(sounds[idx])

    shorts = []
    for key in ("name", "bindName"):
        value = (gunInfo or {}).get(key, "") or ""
        short = value.split(":")[-1]
        if short and short not in shorts:
            shorts.append(short)
    for short in shorts:
        if short in _RELOAD_SOUND_OVERRIDES:
            candidates.append(_RELOAD_SOUND_OVERRIDES[short])
    for short in shorts:
        for pattern in _RELOAD_SOUND_PATTERNS:
            candidates.append(pattern.format(short))

    seen = set()
    ordered = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


# ==================== 弹匣逐发等级记录（EP bullet_list 数字串） ====================


def parseMagList(digitStr, count):
    # type: (str, int) -> list
    """bullet_list 数字串 → 长度 == count 的下标列表。
    缺位补 0（EP 开火端对无记录子弹按下标 0 处理），超长截断。"""
    digits = []
    for ch in digitStr or "":
        digits.append(int(ch) if ch.isdigit() else 0)
    count = max(0, int(count))
    if len(digits) > count:
        digits = digits[:count]
    elif len(digits) < count:
        digits.extend([0] * (count - len(digits)))
    return digits


def magListToStr(digits):
    # type: (list) -> str
    """下标列表 → bullet_list 数字串（单字符位，钳制 0-9）。"""
    return "".join(str(min(9, max(0, int(d)))) for d in digits)


def mergeMagList(digits, addIndex, addCount):
    # type: (list, int, int) -> list
    """补弹合并：现有记录 + 同下标新弹 N 发，降序排列——
    低级弹（大下标）在前、高级弹沉底，开火从末尾取则高级弹优先打出
    （对齐 epGun.ReturnHasBulletOrBulletBox 的降序重排）。"""
    merged = list(digits) + [int(addIndex)] * max(0, int(addCount))
    merged.sort(reverse=True)
    return merged


# ==================== 引擎 userData 读写 ====================


def userDataValue(userData, key, default=None):
    # type: (dict, str, any) -> any
    """读引擎 userData 条目：round-trip 后是 {'__type__':..,'__value__':..}
    包装，刚写入的可能是裸值，两种形态都要兼容。"""
    if not isinstance(userData, dict) or key not in userData:
        return default
    entry = userData[key]
    if isinstance(entry, dict) and "__value__" in entry:
        return entry["__value__"]
    return entry


def dumpUserData(userData):
    # type: (dict) -> str
    """userData dict → JSON 快照串（存 Component 字段用）。失败返回空串。"""
    if not isinstance(userData, dict) or not userData:
        return ""
    try:
        return json.dumps(userData)
    except (TypeError, ValueError):
        print("[sentry] weapon userData not serializable, dropped")
        return ""


def loadUserData(text):
    # type: (str) -> dict
    """JSON 快照串 → userData dict。空串/损坏返回 {}。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        print("[sentry] weapon userData snapshot corrupted, dropped")
        return {}
    return data if isinstance(data, dict) else {}
