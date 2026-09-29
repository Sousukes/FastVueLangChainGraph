"""阶段 18 · Computer Use（仿制）。

先看清 Computer Use 究竟是什么。它的闭环只有四步：

    ① 截图 ─▶ ② 模型决策 ─▶ ③ 执行动作 ─▶ ④ 回灌结果 ──┐
              ▲                                          │
              └──────────────────────────────────────────┘

其中**只有第 ② 步是模型厂商卖的东西**——「看一张屏幕截图，说出点哪里、打什么字」。
①③④ 全是应用侧自己的代码，而这三步本课已经建好了：
阶段 16 的多模态消息结构（怎么把图递给模型）、阶段 05 的工具注册表、阶段 10 的 ReAct 循环。

所以本阶段的路线是：**用 DeepSeek 的视觉把第 ② 步接上**，其余三步复用既有资产，
于是不需要 Claude、不需要 Docker、不需要新密钥，就能跑通一个完整可运行的 Computer Use 闭环。

代价必须说清楚 —— 精度。Claude 的 grounding 能力（OSWorld 从 12% 爬到 ~81%）是它值钱的地方，
DeepSeek 的坐标精度差得远。**而这个差距恰好是最好的教材**：

    被操作的"电脑"是我们自己渲染的 —— 于是每个元素的 bbox 是**已知真值**，
    模型的每一次点击都能被确定性地量出偏差，不必问它"你点准了吗"。

本阶段的判据（延续 14 coverage → 15 faithful → 17 termCheck 这条暗线）：

    lostKeystrokes  键盘被丢弃的次数 —— 模型想打字，但它的点击没落在任何输入框上，
                    于是按键全丢。这是**定位失败最硬的证据**（零成本、纯观测）。
    clickHitRate      点击命中率 —— 有没有点中。
    avgCenterOffsetPx 命中时离目标中心的平均像素距离 —— **点得多准**。
                      这一项在"全部点中"时仍有值，所以只有它可横向比较。

⚠️ 与真实 Claude Computer Use 的差别（文档里如实标注，不冒充）：
    · 模型：DeepSeek 视觉           vs Claude Opus/Sonnet（官方 computer_20251124 工具）
    · 工具形状：自定义 function tool（action 枚举）vs Anthropic 内置 schema-less 工具
    · 操作对象：我们渲染的虚拟表单   vs 真实桌面 / 浏览器
    · 精度与稳健性：差距明显，且我们只做**单个**确定性小任务

⚠️ 安全：本模块的一切动作只作用于**内存里的一个 dict**，
    碰不到真实文件系统、真实鼠标、真实网络。这不是省事，是 Computer Use 的第一原则
    （官方文档明确要求跑在 Docker/VM 里，否则模型能在你本机随便点）。
    危险元素默认被拒（allow_dangerous=False），用来演示"人工确认"这道闸门。

⚠️ 一个协议层的硬约束（本阶段最容易踩的坑）：
    OpenAI 兼容协议里 `role="tool"` 的消息**只能装文本**，装不了图片。
    所以执行完动作后，新截图不能当作 tool 结果回传，必须再追加一条
    `role="user"` 的多模态消息把图带上（见 _result_messages）。
"""

from __future__ import annotations

import base64
import io
import json
import math
import time
from typing import Any, Iterator

from PIL import Image, ImageDraw, ImageFont

from llm import LLMClient

# ---------------------------------------------------------------------------
# 虚拟屏幕：尺寸 + 元素表（元素表就是"真值"）
# ---------------------------------------------------------------------------

SCREEN_W = 1024
SCREEN_H = 768

FIELDS = ("field_order", "field_amount", "field_date")

# 后画的在上层，所以命中判定要**倒序**遍历
_ELEMENTS: list[dict[str, Any]] = [
    {"name": "field_order", "kind": "text_field", "x": 80, "y": 124, "w": 560, "h": 50, "label": "ORDER ID"},
    {"name": "field_amount", "kind": "text_field", "x": 80, "y": 214, "w": 560, "h": 50, "label": "AMOUNT"},
    {"name": "field_date", "kind": "text_field", "x": 80, "y": 304, "w": 560, "h": 50, "label": "DATE"},
    {"name": "btn_submit", "kind": "button", "x": 80, "y": 410, "w": 180, "h": 56, "label": "SUBMIT"},
    {
        "name": "btn_reset",
        "kind": "button",
        "x": 290,
        "y": 410,
        "w": 180,
        "h": 56,
        "label": "RESET ALL",
        "dangerous": True,
    },
]

ALLOWED_ACTIONS = ("screenshot", "left_click", "type", "key", "wait")

FIELD_LABELS = {"field_order": "ORDER ID", "field_amount": "AMOUNT", "field_date": "DATE"}

MAX_FIELD_CHARS = 64

# 默认任务的三项期望值（与 schemas.ComputerRequest 的默认值保持一致）
DEFAULT_ORDER_ID = "ORDER-2026-0917"
DEFAULT_AMOUNT = "2158.50"
DEFAULT_DATE = "2026-09-21"


def screen_spec() -> dict:
    """屏幕规格（给前端画点击落点叠加层用）。

    ⚠️ 每个元素都要**补齐全部字段**（`label` / `dangerous` 用默认值兜底）：
       这张表会原样经 SSE 发给前端，而前端 TS 里声明的是 `dangerous: boolean`；
       少一个键就等于对外契约撒谎 —— 前端拿到的是 undefined，行为全靠巧合。
    """
    return {
        "width": SCREEN_W,
        "height": SCREEN_H,
        "elements": [
            {
                "name": e["name"],
                "kind": e["kind"],
                "x": e["x"],
                "y": e["y"],
                "w": e["w"],
                "h": e["h"],
                "label": e.get("label", ""),
                "dangerous": bool(e.get("dangerous", False)),
            }
            for e in _ELEMENTS
        ],
    }


def initial_state() -> dict:
    """虚拟应用的初始状态 —— 就在这一个 dict 里，别处什么都不碰。"""
    return {
        "field_order": "",
        "field_amount": "",
        "field_date": "",
        "focus": "",
        "submitted": False,
        "status": "Idle. Fill the three fields, then press SUBMIT.",
        "cursor": None,
        "revision": 0,
    }


# ---------------------------------------------------------------------------
# 渲染：把状态画成一张真实的 PNG（模型看的就是这张图）
# ---------------------------------------------------------------------------

_FONTS: dict[int, Any] = {}


def _font(size: int):
    """优先用 Pillow 自带的 TrueType 后备字体。

    `ImageFont.load_default(size)` 从 Pillow 9.2 起返回真正的矢量字体，
    **不依赖系统字体文件** —— 比写死 arial.ttf / msyh.ttc 可移植得多。
    """
    if size not in _FONTS:
        try:
            _FONTS[size] = ImageFont.load_default(size)
        except Exception:  # noqa: BLE001  老版本 Pillow 只接受无参调用
            _FONTS[size] = ImageFont.load_default()
    return _FONTS[size]


def render_screen(state: dict) -> bytes:
    """把虚拟应用画成 PNG 字节。**确定性**：同样的状态永远画出同样的图。"""
    img = Image.new("RGB", (SCREEN_W, SCREEN_H), (246, 246, 243))
    d = ImageDraw.Draw(img)

    # 标题栏
    d.rectangle([0, 0, SCREEN_W, 56], fill=(38, 42, 56))
    d.text((24, 28), "Refund Request  -  virtual desktop", font=_font(20), fill=(238, 238, 242), anchor="lm")
    d.text((SCREEN_W - 24, 28), f"{SCREEN_W}x{SCREEN_H}", font=_font(15), fill=(150, 155, 170), anchor="rm")

    for el in _ELEMENTS:
        if el["kind"] == "text_field":
            d.text((el["x"], el["y"] - 14), el["label"], font=_font(15), fill=(92, 96, 110), anchor="lm")
            focused = state.get("focus") == el["name"]
            d.rectangle(
                [el["x"], el["y"], el["x"] + el["w"], el["y"] + el["h"]],
                fill=(255, 255, 255),
                outline=(70, 118, 224) if focused else (196, 198, 208),
                width=3 if focused else 1,
            )
            d.text(
                (el["x"] + 14, el["y"] + el["h"] // 2),
                state.get(el["name"], ""),
                font=_font(20),
                fill=(28, 30, 38),
                anchor="lm",
            )
        elif el["kind"] == "button":
            if el.get("dangerous"):
                fill = (196, 78, 78)
            elif state.get("submitted"):
                fill = (124, 134, 154)
            else:
                fill = (60, 110, 220)
            d.rectangle([el["x"], el["y"], el["x"] + el["w"], el["y"] + el["h"]], fill=fill)
            d.text(
                (el["x"] + el["w"] // 2, el["y"] + el["h"] // 2),
                el["label"],
                font=_font(20),
                fill=(255, 255, 255),
                anchor="mm",
            )

    # 状态行：模型的"眼睛"要能读到
    d.text((80, 520), state.get("status", ""), font=_font(17), fill=(52, 56, 66), anchor="lm")
    d.text(
        (80, 560),
        "Cursor crosshair = where your last click landed.",
        font=_font(13),
        fill=(140, 144, 156),
        anchor="lm",
    )

    # 光标：把上一次点击画出来，让模型能自己核对"我刚才点在哪"
    cur = state.get("cursor")
    if cur:
        cx, cy = cur
        d.line([cx - 14, cy, cx + 14, cy], fill=(226, 60, 120), width=3)
        d.line([cx, cy - 14, cx, cy + 14], fill=(226, 60, 120), width=3)
        d.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], outline=(226, 60, 120), width=2)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def to_data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# ---------------------------------------------------------------------------
# 几何：命中判定与偏差测量 —— 全部是纯函数，可单独断言
# ---------------------------------------------------------------------------


def element_at(x: int, y: int) -> dict | None:
    """点 (x, y) 落在哪个元素上？空白处返回 None。"""
    for el in reversed(_ELEMENTS):
        if el["x"] <= x <= el["x"] + el["w"] and el["y"] <= y <= el["y"] + el["h"]:
            return el
    return None


def rect_distance(el: dict, x: int, y: int) -> float:
    """点到矩形的最短距离（点在矩形内为 0）。

    用途：**点击落空时**，量出"离最近的元素还差多远"——这就是定位误差的像素读数。
    """
    dx = max(el["x"] - x, 0, x - (el["x"] + el["w"]))
    dy = max(el["y"] - y, 0, y - (el["y"] + el["h"]))
    return round(math.hypot(dx, dy), 1)


def nearest_distance(x: int, y: int) -> float:
    """点到**所有**元素里最近那个的距离。"""
    return min(rect_distance(el, x, y) for el in _ELEMENTS)


def center_offset(el: dict, x: int, y: int) -> float:
    """点到**这个元素中心**的像素距离。

    ⭐ 为什么需要它：命中率高不代表点得准 —— 一个 560×50 的大输入框，
       随便点哪儿都算"命中"；而只有点空才有值的 rect_distance，在**全部点中**时等于 0，
       读起来像"零误差"，其实是"没有样本"。
       命中率回答"有没有点中"，中心偏差回答"点得多准" —— 两个都要看。
    """
    cx = el["x"] + el["w"] / 2
    cy = el["y"] + el["h"] / 2
    return round(math.hypot(x - cx, y - cy), 1)


# ---------------------------------------------------------------------------
# 给模型看的工具说明书 + 系统提示词
# ---------------------------------------------------------------------------

TOOL_NAME = "computer"

COMPUTER_TOOL: dict = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "操作这台虚拟电脑。一次只做一件事，做完会立刻收到新的屏幕截图，再决定下一步。\n"
            "action 取值：\n"
            "  screenshot —— 重新获取当前屏幕截图（无需其它参数）\n"
            "  left_click —— 在 (x, y) 左键单击。坐标原点在屏幕左上角，x 向右、y 向下\n"
            "  type       —— 向**当前聚焦的输入框**敲入 text\n"
            "  key        —— 按一个键：Enter（提交表单）/ Tab（跳到下一个输入框）/ Backspace（删一个字符）\n"
            "  wait       —— 稍等片刻"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ALLOWED_ACTIONS)},
                "x": {"type": "integer", "description": "left_click 的横坐标（像素）"},
                "y": {"type": "integer", "description": "left_click 的纵坐标（像素）"},
                "text": {"type": "string", "description": "type 要敲入的内容（键盘只有 ASCII）"},
                "keys": {"type": "string", "description": "key 要按的键名，如 Enter"},
            },
            "required": ["action"],
        },
    },
}

SYSTEM_PROMPT = """你是一个「计算机操作智能体」。你**只能**通过 computer 工具操作这台虚拟电脑，
此外看不到任何其它信息——不能读代码，也不能直接改数据。

屏幕与输入规则：
1. 屏幕分辨率 1024x768。坐标原点在左上角：x 向右递增，y 向下递增。
2. 每次只发**一个**动作；动作执行后你会收到新的屏幕截图，再决定下一步。
3. 键盘只会打进**被聚焦**的那个输入框。所以要填某个框，必须**先 left_click 点中它**。
4. type 只能输入 ASCII 字符 —— 不是不想，是这块键盘只有 ASCII。
5. 屏幕上有一根品红色十字准星，标出你**上一次点击**落在哪里。可以用它自查有没有点偏。
6. RESET ALL 是危险按钮，未经人工确认会被沙箱直接拒绝，不要浪费步骤去点它。
7. 确认任务全部完成之后，**不要再调用工具**，直接用一句中文说明结果。
"""

TASK_TEMPLATE = """请操作这台虚拟电脑，在这个「Refund Request」表单里完成任务：

1. 在 ORDER ID 输入框里填入：{order_id}
2. 在 AMOUNT 输入框里填入：{amount}
3. 在 DATE 输入框里填入：{date}
4. 点击 SUBMIT 按钮提交表单

四项都做完后，用一句话说明结果。"""


def build_task(order_id: str, amount: str, date: str) -> str:
    return TASK_TEMPLATE.format(order_id=order_id, amount=amount, date=date)


# ---------------------------------------------------------------------------
# 动作执行
# ---------------------------------------------------------------------------


def _safe_args(raw: str) -> dict:
    """解析 tool_call 的 arguments。模型偶尔会吐出坏 JSON —— 坏就当空参数，别抛。"""
    try:
        data = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _norm_action(name: Any) -> str:
    """归一化动作名：模型常写 Left_Click / left-click / LEFT CLICK。"""
    return str(name or "").strip().lower().replace("-", "_").replace(" ", "_")


def _submit(state: dict, how: str) -> str:
    """提交表单。返回给模型的说明文字。"""
    filled = [k for k in FIELDS if state.get(k, "").strip()]
    if len(filled) == len(FIELDS):
        state["submitted"] = True
        state["status"] = "Submitted. All three fields filled."
        state["revision"] += 1
        return f"{how}：表单已提交，状态变为 Submitted。"
    state["status"] = f"Cannot submit: {len(FIELDS) - len(filled)} field(s) still empty."
    state["revision"] += 1
    return f"{how}：表单**拒绝**提交——还有 {len(FIELDS) - len(filled)} 项是空的。"


def _step(
    state: dict,
    raw_action: Any,
    args: dict,
    allow_dangerous: bool,
) -> tuple[dict, str, str | None]:
    """执行一个动作。

    返回 `(记入 actions 的记录, 回传给模型的文本结果, 键盘落进了哪个字段)`。
    第三个返回值是打分用的：**type 落进哪个字段，是观测到的事实，不是模型的声称**。
    """
    action = _norm_action(raw_action)

    if action not in ALLOWED_ACTIONS:
        err = f"不支持的动作 {raw_action!r}。可用动作：{', '.join(ALLOWED_ACTIONS)}。"
        return {"action": action or "?", "ok": False, "error": err}, err, None

    if action == "screenshot":
        return {"action": action, "ok": True}, "已重新截取屏幕，见下一张截图。", None

    if action == "wait":
        return {"action": action, "ok": True}, "已等待（虚拟时钟，未真实阻塞）。", None

    if action == "left_click":
        x, y = args.get("x"), args.get("y")
        if x is None or y is None:
            err = "left_click 需要同时给出 x 与 y（像素坐标）。"
            return {"action": action, "ok": False, "error": err}, err, None
        try:
            xi, yi = int(round(float(x))), int(round(float(y)))
        except (TypeError, ValueError):
            err = f"坐标必须是数字，收到 x={x!r} y={y!r}。"
            return {"action": action, "ok": False, "error": err}, err, None

        # 坐标钳制：模型给出越界坐标是很常见的，钳制而不是报错（报错会让它卡死）
        nx = min(max(xi, 0), SCREEN_W - 1)
        ny = min(max(yi, 0), SCREEN_H - 1)
        clamp_note = f"（原坐标 ({xi}, {yi}) 越界，已钳制到屏幕内）" if (nx, ny) != (xi, yi) else ""

        el = element_at(nx, ny)
        if el is not None and el.get("dangerous") and not allow_dangerous:
            err = (
                f"{el['label']} 是危险操作，沙箱已直接拒绝（需要人工确认）。"
                "请不要再尝试这个按钮，也不要找别的办法绕过。"
            )
            return {"action": action, "x": nx, "y": ny, "ok": False, "error": err, "target": el["name"]}, err, None

        state["cursor"] = (nx, ny)
        state["revision"] += 1
        rec: dict[str, Any] = {
            "action": action,
            "x": nx,
            "y": ny,
            "ok": True,
            "target": el["name"] if el else None,
            "hit": el is not None,
            # 命中时 = 离该元素中心的距离；点空时 = None（点空看 errorPx）
            "centerOffsetPx": center_offset(el, nx, ny) if el is not None else None,
        }

        if el is None:
            state["focus"] = ""
            return rec, f"点击 ({nx}, {ny}){clamp_note}：该位置**没有任何元素**，焦点已清空。", None

        if el["kind"] == "text_field":
            state["focus"] = el["name"]
            return rec, f"点击 ({nx}, {ny}){clamp_note}：已聚焦 {el['label']} 输入框，现在可以用 type 输入。", None

        state["focus"] = ""
        if el["name"] == "btn_submit":
            return rec, _submit(state, f"点击 ({nx}, {ny}){clamp_note}"), None
        return rec, f"点击 ({nx}, {ny}){clamp_note}：该按钮暂无效果。", None

    # ---- action == "type" ----
    if action == "type":
        text = args.get("text")
        if not isinstance(text, str) or text == "":
            err = "type 需要一个非空的 text 参数。"
            return {"action": action, "ok": False, "error": err}, err, None

        ascii_only = "".join(ch for ch in text if ch.isascii())
        dropped = len(text) - len(ascii_only)
        rec = {"action": action, "text": ascii_only or text, "ok": True}

        focus = state.get("focus") or ""
        if focus in FIELDS:
            state[focus] = (state.get(focus, "") + ascii_only)[:MAX_FIELD_CHARS]
            state["revision"] += 1
            note = f"已向 {FIELD_LABELS[focus]} 敲入 {ascii_only!r}；该框当前值 {state[focus]!r}。"
            if dropped:
                note += f"（{dropped} 个非 ASCII 字符被键盘丢弃）"
            return rec, note, focus

        # ⭐ 键盘被丢弃：模型想打字，但它前一次点击没落在任何输入框上。
        #    这是"定位失败"最硬的证据，而且**零成本**就能观测到。
        return rec, "键盘输入被**丢弃**：当前没有聚焦任何输入框。请先 left_click 点中输入框再输入。", None

    # ---- action == "key" ----
    keys = args.get("keys")
    k = str(keys or "").strip()
    kl = k.lower()
    rec = {"action": action, "keys": k, "ok": True}

    if kl in ("enter", "return", "回车"):
        return rec, _submit(state, "按下 Enter"), None

    if kl in ("backspace", "退格"):
        focus = state.get("focus") or ""
        if focus in FIELDS and state.get(focus):
            state[focus] = state[focus][:-1]
            state["revision"] += 1
            return rec, f"已删除 {FIELD_LABELS[focus]} 最后一个字符；当前值 {state[focus]!r}。", None
        return rec, "没有可删除的内容（没有聚焦的输入框，或该框已是空）。", None

    if kl in ("tab", "制表"):
        order = list(FIELDS)
        cur = state.get("focus") or ""
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        state["focus"] = nxt
        state["revision"] += 1
        return rec, f"焦点已移到 {FIELD_LABELS[nxt]}。", None

    err = f"不支持的按键 {k!r}。支持：Enter / Tab / Backspace。"
    return {"action": action, "keys": k, "ok": False, "error": err}, err, None


# ---------------------------------------------------------------------------
# 状态 → 对外字段
# ---------------------------------------------------------------------------


def public_state(state: dict) -> dict[str, str]:
    """ComputerResponse.state 是 dict[str,str]，所以这里只挑字符串字段。"""
    return {
        "field_order": state.get("field_order", ""),
        "field_amount": state.get("field_amount", ""),
        "field_date": state.get("field_date", ""),
        "focus": state.get("focus", ""),
        "status": state.get("status", ""),
    }


def score(state: dict, expected: dict[str, str], actions: list[dict]) -> dict:
    """确定性评分 —— 一个数字都不来自模型的自述。

    · fieldScore       期望值比对（忽略首尾空白与大小写），0–3
    · submitted        是否真的提交了
    · success          三项全对 **且** 已提交
    · clicks / hitClicks / clickHitRate   点空率
    · avgClickErrorPx     **点空时**离最近元素的平均像素距离（全中时为 0）
    · avgCenterOffsetPx   **命中时**离目标元素中心的平均像素距离 —— 真正的精度读数，
                          全中的那次运行也有值（只有它能横向比较）
    · lostKeystrokes   键盘被丢弃的次数（定位失败的硬证据）
    """
    right = 0
    for name, want in expected.items():
        got = (state.get(name) or "").strip()
        if got.upper() == str(want).strip().upper():
            right += 1

    click_recs = [a for a in actions if a.get("action") == "left_click" and a.get("ok")]
    clicks = len(click_recs)
    hit_recs = [a for a in click_recs if a.get("target")]
    miss_recs = [a for a in click_recs if not a.get("target")]
    errs = [a["errorPx"] for a in miss_recs if a.get("errorPx") is not None]
    offs = [a["centerOffsetPx"] for a in hit_recs if a.get("centerOffsetPx") is not None]

    submitted = bool(state.get("submitted"))
    return {
        "fieldScore": right,
        "fieldTotal": len(expected),
        "submitted": submitted,
        "success": right == len(expected) and submitted,
        "clicks": clicks,
        "hitClicks": len(hit_recs),
        "clickHitRate": round(len(hit_recs) / clicks, 4) if clicks else 0.0,
        "avgClickErrorPx": round(sum(errs) / len(errs), 1) if errs else 0.0,
        "avgCenterOffsetPx": round(sum(offs) / len(offs), 1) if offs else 0.0,
        "lostKeystrokes": sum(1 for a in actions if a.get("lostKeys")),
    }


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def _result_messages(step: int, tool_notes: list[tuple[str | None, str]], shot: str) -> list[dict]:
    """执行完动作后要追加的两类消息。

    ⚠️ 这里是本阶段**最容易踩的协议坑**：
      `role="tool"` 的消息只能装文本，装不了图片。所以新截图不能当 tool 结果回传，
      必须再补一条 `role="user"` 的多模态消息把图带上。
      而每个 tool_call 又**必须**有配对的 tool 消息，否则 API 直接报错。
      两件事都得做，顺序也不能反 —— 见下面的 callers。
    """
    msgs: list[dict] = []
    for call_id, note in tool_notes:
        msgs.append({"role": "tool", "tool_call_id": call_id, "content": note})
    msgs.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"第 {step} 轮动作已执行完毕。这是**执行后**的新屏幕截图："},
                {"type": "image_url", "image_url": {"url": shot}},
            ],
        }
    )
    return msgs


def _shot(state: dict, buckets: dict[str, float]) -> str:
    t0 = time.perf_counter()
    png = render_screen(state)
    buckets["render"] = round(buckets.get("render", 0.0) + (time.perf_counter() - t0) * 1000, 2)
    return to_data_url(png)


def run_computer(
    client: LLMClient,
    task: str | None = None,
    *,
    order_id: str = DEFAULT_ORDER_ID,
    amount: str = DEFAULT_AMOUNT,
    date: str = DEFAULT_DATE,
    model: str | None = None,
    temperature: float = 0.2,
    max_steps: int = 8,
    allow_dangerous: bool = False,
) -> Iterator[dict]:
    """跑一轮 Computer Use。逐帧产出（`type` 即帧类型）：

        start    任务 / 模型 / 屏幕规格（含元素真值）/ 期望值 / 上限
        screen   一张屏幕截图 + 当前状态（step=0 是初始屏）
        delta    模型这一轮正在说的话（流式增量）
        action   执行并评分过的一个动作
        finish   完整结果 + 两套确定性读数 + 耗时
        error    失败原因（发出后**仍会**继续走到 finish，带上已有的部分结果）

    ⭐ 为什么 finish 一定要发：异常一旦冒出生成器，流就断了，前端连半截结果都看不到。
    """
    expected = {"field_order": order_id, "field_amount": amount, "field_date": date}
    task_text = (task or "").strip() or build_task(order_id, amount, date)

    started = time.perf_counter()
    buckets: dict[str, float] = {"render": 0.0, "llm": 0.0}

    state = initial_state()
    first_shot = _shot(state, buckets)

    yield {
        "type": "start",
        "task": task_text,
        "model": model or client.model,
        "screen": screen_spec(),
        "expected": expected,
        "maxSteps": max_steps,
        "allowDangerous": allow_dangerous,
    }
    yield {"type": "screen", "step": 0, "image": first_shot, "state": public_state(state), "revision": state["revision"]}

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": task_text},
                {"type": "image_url", "image_url": {"url": first_shot}},
            ],
        },
    ]

    actions: list[dict] = []
    transcript: list[str] = []
    failure: str | None = None
    rounds = 0

    for step in range(1, max_steps + 1):
        t0 = time.perf_counter()
        content = ""
        message: dict | None = None
        try:
            for ev in client.stream_message(
                messages, model=model, temperature=temperature, tools=[COMPUTER_TOOL], tool_choice="auto"
            ):
                if "delta" in ev:
                    content += ev["delta"]
                    yield {"type": "delta", "step": step, "text": ev["delta"]}
                elif "message" in ev:
                    message = ev["message"]
        except Exception as e:  # noqa: BLE001
            buckets["llm"] = round(buckets["llm"] + (time.perf_counter() - t0) * 1000, 2)
            failure = f"第 {step} 轮调用模型失败：{e}"
            yield {"type": "error", "message": failure}
            break
        buckets["llm"] = round(buckets["llm"] + (time.perf_counter() - t0) * 1000, 2)

        content = (content or "").strip()
        if content:
            transcript.append(content)

        if message is None:
            failure = f"第 {step} 轮没有拿到模型回复。"
            yield {"type": "error", "message": failure}
            break

        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            # 模型不再调用工具 → 它认为任务结束（这是唯一的正常出口）
            break

        rounds = step
        notes: list[tuple[str | None, str]] = []

        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args = _safe_args(fn.get("arguments") or "")
            call_id = call.get("id")

            if name != TOOL_NAME:
                note = f"没有名为 {name!r} 的工具，请使用 {TOOL_NAME}。"
                notes.append((call_id, note))
                continue

            rec, note, landed = _step(state, args.get("action"), args, allow_dangerous)
            rec["step"] = step
            rec["note"] = note  # 原样带上：这是模型**看到**的反馈，时间线直接显示
            # 键盘被丢弃 → 打上标记（这是定位失败的硬证据，评分要用）
            if rec.get("action") == "type" and landed is None and rec.get("ok"):
                rec["lostKeys"] = True
            # 点空的点击：量出它离最近的元素还有多远
            if rec.get("action") == "left_click" and rec.get("ok") and rec.get("target") is None:
                rec["errorPx"] = nearest_distance(rec["x"], rec["y"])
            actions.append(rec)
            yield {"type": "action", **rec}
            notes.append((call_id, note))

        # ⚠️ 先补齐 tool 结果（每个 call 都要有一条），再用 user 消息把新截图带上
        shot = _shot(state, buckets)
        messages.extend(_result_messages(step, notes, shot))
        yield {"type": "screen", "step": step, "image": shot, "state": public_state(state), "revision": state["revision"]}

    sc = score(state, expected, actions)
    payload: dict[str, Any] = {
        "task": task_text,
        "model": model or client.model,
        "screen": screen_spec(),
        "actions": actions,
        "steps": rounds,
        "state": public_state(state),
        **sc,
        "transcript": transcript,
        "times": dict(buckets),
        "totalMs": round((time.perf_counter() - started) * 1000, 2),
        "llmMs": round(buckets["llm"], 2),
        "error": failure,
    }
    yield {"type": "finish", **payload}


def run_computer_blocking(client: LLMClient, task: str | None = None, **kwargs: Any) -> dict:
    """把生成器跑完，收成一份完整结果（给非流式调用方 / 端点用）。"""
    events = list(run_computer(client, task, **kwargs))
    start = next((e for e in events if e["type"] == "start"), {})
    finish = next((e for e in events if e["type"] == "finish"), None)

    if finish is not None:
        out = dict(finish)
        out.pop("type", None)
        return out

    # 理论上到不了这里（finish 一定会发）；保底也给一份**形状完整**的结果，
    # 别返回残缺 dict —— 端点要拿它 `ComputerResponse(**result)`。
    err = next((e for e in events if e["type"] == "error"), None)
    action_frames = [{k: v for k, v in e.items() if k != "type"} for e in events if e["type"] == "action"]
    empty_state = initial_state()
    fallback_expected = {
        "field_order": kwargs.get("order_id", DEFAULT_ORDER_ID),
        "field_amount": kwargs.get("amount", DEFAULT_AMOUNT),
        "field_date": kwargs.get("date", DEFAULT_DATE),
    }
    return {
        "task": start.get("task") or (task or ""),
        "model": start.get("model") or kwargs.get("model") or client.model,
        "screen": start.get("screen") or screen_spec(),
        "actions": action_frames,
        "steps": 0,
        "state": public_state(empty_state),
        **score(empty_state, fallback_expected, action_frames),
        "transcript": [],
        "times": {},
        "totalMs": 0.0,
        "llmMs": 0.0,
        "error": (err or {}).get("message") or "未知错误",
    }
