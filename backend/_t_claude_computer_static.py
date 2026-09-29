"""阶段 18B 静态冒烟测试（**不需要 Claude Key**）。

核心问题：本机没有 `CLAUDE_API_KEY`，那还能验证什么？

答案是——**能验证我们发出去的东西**。线格式（wire format）是**我方**的产物：
请求头、工具定义、消息结构、SSE 解析、tool_result 的形状，全都由我们的代码决定。
既然决定权在我们手里，就不必等对方回话才能测。

做法：起一个本地 `http.server` **假扮 Anthropic Messages API**，它做两件事：
  ① 记录我们**真实发出的** HTTP 请求（头 + body）；
  ② 按脚本回放**真实形状的 SSE**（含被切成三段的 input_json_delta）。

于是这一条链路被完整验证了：
    ClaudeBrain._payload ─▶ httpx ─▶ 真实 HTTP ─▶ 记录并断言
    ClaudeBrain.decide 的 SSE 解析 ◀─ 脚本化事件流 ◀─┘

⚠️ 但必须说清楚**没验证什么**：mock 是按「我以为的协议」写的，
   它只能证明「我的实现和我的理解自洽」，**不能**证明我的理解与 Anthropic 一致。
   真正的交叉验证需要一把 Key 打一次真接口。这一点在 18B 文档里如实标注。

验证十五件事：
  1. 无 Key → ClaudeNotConfigured（显式报错，**不静默降级**）。
  2. beta 头与 tool 版本**成对**；未知版本 → ClaudeConfigError。
  3. messages_url 归一化：四种 base_url 写法得到同一个 URL。
  4. split_data_url / image_block：data URL 正确拆成 media_type + base64。
  5. claude_input_to_args：coordinate→x/y、key 用 text→我们内部的 keys。
  6. SSE 行解析健壮性：非 data 行 / 坏 JSON / [DONE] 一律安静略过。
  7. 请求头：x-api-key / anthropic-version / anthropic-beta 三件套齐全。
  8. 请求体：内置工具**没有 input_schema**（与 DeepSeek 的自写 schema 形成对照）。
  9. 开场消息：text 块 + image 块，图是 base64 PNG。
 10. tool_result 形状：**一条** user 消息、tool_result 排在文字**前面**、图塞在 tool_result 里。
 11. tool_use_id 配对，且 assistant 消息里回放的 input 是**拼装后**的完整对象。
 12. 完整任务闭环：success、3/3 字段、4/4 点击命中、rejectedActions=0。
 13. 白名单挡住 Claude 的丰富动作词（mouse_move）→ 计数 +1，且流程继续走到 finish。
 14. 401 → ClaudeAPIError（大脑层），端点层转 502。
 15. 流式端点**先校验再开流**：无 Key 时立刻 400，而不是开流后才发 error 帧。
"""

import contextlib
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brains
import computer
import main
import schemas

TEST_KEY = "test-key-12345"

# 屏幕上四个元素的**真实中心**（由 computer.screen_spec() 的 bbox 算出）。
# 刻意写死而不是在测试里现算：这样几何一旦被改动，测试会失败，而不是悄悄跟着变。
CLICKS = [
    ("toolu_01", (360, 149), "field_order", "先点 ORDER ID 输入框。"),
    ("toolu_03", (360, 239), "field_amount", None),
    ("toolu_05", (360, 329), "field_date", None),
    ("toolu_07", (170, 438), "btn_submit", "填完了，提交。"),
]
TYPES = [
    ("toolu_02", "ORDER-2026-0917"),
    ("toolu_04", "2158.50"),
    ("toolu_06", "2026-09-21"),
]


# ---------------------------------------------------------------------------
# 假扮 Anthropic 的本地服务
# ---------------------------------------------------------------------------


def _sse(*events: dict) -> list[str]:
    """把事件序列编成 SSE 行（含结尾空行）。"""
    out: list[str] = []
    for ev in events:
        out.append("event: " + str(ev.get("type")))
        out.append("data: " + json.dumps(ev, ensure_ascii=False))
        out.append("")
    return out


def _text_reply(text: str) -> list[str]:
    """Claude 说一句话就收工（stop_reason=end_turn）—— 这是闭环的正常出口。"""
    return _sse(
        {"type": "message_start", "message": {"id": "msg_x", "role": "assistant", "content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    )


def _tool_reply(
    tool_id: str,
    action: dict,
    *,
    narration: str | None = None,
    fragment: bool = False,
    tool_name: str = "computer",
) -> list[str]:
    """造一条「（可选）先说一句 + 调一次 computer 工具」的响应。

    `fragment=True` 时把工具入参的 JSON **切成三段**下发 —— 真实 API 就是一个
    token 一个 token 吐的，这里刻意在 `left_cli|ck` 这种地方断开，
    用最笨的「覆盖式」实现会立刻拼坏。
    """
    payload = json.dumps(action, ensure_ascii=False)
    evs: list[dict] = [
        {"type": "message_start", "message": {"id": "msg_x", "role": "assistant", "content": []}}
    ]
    if narration:
        evs += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": narration}},
            {"type": "content_block_stop", "index": 0},
        ]
    evs.append(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
        }
    )
    pieces = (payload[:9], payload[9:17], payload[17:]) if fragment else (payload,)
    for piece in pieces:
        evs.append(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": piece},
            }
        )
    evs += [
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    return _sse(*evs)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # 让 body 以「连接关闭」界定，省掉 chunked 的麻烦

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {"_unparsable": raw.decode("utf-8", "replace")}

        srv = self.server
        srv.requests.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )

        if srv.status >= 400:
            payload = json.dumps(srv.error_body, ensure_ascii=False).encode("utf-8")
            self.send_response(srv.status)
            self.send_header("content-type", "application/json")
        else:
            payload = ("\n".join(srv.next_sse()) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # 别把测试输出刷满
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, script: list[list[str]], status: int = 200) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = list(script)
        self.requests: list[dict] = []
        self.status = status
        self.error_body = {
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        }
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def next_sse(self) -> list[str]:
        with self._lock:
            if self.script:
                return self.script.pop(0)
        return _text_reply("任务已完成。")  # 脚本用完了就收工，避免测试挂死


@contextlib.contextmanager
def mock_anthropic(script: list[list[str]], status: int = 200):
    """起 mock 服务，并把环境变量临时指向它（退出时完整还原）。"""
    srv = _Server(script, status=status)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    saved = {k: os.environ.get(k) for k in ("CLAUDE_API_KEY", "CLAUDE_BASE_URL")}
    os.environ["CLAUDE_API_KEY"] = TEST_KEY
    os.environ["CLAUDE_BASE_URL"] = f"http://127.0.0.1:{srv.port}"
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _script_fill_form() -> list[list[str]]:
    """7 步填完表单 + 一步收尾 —— 与阶段 18 的 DeepSeek 走的是同一个任务。"""
    script: list[list[str]] = []
    by_id = {cid: (xy, name, nar) for cid, xy, name, nar in CLICKS}
    for cid, (x, y), name, nar in CLICKS:
        script.append(
            _tool_reply(
                cid,
                {"action": "left_click", "coordinate": [x, y]},
                narration=nar,
                fragment=True,  # 第一个动作刻意用碎片下发，压一压解析器
            )
        )
        for tid, text in TYPES:
            if int(tid.split("_")[1]) == int(cid.split("_")[1]) + 1:
                script.append(_tool_reply(tid, {"action": "type", "text": text}))
                break
    script.append(_text_reply("三个字段都填好并提交了。"))
    return script


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_no_key_is_explicit():
    """① 无 Key → ClaudeNotConfigured，且 **不会** 偷偷退化成 DeepSeek。"""
    saved = os.environ.pop("CLAUDE_API_KEY", None)
    try:
        try:
            brains.ClaudeBrain(width=1024, height=768)
            raise AssertionError("无 Key 时竟然构造成功了")
        except brains.ClaudeNotConfigured as e:
            assert "CLAUDE_API_KEY" in str(e), e

        # make_brain('claude') 同样要报错，不能回退
        try:
            brains.make_brain("claude", width=1024, height=768)
            raise AssertionError("provider=claude 无 Key 时竟然没报错")
        except brains.ClaudeNotConfigured:
            pass

        # auto 无 Key → 走 DeepSeek（client 为 None 时给出明确提示）
        try:
            brains.make_brain("auto", client=None, width=1024, height=768)
            raise AssertionError("auto 无 Key 无 client 时竟然没报错")
        except brains.ClaudeNotConfigured:
            pass

        # 非法 provider 名
        try:
            brains.make_brain("gpt", width=1024, height=768)
            raise AssertionError("非法 provider 竟然通过")
        except ValueError:
            pass
    finally:
        if saved is not None:
            os.environ["CLAUDE_API_KEY"] = saved
    print("✅ 1. 无 Key → ClaudeNotConfigured（显式报错，绝不静默降级）")


def test_beta_pairs_with_tool():
    """② beta 头与 tool 版本成对；未知版本要被拦下（空值 = 用默认，不算错）。"""
    assert brains.claude_beta_header("computer_20250124") == "computer-use-2025-01-24"
    assert brains.claude_beta_header("computer_20251124") == "computer-use-2025-11-24"
    assert brains.claude_beta_header("computer_20241022") == "computer-use-2024-10-22"
    # 空 / None 表示"没指定"，回落到默认值 —— 这是刻意语义，不是漏校验
    assert brains.claude_beta_header("") == brains.claude_beta_header(None)
    assert brains.claude_beta_header("") == brains.CLAUDE_BETA_BY_TOOL[brains.DEFAULT_CLAUDE_TOOL]
    for bad in ("computer_9999", "computer", "not_a_tool"):
        try:
            brains.claude_beta_header(bad)
            raise AssertionError(f"beta: {bad!r} 竟然通过")
        except brains.ClaudeConfigError:
            pass
        try:
            brains.claude_tool_spec(bad, width=1024, height=768)
            raise AssertionError(f"tool: {bad!r} 竟然通过")
        except brains.ClaudeConfigError:
            pass
    tool = brains.claude_tool_spec(width=1024, height=768)
    assert tool["name"] == "computer" and tool["display_width_px"] == 1024
    print("✅ 2. beta 头与 tool 版本成对（三种版本），未知版本 → ClaudeConfigError，空值→默认")


def test_url_normalisation():
    """③ 四种 base_url 写法都归一成同一个 messages 端点。"""
    want = "https://api.anthropic.com/v1/messages"
    for base in (
        "https://api.anthropic.com",
        "https://api.anthropic.com/",
        "https://api.anthropic.com/v1",
        "https://api.anthropic.com/v1/",
    ):
        got = brains.messages_url(base)
        assert got == want, (base, got)
    assert brains.messages_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080/v1/messages"
    print("✅ 3. messages_url 归一化：4 种写法 → https://api.anthropic.com/v1/messages")


def test_image_helpers():
    """④ data URL → Anthropic 的 base64 source。"""
    media, b64 = brains.split_data_url("data:image/png;base64,QUJD")
    assert (media, b64) == ("image/png", "QUJD"), (media, b64)
    blk = brains.image_block("data:image/png;base64,QUJD")
    assert blk == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }, blk
    # 裸 base64 也不该炸
    media2, b642 = brains.split_data_url("QUJD")
    assert (media2, b642) == ("image/png", "QUJD")
    print("✅ 4. split_data_url / image_block：data URL 正确拆成 media_type + base64")


def test_input_normalisation():
    """⑤ Claude 的入参 → 我们内部 _step 认的字段名。"""
    assert brains.claude_input_to_args({"action": "left_click", "coordinate": [360, 149]}) == {
        "action": "left_click",
        "x": 360,
        "y": 149,
    }
    # ⚠️ key 的键名 Claude 放在 text 里，我们内部叫 keys
    assert brains.claude_input_to_args({"action": "key", "text": "Return"}) == {
        "action": "key",
        "keys": "Return",
    }
    assert brains.claude_input_to_args({"action": "type", "text": "AB"}) == {
        "action": "type",
        "text": "AB",
    }
    # 不认识的动作原样留着 → 交给白名单拒绝（这是设计，不是遗漏）
    got = brains.claude_input_to_args({"action": "scroll", "scroll_direction": "down"})
    assert got == {"action": "scroll"}, got
    print("✅ 5. claude_input_to_args：coordinate→x/y、key 的 text→keys、未知动作原样透传")


def test_sse_line_robustness():
    """⑥ SSE 行解析：非 data 行 / 坏 JSON / [DONE] 安静略过。"""
    p = brains._parse_sse_line
    assert p("") is None
    assert p("event: content_block_delta") is None
    assert p(": 注释行") is None
    assert p("data: [DONE]") is None
    assert p("data: {坏 JSON") is None
    assert p("data: [1,2,3]") is None  # 不是对象
    assert p('data: {"type":"message_stop"}') == {"type": "message_stop"}
    assert p('data:{"type":"ping"}') == {"type": "ping"}  # 冒号后没空格也要认
    print("✅ 6. SSE 解析健壮性：6 种坏行全部安静略过，正常行照常解析")


def test_wire_format_and_loop():
    """⑦⑧⑨⑩⑪⑫ 一次真实闭环，逐项断言我们发出的线格式 + 最终读数。"""
    with mock_anthropic(_script_fill_form()) as srv:
        events = list(computer.run_computer(None, provider="claude", max_steps=8))
        fin = next(e for e in events if e["type"] == "finish")
        start = next(e for e in events if e["type"] == "start")

        # ---- ⑧ 请求头三件套 ----
        assert len(srv.requests) == 8, len(srv.requests)
        h = srv.requests[0]["headers"]
        assert h.get("x-api-key") == TEST_KEY, h.get("x-api-key")
        assert h.get("anthropic-version") == brains.ANTHROPIC_VERSION
        assert h.get("anthropic-beta") == "computer-use-2025-01-24", h.get("anthropic-beta")
        assert srv.requests[0]["path"] == "/v1/messages", srv.requests[0]["path"]

        # ---- ⑨ 请求体：内置工具没有 input_schema ----
        b0 = srv.requests[0]["body"]
        assert b0["model"] and isinstance(b0["max_tokens"], int) and b0["max_tokens"] > 0
        assert b0["stream"] is True
        assert isinstance(b0["system"], str) and len(b0["system"]) > 50
        tools = b0["tools"]
        assert len(tools) == 1 and tools[0]["name"] == "computer"
        assert tools[0]["type"] == "computer_20250124"
        assert tools[0]["display_width_px"] == 1024 and tools[0]["display_height_px"] == 768
        # ⭐ 与 DeepSeek 的自写 schema 最根本的差别：这里**没有**入参定义
        for forbidden in ("parameters", "input_schema"):
            assert forbidden not in tools[0], f"内置工具竟然带了 {forbidden}"

        # ---- ⑩ 开场消息：text + image ----
        m0 = b0["messages"][0]
        assert m0["role"] == "user"
        assert m0["content"][0]["type"] == "text"
        img = m0["content"][1]
        assert img["type"] == "image" and img["source"]["type"] == "base64"
        assert img["source"]["media_type"] == "image/png" and len(img["source"]["data"]) > 1000

        # ---- ⑪ 第二轮：tool_result 的形状与配对 ----
        msgs1 = srv.requests[1]["body"]["messages"]
        asst = [m for m in msgs1 if m["role"] == "assistant"]
        assert len(asst) == 1, asst
        blk = [c for c in asst[0]["content"] if c["type"] == "tool_use"]
        assert len(blk) == 1 and blk[0]["id"] == "toolu_01"
        # ⭐ 工具入参被切成三段下发，这里必须是**拼装后**的完整对象
        assert blk[0]["input"] == {"action": "left_click", "coordinate": [360, 149]}, blk[0]["input"]

        last = msgs1[-1]
        assert last["role"] == "user", last["role"]
        assert last["content"][0]["type"] == "tool_result", "tool_result 必须排在最前"
        tr = last["content"][0]
        assert tr["tool_use_id"] == "toolu_01", tr["tool_use_id"]
        assert tr["content"][0]["type"] == "text"
        # ⭐ 图直接塞在 tool_result 里 —— 与 OpenAI 的「tool 消息只能装文本」正相反
        assert tr["content"][1]["type"] == "image", tr["content"]
        assert len(tr["content"][1]["source"]["data"]) > 1000
        # ⭐ 整个对话只有 3 条消息、**没有 tool 角色** —— 这正是与 OpenAI 协议的区别：
        #    OpenAI 那边必须补一条 role="tool" 再另起一条 role="user" 带图，共 4 条。
        assert [m["role"] for m in msgs1] == ["user", "assistant", "user"], [
            m["role"] for m in msgs1
        ]
        assert not [m for m in msgs1 if m["role"] == "tool"], "Anthropic 协议里不该出现 tool 角色"

        # ---- ⑫ 最终读数（与阶段 18 用同一套评分函数）----
        assert start["provider"] == "claude" and start["provider"] == fin["provider"]
        assert fin["model"], fin["model"]
        assert fin["steps"] == 7, fin["steps"]
        assert fin["fieldScore"] == 3 and fin["submitted"] and fin["success"], fin
        assert fin["clicks"] == 4 and fin["hitClicks"] == 4 and fin["clickHitRate"] == 1.0, fin
        assert fin["lostKeystrokes"] == 0 and fin["rejectedActions"] == 0, fin
        assert fin["state"]["field_order"] == "ORDER-2026-0917"
        assert fin["state"]["field_amount"] == "2158.50"
        assert fin["state"]["field_date"] == "2026-09-21"
        assert fin["error"] is None
        # 耗时自洽
        assert fin["totalMs"] >= fin["llmMs"] >= 0
        assert fin["totalMs"] >= fin["times"].get("render", 0)
        # 事件序列：start → screen → (delta/action/screen)* → finish
        kinds = [e["type"] for e in events]
        assert kinds[0] == "start" and kinds[1] == "screen" and kinds[-1] == "finish"
        assert kinds.count("action") == 7
        assert kinds.count("screen") == 8  # step 0 + 7 轮
        print(
            f"✅ 7-12. 完整闭环（Claude）：8 次请求 / 7 步 / 3 字段全对 / 4 次点击全中 / "
            f"rejectedActions=0；平均中心偏差 {fin['avgCenterOffsetPx']} px"
        )
        print("        · 请求头 x-api-key + anthropic-version + anthropic-beta 齐全")
        print("        · 内置工具**没有** input_schema（与 DeepSeek 自写 schema 的对照）")
        print("        · 工具入参被切成 3 段下发 → 拼装回完整对象并原样回放")
        print("        · tool_result 排在文字前、图塞在 tool_result 里、tool_use_id 配对")


def test_whitelist_blocks_rich_vocabulary():
    """⑬ Claude 的动作词表比宿主宽 —— 白名单要挡住它，且流程不能崩。"""
    script = [
        _tool_reply("toolu_a", {"action": "mouse_move", "coordinate": [10, 10]}, narration="先挪鼠标。"),
        _tool_reply("toolu_b", {"action": "double_click", "coordinate": [360, 149]}),
        _text_reply("宿主不支持这些动作，我改用 left_click 吧。"),
    ]
    with mock_anthropic(script) as srv:
        events = list(computer.run_computer(None, provider="claude", max_steps=5))
        fin = next(e for e in events if e["type"] == "finish")
        acts = [e for e in events if e["type"] == "action"]

        assert len(acts) == 2, acts
        assert [a["action"] for a in acts] == ["mouse_move", "double_click"], acts
        assert all(a["ok"] is False for a in acts), acts
        # 拒绝原因里要列出可用动作，模型才有机会自己改
        assert "left_click" in acts[0]["note"], acts[0]["note"]
        assert fin["rejectedActions"] == 2, fin["rejectedActions"]
        # 被拒的动作不改状态、不计入点击统计
        assert fin["clicks"] == 0 and fin["state"]["field_order"] == ""
        # 但流程必须继续走到 finish（不是崩掉）
        assert fin["error"] is None and fin["steps"] == 2, fin
        # 拒绝理由确实回传给了模型
        nxt = srv.requests[1]["body"]["messages"][-1]
        assert nxt["content"][0]["type"] == "tool_result"
        assert "不支持的动作" in nxt["content"][0]["content"][0]["text"]
        print("✅ 13. 白名单挡住 Claude 的 mouse_move / double_click：2 次被拒、状态不变、流程走到 finish")


def test_api_error_surfaces():
    """⑭ 401 → ClaudeAPIError（大脑层硬失败）；循环层**刻意兜住**它，端点不 500。

    这里有个容易想错的点：**大脑层和循环层的失败语义是故意不同的**。
      · 大脑层：401 就是 401，必须抛 —— 它不该假装没事。
      · 循环层：兜住任何异常，发一帧 error **再照常发 finish**（带部分结果）。
        否则异常冒出生成器，流就断了，前端连半截都看不到。
    所以端点返回的是「200 + error 字段」，而不是 500。这是设计，不是漏了错误处理。
    """
    with mock_anthropic([], status=401) as srv:
        brain = brains.ClaudeBrain(width=1024, height=768)
        msgs = brain.open(
            "任务", computer.to_data_url(computer.render_screen(computer.initial_state()))
        )
        try:
            list(brain.decide(msgs))
            raise AssertionError("401 竟然没抛")
        except brains.ClaudeAPIError as e:
            assert e.status == 401 and "x-api-key" in str(e), e
        assert srv.requests, "mock 没收到请求"

        # 循环层：error 帧 + finish 都要有，且 finish 里带着失败原因与空动作表
        events = list(computer.run_computer(None, provider="claude", max_steps=3))
        errs = [e for e in events if e["type"] == "error"]
        fin = next(e for e in events if e["type"] == "finish")
        assert len(errs) == 1 and "401" in errs[0]["message"], errs
        assert fin["error"] and "401" in fin["error"], fin["error"]
        assert fin["success"] is False and fin["actions"] == [], fin

        # 端点层：不 500，把失败原因放进正常响应
        resp = main.computer_run(schemas.ComputerRequest(provider="claude", maxSteps=3))
        assert resp.error and "401" in resp.error, resp.error
        assert resp.provider == "claude" and resp.success is False
    print("✅ 14. 401 → ClaudeAPIError（大脑层硬失败）→ 循环层兜住 → 端点 200 + error 字段")


def test_stream_preflights_before_opening():
    """⑮ 流式端点先校验再开流：无 Key 立刻 400。"""
    saved = os.environ.pop("CLAUDE_API_KEY", None)
    try:
        try:
            main.computer_stream(schemas.ComputerRequest(provider="claude"))
            raise AssertionError("无 Key 时流式端点竟然没报错")
        except main.HTTPException as e:
            assert e.status_code == 400, e.status_code
            assert "CLAUDE_API_KEY" in str(e.detail)
    finally:
        if saved is not None:
            os.environ["CLAUDE_API_KEY"] = saved
    print("✅ 15. 流式端点先校验再开流：无 Key → 立刻 400（不是开流后才发 error 帧）")


def test_blocking_shape():
    """补：blocking 形状完整（含 provider / protocol），能被 ComputerResponse 收下。"""
    with mock_anthropic(_script_fill_form()):
        result = computer.run_computer_blocking(None, provider="claude", max_steps=8)
        resp = schemas.ComputerResponse(**result)
        assert resp.provider == "claude" and resp.protocol
        assert resp.success and resp.fieldScore == 3
        # 前端要画点击落点，screen.elements 必须字段齐全
        el = resp.screen.elements[0]
        for key in ("name", "kind", "x", "y", "w", "h", "label", "dangerous"):
            assert hasattr(el, key), key
        assert [a.target for a in resp.actions if a.action == "left_click"] == [
            "field_order",
            "field_amount",
            "field_date",
            "btn_submit",
        ]
    print("✅ 16. blocking：provider=claude / protocol 齐全 / ComputerResponse 校验通过 / 落点目标依次正确")


if __name__ == "__main__":
    print("阶段 18B 静态冒烟（mock Anthropic，无需 Key）\n")
    test_no_key_is_explicit()
    test_beta_pairs_with_tool()
    test_url_normalisation()
    test_image_helpers()
    test_input_normalisation()
    test_sse_line_robustness()
    test_wire_format_and_loop()
    test_whitelist_blocks_rich_vocabulary()
    test_api_error_surfaces()
    test_stream_preflights_before_opening()
    test_blocking_shape()
    print("\n🎉 阶段 18B 静态冒烟全部通过")
