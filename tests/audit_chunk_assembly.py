"""★ 分片上传拼装正确性回归（850019「富媒体文件格式不支持」的根因）。

## 线上事故（2026-10-07）

用户日志：

    [botpy] 接口请求异常，请求连接: https://api.sgroup.qq.com/v2/users/.../files
    错误代码: 400, {'message': '富媒体文件格式不支持', 'code': 850019}

## 根因

我第一版把分片偏移写成：

    chunk = data[idx * bsize : (idx + 1) * bsize]

而**每个分片的 `block_size` 不都一样** —— 官方默认 5 MB 一块，
最后一块是**剩下的零头**。例如 12 MB 的文件 ⇒ `[5MB, 5MB, 2MB]`：

    idx=0 → data[0 : 5MB]        ✓
    idx=1 → data[5MB : 10MB]     ✓
    idx=2 → data[2*2MB : 3*2MB]  ✗ 应为 data[10MB : 12MB]

⇒ 拼出来的文件是**坏的** ⇒ 平台合并后校验格式失败 ⇒ `400 / 850019`。

**教训**：分片一定要**按 index 排序、用各自 block_size 累加偏移**，
并在上传前做一次**完整性自检**（拼出来必须跟原文件一字不差）。

## ⚠ 测试数据必须用**非周期**数据

我第一版测试用 `bytes(range(256)) * N` 造数据 —— 周期是 256 字节，
错误的偏移**碰巧**能对上，测试反而全绿（差点放过这个 bug）。
这里用 `random` 生成非周期数据。
"""
import hashlib
import os
import random
import sys

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def _make(size: int) -> bytes:
    """**真正非周期**的数据。

    ⚠ 两次踩坑：先用 `bytes(range(256))*N`（周期 256），后用 64KB 块重复
    （周期 65536）—— 而错误的偏移差常常正好是 64KB 的整数倍，
    于是错误算法也"碰巧"拼对，测试全绿却漏掉真 bug。
    必须整段真随机。
    """
    random.seed(20261007 + size)
    return random.randbytes(size)


def old_way(data: bytes, parts: list) -> bytes:
    """旧（错误）算法：`idx * bsize`。"""
    out = []
    for p in parts:
        i, b = int(p["index"]), int(p["block_size"])
        out.append(data[i * b:(i + 1) * b])
    return b"".join(out)


def new_way(data: bytes, parts: list) -> bytes:
    """新（正确）算法：按 index 排序 + 累加偏移。

    与 `md_media._upload_bytes_to_qq` 里的实现保持同一逻辑。
    """
    ordered = sorted([p for p in parts if p.get("index") is not None],
                     key=lambda p: int(p["index"]))
    out, offset = [], 0
    for p in ordered:
        b = int(p["block_size"] or 0)
        chunk = data[offset:offset + b] if b > 0 else data[offset:]
        if not chunk:
            break
        offset += len(chunk)
        out.append(chunk)
    return b"".join(out), offset


print("═══ 分片上传拼装回归 ═══")

BS = 5 * 1024 * 1024
for total, label in [
    (BS // 2, "单分片（<5MB）"),
    (BS, "整一块"),
    (BS + 12345, "两块，尾块是零头"),
    (12 * 1024 * 1024, "三块（5+5+2）—— 线上踩坑的形状"),
]:
    data = _make(total)
    n = max(1, (total + BS - 1) // BS)
    parts = []
    for i in range(n):
        sz = min(BS, total - i * BS)
        parts.append({"index": i, "block_size": str(sz),
                      "presigned_url": f"http://x/{i}"})
    good, off = new_way(data, parts)
    print(f"\n[{label}] 文件 {total} 字节 | 分片 {[p['block_size'] for p in parts]}")
    check("★ 新算法拼出来与原文件逐字节一致", good == data,
          f"sha1 {hashlib.sha1(good).hexdigest()[:10]} vs {hashlib.sha1(data).hexdigest()[:10]}")
    check("★ 新算法累计偏移 == 文件长度（完整性自检依据）", off == total,
          f"{off} vs {total}")
    bad = old_way(data, parts)
    if n > 1:
        check("旧算法确实会拼错（证明本测试能抓到该 bug）", bad != data,
              "旧算法竟然也对？")
    else:
        check("单分片时新旧算法一致", bad == data)


# ---- 乱序到达也必须拼对（平台不保证顺序）----
print("\n[乱序分片]")
data = _make(12 * 1024 * 1024)
parts = [{"index": i, "block_size": str(min(BS, len(data) - i * BS)),
          "presigned_url": f"http://x/{i}"} for i in range(3)]
shuffled = [parts[2], parts[0], parts[1]]
good, off = new_way(data, shuffled)
check("★ 分片乱序返回时仍能拼对（按 index 排序）", good == data and off == len(data))

print(f"\n结果：{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
