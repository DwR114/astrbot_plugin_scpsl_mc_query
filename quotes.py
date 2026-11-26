"""语录导入：从 txt / md / csv / json 文本里解析出语录列表。"""
import json
import re

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LEN = 200
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_BULLET = re.compile(r"^\s*(?:[-*+•·]|\d{1,4}[.)、])\s+")


class QuoteError(Exception):
    pass


def clean(text) -> str:
    t = _CTRL.sub("", str(text)).replace("\t", " ")
    return re.sub(r"\s+", " ", t).strip()


def decode(raw: bytes) -> str:
    if len(raw) > MAX_FILE_BYTES:
        raise QuoteError("文件过大（上限 2MB）")
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise QuoteError("无法识别文件编码，请保存为 UTF-8 文本")


def _from_json(text: str) -> list:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise QuoteError("JSON 格式错误") from e
    if isinstance(data, dict):
        for k in ("quotes", "data", "items", "list"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    if not isinstance(data, list):
        raise QuoteError("JSON 应为字符串数组，或 {\"quotes\": [...]}")
    out = []
    for it in data:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in ("text", "quote", "content", "hitokoto", "msg"):
                if isinstance(it.get(k), str):
                    out.append(it[k])
                    break
    return out


def parse(raw: bytes, filename: str = "") -> dict:
    """返回 {quotes: [...], skipped_long: n, skipped_empty: n}。"""
    text = decode(raw)
    name = (filename or "").lower()
    if name.endswith(".json") or text.lstrip().startswith(("[", "{")):
        try:
            lines = _from_json(text)
        except QuoteError:
            if name.endswith(".json"):
                raise
            lines = text.splitlines()
    else:
        lines = text.splitlines()
        if name.endswith(".md"):
            lines = [_BULLET.sub("", l) for l in lines if not l.lstrip().startswith("#")]
    quotes, seen, long_n, empty_n = [], set(), 0, 0
    for l in lines:
        t = clean(l)
        if not t:
            empty_n += 1
            continue
        if len(t) > MAX_LEN:
            long_n += 1
            continue
        if t not in seen:
            seen.add(t)
            quotes.append(t)
    if not quotes:
        raise QuoteError("没有解析到任何语录（txt 请一行一条，单条不超过 %d 字）" % MAX_LEN)
    return {"quotes": quotes, "skipped_long": long_n, "skipped_empty": empty_n}
