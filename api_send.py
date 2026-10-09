"""发送层解耦（api 层补丁）—— markdown / keyboard / 引用 / @ 的唯一注入点。

为什么必须从 `adapter._send_message` 挪到这里
-------------------------------------------
* **2.x**：框架调 `adapter.send_group_message()` → `adapter._send_message()` → `client.api.post_group_message()`。
* **3.0**：框架调 `adapter.send_group_message()` → `capability.send_group_message()` →
  `capability._send_message()` → `client.api.post_group_message()`。

`adapter._send_message` 在 3.0 上**根本不存在**（实测：`callable=True` 在 2.x、`False` 在 3.0），
所以挂在那一层的补丁在 3.0 上会**整条提前 return**，主动兜底 / 引用注入 / @markdown 全部失效。

而 `client.api.post_group_message` **两版结构完全一致**，且 botpy 用
``payload = locals()`` 组装请求体 —— 多传一个 kwarg 就会进 JSON（已实测）。
⇒ 把补丁挂在这里，代码只写一份，两边都生效。

补丁职责（按优先级）
------------------
1. **markdown**：正文里出现 `<markdown>`（由 rich_content 提取）→ `msg_type=2` + `markdown.content`；
   失败按错误码白名单退回纯文本（剥掉标记）。
2. **keyboard**：把 `keyboard` kwarg 塞进去（官方 `keyboard` 字段）。
3. **引用**：`message_reference`（contextvar 传入的 REFIDX）。
4. **@ 自动转 md**：正文含平台 @ 标记时按 markdown 发（2.x 原有行为，保留）。
5. **记录自己发的消息的 ref_idx**：以后才能引用自己。

作用域保护
--------
`BotAPI` 上没有反指 client 的引用，所以用 `id(api)` 建白名单，
只对**我们登记过的** QQ 官方 client 的 api 生效，避免误伤同进程里的其它 botpy 客户端。
"""

from __future__ import annotations

import contextvars
from typing import Any, Optional

#: 这一次发送要引用哪条消息（REFIDX）。用 contextvar 传给 api 层包装，
#: 避免为了注入 message_reference 去复制一遍适配器的发送逻辑。
QUOTE_REF: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "qqbot_bridge_quote_ref", default=None)

#: 这一次发送的 markdown / keyboard（同样用 contextvar，逐条传递，互不串味）
PENDING_MD: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "qqbot_bridge_pending_md", default=None)
PENDING_KB: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "qqbot_bridge_pending_kb", default=None)

#: markdown 被拒时的错误码（= 可以安全退回纯文本的信号）。
#: 只有这些码才回退 —— 其它异常必须原样抛出，否则会吞掉真问题。
MD_REJECT_CODES = ("304036", "40034127", "40034011", "40034008", "40034009",
                   "40034124", "40034010", "22006", "340069")


def looks_like_md_reject(exc: Any) -> bool:
    text = str(exc)
    return any(code in text for code in MD_REJECT_CODES)


class ApiSendPatcher:
    """把发送增强挂到 `client.api.post_group_message` / `post_c2c_message` 上。"""

    #: 只对我们登记过的 api 生效（id 白名单）
    _OWNED: set = set()

    def __init__(self, plugin: Any, logger: Any):
        self.plugin = plugin
        self.logger = logger
        self._originals: dict = {}      # name -> [(api, method_name, orig)]
        self._flags: dict = {}          # name -> dict（一次性日志标记）

    # ------------------------------------------------------------------ #
    def install(self, adapter: Any, name: str, client: Any) -> bool:
        """安装补丁（幂等）。返回是否新装上了。

        行为开关（markdown / 键盘 / 引用 / @自动转md）统一由插件实例上的
        配置决定 —— 见 `_send`，**不接受 per-install 参数**，避免"装了一套行为、
        跑的时候又按另一套行为"的双份配置漂移。
        """
        api = getattr(client, "api", None)
        if api is None:
            return False
        self._OWNED.add(id(api))

        patched = self._originals.setdefault(name, [])
        flags = self._flags.setdefault(name, {})
        installed = False

        originals = self._originals_of(api)
        for method_name, is_group in (("post_group_message", True),
                                      ("post_c2c_message", False)):
            current = getattr(api, method_name, None)
            if not callable(current):
                continue
            if getattr(current, "_kira_bridge_send", False):
                # 已有我们（或旧实例）的补丁 —— 取回真正的原始实现
                orig = originals.get(method_name) or getattr(current, "_kira_bridge_orig", None)
                if not callable(orig):
                    continue
            else:
                orig = current
            originals[method_name] = orig

            bridge = self

            async def _patched(*args, _orig=orig, _is_group=is_group, _flags=flags,
                               _api=api, _cli=client, **kwargs):
                # 作用域保护：只对我们**当前登记**的 api 生效。
                # 还原（restore）会把 id 从白名单摘掉 —— 之后即使函数引用还残留在
                # 某个对象上（热重载/多实例），也只会原样透传，绝不误伤别的 botpy 客户端。
                if not bridge.owns(_api):
                    return await _orig(*args, **kwargs)
                return await bridge._send(adapter, _orig, _is_group, _flags,
                                          *args, _client=_cli, **kwargs)

            setattr(_patched, "_kira_bridge_send", True)
            setattr(_patched, "_kira_bridge_orig", orig)
            setattr(api, method_name, _patched)
            patched.append((api, method_name, orig))
            installed = True
        return installed

    @staticmethod
    def _originals_of(api: Any) -> dict:
        originals = getattr(api, "_qqbot_bridge_api_orig", None)
        if not isinstance(originals, dict):
            originals = {}
            try:
                api._qqbot_bridge_api_orig = originals
            except Exception:
                pass
        return originals

    def restore(self, name: str) -> int:
        """还原某适配器的全部 api 补丁。"""
        count = 0
        for api, method_name, orig in self._originals.pop(name, []):
            try:
                setattr(api, method_name, orig)
                self._OWNED.discard(id(api))
                count += 1
            except Exception:
                pass
        self._flags.pop(name, None)
        return count

    def owns(self, api: Any) -> bool:
        return id(api) in self._OWNED

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    async def _fix_md_images(self, md_text: str, flags: dict, client: Any,
                             target_id: str, is_group: bool) -> str:
        """把 md 里的图片地址换成 QQ 能真正下载到的公网地址（**不改 md 结构**）。

        * 本地路径 → 走官方「分片上传」拿 `raw_url`（COS 预签名 GET URL）；
        * 公网 URL → 验真（跟随跳转 / 查 Content-Type），
          不是可下载的图片就**原样保留**并告警（不静默改动用户内容）。

        任何失败都**原样返回**，绝不因为图片转存失败而丢掉整条消息。
        """
        if not md_text or "![" not in md_text:
            return md_text
        try:
            from md_media import fix_markdown_images
            out = await fix_markdown_images(
                md_text,
                client=client,
                target_id=str(target_id),
                is_group=is_group,
                logger=self.logger,
            )
            if out != md_text and not flags.get("md_img_logged"):
                flags["md_img_logged"] = True
                self.logger.info(
                    "[QQBOT-BRIDGE] markdown 内图片已换成公网可访问地址"
                    "（QQ 只认公网 URL，本地路径会退化成 alt 文字）"
                )
            # ★ 诊断：把**最终要发出去的 markdown** 原样记一条（图片相关时才记）。
            #   线上排查图片问题时，"我们到底发了什么"是最关键的信息，
            #   而之前日志里完全没有 —— 只能靠猜（2026-10-08 教训）。
            if "![" in out and not flags.get("md_final_logged"):
                flags["md_final_logged"] = True
                self.logger.info(
                    "[QQBOT-BRIDGE] 本条 markdown 实际内容（含图片地址）：\n%s", out)
            return out
        except Exception as exc:
            self.logger.debug("[QQBOT-BRIDGE] markdown 图片处理失败（原样发送）: %s", exc)
            return md_text

    @staticmethod
    def _target_of(args: tuple, kwargs: dict, is_group: bool) -> str:
        """从 botpy 的调用参数里取出目标 id。

        `post_group_message(group_openid=..., ...)` / `post_c2c_message(openid=..., ...)`，
        也可能按位置传。取不到就返回空串（图片转存会跳过，不影响发送）。
        """
        key = "group_openid" if is_group else "openid"
        v = kwargs.get(key)
        if not v and args:
            v = args[0]
        return str(v or "")

    async def _send(self, adapter: Any, orig: Any, is_group: bool, flags: dict,
                    *args, _client: Any = None, **kwargs):
        # 作用域由 install 时的白名单（id(api)）保证 —— 补丁只挂在登记过的 api 上。
        # ① 引用（只在有明确引用意图时注入）
        ref = QUOTE_REF.get()
        if ref and not kwargs.get("message_reference"):
            kwargs = dict(kwargs)
            kwargs["message_reference"] = {"message_id": ref}

        # ② 显式 markdown / keyboard（由 rich_content 从消息链提取后放进 contextvar）
        md_text = PENDING_MD.get()
        keyboard = PENDING_KB.get()

        # ③ 兼容原有行为：正文含 @ 标记 → 自动走 markdown
        auto_md = None
        content = kwargs.get("content")
        if (md_text is None and isinstance(content, str)
                and ("<@" in content or "qqbot-at-user" in content)):
            auto_md = content

        target_md = md_text or auto_md
        if target_md:
            # ★★★ 图片修复：markdown 里的图片必须是**公网可访问的地址**，
            #   本地路径（data/temp/x.jpg）QQ 根本下不到 ⇒ 会渲染成 alt 文字
            #   （用户实测：`![香香](data/temp/...)` 显示成「[香香]」）。
            #   这里只替换 `(...)` 里的 URL，**md 结构一字不动**
            #   （标题/列表/引用/链接/代码块全部保留，行数也不变）。
            target_md = await self._fix_md_images(
                target_md, flags, _client, self._target_of(args, kwargs, is_group),
                is_group)

            kwargs = dict(kwargs)
            kwargs["msg_type"] = 2
            kwargs["markdown"] = {"content": target_md}
            kwargs["content"] = None
            # ★★★ markdown 与 message_reference **互斥**（2026-10-09 官方源码定案）
            #
            #   腾讯官方 Node SDK `dist/protocol/api/messages.js`：
            #
            #       if (messageReference && !this.markdownSupport) {
            #           body.message_reference = { message_id: messageReference };
            #       }
            #       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 只有"非 markdown"时才带引用
            #
            #   而 KiraAI 核心**不看 msg_type 一律填 message_reference**
            #   （`im.py`: `reference = self._resolve_reference(...)`；
            #     2.x 适配器同理）⇒ 只要模型同时写了 `<reply>` 和 `<markdown>`，
            #   本条就会带上引用 ⇒ **QQ 里 markdown 渲染不正常（图不显示等）**。
            #   用户实测：同样内容**不带 reply 就一切正常** ✓
            #
            #   ⇒ 发 markdown 时**丢掉 message_reference**（保留 msg_id：
            #     仍是被动回复，配额/时效完全不变），行为与官方 SDK 一致。
            if self.plugin.md_drop_reference and kwargs.get("message_reference"):
                dropped = kwargs.pop("message_reference", None)
                if not flags.get("md_ref_dropped_logged"):
                    flags["md_ref_dropped_logged"] = True
                    self.logger.info(
                        "[QQBOT-BRIDGE] 本条是 markdown，已**去掉引用**（message_reference=%s）——"
                        "官方 Node SDK 明确让两者互斥（markdown 时绝不带引用），"
                        "实测带引用会让 QQ 的 md 渲染不正常（图片不显示）。"
                        "被动回复锚点 msg_id 保留，配额与时效不受影响",
                        str(dropped)[:60],
                    )
            if auto_md and not flags.get("at_md_logged"):
                flags["at_md_logged"] = True
                self.logger.info(
                    "[QQBOT-BRIDGE] 正文含 @ 标记 → 本条改按 markdown 发送"
                    "（纯文本消息没有 @ 能力，会被显示成文本）"
                )
            elif md_text and not flags.get("md_logged"):
                flags["md_logged"] = True
                self.logger.info("[QQBOT-BRIDGE] 首次按 markdown 发送（<markdown> 标签生效）")

        # ★ 观测（用户 2026-10-09 反馈"reply + md 渲染不正常"）：
        #   把"引用 + markdown 同条发出"的报文形状如实记一条 —— 以后不用猜。
        try:
            if target_md and kwargs.get("message_reference") and not flags.get("md_ref_logged"):
                flags["md_ref_logged"] = True
                self.logger.info(
                    "[QQBOT-BRIDGE] 本条**同时带引用与 markdown**：msg_type=2、"
                    "message_reference=%s、markdown 长度=%d 字符；图片数=%d。"
                    "（官方接口允许两者共存；若客户端显示出问题，请把这条连同"
                    "「本条 markdown 实际内容」一起发来）",
                    str(kwargs.get("message_reference"))[:64], len(str(target_md)),
                    str(target_md).count("!["),
                )
        except Exception:
            pass

        if keyboard:
            kwargs = dict(kwargs)
            kwargs["keyboard"] = keyboard
            # ★★★ 官方硬要求：**仅 markdown 消息支持消息按钮**
            #   （官方 Node/Python SDK 的「发送带有按钮的消息」原话）。
            #   纯文本（msg_type=0）+ keyboard ⇒ 平台不渲染按钮，
            #   用户只看到文字（2026-10-10 用户实测截图实证）。
            #   ⇒ 有键盘但本条不是 markdown 时，**就地升格成 markdown 消息**：
            #     纯文本本身是合法 markdown，按钮才挂得上去。
            #     `content` 必须留空（官方：传了 markdown 后 content 必须为空）。
            _kb_reason = ""
            if not target_md and not kwargs.get("media") \
                    and int(kwargs.get("msg_type") or 0) != 7:
                try:
                    from rich_content import strip_kb_placeholder as _strip_kb

                    _kb_md = _strip_kb(content if isinstance(content, str) else "")
                except Exception:
                    _kb_md = str(content or "").strip()
                if not _kb_md:
                    _kb_md = "\u200b"      # 只有键盘、没有正文：不可见占位
                kwargs["msg_type"] = 2
                kwargs["markdown"] = {"content": _kb_md}
                kwargs["content"] = None
                target_md = _kb_md          # 后面失败回退时也有正文可用
                if self.plugin.md_drop_reference and kwargs.get("message_reference"):
                    kwargs.pop("message_reference", None)
                _kb_reason = "upgraded"
            elif kwargs.get("media") or int(kwargs.get("msg_type") or 0) == 7:
                _kb_reason = "with_media"
            # ★ 2026-10-10：本条含**回调按钮**（type=1）时给一次说明 ——
            #   回调按钮要平台能把互动事件推给机器人（长连接已订阅 INTERACTION）。
            #   若开放平台后台把"消息推送方式"设成 Webhook 而地址不可达，
            #   用户点按钮会看到「请求第三方失败」。点一下就发的替代方案是
            #   type=2 指令按钮（插件默认已加 enter:true）。
            try:
                _kb_rows = ((keyboard or {}).get("content") or {}).get("rows") or []
                _cb = 0
                for _row in _kb_rows:
                    for _btn in ((_row or {}).get("buttons") or []):
                        _act = (_btn or {}).get("action") or {}
                        try:
                            if int(_act.get("type")) == 1:
                                _cb += 1
                        except Exception:
                            pass
                if _cb and not flags.get("kb_cb_logged"):
                    flags["kb_cb_logged"] = True
                    self.logger.warning(
                        "[QQBOT-BRIDGE] 本条含 %d 个**回调按钮**（action.type=1）：需要平台能把"
                        "互动事件推给机器人（长连接已订阅 INTERACTION 位；点按钮会先回执、"
                        "再作为一条消息转给模型）。⚠ 若开放平台后台把“消息推送方式”设成 "
                        "Webhook 且地址不可达，客户端点按钮会提示「请求第三方失败」——"
                        "查一下后台的推送方式，或改用 type=2 指令按钮"
                        "（插件默认已给指令按钮加 enter:true，点一下就自动发送）", _cb)
            except Exception:
                pass
            if not flags.get("kb_logged"):
                flags["kb_logged"] = True
                if _kb_reason == "upgraded":
                    self.logger.info(
                        "[QQBOT-BRIDGE] 首次发送内联键盘：本条原本是纯文本，已**自动升格成 "
                        "markdown 消息**（官方：仅 markdown 消息支持消息按钮）—— "
                        "正文 %d 字，按钮在下方",
                        len(target_md or ""))
                elif _kb_reason == "with_media":
                    self.logger.warning(
                        "[QQBOT-BRIDGE] 本条同时有**富媒体与键盘**：官方只支持 markdown "
                        "消息带按钮（msg_type=7 的富媒体消息按钮可能不显示）。"
                        "想要按钮稳显示，请让模型把键盘与图片/语音分成两条发")
                else:
                    self.logger.info(
                        "[QQBOT-BRIDGE] 首次发送内联键盘（<keyboard> 标签生效，挂在 markdown 消息上）")

        # ③.5 ★ C2C 流式消息（官方 stream_messages）
        #
        #     把**同一轮私聊回复的多个分段**写成同一条会生长的消息（见 c2c_stream.py）。
        #     只在"纯文本私聊 + 有被动 msg_id"时接管；**返回 None 就照常发送** ——
        #     所以任何不确定/失败都不会丢消息，也不会改变群聊和富媒体的既有行为。
        if not is_group and not keyboard and not kwargs.get("markdown") \
                and int(kwargs.get("msg_type") or 0) == 0:
            try:
                manager = getattr(self.plugin, "c2c_stream", None)
                if manager is not None:
                    streamed = await manager.maybe_stream(
                        adapter, _client, self._target_of(args, kwargs, is_group), kwargs)
                    if streamed is not None:
                        return streamed
            except Exception as exc:
                self.logger.debug("[QQBOT-BRIDGE] 流式消息接管失败（回退普通发送）: %s", exc)

        # ④ 发送（markdown 失败 → 退纯文本）
        if not target_md:
            result = await orig(*args, **kwargs)
        else:
            try:
                result = await orig(*args, **kwargs)
            except Exception as exc:
                if not looks_like_md_reject(exc):
                    raise
                fallback = dict(kwargs)
                fallback["msg_type"] = 0
                fallback["markdown"] = None
                fallback["content"] = strip_at_markup(target_md)
                fallback.pop("keyboard", None)   # 键盘依赖 markdown 的消息类型，一并放弃
                if not flags.get("md_fallback_logged"):
                    flags["md_fallback_logged"] = True
                    self.logger.warning(
                        "[QQBOT-BRIDGE] markdown 发送失败（%s），已退回纯文本%s —— "
                        "若群里看到的就是这种情况，说明该机器人没有 markdown 消息权限",
                        str(exc)[:120],
                        "并剥掉 @ 标记" if auto_md else "",
                    )
                result = await orig(*args, **fallback)

        # ⑤ 记住"机器人自己发的这条"的 ref_idx（以后才能引用它）
        try:
            sent_ref = _extract_ref_idx(result)
            if sent_ref:
                target = kwargs.get("group_openid") if is_group else kwargs.get("openid")
                sent_id = result.get("id") if isinstance(result, dict) else None
                if target and sent_id:
                    self.plugin.remember_sent_ref(adapter, str(target), str(sent_id),
                                                  sent_ref, is_group)
        except Exception as exc:
            self.logger.debug("[QQBOT-BRIDGE] 记录已发送消息的 ref_idx 失败: %s", exc)
        return result


def _extract_ref_idx(result: Any) -> Optional[str]:
    # 容错：`qqbot_bridge` 会 import 核心，拿不到就当"这条响应没有 ref_idx"
    # （只影响"以后能不能引用这条消息"这一项增强，绝不影响发送本身）。
    try:
        from qqbot_bridge import extract_sent_ref_idx
    except Exception:
        return None
    return extract_sent_ref_idx(result)


def strip_at_markup(text: str) -> str:
    """把正文里的平台 @ 标记整个去掉（markdown 发不出去时的兜底）。"""
    if not text or ("<@" not in text and "qqbot-at-user" not in text):
        return text
    from qqbot_bridge import strip_at_markup as _strip

    return _strip(text)
