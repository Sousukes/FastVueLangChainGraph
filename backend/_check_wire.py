#!/usr/bin/env python3
"""线格式契约检查器：后端「发的字段」vs 前端「读的字段」。

## 为什么需要它

阶段 10/11/12 出过同一族缺陷，而**五道验收全都抓不到**：

  1. 后端在 `finish` 帧发了 `trace`（含每步真实 `llmMs`），前端**从没读** `p.trace`
     → 时间线每步恒显示 `LLM 0ms`（而总耗时是秒级）。
  2. `team.py` 的 `agent_end` **只发 `task`**，而 `useTeam.ts` 读的是 `p.agent`
     → `ensureColumn('')` 造出一个幽灵列并把它标 ✓，真正的角色列永远停在「运行中」。

这两类都是「**读了但读错 / 该读的没读**」：
`vue-tsc` 全绿（两个字段都是可选字符串，读错只得到 `undefined`）、静态测试全绿、构建全绿。
所以需要一个**专门的线格式对账**，和 `_check_globals.py` 是一对：
那个查「名字没定义」，这个查「字段对不上」。

## 它查什么

  ① 【错误】前端读了某帧的字段 X，而 X 在后端**连字符串字面量都不是**
     → 任何发射路径（静态的、或 `**` 动态构造的）都产不出它 → 铁定是键名写错。
     ⚠️ 这条判定**不受「该帧有 `**` 展开」影响**（踩过：曾把它也降级，结果往 `finish`
        里塞一个根本不存在的字段时检查器竟然 exit 0 —— 等于废了最有价值的一条判据）。
  ② 【需人工确认】前端读了某帧的字段 X，该帧不发 X，但 X 在**别处**出现过
     （多半是 `{**ev, "k": v}` 注入，无法静态归属）。**阶段 11 的 `agent_end` bug 属这一档**
     ——`agent` 被注入进了 `delta`/`observation`，但**没有**注入进 `agent_end`。
     ⚠️ 这一档**不能静默**，否则当初那个 bug 就抓不到了。
  ③ 【警告】后端在某帧发了字段 X，但前端**从没读** → 大概率是「算了没接」（`trace` 属这一类）。
     ⚠️ 警告**大多是良性的**（请求回显字段、只给后端日志用的 `ms`/`raw`/`detail`）。
  ④ 【提示】某帧只有单边存在（后端在发但前端无 `case`，或反过来）。

## ⚠️ 为了不「乱报警」，做了两处克噪（都很关键，别删）

  - **`??` 回落组**：`p.agent ?? p.task ?? ''` 是**故意**读一个可能不存在的键。
    同一串 `??` 链里的字段视为**或组**，只要组内**有任意一个**字段该帧确实会发，整组都算合法。
  - **注入键**：`yield {**ev, "agent": tid}`（team.py:609/629）把字段**动态并进**某个帧，
    无法按帧归属。这些键（以及任何在后端出现过的名字）**只用来把①降级为②**，
    绝不直接静默 —— 否则真 bug 会被一起吞掉。

## 判定范围与已知局限（**别把它的沉默当成证明**）

- 后端：`ast` 扫 `backend/*.py`（跳过 `_t_*.py` / 本文同级自检脚本）。
- 前端：文本扫 `frontend/src/composables/use*.ts` 里 `switch (p.type)` 的 `case '<帧>':` 块，
  收集块内的 `p.<字段>`。
  ⇒ **只覆盖「同一层」的读取**。像 `useTeam` 里 `const r = p.result` 之后再读 `r.answer`
  这种**下一层嵌套**看不到（那层另有字段，例如 `agent_result.result.trace`）。
  ⇒ **前端读取是跨 composable 求并集的**：只要**任一**页面读了某字段，别处的漏读就看不出来。
- **它只看「读没读这个字段」，看不出「读了但没用到对的地方」。**
  最典型的就是 `trace`：`useAgent` 从一开始就写了 `trace.value = p.trace ?? []`
  （只拿去渲染「查看结构化 trace」的原始 JSON），**字段读了**，但没拿它回填每步的 `llmMs`
  —— 于是 `LLM 0ms` 那个 bug **本检查器抓不到**，`WARNS` 里也不会出现 `trace`。
- 因此：**它抓到的一定值得看；它没抓到不代表没问题。**
  「键名错 / 漏读」它能管，「读了没用对」得靠人看读数对不对得上。

## 退出码

- `0` 只有警告/提示（或全干净）
- `1` 出现①号错误 —— 可挂 CI（与 `_check_globals.py` 并列成第六道验收）
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
FRONTEND = BACKEND.parent / "frontend" / "src" / "composables"

# 帧里必然存在的、不需要对账的键
IGNORED = {"type"}


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _split_dict(d: ast.Dict, locals_: dict[str, set[str]]) -> tuple[set[str], set[str], bool]:
    """拆一个字典字面量 → (常量键集合, 被 `**` 并入的键集合, 是否有解析不出的 `**`)."""
    fields: set[str] = set()
    injected: set[str] = set()
    unresolved = False
    for k, v in zip(d.keys, d.values):
        if k is None:  # `**expr`
            name = _const_str(v)
            if name is not None and name in locals_:
                merged = locals_[name]
                fields |= merged
                injected |= merged          # 这台帧吃到了别处的键 → 记入全局注入键
            else:
                unresolved = True
            continue
        key = _const_str(k)
        if key is not None:
            fields.add(key)
    return fields, injected, unresolved


def scan_backend() -> tuple[dict[str, dict], set[str]]:
    """返回 ({帧名: {fields, dynamic, where}}, 全局注入键集合)。"""
    frames: dict[str, dict] = {}
    all_injected: set[str] = set()

    for path in sorted(BACKEND.glob("*.py")):
        if path.name.startswith(("_t_", "_check_")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as e:
            print(f"  [skip] {path.name}: {e}")
            continue

        # 第一遍：模块级 + 函数级的「变量名 -> 常量键集合」，供 `**name` 解析
        locals_by_fn: dict[int, dict[str, set[str]]] = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            table: dict[str, set[str]] = {}
            for stmt in ast.walk(fn):
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    tgt, val = stmt.targets[0], stmt.value
                    if isinstance(tgt, ast.Name) and isinstance(val, ast.Dict):
                        table.setdefault(tgt.id, set()).update(_split_dict(val, table)[0])
                    elif (
                        isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and _const_str(tgt.slice) is not None
                    ):
                        # payload["x"] = ... 这种逐项写入
                        table.setdefault(tgt.value.id, set()).add(_const_str(tgt.slice))
            locals_by_fn[id(fn)] = table

        # 第二遍：找「含常量 type 的字典」
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            table = locals_by_fn.get(id(fn), {})
            for d in ast.walk(fn):
                if not isinstance(d, ast.Dict):
                    continue
                type_val = None
                for k, v in zip(d.keys, d.values):
                    if _const_str(k) == "type":
                        type_val = _const_str(v)
                        break
                if not type_val:
                    continue
                fields, injected, unresolved = _split_dict(d, table)
                slot = frames.setdefault(
                    type_val, {"fields": set(), "dynamic": False, "where": []}
                )
                slot["fields"] |= fields - IGNORED
                slot["dynamic"] = slot["dynamic"] or unresolved
                slot["where"].append(f"{path.name}:{getattr(d, 'lineno', '?')}")
                all_injected |= injected - IGNORED

    # 全局注入键：任何 `{**ev, "k": v}` 里的字面量键（不论归属哪个帧）
    for path in sorted(BACKEND.glob("*.py")):
        if path.name.startswith(("_t_", "_check_")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for d in ast.walk(tree):
            if isinstance(d, ast.Dict) and any(k is None for k in d.keys):
                for k in d.keys:
                    key = _const_str(k)
                    if key is not None:
                        all_injected.add(key)

    return frames, all_injected - IGNORED


CASE_RE = re.compile(r"case\s+'([^']+)'\s*:")
FIELD_RE = re.compile(r"\bp\.([A-Za-z_$][\w$]*)")


def collect_all_backend_strings() -> set[str]:
    """后端所有字符串字面量（含任意字典/赋值/f-string 片段）。

    用途：判定「这个字段名在全仓库**任何地方**都没出现过」。
    若一个标识符连字面量都不是，那么**任何**发射路径（静态的或 `**` 动态构造的）
    都不可能产出它 → 前端读它就是铁定写错了，与「该帧字段是否看得全」无关。
    这正是「dynamic 帧」不该把这条判定也一起降级的原因。
    """
    found: set[str] = set()
    for path in sorted(BACKEND.glob("*.py")):
        if path.name.startswith(("_t_", "_check_")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
            elif isinstance(node, ast.JoinedStr):  # f-string 的常量片段
                for part in node.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        found.add(part.value)
    return found
# 一串 `p.a ?? p.b ?? ...`（故意读可能不存在的键）
CHAIN_RE = re.compile(r"(?:p\.[A-Za-z_$][\w$]*\s*\?\?\s*)+p\.[A-Za-z_$][\w$]*")
CHAIN_FIELD_RE = re.compile(r"\bp\.([A-Za-z_$][\w$]*)")


def _switch_blocks(text: str) -> list[str]:
    """找出所有 `switch (X.type) { ... }` 的块内容。"""
    out = []
    for m in re.finditer(r"switch\s*\(\w+\.type\)\s*\{", text):
        start, depth, i = m.end(), 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        out.append(text[start : i - 1])
    return out


def scan_frontend() -> tuple[dict[str, set[str]], dict[str, list[set[str]]]]:
    """返回 ({帧名: 读到的字段}, {帧名: [或组, ...]})。"""
    frames: dict[str, set[str]] = {}
    groups: dict[str, list[set[str]]] = {}

    for path in sorted(FRONTEND.glob("use*.ts")):
        for block in _switch_blocks(path.read_text(encoding="utf-8")):
            marks = [(m.start(), m.group(1)) for m in CASE_RE.finditer(block)]
            for idx, (pos, name) in enumerate(marks):
                end = marks[idx + 1][0] if idx + 1 < len(marks) else len(block)
                seg = block[pos:end]
                frames.setdefault(name, set()).update(FIELD_RE.findall(seg))
                for chain in CHAIN_RE.finditer(seg):
                    members = set(CHAIN_FIELD_RE.findall(chain.group(0)))
                    if len(members) > 1:
                        groups.setdefault(name, []).append(members)
    return frames, groups


def main() -> int:
    bframes, injected = scan_backend()
    all_strings = collect_all_backend_strings()
    fframes, groups = scan_frontend()

    print("=== 后端发出的帧 ===")
    for name in sorted(bframes):
        info = bframes[name]
        flag = "  ← 有 ** 展开，字段看不全" if info["dynamic"] else ""
        print(f"  {name:<20} {len(info['fields']):>2} 字段{flag}")

    print()
    print(f"=== 全局注入键（来自 `{{**ev, \"k\": v}}`，无法按帧归属）===")
    print(f"  {sorted(injected) if injected else '(无)'}")

    errors: list[str] = []
    reviews: list[str] = []
    warns: list[str] = []
    notes: list[str] = []

    for name in sorted(set(bframes) | set(fframes)):
        b = bframes.get(name)
        f = fframes.get(name)
        if b is None:
            notes.append(f"前端有 case '{name}'，但后端没有任何模块发这个帧")
            continue
        if f is None:
            notes.append(f"后端在发 '{name}'，但前端没有 case 分支（{b['where'][0]}）")
            continue

        # 或组：只要组内有成员该帧确实会发，整组合法
        excused: set[str] = set()
        for group in groups.get(name, []):
            if group & b["fields"]:
                excused |= group

        f_only = sorted(f - b["fields"] - excused)
        if f_only:
            where = ", ".join(b["where"][:3])
            # hard：这个名字在后端**连字符串字面量都不是** → 任何发射路径（含 `**` 动态构造）
            #       都产不出它。因此这条判定**与「该帧是否看得全」无关，dynamic 也不降级**。
            #       （踩过：一开始把 dynamic 帧的 hard 也降级成提示，于是往 finish 里塞一个
            #        根本不存在的字段时，检查器竟然 exit 0 —— 等于把最有价值的一条判据废了。）
            hard = [x for x in f_only if x not in all_strings]
            soft = [x for x in f_only if x in all_strings]
            if hard:
                errors.append(
                    f"case '{name}' 读了 {hard} —— 这些名字在后端**连字符串字面量都不是**，"
                    f"任何发射路径都产不出（该帧发的是 {sorted(b['fields'])}；出处 {where}）"
                )
            if soft:
                reviews.append(
                    f"case '{name}' 读了 {soft} —— 该帧不发，但这些名字在别处出现过"
                    f"（多半是 `{{**ev, \"k\": v}}` 注入或别的帧的字段，无法静态归属）。"
                    f"该帧实际发 {sorted(b['fields'])}；出处 {where}"
                )

        b_only = sorted(b["fields"] - f)
        if b_only:
            where = ", ".join(b["where"][:3])
            tag = "[含 ** 展开，请人工确认] " if b["dynamic"] else ""
            warns.append(f"帧 '{name}' 后端发了 {b_only} 但前端从没读（出处 {where}）{tag}")

    print()
    if errors:
        print(f"❌ 错误 {len(errors)} 处 —— 前端读的字段，后端**任何地方**都没发过：")
        for e in errors:
            print("   · " + e)
    if reviews:
        print(f"\n🔍 需人工确认 {len(reviews)} 处 —— 该帧不发这个字段，但它被别处注入过：")
        print("   （无法静态判断是否落到该帧；**阶段 11 的 agent_end bug 就属这一档**）")
        for e in reviews:
            print("   · " + e)
    if warns:
        print(f"\n⚠️  警告 {len(warns)} 处 —— 后端发了但前端从没读（疑似「算了没接」）：")
        print("   （多数是良性的请求回显 / 调试字段，扫一眼即可）")
        for e in warns:
            print("   · " + e)
    if notes:
        print(f"\nℹ️  提示 {len(notes)} 处：")
        for e in notes:
            print("   · " + e)
    if not (errors or reviews or warns or notes):
        print("\n✅ 线格式对账干净：没有「读了不发」，也没有「发了不读」。")

    print()
    print(f"ERRORS={len(errors)}  REVIEWS={len(reviews)}  WARNS={len(warns)}  NOTES={len(notes)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
