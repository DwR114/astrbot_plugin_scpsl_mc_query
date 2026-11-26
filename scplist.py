"""scplist.kr 数据源（官方前端专用 API 主机 api.scplist.kr）。

注意：
- 只能请求 api.scplist.kr，绝不要请求 backend.scplist.kr（直连会被封 IP / ASN）。
- 限流：/servers 与 /servers/{id} 每分钟 10 次，/servers/players 每分钟 20 次，
  所以这里对结果做了很短的缓存（players 10 秒、列表 60 秒），避免多个群同时 /cx 触发限流。
- 返回 JSON 的字段名在文档里没有写明，这里的解析尽量宽松（见 _parse_entry）。
"""
import asyncio
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.scplist.kr"
UA = "astrbot_plugin_scpsl_a2s/1.2 (AstrBot plugin)"
PLAYERS_TTL = 10.0
LIST_TTL = 60.0
CHUNK = 50
_cache: dict = {}
_TAG_RE = re.compile(r"<[^>]{0,40}>")


class ScplistError(Exception):
    pass


def _http_get(url: str, timeout: float = 8.0):
    assert url.startswith(BASE + "/"), "只允许请求 api.scplist.kr"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise ScplistError("scplist 请求过于频繁（被限流），请稍后再试") from e
        raise ScplistError(f"scplist 返回 HTTP {e.code}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        raise ScplistError("无法连接 scplist.kr") from e
    try:
        return json.loads(body)
    except ValueError as e:
        raise ScplistError("scplist 返回的不是 JSON") from e


async def _get(path: str, params: dict = None, ttl: float = 0.0):
    url = BASE + path + (("?" + urllib.parse.urlencode(params, safe=",")) if params else "")
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = await asyncio.to_thread(_http_get, url)
    _cache[url] = (now, data)
    if len(_cache) > 64:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:32]:
            _cache.pop(k, None)
    return data


# ---------- 宽松解析 ----------
def _pick(d, keys):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return None


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _entries(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for _ in range(2):
        for k in ("servers", "data", "results", "items", "list"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                data = v
                break
        else:
            break
    out = []
    for k, v in data.items():  # 形如 {"123": {...}} 或 {"123": "4/45"}
        if isinstance(v, dict):
            out.append({"id": k, **v})
        elif isinstance(v, (str, int, list)):
            out.append({"id": k, "players": v})
    return out


def _parse_entry(e):
    if not isinstance(e, dict):
        return None
    sid = _int(_pick(e, ("id", "serverId", "server_id", "ID", "serverID")))
    raw = _pick(e, ("players", "currentPlayers", "current", "playerCount", "online", "playersCount"))
    mx = _pick(e, ("maxPlayers", "max", "maxSlots", "slots", "max_players", "maximum"))
    cur = None
    if isinstance(raw, dict):
        cur = _int(_pick(raw, ("current", "count", "online", "players")))
        mx = _pick(raw, ("max", "maximum", "maxPlayers")) if mx is None else mx
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        cur, mx = _int(raw[0]), raw[1]
    elif isinstance(raw, str) and "/" in raw:
        a, b = raw.split("/", 1)
        cur, mx = _int(a), b
    else:
        cur = _int(raw)
    mx = _int(mx)
    if sid is None or cur is None:
        return None
    name = _pick(e, ("name", "serverName", "hostname"))
    return {"id": sid, "players": cur, "max_players": mx if mx is not None else 0,
            "name": _TAG_RE.sub("", str(name)).strip() if name else ""}


def parse_servers(data) -> dict:
    """返回 {id: {id, players, max_players, name}}。"""
    out = {}
    for e in _entries(data):
        p = _parse_entry(e)
        if p:
            out[p["id"]] = p
    return out


# ---------- 对外接口 ----------
async def players_for(ids) -> dict:
    """查询若干服务器的人数。离线服务器不在返回值里。"""
    ids = sorted({int(i) for i in ids})
    found = {}
    for i in range(0, len(ids), CHUNK):
        part = ids[i:i + CHUNK]
        data = await _get("/api/v2/servers/players", {"serverIds": ",".join(map(str, part))}, PLAYERS_TTL)
        parsed = parse_servers(data)
        if not parsed and _entries(data):
            raise ScplistError("scplist 返回格式无法识别（请把 /api/v2/servers/players 的返回样例反馈给插件作者）")
        found.update(parsed)
    return found


async def search(keyword: str, limit: int = 30) -> list:
    """在在线服务器列表里按名称关键词搜索（客户端过滤，列表缓存 60 秒）。"""
    data = await _get("/api/v2/servers", None, LIST_TTL)
    servers = parse_servers(data)
    if not servers and _entries(data):
        raise ScplistError("scplist 返回格式无法识别（请把 /api/v2/servers 的返回样例反馈给插件作者）")
    kw = (keyword or "").strip().lower()
    items = [s for s in servers.values() if not kw or kw in s["name"].lower()]
    items.sort(key=lambda s: -s["players"])
    return items[:limit]
