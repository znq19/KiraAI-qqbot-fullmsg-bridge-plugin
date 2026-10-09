"""★ 新能力：私聊「输入中…」状态（msg_type=6）。官方有、KiraAI 核心没有。

## 依据

* 腾讯官方 Node SDK：`bot.sendTyping(target, 30)`
  —— 源码注释「**仅在 `target.scope === "c2c"` 时可用**」，载荷
  `{msg_type: 6, msg_id, input_notify: {input_type: 1, input_second: N}}`
  （`src/protocol/api/messages.ts`：`input_notify: { input_type: 1, input_second: inputSecond }`）。
* QQ 官方推荐的 Hermes（`gateway/platforms/qqbot/adapter.py`）：`send_typing()`
  —— C2C-only、60 秒时长、50 秒防抖、必须有入站 `msg_id`。

## 本测试断言

1. 单聊 + 有入站 msg_id ⇒ 真的发出 `msg_type=6` 且 `input_notify` 形状正确；
2. 群里**不发**（官方只支持 C2C）；
3. 没有入站 msg_id ⇒ 不发（发了也会被拒）；
4. 50 秒防抖：同会话短时间内只发一次；
5. 发失败**绝不影响**调用方（内部吞掉）；
6. 请求走底层 Route —— **不能用 botpy 的 `post_c2c_message`**
   （它 `payload = locals()`，多传的 `input_notify` 会被静默丢掉）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT

import asyncio
import sys
from types import SimpleNamespace

CORE = str(_CORE_ROOT("3"))
# ⚠ 顺序：先核心、后插件 —— 插件要排在 sys.path[0]，
#   否则 `import main` 会命中**核心仓库根目录的 main.py**（踩过）
sys.path.insert(0, CORE)
sys.path.insert(0, _BR())

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class HTTP:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    async def request(self, route, **kw):
        if self.boom:
            raise RuntimeError("平台抽风了")
        self.calls.append({"path": getattr(route, "path", ""), "json": kw.get("json") or {}})
        return {"id": "X"}


class Client:
    def __init__(self, boom=False):
        self.api = SimpleNamespace(_http=HTTP(boom))


class Adapter:
    def __init__(self, client, reply_ids):
        self.info = SimpleNamespace(name="qqo")
        self._direct_reply_ids = reply_ids
        self._client = client

    def get_client(self):
        return self._client


def event(is_group: bool, uid: str):
    return SimpleNamespace(
        adapter=SimpleNamespace(name="qqo"),
        message=SimpleNamespace(
            group=SimpleNamespace(group_id="G1") if is_group else None,
            sender=SimpleNamespace(user_id=uid),
        ),
    )


async def main():
    print("═══ 私聊「输入中…」状态 ═══")
    try:
        import main as bridge_main
    except Exception as exc:
        print(f"  skip  需要真实核心（{exc}）")
        return 0

    client = Client()
    adapter = Adapter(client, {"OPENID": "MSGID-1"})

    class Mgr:
        def get_adapter(self, n):
            return adapter if n == "qqo" else None

        def get_adapters(self):
            return {"qqo": adapter}

    ctx = SimpleNamespace(adapter_mgr=Mgr())
    plugin = bridge_main.QQOfficialGroupBridge(
        ctx, {"section_basic": {"enabled": True, "typing_enabled": True}})

    print("\n[1] 单聊 + 有入站 msg_id ⇒ 发出 msg_type=6")
    sent = plugin._maybe_send_typing(event(False, "OPENID"))
    check("★ 已排队", sent is True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    calls = client.api._http.calls
    check("★ 真的发出了一个请求", len(calls) == 1, str(len(calls)))
    body = calls[0]["json"] if calls else {}
    check("★ msg_type=6", body.get("msg_type") == 6, str(body))
    check("★ input_notify 形状正确",
          body.get("input_notify") == {"input_type": 1, "input_second": 60},
          str(body.get("input_notify")))
    check("★ 带上入站 msg_id", body.get("msg_id") == "MSGID-1", str(body.get("msg_id")))
    check("★ 路径是单聊消息接口", calls[0]["path"] == "/v2/users/{openid}/messages",
          calls[0]["path"] if calls else "")
    check("★ msg_seq 与核心的 1..N 错开（避免撞重复）",
          isinstance(body.get("msg_seq"), int) and body["msg_seq"] > 100,
          str(body.get("msg_seq")))

    print("\n[2] 50 秒防抖：同会话短时间内第二次不发")
    check("★ 第二次被防抖挡下",
          plugin._maybe_send_typing(event(False, "OPENID")) is False)
    await asyncio.sleep(0)
    check("★ 请求数仍然是 1", len(client.api._http.calls) == 1, str(len(client.api._http.calls)))

    print("\n[3] 群里**不发**（官方只支持 C2C）")
    check("★ 群事件直接返回 False",
          plugin._maybe_send_typing(event(True, "OPENID")) is False)

    print("\n[4] 没有入站 msg_id ⇒ 不发")
    adapter2 = Adapter(Client(), {})
    mgr2 = SimpleNamespace(get_adapter=lambda n: adapter2, get_adapters=lambda: {"qqo": adapter2})
    plugin2 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=mgr2),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    check("★ 无 msg_id ⇒ 不排队", plugin2._maybe_send_typing(event(False, "OPENID")) is False)

    print("\n[5] 开关关掉 ⇒ 不发")
    plugin3 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: adapter, get_adapters=lambda: {"qqo": adapter})),
        {"section_basic": {"enabled": True, "typing_enabled": False}})
    check("★ 关掉后不排队", plugin3._maybe_send_typing(event(False, "NEWOPENID")) is False)

    print("\n[6] 发送失败绝不影响调用方（异常被吞）")
    boom_client = Client(boom=True)
    adapter4 = Adapter(boom_client, {"X": "MSGID-2"})
    plugin4 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: adapter4, get_adapters=lambda: {"qqo": adapter4})),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    sent = plugin4._maybe_send_typing(event(False, "X"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    check("★ 调度成功（不抛异常）", sent is True)
    check("★ 失败被吞掉（任务已结束、无 pending 异常）",
          all(t.done() for t in plugin4._typing_tasks) or plugin4._typing_tasks == [])

    print("\n[7] 不走 botpy 的 post_c2c_message（它会丢掉 input_notify）")
    src = open(_BR() + "/main.py", encoding="utf-8").read()
    seg = src.split("async def _send_typing")[1].split("def _adapter_attr")[0]
    # 去掉文档字符串再判断（注释里提到 post_c2c_message 是"解释为什么不用它"）
    body = seg.split('"""', 2)[2] if seg.count('"""') >= 2 else seg
    check("★★ 用的是底层 Route（不是 post_c2c_message）",
          "Route(" in body and "post_c2c_message" not in body,
          body.strip()[:80])

    print("\n[8] ★★ 群聊里：流式 / 输入中**都不生效**（官方只支持 C2C）")
    check("★ 群事件拿不到 C2C 目标（判据在 main 里共用）",
          bridge_main.QQOfficialGroupBridge._c2c_target_of(event(True, "OPENID")) == "")
    _calls_before = len(client.api._http.calls)
    before_turns = dict(plugin.llm_stream._turns)
    plugin._register_c2c_turn(event(True, "OPENID"), request=object(), target="")
    check("★ 群聊不登记 LLM 流式轮次 ⇒ 旁听通道对群聊零动作",
          plugin.llm_stream._turns == before_turns == {})
    check("★ 群聊不打开流式消息会话", plugin.c2c_stream._sessions == {})
    check("★ 群聊也不会发送「输入中」状态（官方：仅单聊）",
          plugin._maybe_send_typing(event(True, "OPENID")) is False)
    check("★ 群聊没有新增任何请求（既不发输入中、也不发流式）",
          len(client.api._http.calls) == _calls_before,
          str(client.api._http.calls[_calls_before:]))

    print("\n[9] ★★ 3.0 形状：回复 id 挂在**能力对象**上也要能发（真实适配器/能力对象）")
    try:
        from core.adapter.adapter_info import AdapterInfo
        from core.adapter.capabilities import IMCapability
        from core.adapter.context import AdapterContext
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter

        _info = AdapterInfo(adapter_id="t", enabled=True, name="qqo", platform="QQ Official",
                            config={"app_id": "a", "app_secret": "b",
                                    "permission_mode": "deny_list",
                                    "group_deny_list": [], "user_deny_list": []})
        real_ad = QQOfficialAdapter(AdapterContext(info=_info, event_queue=asyncio.Queue()))
        real_http = HTTP()
        real_ad.client = Client()
        real_ad.client.api._http = real_http
        cap = real_ad.get_capability(IMCapability)
        check("★ 3.0 的回复 id 确实在**能力对象**上（适配器实例上没有）",
              hasattr(cap, "_direct_reply_ids") and not hasattr(real_ad, "_direct_reply_ids"))
        cap._direct_reply_ids["U9"] = "MSGID-9"
        plugin9 = bridge_main.QQOfficialGroupBridge(
            SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: real_ad)),
            {"section_basic": {"enabled": True, "typing_enabled": True}})
        ok9 = plugin9._maybe_send_typing(event(False, "U9"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        check("★★ `_adapter_attr` 会回落到能力对象 ⇒ 真的发出去了", ok9 is True)
        check("★ 发出的是 msg_type=6",
              bool(real_http.calls) and real_http.calls[-1]["json"].get("msg_type") == 6,
              str(real_http.calls[-1:]))
        seqs = getattr(cap, "_reply_msg_seqs", {})
        check("★★ msg_seq 接进**框架自己的计数器**并写回（不会与回复撞重复）",
              seqs.get((False, "U9", "MSGID-9")) == 1, str(dict(seqs)))
    except Exception as exc:
        check("★ 3.0 能力对象用例无异常", False, f"{type(exc).__name__}: {exc}")

    print("\n[10] ★★ 额度保护：同一条入站消息最多 typing_max_frames 帧（默认 2）")
    adapter10 = Adapter(Client(), {"U10": "MSGID-10"})   # 注意：Client() 自己建 HTTP 实例
    plugin10 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: adapter10)),
        {"section_basic": {"enabled": True, "typing_enabled": True,
                           "typing_max_frames": 2}})
    got = []
    for _i in range(3):
        plugin10._typing_sent_at.clear()          # 模拟"防抖窗口已过"
        got.append(plugin10._maybe_send_typing(event(False, "U10")))
        for _t in list(plugin10._typing_tasks):   # 等真正发完（确定性，别靠 sleep 次数）
            await _t
    check("★ 前 2 帧放行、第 3 帧被额度挡住（保护被动回复配额）",
          got == [True, True, False], str(got))
    _calls10 = adapter10.get_client().api._http.calls
    check("★ 实际只发了 2 个请求", len(_calls10) == 2, str(len(_calls10)))
    check("★ 有可见日志说明是帧数上限挡的",
          "typing_skip_frame_cap" in plugin10._typing_skip_done,
          str(plugin10._typing_skip_done))

    print("\n[11] ★ 为什么没发：每种原因各写一条可见日志（不再查无可查）")
    plugin11 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: Adapter(Client(), {}))),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    check("★ 没有入站 msg_id ⇒ 记下 no_msg_id",
          plugin11._maybe_send_typing(event(False, "NOPE")) is False
          and "typing_skip_no_msg_id" in plugin11._typing_skip_done,
          str(plugin11._typing_skip_done))
    check("★ 群聊事件 ⇒ 记下 not_c2c",
          plugin11._maybe_send_typing(event(True, "NOPE")) is False
          and "typing_skip_not_c2c" in plugin11._typing_skip_done,
          str(plugin11._typing_skip_done))
    plugin_off2 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: Adapter(Client(), {"X": "M"}))),
        {"section_basic": {"enabled": True, "typing_enabled": False}})
    check("★ 配置关掉 ⇒ 记下 disabled",
          plugin_off2._maybe_send_typing(event(False, "X")) is False
          and "typing_skip_disabled" in plugin_off2._typing_skip_done,
          str(plugin_off2._typing_skip_done))

    print("\n[12] ★ 日志可查：统一带【输入中】前缀、成功时带平台响应")
    import logging as _logging

    class _Cap(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, record):
            try:
                self.msgs.append(record.getMessage())
            except Exception:
                pass

    _cap = _Cap()
    _lg = bridge_main.logger          # 插件用的是名为 plugin 的 logger
    _lg.addHandler(_cap)
    _lg.setLevel(_logging.INFO)
    try:
        _ad12 = Adapter(Client(), {"U12": "MSGID-12"})
        _p12 = bridge_main.QQOfficialGroupBridge(
            SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: _ad12)),
            {"section_basic": {"enabled": True, "typing_enabled": True}})
        _p12._maybe_send_typing(event(False, "U12"))
        for _t in list(_p12._typing_tasks):
            await _t
        _sent_msgs = [m for m in _cap.msgs if "【输入中】" in m]
        check("★★ 成功日志带【输入中】前缀", bool(_sent_msgs), str(_cap.msgs[-2:]))
        check("★★ 成功日志里带**平台响应**（能看出平台收没收）",
              any("平台响应" in m for m in _sent_msgs), str(_sent_msgs[:1]))
        _cap.msgs.clear()
        _p13 = bridge_main.QQOfficialGroupBridge(
            SimpleNamespace(adapter_mgr=SimpleNamespace(
                get_adapter=lambda n: Adapter(Client(), {}))),
            {"section_basic": {"enabled": True, "typing_enabled": True}})
        _p13._maybe_send_typing(event(False, "NOPE2"))
        _skip_msgs = [m for m in _cap.msgs if "【输入中】" in m]
        check("★★ 未发送的原因也带同一前缀（一条 grep 就能定位）",
              any("本次未发送" in m for m in _skip_msgs), str(_cap.msgs[-2:]))
    finally:
        _lg.removeHandler(_cap)

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
