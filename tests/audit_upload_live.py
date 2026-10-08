"""★ 真跑 `_upload_bytes_to_qq`（完整代码路径），验证不再有 NameError。

为什么必须这么测：
    `NameError: name 'sess' is not defined` 这类错误 **语法检查抓不到**
    （AST 完全合法），只有真跑一遍才炸。用户线上就是这么中招的
    —— 图片转存整条链路全废，群里图全变 alt 文字。

做法：真 aiohttp，只把 `ClientSession.put` 换成**记录调用**的版本
（不发真网络），其余（Route / 排序 / 累加偏移 / 完整性自检 / 合并请求）全部真跑。
"""
import asyncio
import os
import sys

sys.path.insert(0, "/var/minis/workspace/qqbot_bridge_review/bridge")
sys.path.insert(0, "/tmp/botpy_src/botpy-master")

import aiohttp as _ah

_SEEN = {}
_RealSession = _ah.ClientSession


class _FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _PatchedSession(_RealSession):
    def put(self, url, **kw):
        _SEEN[url] = len(kw.get("data", b""))
        return _FakeResp()          # ★ 不发真网络：只记录分片大小


_ah.ClientSession = _PatchedSession

import md_media as M  # noqa: E402


class L:
    def warning(self, *a):
        print("    WARNING:", (a[0] % tuple(a[1:])) if len(a) > 1 else a[0])

    def debug(self, *a):
        print("    debug:", (a[0] % tuple(a[1:])) if len(a) > 1 else a[0])

    def info(self, *a):
        print("    INFO:", (a[0] % tuple(a[1:])) if len(a) > 1 else a[0])


# ---- 假 API：Route 是真的 botpy Route，HTTP 只拦 prep / files ----
class HTTP:
    def __init__(self, parts):
        self.parts = parts

    async def request(self, route, **kw):
        path = getattr(route, "path", "")
        if "upload_prepare" in path:
            return {"upload_id": "UP1", "parts": self.parts}
        if "/files" in path:
            return {"file_info": "FI", "raw_url": "https://cos.example.com/pub.png?sig=x"}
        return {}


class API:
    def __init__(self, parts):
        self._http = HTTP(parts)


class Client:
    def __init__(self, parts):
        self.api = API(parts)


PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def make_parts(total, bs):
    parts = []
    n = max(1, (total + bs - 1) // bs)
    for i in range(n):
        parts.append({"index": i, "block_size": str(min(bs, total - i * bs)),
                      "presigned_url": f"http://cos/put/{i}"})
    return parts


async def main():
    print("═══ 真跑 _upload_bytes_to_qq（完整路径）═══")

    # 1 MB，分两片（最后一片是零头）—— 最容易踩"偏移算错"的形状
    import random
    random.seed(42)
    total = 1_000_000
    data = random.randbytes(total)
    bs = 600_000
    parts = make_parts(total, bs)
    _SEEN.clear()
    raw = await M._upload_bytes_to_qq(Client(parts), "G1", True, data, "x.png", logger=L())
    print(f"  → 返回: {raw}")
    print(f"  → 分片记录: {_SEEN}")
    check("★ 不抛异常、成功拿到 raw_url（sess 已定义）",
          raw == "https://cos.example.com/pub.png?sig=x", repr(raw))
    check("★ 分片总字节 == 原文件长度（累加偏移正确）",
          sum(_SEEN.values()) == total, f"{sum(_SEEN.values())} vs {total}")
    check("★ 每个分片都真的 PUT 了", len(_SEEN) == len(parts), f"{len(_SEEN)}/{len(parts)}")

    # 乱序返回
    _SEEN.clear()
    raw2 = await M._upload_bytes_to_qq(Client(list(reversed(parts))), "G1", True,
                                       data, "x.png", logger=L())
    check("★ 分片乱序返回时仍然成功", raw2 is not None, repr(raw2))
    check("★ 乱序时字节数仍正确", sum(_SEEN.values()) == total, str(_SEEN))

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
