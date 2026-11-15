"""Pinned v2.14 working-FVG amendment to the existing v2.11 Pine replay.

The full Pine source is archived in reference/TradingView_v2.14.pine.
Relative to v2.13, v2.14 changes the guard-selection block: once a first
confirmed guard exists, an unready same-flow candidate nearer the impulse
extreme may replace it. Broken replacement must stay broken; no fallback.
All other numeric v2.11 replay blocks and 74 settings remain pinned.
"""
import ast

BASE_SHA256 = "CC2E1422F6D865C7D4814B7FF7CD4EEA82F87EB89642E8EF5F158A47B85D057F"
ACTIVE_VERSION = "2.14 H1 + H4 (v2.11 base, v2.14 FVG guard)"


def patch_program(data):
    """Apply a strictly anchored, fail-closed Pine v2.14 guard translation."""
    if data.get("sha256") != BASE_SHA256 or data.get("version") != "2.11 H1 + H4":
        raise ValueError("Unexpected baseline Pine replay")
    code = data["functions"]["f_flowSnapshot"]["code"]

    def replace_once(before, after):
        nonlocal code
        if code.count(before) != 1:
            raise ValueError("Pinned flow snapshot diverged from expected v2.11")
        code = code.replace(before, after)

    # Pine v2.14, lines 2615–2635: this block must be OUTSIDE the
    # "saved.ready and not stale and not dead" processing block. Otherwise
    # an unready candidate cannot replace a previously selected guard.
    replace_once(
        "    dead = not na(saved.zone.brokenAt)\n"
        "    if saved.ready and (not stale) and (not dead):",
        "    dead = not na(saved.zone.brokenAt)\n"
        "    sameFlowZone = _compare(flowDir, 0, 'NotEq') and _compare(saved.zone.side, flowDir, 'Eq') and (not stale) and _compare(saved.zone.impulseTime, flowStart, 'GtE')\n"
        "    workingCandidate = sameFlowZone and (saved.ready or ((not na(guardTop)) and _compare(saved.originTime, flowStart, 'GtE')))\n"
        "    nearerToExtreme = na(guardTop) or (_compare(saved.zone.top, guardTop, 'Gt') if _compare(saved.zone.side, 1, 'Eq') else _compare(saved.zone.bottom, guardBottom, 'Lt'))\n"
        "    if workingCandidate and nearerToExtreme:\n"
        "        flowHasFvg = True\n"
        "        guardBalanced = saved.zone.balanced\n"
        "        guardTop = saved.zone.top\n"
        "        guardLeftTime = saved.zone.leftTime\n"
        "        guardBottom = saved.zone.bottom\n"
        "        guardImpulse = saved.zone.impulseTime\n"
        "        firstBroken = dead\n"
        "        firstBrokenAt = saved.zone.brokenAt\n"
        "        ruleOneZone = None\n"
        "        ruleOneStart = None\n"
        "        ruleOneFvgAt = None\n"
        "        _return = array.clear(ruleOneCandidates)\n"
        "    if saved.ready and (not stale) and (not dead):"
    )

    # Only ready, intact legs can produce continuation/reversal candidates.
    # Selection of a nearer guard is already handled above, regardless of
    # saved.ready and saved.zone.brokenAt, matching the original v2.14.
    replace_once(
        "        if _compare(flowDir, 0, 'NotEq') and _compare(side, flowDir, 'Eq') and _compare(saved.zone.impulseTime, flowStart, 'GtE'):\n"
        "            closer = na(guardTop) or (_compare(top, guardTop, 'Gt') if _compare(side, 1, 'Eq') else _compare(bottom, guardBottom, 'Lt'))\n"
        "            if closer:\n"
        "                flowHasFvg = True\n"
        "                guardBalanced = saved.zone.balanced\n"
        "                guardTop = top\n"
        "                guardLeftTime = saved.zone.leftTime\n"
        "                guardBottom = bottom\n"
        "                guardImpulse = saved.zone.impulseTime\n"
        "                firstBroken = not na(saved.zone.brokenAt)\n"
        "                firstBrokenAt = saved.zone.brokenAt\n"
        "                ruleOneZone = None\n"
        "                ruleOneStart = None\n"
        "                ruleOneFvgAt = None\n"
        "                _return = array.clear(ruleOneCandidates)\n",
        ""
    )

    replace_once(
        "    if flowDiagnosticEnabled:\n"
        "        bindingState = 'отменён: зона пробита' if dead else 'не относится к текущему потоку' if stale else 'плечо принято' if saved.ready else 'ожидание уточнения плеча (начало и экстремум на разных свечах)'",
        "    if flowDiagnosticEnabled:\n"
        "        selectedWorking = sameFlowZone and (not na(guardTop)) and _compare(saved.zone.top, guardTop, 'Eq') and _compare(saved.zone.bottom, guardBottom, 'Eq') and _compare(saved.zone.impulseTime, guardImpulse, 'Eq')\n"
        "        bindingState = ('рабочий FVG: пробит' if firstBroken else 'рабочий FVG: действует') if selectedWorking else 'отменён: зона пробита' if dead else 'не относится к текущему потоку' if stale else 'плечо принято' if saved.ready else 'ожидание уточнения плеча (начало и экстремум на разных свечах)'"
    )

    # v2.14 adds the independently selected guard's status to the flow
    # diagnostics. It does not alter direction, guard bounds or prices.
    replace_once(
        "current = None\nif exportNow:\n    current = f_mtfCopy(",
        "current = None\nif exportNow:\n"
        "    workingZoneAudit = '' if not flowDiagnosticEnabled else '\\nРабочий FVG: не выбран' if na(guardTop) else '\\nРабочий FVG: [' + str.tostring(guardBottom, format.mintick) + '; ' + str.tostring(guardTop, format.mintick) + ']' + (' | пробит ' + str.format_time(firstBrokenAt, 'yyyy-MM-dd HH:mm', 'Europe/Moscow') if firstBroken else ' | действует')\n"
        "    current = f_mtfCopy("
    )
    replace_once(
        "flowDiagnostic, f_ruleOneState(flowDir, firstBroken, ruleOneStart, requireLiquidity, lastLongEventTime, lastShortEventTime, firstBrokenAt, ruleOneFvgAt), 'Add'), '\\n', 'Add')",
        "_binary(flowDiagnostic, f_ruleOneState(flowDir, firstBroken, ruleOneStart, requireLiquidity, lastLongEventTime, lastShortEventTime, firstBrokenAt, ruleOneFvgAt), 'Add'), workingZoneAudit, 'Add'), '\\n', 'Add')"
    )

    ast.parse(code)  # Fail before running market data if the translation drifts.
    data["functions"]["f_flowSnapshot"]["code"] = code
    data["version"] = ACTIVE_VERSION
    return data
