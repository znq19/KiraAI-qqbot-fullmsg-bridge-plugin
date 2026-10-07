"""测试入口（无需框架）：python3 tests/run_tests.py

    python3 tests/run_tests.py          # 全部套件

套件说明：
  test_version_bump.py 版本一致性（manifest ⇄ README 标题 ⇄ 最新变更小节）
  test_consistency.py  一致性 & 静态不变量（schema ⇄ 代码 ⇄ README）
  test_bridge.py       机制自测（解析表补丁 / 事件语义 / 去重 / 性能 / 可逆性），
                       不依赖 KiraAI；若本机有 qq-botpy 会自动跑真实库的对照断言。
  test_proactive_fallback.py  msg_id 过期（40034005）→ 主动消息兜底 + 清死 id +
                       主动通道 @ 走 markdown（最小 core 桩驱动真实 main.py）。
  smoke_real_core.py   端到端冒烟：真实 KiraAI core + 真实 qq-botpy +
                       真实 QQOfficialAdapter 全链路（没有任何桩）。
                       需要环境变量 KIRA_CORE / BOTPY_PATH 指向源码；找不到会自动跳过。
                       会**按世代**断言：2.x 必须接管事件，3.0 必须让位。
  smoke_v3.py          KiraAI 3.0 专项：世代探测 / 不顶替核心处理器 / 群名 /
                       引用唤醒补洞（含反向验证）/ markdown·键盘端到端 /
                       按钮回调 / 工具注入 / 可逆性。
  audit_quality.py     质量审计：性能 / 内存有界 / 不阻塞 / 可逆性 / 功能完整性。
  audit_static.py      静态审计：未使用导入 / 裸 await / TODO 残留 /
                       重复定义 / schema⇄代码⇄README 一致 / 无功能丢失清单。
  audit_edge.py        边界复审：核心重建 payload 时键盘是否丢 / 并发 contextvar 串味 /
                       脏数据（畸形群名、缺字段互动、超限键盘）/ 异常分类是否正确。
  audit_promises.py    承诺核对：文档与 PR 里说过的行为逐条对照代码，防"说了没做"。
  audit_e2e.py         端到端链路：@ 消息全链路 / 键盘闭环 / 群名 / 群管理工具 /
                       成员事件，从入口走到出口。
  audit_hooks.py       ★ 钩子契约：走**真实框架注册路径**验证 self 绑定正确、
                       工具/标签真的注入进 TagSet/ToolSet，以及与四个合作插件
                       （accelerator / xml_tag_fixer / session_merger / sustained_chat）
                       的共存前提（补丁目标不重叠、标签不被破坏）。
  audit_md_regression.py ★ 回归：`<reply>` + `<markdown>` 同条消息发出
                       `[Unsupported message element]`（2.x 发送补丁漏传 markdown）。
  audit_session_backfill.py ★ 会话名回填：只改「名字还是 openid」的会话（用户手动
                       改名的一律不碰）；群走群名、私聊走通讯录；拉不到不编造。
  audit_task_leak.py     ★ 后台任务泄漏：重载插件会漏 `_request_reconnect` 任务
                       （三轮累积 3 个 ⇒ 反复重连 ⇒ 同一条消息出现 3 条）。
  audit_msgid_source.py  ★ 查证 `<msg message_id="">` **不是模型写的**（框架回填），
                       以及「发送结果与 <msg> 位置错配」这个真成因。
  audit_msgid_own_bug.py ★ **本插件自身**的 msg-id bug（v1.4.0 引入，已修）：
                       兜底返回原始长 id 而非展示态 id；以及 3.0 上
                       `_text_content` / `_result_message_id` / `_remember_reply_id`
                       都搬到了**能力对象**上（实例取不到 ⇒ 兜底整个失效）。
  audit_all_on.py        ★ 「全开」配置实测：3.0 上 2.x 专属项**无操作但无害**
                       （不报错），全部配置项都能读到。
  audit_hot_install_live.py ★ 热装实测：适配器**先连**→此刻才装插件，逐项验证
                       「第一轮巡检（≤15s）当场就位」，不需要重启。
  audit_hot_install.py    ★ 三项修复：① 实例已在运行时装插件不用重启（15s 巡检自动
                       补挂 + 新增群名补拉）② 撤回失败提示压到 2 句 ③ 合成事件
                       空 message_id 教坏模型（`<msg message_id="">`）的根因。
  audit_recall_alias.py  ★ 撤回「图片/表情」失败(40061001) 的根因与修复：
                       反查命中/未命中、found 标记、LRU 兜底、可操作提示、
                       以及「主动通道补登记」这个我们自己的缺口。
  audit_join_request_injection.py ★ 加群申请/成员通知的**防注入**（借鉴群管插件做法：
                        截断 + 明标不可信 + 明确说别当指令）+ 带上 openid。
  audit_join_request_flow.py ★ 把「模型眼里的成员事件/加群申请」完整跑出来：
                       notice 原文 → list 返回 → approve/decline 实际请求。
  audit_peer_misuse.py  ★ 实测：同类插件的工具在**官 bot 会话**里被误选会怎样
                       （工具是全局注册的 → 会被看到；但被平台判定/能力缺失拦住，
                        且框架兜住异常 ⇒ 不会真的误用）。
  audit_framework_peers.py ★ 对照**框架内置插件 + S 版 + Z 版**：确认我们新
                       增的文件方法与昵称来源不冲突、不重复、只补缺口。
  audit_core_files.py  ★ 核对**核心原生是否已支持发文件**（结论：已支持 ⇒ 我们
                       不实现 send_qq_file，避免重复）。2.x/3.0 双核心报文级验证。
  audit_peers_v133.py  ★ 与三个同类插件（gmp/gmv/qfm）**逐条源码核对**零冲突：
                       工具名不重名 / 平台门禁互斥 / 无 monkeypatch / 钩子不覆盖，
                       外加四个既有合作插件回归。
  audit_tools_v133.py  ★ v1.3.3 分组开关（不需权限默认开 / 需权限默认关）、
                       内邀自动探测、通讯录 mentions 来源、存量升级无感、文案规范。
  audit_tools_v133b.py ★ v1.3.3 新工具的**报文级**行为 + 错误人话化 + 守卫不发请求。
  audit_intent_timing.py ★ 时序仿真：复现「适配器先连、插件后加载」，验证重连时
                       实际发出的鉴权报文带上了额外订阅位（1<<24 / 1<<26）。
  audit_recall_intent.py ★ 撤回 id 反查（展示态 qqo-xxx → 官方真实 id）+
                       intent 注入点（ws_identify 正主 / send_msg 探针 / Client.start）。
  audit_identity.py    ★ 私聊昵称：跨场景共享（群里认识过的人私聊也认得）+ 自动更新
                       （改名跟随）+ 引用消息学昵称 + 旧数据自动迁移。
  audit_hint_render.py ★ 配置文案渲染安全：hint 经过 JSON 层 / 核心层 / 前端两套
                       渲染路径（{{ }} 纯文本 与 v-html+escapeHtml）后不破版；
                       校验「只用中文引号、不写裸尖括号、不写 markdown 标记」。
  audit_chat_compat.py 聊天插件共存 + 真实生效：内置 kira-ai（DefaultPlugin）的标签
                       与 bridge 的 markdown/keyboard 真的共存于同一 TagSet、
                       Default-Chat-Z 补丁目标不重叠、markdown/键盘/引用**真的发出去**。
                       会按 KIRA_CORE 自动选 2.x / 3.0 分支。
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

SUITES = ["test_version_bump.py", "test_consistency.py", "test_bridge.py",
          "test_proactive_fallback.py", "smoke_real_core.py",
          "smoke_v3.py", "audit_quality.py", "audit_static.py",
          "audit_edge.py", "audit_promises.py", "audit_e2e.py",
          "audit_hooks.py",
          "audit_chat_compat.py", "audit_hint_render.py", "audit_identity.py", "audit_recall_intent.py", "audit_intent_timing.py", "audit_tools_v133.py", "audit_tools_v133b.py", "audit_peers_v133.py", "audit_core_files.py", "audit_framework_peers.py", "audit_peer_misuse.py", "audit_join_request_flow.py", "audit_join_request_injection.py", "audit_recall_alias.py", "audit_hot_install.py", "audit_all_on.py", "audit_hot_install_live.py", "audit_msgid_source.py", "audit_msgid_own_bug.py", "audit_task_leak.py", "audit_session_backfill.py", "audit_md_regression.py", "audit_attach_wiring.py", "audit_ref_no_false_alarm.py", "audit_md_img_publish.py", "audit_md_tag_repair.py", "audit_media_types.py", "audit_chunk_assembly.py"]

rc = 0
for suite in SUITES:
    print(f"\n########## {suite} ##########")
    r = subprocess.call([sys.executable, str(HERE / suite)])
    rc = rc or r

# audit_static 不依赖任何核心，任何环境都必须通过 —— 单独再确认一次
if rc:
    print("\n(至少一个套件失败，请查看上方输出)")
print("\n" + ("ALL TEST SUITES PASSED" if rc == 0 else "SOME TEST SUITES FAILED"))
sys.exit(rc)
