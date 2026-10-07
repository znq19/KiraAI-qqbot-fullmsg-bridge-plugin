"""让 QQ 官方 bot 的 markdown **真的显示出图片** —— 且**不破坏 md 格式**。

## 背景：官方三条硬约束（全部有文档出处）

1. **md 内图片只吃公网 URL**
   ：「对于 markdown 消息内的图片资源，请使用**可在公网访问的资源 url**，
     开放平台会下载转存该资源。」
2. **`msg_type` 互斥**
   ：`0=纯文本(content) / 2=Markdown(markdown) / 7=富媒体(media)`，
     「传了 markdown 后此字段必须为空」⇒ **图文无法在一条消息里**。
3. **富媒体上传只返回 `file_info`**（一串不透明二进制），
   ⇒ 本地图片**无法**变成 md 能引用的地址。

## 但官方留了一个口子（本模块的关键）

上传接口的响应字段里有：

    raw_url  string  文件下载链接（COS 预签名 GET URL），有效期与 ttl 一致
             ★ 仅分片上传合并（upload_id 路径）且 file_type 为图片/视频/语音时返回

⇒ **走「分片上传」路线，能拿到一个公网可访问的 COS URL**。

于是本地图片也能这样发：

    ![香香](data/temp/compressed_xxx.jpg)      ← md 里原本是本地路径（QQ 看不见）
    ![香香](https://cos.xxx/xxx?sign=...)      ← 换成分片上传拿到的公网 URL

**md 的标题 / 列表 / 引用 / 链接 / 代码块全部原样保留** —— 只是把方括号里那个
URL 换成一个能用的，**不拆消息、不改结构**。

## 另外：URL 图片要先「验真」

实测用户给过的一个维基地址：

    https://zh.wikipedia.org/wiki/Special:FilePath/Luka_Megurine.png
    → HTTP 302, content-type: text/html, content-length: 0   ← 下载到 0 字节

**那不是图片地址，是跳转地址**。QQ 下载器拿到空 HTML，校验失败，
就回退成 alt 文字（`![香香](...)` 渲染成「[香香]」）。
官方错误码 `850026`「下载原始文件失败——请检查 URL 是否可访问」正是这个。

所以本模块还有一个 `resolve_image_url()`：**先 HEAD/GET 探一下**，
若是跳转就跟随到真实图片地址；若确实不是图片，就如实告诉调用方
（而不是把坏地址塞进 md 让它又变成文字）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

#: markdown 图片语法：`![alt](url)`，alt 里可能带 `#208px #320px` 尺寸标记
_MD_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: 判定"这已经是公网地址"，不需要转存
_REMOTE_RE = re.compile(r"^https?://", re.I)

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def _is_remote(url: str) -> bool:
    return bool(_REMOTE_RE.match(url or ""))


def _guess_mime(path: str) -> str:
    return _MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "image/jpeg")


# --------------------------------------------------------------------------- #
# 结果缓存 —— 避免「每次发 markdown 都重新验真 / 重新上传」
# --------------------------------------------------------------------------- #
#: url → (最终公网 url, 过期时刻)。验真结果缓存 10 分钟；
#: 本地文件的转存结果缓存到 ttl（QQ 的 raw_url 自带有效期，取小值保守些）。
_URL_CACHE: dict = {}
_LOCAL_CACHE: dict = {}
_URL_TTL = 600.0
_LOCAL_TTL = 240.0


def _cache_get(cache: dict, key: str):
    import time
    hit = cache.get(key)
    if not hit:
        return None
    val, exp = hit
    if time.monotonic() >= exp:
        cache.pop(key, None)
        return None
    return val


def _cache_put(cache: dict, key: str, val: Any, ttl: float) -> None:
    import time
    cache[key] = (val, time.monotonic() + ttl)


def _digest(path: str) -> str:
    """用 (路径, 大小, mtime) 做键 —— 文件被换掉时自然会重新上传。"""
    try:
        st = os.stat(path)
        return f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except Exception:
        return path


def clear_caches() -> None:
    """清空缓存（测试 / 配置变更时用）。"""
    _URL_CACHE.clear()
    _LOCAL_CACHE.clear()


# --------------------------------------------------------------------------- #
# 一、公网 URL 验真
# --------------------------------------------------------------------------- #
async def resolve_image_url(client: Any, url: str, logger: Any = None,
                            timeout: float = 12.0) -> Optional[str]:
    """把「可能是跳转/非图片」的 URL 解析成**真正可直接下载的图片地址**。

    返回真实图片 URL；确认不是图片时返回 None（调用方据此如实告警，不要硬塞）。

    为什么需要（2026-10-07 用户实测）：QQ 的 md 下载器不做 HTML 解析，
    拿到 302 / text/html 就判定失败，于是 `![香蛋](坏地址)` 渲染成「[香蛋]」。
    """
    if not url:
        return None
    try:
        import aiohttp
    except Exception:  # pragma: no cover
        return url          # 没有 aiohttp 就不验（保持原行为，不阻断）
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; qqbot-bridge/1.0)"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            # 先 HEAD（省流量）；有些图床不支持 HEAD，失败再 GET 读前几字节
            ctype = None
            ctype2 = None
            try:
                async with s.head(url, headers=headers, allow_redirects=True) as r:
                    ctype = r.headers.get("Content-Type")
                    final = str(r.url)
                    if r.status == 200 and ctype and ctype.startswith("image/"):
                        return final
            except Exception:
                pass
            try:
                async with s.get(url, headers=headers, allow_redirects=True) as r:
                    ctype2 = r.headers.get("Content-Type")
                    final = str(r.url)
                    head = await r.content.read(16)
                    if r.status == 200 and ctype2 and ctype2.startswith("image/"):
                        return final
                    # 魔数兜底：有些图床 Content-Type 不准
                    if head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff" \
                            or head[:6] in (b"GIF87a", b"GIF89a"):
                        return final
            except Exception:
                pass
            if logger is not None:
                logger.warning(
                    "[QQBOT-BRIDGE] 图片地址不是可直接下载的图片（Content-Type=%s/%s）"
                    "——QQ 会把它渲染成 alt 文字。若这是跳转地址，请改用真实图片 URL",
                    ctype, ctype2,
                )
            return None
    except Exception:
        return url


# --------------------------------------------------------------------------- #
# 二、本地图片 → 公网 COS URL（走分片上传，拿 raw_url）
# --------------------------------------------------------------------------- #
async def _route_request(api: Any, route: Any, **kwargs: Any) -> Any:
    """调用 botpy 的底层 HTTP（框架就是这么干的）。"""
    return await api._http.request(route, **kwargs)


async def _upload_bytes_to_qq(
    client: Any, target_id: str, is_group: bool, data: bytes, name: str,
    logger: Any = None,
) -> Optional[str]:
    """把**字节内容**上传到 QQ，返回公网可访问的 COS URL（`raw_url`）。

    走官方「分片上传」路线 —— **只有这条路**才会返回 `raw_url`：

        upload_prepare  →  upload_id / block_size / parts[].presigned_url
        分片 PUT 到 presigned_url
        upload_part_finish（逐片确认）
        上传接口（带 upload_id）合并  →  file_info **+ raw_url**

    官方原文：

        raw_url  string  文件下载链接（COS 预签名 GET URL），有效期与 ttl 一致
                 ★ 仅分片上传合并（upload_id 路径）且 file_type 为图片/视频/语音时返回；
                   URL 直传和文件类型(file_type=4) 不返回此字段

    失败返回 None（调用方按原样发送，绝不会因此丢掉整条消息）。
    """
    try:
        from botpy.http import Route
    except Exception:
        return None
    api = getattr(client, "api", None)
    if api is None or not data:
        return None

    size = len(data)
    name = name or "image.png"
    md5 = hashlib.md5(data).hexdigest()
    sha1 = hashlib.sha1(data).hexdigest()
    md5_10m = hashlib.md5(data[:10002432]).hexdigest()

    try:
        if is_group:
            prep_route = Route("POST", "/v2/groups/{group_id}/upload_prepare",
                               group_id=target_id)
            finish_route = Route("POST", "/v2/groups/{group_id}/upload_part_finish",
                                 group_id=target_id)
            files_route = Route("POST", "/v2/groups/{group_openid}/files",
                                group_openid=target_id)
        else:
            prep_route = Route("POST", "/v2/users/{user_id}/upload_prepare",
                               user_id=target_id)
            finish_route = Route("POST", "/v2/users/{user_id}/upload_part_finish",
                                 user_id=target_id)
            files_route = Route("POST", "/v2/users/{openid}/files", openid=target_id)

        prep = await _route_request(api, prep_route, json={
            "file_type": 1,               # 1 = 图片
            "file_size": str(size),
            "file_name": name,
            "md5": md5,
            "sha1": sha1,
            "md5_10m": md5_10m,
        })
        upload_id = _get(prep, "upload_id")
        parts = _get(prep, "parts") or []
        if not upload_id or not parts:
            if logger is not None:
                logger.debug("[QQBOT-BRIDGE] 预上传没拿到 upload_id/parts: %s", prep)
            return None

        import aiohttp
        # ★★★ 分片必须**按 index 排序、用各自 block_size 累加偏移**。
        #
        #   踩过的坑（2026-10-07 线上 850019「富媒体文件格式不支持」）：
        #   原来写成 `chunk = data[idx * bsize:(idx+1)*bsize]`，而**最后一片的
        #   block_size 比前面小**（例：12 MB 文件按 5 MB 分片 ⇒ [5MB, 5MB, 2MB]），
        #   第 2 片就会算成 `data[4MB:6MB]`（应该是 10MB 起）⇒ 拼出来的文件是坏的
        #   ⇒ 平台合并后校验格式失败，回 400 / 850019，图还是显示不出来。
        ordered = [p for p in parts if _get(p, "index") is not None]
        ordered.sort(key=lambda p: int(_get(p, "index")))
        offset = 0
        for part in ordered:
            idx = int(_get(part, "index"))
            purl = _get(part, "presigned_url")
            bsize = int(_get(part, "block_size") or 0)
            if not purl:
                continue
            # bsize<=0 时按「剩下的全部」兜底（不该发生，但不至于拼错）
            chunk = data[offset:offset + bsize] if bsize > 0 else data[offset:]
            if not chunk:
                break
            offset += len(chunk)
            async with sess.put(purl, data=chunk) as r:
                if r.status >= 300:
                    if logger is not None:
                        logger.debug("[QQBOT-BRIDGE] 分片 %s PUT 失败: %s", idx, r.status)
                    return None
            await _route_request(api, finish_route, json={
                "upload_id": upload_id,
                "part_index": idx,
                "block_size": str(len(chunk)),
                "md5": hashlib.md5(chunk).hexdigest(),
            })

        # ★ 完整性自检：拼出来的必须和原文件一字不差，否则**宁可不传**
        #   （传个坏文件上去只会换来一个看不懂的平台错误）。
        if offset != len(data):
            if logger is not None:
                logger.warning(
                    "[QQBOT-BRIDGE] 分片拼装不完整（%s/%s 字节），已放弃转存，按原样发送",
                    offset, len(data),
                )
            return None

        merged = await _route_request(api, files_route, json={
            "file_type": 1,
            "srv_send_msg": False,
            "file_name": name,
            "url": "",              # 分片合并路径可留空，但字段要带上
            "upload_id": upload_id,
        })
        raw = _get(merged, "raw_url")
        if raw:
            if logger is not None:
                logger.info("[QQBOT-BRIDGE] 图片已转存为公网地址，markdown 可直接引用")
            return str(raw)
        if logger is not None:
            logger.debug("[QQBOT-BRIDGE] 合并响应没有 raw_url: %s", merged)
        return None
    except Exception as exc:
        # ★ 这条**必须可见**（原来是 debug）：
        #   转存失败 ⇒ 图还是 alt 文字。若不提示，用户只会看到"图片又不显示"，
        #   然后来问"为什么"——而日志里什么都没有。线上就被这个坑过一次
        #   （`400 / 850019 富媒体文件格式不支持`，因为分片拼装错了）。
        if logger is not None:
            logger.warning(
                "[QQBOT-BRIDGE] 图片转存到 QQ 失败（%s: %s）—— 本条图片按原地址发送，"
                "QQ 可能仍显示成 alt 文字；若持续如此请把本条连同日志反馈",
                type(exc).__name__, str(exc)[:160],
            )
        return None


async def upload_local_to_public_url(
    client: Any, target_id: str, is_group: bool, file_path: str,
    logger: Any = None,
) -> Optional[str]:
    """本地图片 → 公网 COS URL。读文件后交给 `_upload_bytes_to_qq`。"""
    try:
        with open(file_path, "rb") as f:
            data = f.read()
    except Exception as exc:
        if logger is not None:
            logger.debug("[QQBOT-BRIDGE] 读本地图片失败: %s", exc)
        return None
    return await _upload_bytes_to_qq(client, target_id, is_group, data,
                                     os.path.basename(file_path), logger=logger)


async def upload_remote_to_public_url(
    client: Any, target_id: str, is_group: bool, url: str,
    logger: Any = None, timeout: float = 30.0,
) -> Optional[str]:
    """**远程图片** → 转存到 QQ 自己的 COS，返回公网 URL。

    ## ★★★ 为什么要做这件事（2026-10-07 的教训）

    官方文档说「md 内图片请使用**可在公网访问**的资源 url，开放平台会下载转存」，
    但实测**公网 + 国内 + 200 + 真图片**照样失败：

        用户日志：萌娘百科国内站直链（HEAD 200、content-type: image/png）
                 ⇒ 群里依旧只显示 [巡音流歌 V4X]（alt 文字）

    原因是**平台的转存是异步的、失败只回一个错误码给日志**（`304010 CHANGE_IMAGE_URL
    图片转存错误` / `304021 GET_FILE 下载文件错误` / `304020 FILE_SIZE 文件大小超限`），
    前端拿不到任何反馈 ⇒ 用户只看到 alt 文字。用户那张 `Luka1.jpg` 有 **13.7 MB**，
    很可能就是撞了大小限制。

    ⇒ **最可靠的做法：我们自己把图取下来，走 QQ 自己的上传通道转存一次**
    （`raw_url` 是 QQ 自己的 COS 预签名地址，平台去下载它必然成功），
    顺便还能把过大的图**压缩**到软限制内。
    """
    if not url:
        return None
    data = await _fetch_bytes(url, logger=logger, timeout=timeout)
    if not data:
        return None
    # 太大的图先压缩（官方软限制：图片 20 MB，超过会降级成"文件"）
    data, name = await _shrink_if_needed(data, url, logger=logger)
    return await _upload_bytes_to_qq(client, target_id, is_group, data, name,
                                     logger=logger)


async def _fetch_bytes(url: str, logger: Any = None,
                       timeout: float = 30.0) -> Optional[bytes]:
    """下载远程图片字节。失败返回 None（调用方原样发送）。"""
    try:
        import aiohttp
    except Exception:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (compatible; qqbot-bridge/1.0)"}
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(url, headers=headers, allow_redirects=True) as r:
                if r.status != 200:
                    if logger is not None:
                        logger.debug("[QQBOT-BRIDGE] 下载图片失败 HTTP %s: %s", r.status, url)
                    return None
                ctype = (r.headers.get("Content-Type") or "").lower()
                data = await r.content.read()
                head = data[:16]
                is_img = ctype.startswith("image/") or head[:8] == b"\x89PNG\r\n\x1a\n" \
                    or head[:3] == b"\xff\xd8\xff" or head[:6] in (b"GIF87a", b"GIF89a") \
                    or (head[:4] == b"RIFF" and data[8:12] == b"WEBP")
                if not is_img:
                    if logger is not None:
                        logger.debug("[QQBOT-BRIDGE] 该地址不是图片（Content-Type=%s）: %s",
                                     ctype, url)
                    return None
                return data
    except Exception as exc:
        if logger is not None:
            logger.debug("[QQBOT-BRIDGE] 下载图片异常: %s: %s", type(exc).__name__, exc)
        return None


async def _shrink_if_needed(data: bytes, url: str, logger: Any = None,
                            soft_limit: int = 20 * 1024 * 1024) -> tuple:
    """超过官方软限制（图片 20 MB）就压一下 —— 避免被降级成"文件"。"""
    name = os.path.basename(url.split("?")[0]) or "image.jpg"
    if len(data) <= soft_limit:
        return data, name
    try:
        import io
        from PIL import Image as PILImage
    except Exception:
        return data, name
    try:
        def _do() -> bytes:
            im = PILImage.open(io.BytesIO(data))
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            quality = 85
            for _ in range(4):
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=quality, optimize=True)
                out = buf.getvalue()
                if len(out) <= soft_limit:
                    return out
                quality -= 15
            # 还大就缩尺寸
            im.thumbnail((1600, 1600))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80, optimize=True)
            return buf.getvalue()

        import asyncio
        out = await asyncio.to_thread(_do)
        if logger is not None:
            logger.info("[QQBOT-BRIDGE] 图片过大已压缩：%.1f MB → %.1f MB",
                        len(data) / 1048576, len(out) / 1048576)
        return out, os.path.splitext(name)[0] + ".jpg"
    except Exception as exc:
        if logger is not None:
            logger.debug("[QQBOT-BRIDGE] 压缩图片失败（按原图上传）: %s", exc)
        return data, name


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# --------------------------------------------------------------------------- #
# 三、把 md 里的图片地址「就地换掉」
# --------------------------------------------------------------------------- #
async def fix_markdown_images(
    md_text: str, *, client: Any, target_id: str, is_group: bool,
    adapter: Any = None, logger: Any = None,
) -> str:
    """把 markdown 里的图片地址换成**QQ 能真正下载到的公网地址**。

    * **本地路径** → 分片上传拿 `raw_url`（公网 COS）；
    * **公网 URL** → 先验真（跟随跳转 / 查 Content-Type）；
      确实不是图片就**保持原样**（不静默删改用户内容），并给出告警。

    返回**结构完全不变**的 markdown（只替换了 `(...)` 里的 URL）。
    """
    if not md_text or "![" not in md_text:
        return md_text

    out = []
    last = 0
    changed = 0
    for m in _MD_IMAGE_RE.finditer(md_text):
        out.append(md_text[last:m.start()])
        alt, url = m.group("alt"), m.group("url")
        new_url = url

        if _is_remote(url):
            # ★★★ 远程图也**转存到 QQ 自己的 COS**（不再只是"验真"）。
            #
            #   为什么（2026-10-07 用户实测）：官方说「用公网 URL，平台会下载转存」，
            #   但**公网 + 国内 + 200 + 真图片**照样失败 —— 用户给的萌娘百科国内站
            #   直链（HEAD 200、content-type: image/png）在群里依旧只显示 alt 文字。
            #   平台转存是**异步**的、失败只回错误码（304010 CHANGE_IMAGE_URL /
            #   304021 GET_FILE / 304020 FILE_SIZE），前端完全拿不到反馈。
            #   用户那张 Luka1.jpg 有 13.7 MB，很可能撞了大小限制。
            #
            #   ⇒ 我们自己取下来，走 QQ 自己的上传通道转存一次：
            #     raw_url 是 QQ 自己的 COS 预签名地址，平台去下载它**必然成功**；
            #     顺便还能把过大的图压到软限制内。
            cached = _cache_get(_URL_CACHE, url)
            if cached is None:
                pub = None
                try:
                    pub = await upload_remote_to_public_url(
                        client, target_id, is_group, url, logger=logger)
                except Exception as exc:
                    if logger is not None:
                        logger.debug("[QQBOT-BRIDGE] 远程图转存失败: %s", exc)
                if pub:
                    cached = pub
                else:
                    # 转存不成，退回"验真"：能确认是真图就保留原地址
                    # （也许平台那边能转成功），确认不是图就也保留并告警。
                    real = await resolve_image_url(client, url, logger=logger)
                    cached = real or url
                _cache_put(_URL_CACHE, url, cached, _URL_TTL)
            new_url = cached
        else:
            # 本地路径：转存成公网 URL（按 (路径,大小,mtime) 缓存，避免重复上传）
            path = url
            if not os.path.isabs(path):
                base = getattr(adapter, "workspace", None) or os.getcwd()
                cand = os.path.join(str(base), path)
                if os.path.exists(cand):
                    path = cand
            if os.path.exists(path):
                key = _digest(path)
                cached = _cache_get(_LOCAL_CACHE, key)
                if cached is None:
                    pub = await upload_local_to_public_url(
                        client, target_id, is_group, path, logger=logger)
                    cached = pub or url
                    _cache_put(_LOCAL_CACHE, key, cached, _LOCAL_TTL)
                new_url = cached
            else:
                new_url = url

        if new_url != url:
            changed += 1
        out.append(f"![{alt}]({new_url})")
        last = m.end()
    out.append(md_text[last:])

    if changed and logger is not None:
        logger.info("[QQBOT-BRIDGE] markdown 内 %d 张图片已换成可访问地址（格式未改动）", changed)
    return "".join(out)
