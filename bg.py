"""背景图管理：上传校验、缩略图、选择/随机。与 AstrBot 无关，便于单独测试。"""
import json
import os
import random
import re
import secrets
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 60_000_000
MAX_SIDE = 2400
THUMB_W = 360
MAX_IMAGES = 50
ID_RE = re.compile(r"^[0-9a-f]{12}$")


class BgError(Exception):
    pass


class BgManager:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "backgrounds"
        self.thumb_dir = self.dir / ".thumbs"
        self.index_path = self.dir / "index.json"

    # ---- 索引 ----
    def _load(self) -> dict:
        if not self.index_path.exists():
            return {}
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError) as e:
            raise BgError(f"背景索引读取失败：{type(e).__name__}") from e

    def _save(self, idx: dict):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix="index", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.index_path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise BgError(f"背景索引写入失败：{type(e).__name__}") from e

    def path_of(self, bid: str) -> Path:
        if not ID_RE.match(bid or ""):
            raise BgError("背景 ID 无效")
        return self.dir / f"{bid}.jpg"

    def list(self) -> list:
        """返回仍然存在文件的背景列表（按上传时间）。"""
        idx = self._load()
        items = []
        for bid, meta in idx.items():
            if ID_RE.match(bid) and self.path_of(bid).exists():
                items.append({"id": bid, "name": meta.get("name", bid), "size": meta.get("size", 0)})
        return items

    def thumb_bytes(self, bid: str) -> bytes:
        p = self.thumb_dir / f"{bid}.jpg"
        try:
            return p.read_bytes()
        except OSError:
            return b""

    # ---- 增删 ----
    def add_from_file(self, tmp_file: Path, display_name: str) -> dict:
        """tmp_file 是已落盘的上传文件；校验后重新编码保存，原文件由调用方删除。"""
        size = Path(tmp_file).stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise BgError("图片过大（上限 15MB）")
        if len(self._load()) >= MAX_IMAGES:
            raise BgError(f"背景数量已达上限 {MAX_IMAGES} 张")
        try:
            with Image.open(tmp_file) as im:
                w, h = im.size
                if w * h > MAX_PIXELS:
                    raise BgError("图片分辨率过大")
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGB")
        except BgError:
            raise
        except Exception as e:  # noqa: BLE001
            raise BgError("无法识别的图片文件（支持 PNG / JPG / WebP / GIF 等）") from e
        if max(im.size) > MAX_SIDE:
            im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        bid = secrets.token_hex(6)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        im.save(self.path_of(bid), "JPEG", quality=90)
        th = im.copy()
        th.thumbnail((THUMB_W, THUMB_W), Image.LANCZOS)
        th.save(self.thumb_dir / f"{bid}.jpg", "JPEG", quality=78)
        idx = self._load()
        name = re.sub(r"[\r\n\t]", " ", (display_name or "background"))[:60]
        idx[bid] = {"name": name, "size": self.path_of(bid).stat().st_size}
        self._save(idx)
        return {"id": bid, "name": name}

    def delete(self, bid: str) -> bool:
        p = self.path_of(bid)
        idx = self._load()
        existed = bid in idx or p.exists()
        idx.pop(bid, None)
        self._save(idx)
        for f in (p, self.thumb_dir / f"{bid}.jpg"):
            try:
                f.unlink()
            except OSError:
                pass
        return existed

    # ---- 取图 ----
    def pick(self, settings: dict):
        """按设置选出一张背景，返回 Image 或 None（None 表示用内置默认背景）。"""
        items = self.list()
        if not items:
            return None
        chosen = None
        if settings.get("random_bg"):
            chosen = random.choice(items)["id"]
        else:
            sel = settings.get("selected_bg")
            if sel and any(i["id"] == sel for i in items):
                chosen = sel
        if not chosen:
            return None
        try:
            with Image.open(self.path_of(chosen)) as im:
                return im.convert("RGB")
        except Exception:  # noqa: BLE001
            return None

    def load(self, bid: str):
        try:
            with Image.open(self.path_of(bid)) as im:
                return im.convert("RGB")
        except Exception:  # noqa: BLE001
            return None
