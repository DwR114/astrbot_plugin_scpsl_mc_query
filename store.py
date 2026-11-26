"""持久化存储：服务器列表与管理员名单（与 AstrBot 无关，便于单独测试）。"""
import asyncio
import json
import os
import tempfile
from pathlib import Path


class StoreError(Exception):
    """读写或解析失败。出现该异常时绝不会覆盖已有文件。"""


def _read_json(path: Path, key: str, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise StoreError(f"{path.name} 读取失败或已损坏：{type(e).__name__}") from e
    if not isinstance(data, dict):
        raise StoreError(f"{path.name} 格式不正确")
    if key not in data:
        return default
    if not isinstance(data[key], type(default)):
        raise StoreError(f"{path.name} 格式不正确")
    return data[key]


def _write_json_atomic(path: Path, key: str, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    full = {}
    if path.exists():  # 保留文件里其他未知字段，避免误删旧数据
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                full = old
        except (OSError, ValueError) as e:
            raise StoreError(f"{path.name} 读取失败或已损坏：{type(e).__name__}") from e
    full[key] = value
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise StoreError(f"{path.name} 写入失败：{type(e).__name__}") from e


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.servers_path = self.dir / "servers.json"
        self.admins_path = self.dir / "admins.json"
        self.settings_path = self.dir / "settings.json"
        self._lock = asyncio.Lock()  # 所有读改写都在同一把锁内，避免并发覆盖

    async def _rd(self, path, key, default):
        return await asyncio.to_thread(_read_json, path, key, default)

    async def _wr(self, path, key, value):
        await asyncio.to_thread(_write_json_atomic, path, key, value)

    # ---- 服务器（按群隔离：scope 形如 "平台:群号"） ----
    async def list_servers(self, scope: str) -> dict:
        async with self._lock:
            groups = await self._rd(self.servers_path, "groups", {})
            return dict(groups.get(scope, {}))

    async def bind(self, scope: str, name: str, host: str, port: int, sid: int = None, kind: str = None, srv: bool = False) -> bool:
        """返回 True 表示更新了已有记录，False 表示新建。"""
        async with self._lock:
            groups = await self._rd(self.servers_path, "groups", {})
            servers = groups.setdefault(scope, {})
            existed = name in servers
            if sid:
                servers[name] = {"sid": int(sid)}
            else:
                servers[name] = {"host": host, "port": port}
                if kind:
                    servers[name]["kind"] = kind
                if srv:
                    servers[name]["srv"] = True
            await self._wr(self.servers_path, "groups", groups)
            return existed

    async def unbind(self, scope: str, name: str) -> bool:
        async with self._lock:
            groups = await self._rd(self.servers_path, "groups", {})
            servers = groups.get(scope, {})
            if name not in servers:
                return False
            del servers[name]
            if not servers:
                groups.pop(scope, None)
            await self._wr(self.servers_path, "groups", groups)
            return True

    # ---- 管理员 ----
    async def is_admin(self, uid: str) -> bool:
        async with self._lock:
            return uid in await self._rd(self.admins_path, "admins", [])

    async def op_init(self, uid: str) -> str:
        """'ok' 成功成为首位管理员 / 'denied' 已有管理员（含本人已是管理员）。"""
        async with self._lock:
            admins = await self._rd(self.admins_path, "admins", [])
            if admins:
                return "denied"
            await self._wr(self.admins_path, "admins", [uid])
            return "ok"

    async def op_add(self, actor: str, target: str) -> str:
        """'added' / 'exists' / 'denied'。权限检查与写入在同一把锁内。"""
        async with self._lock:
            admins = await self._rd(self.admins_path, "admins", [])
            if actor not in admins:
                return "denied"
            if target in admins:
                return "exists"
            admins.append(target)
            await self._wr(self.admins_path, "admins", admins)
            return "added"

    async def op_remove(self, uid: str) -> bool:
        """从管理员名单移除（仅 WebUI 使用，WebUI 本身已由 Dashboard 登录保护）。"""
        async with self._lock:
            admins = await self._rd(self.admins_path, "admins", [])
            if uid not in admins:
                return False
            admins.remove(uid)
            await self._wr(self.admins_path, "admins", admins)
            return True

    async def op_force_add(self, uid: str) -> bool:
        """WebUI 直接添加管理员。返回 False 表示已存在。"""
        async with self._lock:
            admins = await self._rd(self.admins_path, "admins", [])
            if uid in admins:
                return False
            admins.append(uid)
            await self._wr(self.admins_path, "admins", admins)
            return True

    async def list_admins(self) -> list:
        async with self._lock:
            return list(await self._rd(self.admins_path, "admins", []))

    # ---- WebUI：跨群读写服务器 ----
    async def all_servers(self) -> dict:
        async with self._lock:
            groups = await self._rd(self.servers_path, "groups", {})
            return {k: dict(v) for k, v in groups.items()}

    # ---- 出图设置 ----
    DEFAULT_SETTINGS = {"use_image": True, "random_bg": False, "selected_bg": None}

    async def get_settings(self) -> dict:
        async with self._lock:
            cur = await self._rd(self.settings_path, "settings", {})
        out = dict(self.DEFAULT_SETTINGS)
        for k in out:
            if k in cur:
                out[k] = cur[k]
        return out

    async def update_settings(self, **changes) -> dict:
        async with self._lock:
            cur = await self._rd(self.settings_path, "settings", {})
            for k, v in changes.items():
                if k in self.DEFAULT_SETTINGS:
                    cur[k] = v
            await self._wr(self.settings_path, "settings", cur)
        return await self.get_settings()
