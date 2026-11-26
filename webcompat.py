"""WebUI 后端兼容层：优先使用新版 astrbot.api.web，旧版回退到 quart。"""
import inspect
from pathlib import Path

try:  # AstrBot 新版（Plugin Pages / Views）
    from astrbot.api.web import error_response, json_response, request  # type: ignore
    NEW_API = True
except Exception:  # noqa: BLE001
    NEW_API = False
    from quart import jsonify, request  # type: ignore

    def json_response(data, status_code=200):
        r = jsonify(data)
        return r if status_code == 200 else (r, status_code)

    def error_response(message, status_code=400):
        return jsonify({"status": "error", "message": message}), status_code


async def read_json() -> dict:
    if NEW_API:
        data = await request.json(default={})
    else:
        data = await request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


async def save_upload(target: Path):
    """把上传字段 file 保存到 target，返回原文件名；没有文件返回 None。"""
    files = await request.files() if NEW_API else await request.files
    up = files.get("file") if files else None
    if up is None or not getattr(up, "filename", None):
        return None
    res = up.save(target)
    if inspect.isawaitable(res):
        await res
    return up.filename
