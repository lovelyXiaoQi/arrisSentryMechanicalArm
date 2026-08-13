# -*- coding: utf-8 -*-
"""
SentryTargetMatcher - 自定义索敌目标匹配引擎（双端共享，纯逻辑）

匹配键只来自两个引擎接口（调用方负责取值）：
    typeStr    = CreateEngineType(eid).GetEngineTypeStr()   实体ID
    entityName = CreateName(eid).GetName()                  玩家名 / 命名牌名

customTargets（逗号分隔 token 串）的匹配语义：

    token 形态                      来源            语义
    ----------------------------------------------------------------------
    "minecraft:player@<名字>"       登记板打玩家     只匹配玩家（typeStr 必须是
                                                    minecraft:player，名字可含 *；
                                                    同名命名牌生物不会被冒充命中）
    "<typeStr>"                     登记板打生物     实体ID精确匹配
    "文本"                          输入框手输       精确匹配 实体ID 或 名字
    "文本*" / "*文本" / "a*b"       输入框手输       * 通配（任意一段字符）
    "!<以上任意形态>"               输入框手输       取反（排除命中的目标）

列表组合语义（对齐常见过滤器设计）：
    - 存在正向条目时：必须命中任一正向条目，且不被任何 ! 条目命中
    - 只有 ! 条目时：除被 ! 命中的以外，全部视为目标

登记板 userData 中手输条目的存储形态：{"typeStr": CUSTOM_PATTERN_TYPE, "name": 规则文本}。
本模块不 import 引擎跳板，服务端（索敌/登记板）与客户端（UI 校验）共用。
"""

# 登记板 userData 里手输规则条目的 typeStr 标记
CUSTOM_PATTERN_TYPE = "custom:pattern"

PLAYER_TYPE = "minecraft:player"
PLAYER_TOKEN_PREFIX = PLAYER_TYPE + "@"

# 手输规则的最大长度（与 UI 输入框 max_length 无关，这是入库上限）
MAX_TOKEN_LENGTH = 64


def toUtf8(text):
    # type: (any) -> str
    """引擎 UI / RPC 边界可能给 unicode，统一转 utf-8 str 再参与比较。"""
    if text is None:
        return ""
    try:
        if isinstance(text, unicode):  # noqa: F821  # Py2 引擎运行时
            return text.encode("utf-8")
    except NameError:
        pass
    return str(text)


def globMatch(pattern, text):
    # type: (str, str) -> bool
    """只支持 * 的通配符匹配（* = 任意长度任意字符）。无 * 时为全等比较。"""
    if pattern is None or text is None:
        return False
    if "*" not in pattern:
        return pattern == text
    parts = pattern.split("*")
    head, tail = parts[0], parts[-1]
    if head and not text.startswith(head):
        return False
    if tail and not text.endswith(tail):
        return False
    pos = len(head)
    end = len(text) - len(tail)
    if pos > end:
        return False
    # 中间段按顺序出现在 (pos, end) 区间内
    for seg in parts[1:-1]:
        if not seg:
            continue
        idx = text.find(seg, pos)
        if idx < 0 or idx + len(seg) > end:
            return False
        pos = idx + len(seg)
    return True


def compileTargets(customRaw):
    # type: (str) -> tuple
    """
    customTargets 串 → (正向条目列表, 取反条目列表)。
    条目 = (playerOnly, pattern)：playerOnly 表示只匹配玩家名
    （来自登记板的 "minecraft:player@名字" 形态）。
    """
    positives = []
    negatives = []
    for token in (customRaw or "").split(","):
        token = token.strip()
        if not token:
            continue
        negate = token.startswith("!")
        if negate:
            token = token[1:].strip()
            if not token:
                continue
        playerOnly = False
        if token.startswith(PLAYER_TOKEN_PREFIX):
            playerOnly = True
            token = token[len(PLAYER_TOKEN_PREFIX):]
            if not token:
                continue
        entry = (playerOnly, token)
        if negate:
            negatives.append(entry)
        else:
            positives.append(entry)
    return positives, negatives


def _entryHits(entry, typeStr, entityName):
    # type: (tuple, str, str) -> bool
    playerOnly, pattern = entry
    if playerOnly:
        # 玩家专属条目：typeStr 必须是玩家，防止同名命名牌生物冒充
        if typeStr != PLAYER_TYPE:
            return False
        return bool(entityName) and globMatch(pattern, entityName)
    if globMatch(pattern, typeStr):
        return True
    return bool(entityName) and globMatch(pattern, entityName)


def matchTarget(compiled, typeStr, entityName=""):
    # type: (tuple, str, str) -> bool
    """
    判定实体是否为自定义索敌目标。
    compiled 为 compileTargets 的返回值；typeStr 必传，
    entityName 传 GetName 结果（玩家名 / 命名牌名，无名字传空串）。
    """
    positives, negatives = compiled
    if not positives and not negatives:
        return False
    for entry in negatives:
        if _entryHits(entry, typeStr, entityName):
            return False
    if not positives:
        return True  # 纯取反列表：排除之外全部命中
    for entry in positives:
        if _entryHits(entry, typeStr, entityName):
            return True
    return False


def validateManualToken(text):
    # type: (str) -> tuple
    """
    输入框手输规则校验（客户端预检 + 服务端权威校验共用）。
    返回 (清洗后的token, "") 或 ("", 错误提示)。
    """
    token = toUtf8(text).strip()
    if not token:
        return "", "内容为空"
    if len(token) > MAX_TOKEN_LENGTH:
        return "", "内容过长(最多{}字符)".format(MAX_TOKEN_LENGTH)
    if "," in token:
        return "", "不能包含逗号"
    body = token[1:].strip() if token.startswith("!") else token
    if not body:
        return "", "取反符 ! 后需要内容"
    return token, ""
