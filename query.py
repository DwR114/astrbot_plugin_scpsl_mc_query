"""A2S 实时查询与结果格式化。每次调用都会重新发起网络查询，不做任何缓存。"""
import asyncio
import socket
import time

import a2s

from . import mcping, scplist

QUERY_TIMEOUT = 3.0   # 单次 A2S 套接字超时（秒）
HARD_TIMEOUT = 6.0    # 单台服务器总耗时上限（含域名解析）
MAX_CONCURRENCY = 10

_A2S_ERRORS = tuple(
    getattr(a2s, n) for n in ("BrokenMessageError", "BufferExhaustedError") if hasattr(a2s, n)
)


def _classify(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, socket.timeout, TimeoutError)):
        return "连接超时"
    if isinstance(exc, socket.gaierror):
        return "域名解析失败"
    if _A2S_ERRORS and isinstance(exc, _A2S_ERRORS):
        return "服务器返回了无效响应"
    if isinstance(exc, (ConnectionError, OSError)):
        return "网络连接失败"
    return "未知错误"


async def query_one(name: str, host: str, port: int, logger=None) -> dict:
    res = {"name": name, "host": host, "port": port, "ok": False}
    t0 = time.perf_counter()
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(a2s.info, (host, port), QUERY_TIMEOUT), HARD_TIMEOUT
        )
        res.update(
            ok=True,
            players=int(info.player_count),
            max_players=int(info.max_players),
            server_name=str(getattr(info, "server_name", "") or ""),
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as e:  # noqa: BLE001  单台失败不能影响其他服务器
        res["error"] = _classify(e)
        if logger:
            logger.warning(f"A2S 查询失败 {name} {host}:{port}: {type(e).__name__}: {e}")
    return res


async def query_all(servers: dict, logger=None) -> list:
    """servers: {名称: {"host","port"} 或 {"sid": scplist 服务器ID}}。返回顺序与 servers 一致。"""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def run(n, s):
        async with sem:
            if s.get("kind") == "mc":
                return await mcping.query_mc(n, s["host"], s["port"], s.get("srv", False), logger)
            return await query_one(n, s["host"], s["port"], logger)

    sl_names = [n for n, s in servers.items() if s.get("sid")]
    sl_found, sl_err = {}, None
    if sl_names:
        try:
            sl_found = await scplist.players_for({servers[n]["sid"] for n in sl_names})
        except scplist.ScplistError as e:
            sl_err = str(e)
            if logger:
                logger.warning(f"scplist 查询失败：{e}")
        except Exception as e:  # noqa: BLE001
            sl_err = "scplist 查询出错"
            if logger:
                logger.warning(f"scplist 查询异常：{type(e).__name__}: {e}")

    async def one(n, s):
        if not s.get("sid"):
            return await run(n, s)
        res = {"name": n, "host": "scplist.kr", "port": 0, "addr": f"scplist · ID {s['sid']}", "ok": False}
        hit = sl_found.get(s["sid"])
        if hit:
            res.update(ok=True, players=hit["players"], max_players=hit["max_players"],
                       server_name=hit.get("name", ""))
        else:
            res["error"] = sl_err or "离线（scplist 未收录）"
        return res

    return list(await asyncio.gather(*(one(n, s) for n, s in servers.items())))


SEP = "━━━━━━━━━━━━"


def _addr_lines(r):
    if r.get("addr"):
        return [r["addr"]]
    return [f"IP：{r['host']}", f"端口：{r['port']}"]


def format_report(results: list) -> str:
    """手机端友好：每行都很短，不使用会在窄屏折行的长边框。"""
    kinds = {r.get("kind") == "mc" for r in results}
    head = "Minecraft 服务器查询" if kinds == {True} else ("服务器查询" if True in kinds else "SCP:SL 服务器查询")
    lines = [f"📡 {head}", SEP]
    for r in results:
        if r["ok"]:
            lines += [
                f"🟢 {r['name']}",
                *_addr_lines(r),
                f"人数：{r['players']} / {r['max_players']}",
                *([f"版本：{r['version']}"] if r.get("version") else []),
            ]
        else:
            lines += [
                f"🔴 {r['name']}",
                *_addr_lines(r),
                f"失败：{r['error']}",
            ]
        lines.append(SEP)
    return "\n".join(lines)
