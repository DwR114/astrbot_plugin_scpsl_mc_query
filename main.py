import asyncio
import base64
import re
import secrets
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import mcping, scplist
from . import webcompat as web
from .bg import BgError, BgManager
from .query import format_report, query_all, query_one
from .render import render_jpeg, render_report
from .store import Store, StoreError

PLUGIN_NAME = "astrbot_plugin_scpsl_a2s"
SCOPE_RE = re.compile(r"^[\w\-.]{1,64}:[\w\-.@]{1,128}$")

NAME_RE = re.compile(r"^\S{1,32}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?$|^[0-9A-Fa-f:]+$")
GROUP_ONLY = "该命令仅限群聊使用，私聊中不可用。"
UID_RE = re.compile(r"^[\w\-.@:]{1,128}$")


def _data_dir(plugin_file: str) -> Path:
    """优先使用 AstrBot 提供的插件数据目录（更新插件时不会丢失数据）。"""
    try:
        from astrbot.api.star import StarTools
        return Path(StarTools.get_data_dir())
    except Exception:  # 旧版本没有该接口时退回到插件目录下的 data/
        return Path(plugin_file).parent / "data"


class ScpslA2SPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = _data_dir(__file__)
        self.store = Store(self.data_dir)
        self.bg = BgManager(self.data_dir)
        self._mc_last = {}
        self._register_web(context)

    # ---------- 出图 ----------
    async def _render(self, results, title=None, bg_image="auto"):
        settings = await self.store.get_settings()
        if bg_image == "auto":
            bg_image = await asyncio.to_thread(self.bg.pick, settings)
        return await asyncio.to_thread(
            render_report, results, bg_image, title, self.data_dir
        )

    # ---------- 工具 ----------
    @staticmethod
    def _args(event: AstrMessageEvent, cmd: str) -> list:
        parts = (event.message_str or "").strip().split()
        if parts and parts[0].lstrip("/!！").lower() == cmd:
            parts = parts[1:]
        return parts

    @staticmethod
    def _uid(event: AstrMessageEvent) -> str:
        # 带平台前缀，避免不同平台的相同数字 ID 互相串权限
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    @staticmethod
    def _scope(event: AstrMessageEvent):
        """返回当前群的作用域键；私聊返回 None。服务器绑定按群隔离。"""
        gid = event.get_group_id()
        if not gid:
            return None
        return f"{event.get_platform_name()}:{gid}"

    async def _require_admin(self, event: AstrMessageEvent):
        """返回 None 表示通过；否则返回错误文本。必须在任何管理操作之前调用。"""
        try:
            if await self.store.is_admin(self._uid(event)):
                return None
        except StoreError as e:
            return f"⚠️ 无法读取管理员名单：{e}"
        return "⛔ 权限不足：该命令仅限管理员使用。"

    # ---------- 命令 ----------
    @filter.command("cx")
    async def cx(self, event: AstrMessageEvent):
        '''查询所有已绑定 SCP:SL 服务器的实时在线人数'''
        event.stop_event()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result(GROUP_ONLY)
            return
        try:
            servers = await self.store.list_servers(scope)
        except StoreError as e:
            yield event.plain_result(f"⚠️ 读取服务器配置失败：{e}")
            return
        if not servers:
            yield event.plain_result("本群尚未绑定任何服务器，请管理员先使用 /bind 服务器名 IP 端口 绑定。")
            return
        results = await query_all(servers, logger)
        yield await self._report(event, results)

    async def _report(self, event, results):
        """按设置出图；出图失败退回文字，保证一定有输出。"""
        try:
            settings = await self.store.get_settings()
            if settings.get("use_image", True):
                png = await self._render(results)
                from astrbot.api.message_components import Image as ImageComp
                return event.chain_result([ImageComp.fromBytes(png)])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"本地出图失败，已退回文字输出：{type(e).__name__}: {e}")
        return event.plain_result(format_report(results))

    @filter.command("mc")
    async def mc(self, event: AstrMessageEvent):
        '''/mc 查询本群绑定的 Minecraft 服务器；/mc 地址[:端口] 临时查询任意 Java 版服务器（含服务端版本）'''
        event.stop_event()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result(GROUP_ONLY)
            return
        a = self._args(event, "mc")
        if len(a) > 1:
            yield event.plain_result("用法：/mc 查询本群已绑定的 MC 服务器\n/mc 地址[:端口] 临时查询一台")
            return
        if a:
            me, now = self._uid(event), time.monotonic()
            if now - self._mc_last.get(me, 0) < 5:
                yield event.plain_result("查询太快了，请 5 秒后再试。")
                return
            self._mc_last[me] = now
            if len(self._mc_last) > 500:
                self._mc_last = {k: v for k, v in self._mc_last.items() if now - v < 5}
            try:
                host, port, explicit = mcping.parse_host_port(a[0])
            except mcping.McError as e:
                yield event.plain_result(f"⚠️ {e}")
                return
            res = await mcping.query_mc(a[0], host, port, not explicit, logger)
            yield await self._report(event, [res])
            return
        try:
            servers = await self.store.list_servers(scope)
        except StoreError as e:
            yield event.plain_result(f"⚠️ 读取服务器配置失败：{e}")
            return
        servers = {n: s for n, s in servers.items() if s.get("kind") == "mc"}
        if not servers:
            yield event.plain_result("本群尚未绑定 MC 服务器，请管理员使用 /mcbind 名称 地址[:端口] 绑定。")
            return
        yield await self._report(event, await query_all(servers, logger))

    @filter.command("mcbind")
    async def mcbind(self, event: AstrMessageEvent):
        '''绑定 Minecraft Java 服务器：/mcbind 名称 地址[:端口]（仅管理员；解除绑定用 /unbind 名称）'''
        event.stop_event()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result(GROUP_ONLY)
            return
        err = await self._require_admin(event)
        if err:
            yield event.plain_result(err)
            return
        a = self._args(event, "mcbind")
        if len(a) != 2 or not NAME_RE.match(a[0]):
            yield event.plain_result("用法：/mcbind 服务器名 地址[:端口]\n示例：/mcbind 生存服 mc.example.com\n端口省略时默认 25565")
            return
        try:
            host, port, explicit = mcping.parse_host_port(a[1])
        except mcping.McError as e:
            yield event.plain_result(f"⚠️ {e}")
            return
        try:
            updated = await self.store.bind(scope, a[0], host, port, kind="mc", srv=not explicit)
        except StoreError as e:
            yield event.plain_result(f"⚠️ 保存失败，已有配置未被修改：{e}")
            return
        yield event.plain_result(f"✅ 已{'更新' if updated else '绑定'} MC 服务器\n名称：{a[0]}\n地址：{host}\n端口：{port}")

    @filter.command("bind")
    async def bind(self, event: AstrMessageEvent):
        '''绑定/更新服务器：/bind 服务器名 服务器IP 端口（仅管理员）'''
        event.stop_event()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result(GROUP_ONLY)
            return
        err = await self._require_admin(event)
        if err:
            yield event.plain_result(err)
            return
        a = self._args(event, "bind")
        usage = ("用法：\n/bind 服务器名 scplist服务器ID\n/bind 服务器名 服务器IP 端口（A2S）\n"
                 "示例：/bind 一号服 12345")
        if len(a) not in (2, 3):
            yield event.plain_result(usage)
            return
        name = a[0]
        if not NAME_RE.match(name):
            yield event.plain_result("服务器名称无效：不能含空格，长度 1–32。")
            return
        sid, host, port = None, "", 0
        if len(a) == 2:
            if not (a[1].isdigit() and 1 <= int(a[1]) <= 10**9):
                yield event.plain_result(usage)
                return
            sid = int(a[1])
        else:
            host, port_s = a[1], a[2]
            if not HOST_RE.match(host):
                yield event.plain_result("服务器 IP 或域名格式无效。")
                return
            if not port_s.isdigit() or not (1 <= int(port_s) <= 65535):
                yield event.plain_result("端口无效：必须是 1–65535 之间的整数。")
                return
            port = int(port_s)
        try:
            updated = await self.store.bind(scope, name, host, port, sid)
        except StoreError as e:
            yield event.plain_result(f"⚠️ 保存失败，已有配置未被修改：{e}")
            return
        where = f"scplist ID：{sid}" if sid else f"地址：{host}\n端口：{port}"
        yield event.plain_result(f"✅ 已{'更新' if updated else '绑定'}服务器\n名称：{name}\n{where}")

    @filter.command("bindlist")
    async def bindlist(self, event: AstrMessageEvent):
        '''查看已绑定服务器（仅管理员）'''
        event.stop_event()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result(GROUP_ONLY)
            return
        err = await self._require_admin(event)
        if err:
            yield event.plain_result(err)
            return
        try:
            servers = await self.store.list_servers(scope)
        except StoreError as e:
            yield event.plain_result(f"⚠️ 读取服务器配置失败：{e}")
            return
        if not servers:
            yield event.plain_result("本群当前没有已绑定的服务器。")
            return
        lines = [f"本群已绑定服务器（{len(servers)}）："]
        for n, s in servers.items():
            lines.append(f"• {n}  " + (f"scplist#{s['sid']}" if s.get("sid") else (("MC " if s.get("kind") == "mc" else "") + f"{s['host']}:{s['port']}")))
        yield event.plain_result("\n".join(lines))

    @filter.command("unbind")
    async def unbind(self, event: AstrMessageEvent):
        '''解除服务器绑定：/unbind 服务器名（仅管理员）'''
        event.stop_event()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result(GROUP_ONLY)
            return
        err = await self._require_admin(event)
        if err:
            yield event.plain_result(err)
            return
        a = self._args(event, "unbind")
        if len(a) != 1:
            yield event.plain_result("用法：/unbind 服务器名")
            return
        try:
            ok = await self.store.unbind(scope, a[0])
        except StoreError as e:
            yield event.plain_result(f"⚠️ 保存失败，已有配置未被修改：{e}")
            return
        yield event.plain_result(f"✅ 已解除绑定：{a[0]}" if ok else f"未找到名为「{a[0]}」的服务器。")

    @filter.command("op")
    async def op(self, event: AstrMessageEvent):
        '''/op 初始化首位管理员；/op 用户ID 添加管理员（仅管理员）'''
        event.stop_event()
        scope = self._scope(event)
        if scope is None:
            yield event.plain_result(GROUP_ONLY)
            return
        a = self._args(event, "op")
        me = self._uid(event)
        try:
            if len(a) == 0:
                r = await self.store.op_init(me)
                if r == "ok":
                    yield event.plain_result(f"✅ 你已成为首位管理员（{me}）。")
                else:
                    yield event.plain_result("⛔ 已存在管理员，无法通过 /op 自行提权。请让现有管理员使用 /op 用户ID 添加你。")
                return
            if len(a) != 1:
                yield event.plain_result("用法：/op 或 /op 用户ID")
                return
            err = await self._require_admin(event)  # 先验证权限
            if err:
                yield event.plain_result(err)
                return
            target = a[0]
            if not UID_RE.match(target):
                yield event.plain_result("用户 ID 格式无效。")
                return
            if ":" not in target:
                target = f"{event.get_platform_name()}:{target}"  # 默认同平台
            r = await self.store.op_add(me, target)
            msg = {"added": f"✅ 已添加管理员：{target}",
                   "exists": f"{target} 已经是管理员。",
                   "denied": "⛔ 权限不足。"}[r]
            yield event.plain_result(msg)
        except StoreError as e:
            yield event.plain_result(f"⚠️ 操作失败，已有数据未被修改：{e}")

    # ---------- WebUI 后端 ----------
    def _register_web(self, context):
        routes = [
            ("state", self.api_state, ["GET"], "读取全部配置"),
            ("servers/save", self.api_server_save, ["POST"], "添加/更新服务器"),
            ("servers/delete", self.api_server_delete, ["POST"], "删除服务器"),
            ("servers/test", self.api_server_test, ["POST"], "测试查询服务器"),
            ("scplist/search", self.api_scplist_search, ["POST"], "在 scplist 搜索服务器"),
            ("admins/add", self.api_admin_add, ["POST"], "添加管理员"),
            ("admins/remove", self.api_admin_remove, ["POST"], "移除管理员"),
            ("settings/save", self.api_settings_save, ["POST"], "保存出图设置"),
            ("backgrounds/upload", self.api_bg_upload, ["POST"], "上传背景图"),
            ("backgrounds/delete", self.api_bg_delete, ["POST"], "删除背景图"),
            ("preview", self.api_preview, ["POST"], "预览出图效果"),
        ]
        try:
            for path, handler, methods, desc in routes:
                context.register_web_api(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)
        except Exception as e:  # noqa: BLE001  旧版 AstrBot 没有该接口：命令照常可用，只是没有管理页
            logger.warning(f"当前 AstrBot 不支持插件 WebUI（register_web_api 不可用），已跳过：{e}")

    @staticmethod
    def _bad(msg):
        return web.error_response(msg, status_code=400)

    def _bg_list(self):
        out = []
        for it in self.bg.list():
            raw = self.bg.thumb_bytes(it["id"])
            thumb = "data:image/jpeg;base64," + base64.b64encode(raw).decode() if raw else ""
            out.append({**it, "thumb": thumb})
        return out

    async def api_state(self):
        try:
            servers = await self.store.all_servers()
            admins = await self.store.list_admins()
            settings = await self.store.get_settings()
            bgs = await asyncio.to_thread(self._bg_list)
        except (StoreError, BgError) as e:
            return self._bad(str(e))
        return web.json_response({
            "servers": servers, "admins": admins, "settings": settings, "backgrounds": bgs,
        })

    async def api_server_save(self):
        p = await web.read_json()
        scope, name = str(p.get("scope", "")).strip(), str(p.get("name", "")).strip()
        if not SCOPE_RE.match(scope):
            return self._bad("群标识格式应为 平台:群号，例如 aiocqhttp:123456789")
        if not NAME_RE.match(name):
            return self._bad("服务器名称无效：不能含空格，长度 1–32。")
        sid_raw = str(p.get("sid") or "").strip()
        host, port = str(p.get("host", "")).strip(), 0
        sid = None
        if p.get("kind") == "mc":
            port_raw = str(p.get("port") or "").strip()
            try:
                host, port, explicit = mcping.parse_host_port(host + (":" + port_raw if port_raw else ""))
            except mcping.McError as e:
                return self._bad(str(e))
            try:
                updated = await self.store.bind(scope, name, host, port, kind="mc", srv=not explicit)
            except StoreError as e:
                return self._bad(f"保存失败，已有配置未被修改：{e}")
            return web.json_response({"ok": True, "updated": updated})
        if sid_raw:  # 填了 scplist ID 就走 scplist，忽略 IP/端口
            if not (sid_raw.isdigit() and 1 <= int(sid_raw) <= 10**9):
                return self._bad("scplist 服务器 ID 必须是正整数。")
            sid = int(sid_raw)
        else:
            if not HOST_RE.match(host):
                return self._bad("请填写 scplist 服务器 ID，或填写有效的 IP/域名 + A2S 端口。")
            try:
                port = int(p.get("port"))
            except (TypeError, ValueError):
                return self._bad("端口必须是整数。")
            if not 1 <= port <= 65535:
                return self._bad("端口必须在 1–65535 之间。")
        try:
            updated = await self.store.bind(scope, name, host, port, sid)
        except StoreError as e:
            return self._bad(f"保存失败，已有配置未被修改：{e}")
        return web.json_response({"ok": True, "updated": updated})

    async def api_server_delete(self):
        p = await web.read_json()
        scope, name = str(p.get("scope", "")), str(p.get("name", ""))
        if not SCOPE_RE.match(scope):
            return self._bad("群标识无效。")
        try:
            ok = await self.store.unbind(scope, name)
        except StoreError as e:
            return self._bad(f"保存失败，已有配置未被修改：{e}")
        return web.json_response({"ok": ok})

    async def api_server_test(self):
        p = await web.read_json()
        sid_raw = str(p.get("sid") or "").strip()
        if p.get("kind") == "mc":
            port_raw = str(p.get("port") or "").strip()
            try:
                host, port, explicit = mcping.parse_host_port(str(p.get("host", "")).strip() + (":" + port_raw if port_raw else ""))
            except mcping.McError as e:
                return self._bad(str(e))
            return web.json_response(await mcping.query_mc("test", host, port, not explicit, logger))
        if sid_raw:
            if not (sid_raw.isdigit() and 1 <= int(sid_raw) <= 10**9):
                return self._bad("scplist 服务器 ID 必须是正整数。")
            res = await query_all({"test": {"sid": int(sid_raw)}}, logger)
            return web.json_response(res[0])
        host = str(p.get("host", "")).strip()
        try:
            port = int(p.get("port"))
        except (TypeError, ValueError):
            return self._bad("端口必须是整数。")
        if not HOST_RE.match(host) or not 1 <= port <= 65535:
            return self._bad("IP/域名或端口无效。")
        return web.json_response(await query_one("test", host, port, logger))

    async def api_scplist_search(self):
        """按名称关键词在 scplist 在线列表里找服务器 ID（scplist 限流：列表接口每分钟 10 次，已缓存 60 秒）。"""
        p = await web.read_json()
        kw = str(p.get("keyword", "")).strip()[:40]
        try:
            items = await scplist.search(kw)
        except scplist.ScplistError as e:
            return self._bad(str(e))
        return web.json_response({"items": items})

    async def api_admin_add(self):
        p = await web.read_json()
        uid = str(p.get("uid", "")).strip()
        if ":" not in uid or not UID_RE.match(uid):
            return self._bad("管理员格式应为 平台:用户ID，例如 aiocqhttp:123456789")
        try:
            added = await self.store.op_force_add(uid)
        except StoreError as e:
            return self._bad(f"保存失败，已有数据未被修改：{e}")
        return web.json_response({"ok": True, "added": added})

    async def api_admin_remove(self):
        p = await web.read_json()
        try:
            ok = await self.store.op_remove(str(p.get("uid", "")))
        except StoreError as e:
            return self._bad(f"保存失败，已有数据未被修改：{e}")
        return web.json_response({"ok": ok})

    async def api_settings_save(self):
        p = await web.read_json()
        changes = {}
        for k in ("use_image", "random_bg"):
            if k in p:
                if not isinstance(p[k], bool):
                    return self._bad(f"{k} 必须是布尔值")
                changes[k] = p[k]
        if "selected_bg" in p:
            sel = p["selected_bg"]
            if sel is not None:
                try:
                    self.bg.path_of(str(sel))
                except BgError as e:
                    return self._bad(str(e))
                if not any(i["id"] == sel for i in self.bg.list()):
                    return self._bad("所选背景不存在。")
            changes["selected_bg"] = sel
        try:
            settings = await self.store.update_settings(**changes)
        except StoreError as e:
            return self._bad(f"保存失败：{e}")
        return web.json_response({"ok": True, "settings": settings})

    async def api_bg_upload(self):
        self.bg.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.bg.dir / f".upload-{secrets.token_hex(6)}.tmp"
        try:
            name = await web.save_upload(tmp)
            if name is None:
                return self._bad("没有收到文件（字段名应为 file）。")
            info = await asyncio.to_thread(self.bg.add_from_file, tmp, Path(name).name)
        except BgError as e:
            return self._bad(str(e))
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return web.json_response({"ok": True, **info})

    async def api_bg_delete(self):
        p = await web.read_json()
        bid = str(p.get("id", ""))
        try:
            ok = await asyncio.to_thread(self.bg.delete, bid)
            settings = await self.store.get_settings()
            if settings.get("selected_bg") == bid:
                await self.store.update_settings(selected_bg=None)
        except (BgError, StoreError) as e:
            return self._bad(str(e))
        return web.json_response({"ok": ok})

    async def api_preview(self):
        """用示例数据渲染一张预览图。bg_id 指定背景；不传则按当前设置（随机开启时会随机）。"""
        p = await web.read_json()
        sample = [
            {"name": "一号服", "host": "192.0.2.10", "port": 7777, "ok": True,
             "players": 23, "max_players": 45, "latency_ms": 41},
            {"name": "二号服", "host": "192.0.2.11", "port": 7778, "ok": True,
             "players": 0, "max_players": 35, "latency_ms": 88},
            {"name": "三号服", "host": "192.0.2.12", "port": 7779, "ok": False, "error": "连接超时"},
        ]
        try:
            bid = p.get("bg_id")
            if bid:
                img = await asyncio.to_thread(self.bg.load, str(bid))
                if img is None:
                    return self._bad("所选背景不存在或已损坏。")
                png = await self._render(sample, "效果预览", img)
            else:
                png = await self._render(sample, "效果预览")
            jpg = await asyncio.to_thread(render_jpeg, png)
        except (BgError, StoreError) as e:
            return self._bad(str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"预览出图失败：{type(e).__name__}: {e}")
            return self._bad(f"出图失败：{e}")
        return web.json_response({"image": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()})

    async def terminate(self):
        pass
