"""Minecraft Java 版服务器查询（Server List Ping，TCP，纯标准库）。

返回人数、服务端版本（version.name，例如 "Paper 1.20.4"）、协议号、MOTD、延迟。
只支持 1.7 及以上的 Java 版；基岩版（UDP）与 1.6 以前的旧协议不支持。
"""
import asyncio
import ipaddress
import json
import re
import socket
import struct
import time

DEFAULT_PORT = 25565
TIMEOUT = 5.0
MAX_BYTES = 2 * 1024 * 1024
ALLOW_PRIVATE = False  # 默认禁止查询内网/回环地址，避免被群友拿来探测机器人所在内网
_COLOR_RE = re.compile("§.")


class McError(Exception):
    pass


def _varint(n: int) -> bytes:
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _packet(pid: int, payload: bytes = b"") -> bytes:
    body = _varint(pid) + payload
    return _varint(len(body)) + body


async def _read_varint(reader, limit=5) -> int:
    n = 0
    for i in range(limit):
        b = (await reader.readexactly(1))[0]
        n |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return n
    raise McError("invalid varint")


def parse_host_port(text: str, default_port=DEFAULT_PORT):
    """'host', 'host:port', '[::1]:25565' -> (host, port, explicit_port)。失败抛 McError。"""
    t = (text or "").strip()
    if not t or len(t) > 260:
        raise McError("地址无效")
    m = re.match(r"^\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?$", t)
    if m:
        host, port = m.group(1), m.group(2)
    elif t.count(":") == 1:
        host, port = t.split(":")
    else:
        host, port = t, None
    if not re.match(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?$|^[0-9A-Fa-f:]+$", host):
        raise McError("地址格式无效")
    if port is not None:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise McError("端口必须是 1–65535")
    return host, int(port) if port else default_port, port is not None


def motd_text(desc) -> str:
    """description 可能是字符串、{'text','extra'} 或列表，统一成纯文本。"""
    def walk(d):
        if d is None:
            return ""
        if isinstance(d, str):
            return d
        if isinstance(d, list):
            return "".join(walk(x) for x in d)
        if isinstance(d, dict):
            return str(d.get("text", "")) + walk(d.get("extra"))
        return str(d)
    s = _COLOR_RE.sub("", walk(desc))
    return re.sub(r"\s+", " ", s).strip()[:120]


def _check_public(host: str, port: int):
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise McError("域名解析失败") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise McError("不允许查询内网/保留地址")


def _srv(host: str):
    """可选：有 dnspython 时解析 _minecraft._tcp SRV 记录，没有就跳过。"""
    try:
        import dns.resolver  # type: ignore
        ans = dns.resolver.resolve(f"_minecraft._tcp.{host}", "SRV", lifetime=3)
        r = sorted(ans, key=lambda x: (x.priority, -x.weight))[0]
        return str(r.target).rstrip("."), int(r.port)
    except Exception:  # noqa: BLE001
        return None


async def ping(host: str, port: int = DEFAULT_PORT, try_srv: bool = False) -> dict:
    """成功返回 dict(version, protocol, players, max_players, motd, latency_ms)；失败抛 McError。"""
    conn_host, conn_port = host, port
    if try_srv:
        r = await asyncio.to_thread(_srv, host)
        if r:
            conn_host, conn_port = r
    await asyncio.to_thread(_check_public, conn_host, conn_port)
    try:
        return await asyncio.wait_for(_ping(host, conn_host, conn_port), TIMEOUT + 3)
    except asyncio.TimeoutError as e:
        raise McError("连接超时") from e


async def _ping(orig_host, host, port) -> dict:
    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT)
    except asyncio.TimeoutError as e:
        raise McError("连接超时") from e
    except socket.gaierror as e:
        raise McError("域名解析失败") from e
    except ConnectionRefusedError as e:
        raise McError("连接被拒绝（端口未开放或服务器未启动）") from e
    except OSError as e:
        raise McError("网络连接失败") from e
    try:
        hs = _varint(47) + _varint(len(orig_host.encode())) + orig_host.encode() + struct.pack(">H", port) + _varint(1)
        writer.write(_packet(0x00, hs) + _packet(0x00))
        await writer.drain()
        length = await asyncio.wait_for(_read_varint(reader), TIMEOUT)
        if not 1 <= length <= MAX_BYTES:
            raise McError("服务器返回了无效响应")
        data = await asyncio.wait_for(reader.readexactly(length), TIMEOUT)
        # 包 ID varint + 字符串长度 varint + JSON
        pos = 0

        def vi():
            nonlocal pos
            n = 0
            for i in range(5):
                b = data[pos]; pos += 1
                n |= (b & 0x7F) << (7 * i)
                if not b & 0x80:
                    return n
            raise McError("服务器返回了无效响应")
        if vi() != 0x00:
            raise McError("服务器返回了无效响应")
        slen = vi()
        raw = data[pos:pos + slen]
        info = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(info, dict):
            raise McError("服务器返回了无效响应")
        # 单独测一次往返延迟（失败就用连接+状态耗时）
        latency = int((time.perf_counter() - t0) * 1000)
        try:
            t1 = time.perf_counter()
            writer.write(_packet(0x01, struct.pack(">q", 0)))
            await writer.drain()
            await asyncio.wait_for(_read_varint(reader), 2)
            latency = int((time.perf_counter() - t1) * 1000)
        except Exception:  # noqa: BLE001
            pass
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, IndexError, UnicodeError) as e:
        raise McError("服务器返回了无效响应") from e
    finally:
        writer.close()
    ver = info.get("version") if isinstance(info.get("version"), dict) else {}
    pl = info.get("players") if isinstance(info.get("players"), dict) else {}
    return {
        "version": re.sub(r"\s+", " ", _COLOR_RE.sub("", str(ver.get("name", "未知")))).strip()[:60] or "未知",
        "protocol": ver.get("protocol"),
        "players": int(pl.get("online", 0) or 0),
        "max_players": int(pl.get("max", 0) or 0),
        "motd": motd_text(info.get("description")),
        "latency_ms": latency,
    }


async def query_mc(name: str, host: str, port: int, try_srv: bool = False, logger=None) -> dict:
    res = {"name": name, "host": host, "port": port, "ok": False, "kind": "mc"}
    try:
        res.update(await ping(host, port, try_srv), ok=True)
    except McError as e:
        res["error"] = str(e)
        if logger:
            logger.warning(f"MC 查询失败 {name} {host}:{port}: {e}")
    except Exception as e:  # noqa: BLE001
        res["error"] = "未知错误"
        if logger:
            logger.warning(f"MC 查询异常 {name} {host}:{port}: {type(e).__name__}: {e}")
    return res
