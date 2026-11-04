"""联网搜图插件。

给 AI 一个 search_image 工具：按关键词搜索网络图片，并把图片直接发送到会话。

支持两种图源：

* ``360``（默认）：360 图片的公开 JSON 接口，不需要任何 Key，国内可直连。
* ``custom_json``：任意返回 JSON 的图源。填接口地址（支持 {query}/{count}/{page}
  占位符）和字段映射即可，例如 Bing / Pixabay / 自建图床。

几点实现决定：

* 图片先在插件里下载到临时目录，再以本地文件发送。原因是直接给平台图片 URL 时，
  AstrBot 自己的下载器可能拿不到图（实测出现过 DownloadFileHTTPError），而插件
  下载能完整控制 UA / Referer / 超时 / 校验。
* 查询词会自动降级：图源对"多个词堆在一起"的长查询经常返回 0 条，这时退化成
  更短的查询重试。
* 结果做一次轻量相关性过滤：标题里包含查询词的优先，全都不包含时才退而求其次。
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import os
import re
import urllib.parse
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config import AstrBotConfig
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BUILTIN_360_ENDPOINT = "https://image.so.com/j"
BUILTIN_360_REFERER = "https://image.so.com/"

BING_ENDPOINT = "https://cn.bing.com/images/async"
BING_REFERER = "https://cn.bing.com/"

# 硬上限，即使配置被改成很大也不会超过这个数
HARD_LIMIT = 10

# 下载图片的最小字节数，太小的通常是占位图或防盗链响应
MIN_IMAGE_BYTES = 4096

# 内置敏感词表（仅在开启安全过滤时生效，按标题做子串匹配）
DEFAULT_SAFE_WORDS = [
    "裸", "色情", "成人", "情色", "性爱", "强奸", "性侵", "走光",
    "porn", "nude", "nsfw", "sex", "xxx", "18+", "hentai",
    "血腥", "尸体", "虐杀", "自杀", "割腕",
]

# 判断下载到的字节流是不是图片
_IMAGE_MAGIC = (
    (b"\xff\xd8", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF8", "gif"),
)


def _http_get(url: str, timeout: float, headers: dict | None = None) -> bytes:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_json(url: str, timeout: float, headers: dict | None = None) -> dict | list:
    raw = _http_get(url, timeout, headers=headers)
    return json.loads(raw.decode("utf-8", "replace"))


def _dig(obj, path: str):
    """按点号路径取值，例如 data.items[0].url 里的 data.items。"""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _as_str(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _query_variants(query: str) -> list[str]:
    """生成长度递减的查询变体，用于长查询搜不到时降级重试。"""
    cleaned = query.strip().strip("「」\"'“”")
    cleaned = re.sub(r"[（）()【】\[\]]", " ", cleaned)
    tokens = [t for t in re.split(r"[\s,，、]+", cleaned) if t]

    variants: list[str] = []
    if cleaned:
        variants.append(cleaned)
    if len(tokens) > 2:
        variants.append(" ".join(tokens[:2]))
    if tokens:
        variants.append(tokens[0])

    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _parse_bing_items(html_text: str) -> list[dict]:
    """从 Bing 图片的 async 片段里抽出图片条目。

    Bing 返回的是 HTML 片段，图链和标题藏在转义过的 JSON 里，形如
    murl&quot;:&quot;https://...&quot;。这里按字段分别抽取，再按下标配对。
    """

    def _grab(key: str) -> list[str]:
        for pat in (key + r"&quot;:&quot;(.*?)&quot;", key + r'":"(.*?)"'):
            found = re.findall(pat, html_text or "")
            if found:
                return found
        return []

    urls = _grab("murl")
    titles = _grab("t")
    pages = _grab("purl")

    items: list[dict] = []
    for idx, url in enumerate(urls):
        url = html_lib.unescape(url).strip()
        if not url.startswith(("http://", "https://")):
            continue
        title = html_lib.unescape(titles[idx]).strip() if idx < len(titles) else ""
        page = html_lib.unescape(pages[idx]).strip() if idx < len(pages) else ""
        items.append({"title": title, "thumb": url, "img": url, "link": page})
    return items


def _parse_headers(raw: str) -> dict:
    """把配置里的 JSON 头解析成 dict，解析失败就忽略。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:
        logger.warning("自定义请求头解析失败，已忽略: %s", exc)
    return {}


@register(
    "astrbot_plugin_image_search",
    "MaizioO",
    "按关键词联网搜索图片并发送到会话",
    "1.3.0",
)
class ImageSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _raw(self) -> dict:
        raw: dict = {}
        if self.config is None or not hasattr(self.config, "get"):
            return raw
        for key in (
            "source",
            "default_count",
            "max_count",
            "safe_filter",
            "safe_words",
            "prefer_original",
            "request_timeout",
            "custom_url",
            "custom_referer",
            "custom_results_path",
            "custom_image_fields",
            "custom_title_field",
            "custom_link_field",
            "custom_headers",
        ):
            try:
                value = self.config.get(key)
            except Exception:
                value = None
            if value is not None:
                raw[key] = value
        return raw

    def _cfg(self) -> dict:
        raw = self._raw()

        def _int(value, fallback: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        max_count = max(1, min(_int(raw.get("max_count", 5), 5), HARD_LIMIT))
        default_count = max(1, min(_int(raw.get("default_count", 2), 2), max_count))
        custom_words = [
            w.strip().lower()
            for w in str(raw.get("safe_words") or "").split(",")
            if w.strip()
        ]

        source = str(raw.get("source") or "bing").strip().lower()
        if source not in ("360", "bing", "custom_json"):
            source = "bing"

        image_fields = [
            f.strip()
            for f in str(raw.get("custom_image_fields") or "thumb,thumb_bak,img").split(",")
            if f.strip()
        ]

        return {
            "source": source,
            "default_count": default_count,
            "max_count": max_count,
            "safe_filter": bool(raw.get("safe_filter", False)),
            "safe_words": DEFAULT_SAFE_WORDS + custom_words,
            "prefer_original": bool(raw.get("prefer_original", False)),
            "request_timeout": max(3, min(_int(raw.get("request_timeout", 12), 12), 60)),
            "custom_url": str(raw.get("custom_url") or "").strip(),
            "custom_referer": str(raw.get("custom_referer") or "").strip(),
            "custom_results_path": str(raw.get("custom_results_path") or "").strip(),
            "custom_image_fields": image_fields,
            "custom_title_field": str(raw.get("custom_title_field") or "").strip(),
            "custom_link_field": str(raw.get("custom_link_field") or "").strip(),
            "custom_headers": _parse_headers(str(raw.get("custom_headers") or "")),
        }

    @staticmethod
    def _is_blocked(title: str, words: list[str]) -> bool:
        if not title:
            return False
        lowered = title.lower()
        return any(w in lowered for w in words)

    # ------------------------------------------------------------------
    # 各图源的条目提取
    # ------------------------------------------------------------------
    @staticmethod
    def _url_and_headers(cfg: dict, variant: str, count: int) -> tuple[str, dict]:
        """构造请求地址和请求头。"""
        if cfg["source"] == "custom_json":
            template = cfg["custom_url"]
            if not template:
                raise ValueError("自定义图源未填写接口地址（custom_url）")
            url = template
            if "{query}" in url or "{count}" in url or "{page}" in url:
                url = (
                    url.replace("{query}", urllib.parse.quote(variant))
                    .replace("{count}", str(max(count * 4, 12)))
                    .replace("{page}", "0")
                )
            elif "?" in url:
                url = f"{url}&q={urllib.parse.quote(variant)}"
            else:
                url = f"{url}?q={urllib.parse.quote(variant)}"

            headers = {"Accept": "application/json, text/plain, */*"}
            headers.update(cfg["custom_headers"])
            if cfg["custom_referer"]:
                headers.setdefault("Referer", cfg["custom_referer"])
            return url, headers

        if cfg["source"] == "bing":
            params = {
                "q": variant,
                "first": 1,
                "count": max(count * 4, 12),
                "mkt": "zh-CN",
            }
            url = BING_ENDPOINT + "?" + urllib.parse.urlencode(
                params, quote_via=urllib.parse.quote
            )
            return url, {
                "Referer": BING_REFERER,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }

        # 内置 360
        params = {"q": variant, "pn": 0, "rn": max(count * 4, 12), "src": "srp"}
        url = BUILTIN_360_ENDPOINT + "?" + urllib.parse.urlencode(params)
        return url, {"Referer": BUILTIN_360_REFERER, "Accept": "application/json, text/plain, */*"}

    @staticmethod
    def _extract_items(data, cfg: dict) -> list:
        if cfg["source"] == "360":
            if isinstance(data, dict):
                return data.get("list") or []
            return []

        container = _dig(data, cfg["custom_results_path"]) if cfg["custom_results_path"] else data
        if isinstance(container, dict):
            # 常见退化：结果被包在 data 里但没配路径
            for guess in ("list", "items", "results", "data", "images"):
                if isinstance(container.get(guess), list):
                    return container[guess]
            return []
        if isinstance(container, list):
            return container
        return []

    def _pick_image_url_custom(self, item, cfg: dict) -> str:
        fields = cfg["custom_image_fields"] or ["thumb", "img"]
        if cfg["prefer_original"] and len(fields) > 1:
            fields = list(reversed(fields))
        for field in fields:
            value = _dig(item, field)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, str) and first.startswith(("http://", "https://")):
                    return first
        return ""

    @staticmethod
    def _pick_image_url_360(item: dict, prefer_original: bool) -> str:
        if prefer_original:
            candidates = (item.get("img"), item.get("thumb"), item.get("thumb_bak"))
        else:
            candidates = (item.get("thumb"), item.get("thumb_bak"), item.get("img"))
        return next(
            (
                u
                for u in candidates
                if isinstance(u, str) and u.startswith(("http://", "https://"))
            ),
            "",
        )

    def _collect(self, items: list, count: int, cfg: dict, query: str) -> tuple[list[dict], int]:
        """从一批结果里挑出可用条目，返回 (结果, 被安全过滤跳过的条数)。"""
        tokens = [t.lower() for t in re.split(r"[\s,，、]+", query) if len(t) >= 2]

        picked: list[dict] = []
        others: list[dict] = []
        seen: set[str] = set()
        skipped_unsafe = 0

        for item in items:
            if not isinstance(item, dict):
                continue

            if cfg["source"] in ("360", "bing"):
                title = (item.get("title") or "").strip()
                image_url = self._pick_image_url_360(item, cfg["prefer_original"])
                link = (item.get("link") or "").strip()
            else:
                title = _as_str(_dig(item, cfg["custom_title_field"])) if cfg["custom_title_field"] else ""
                title = title.strip()
                image_url = self._pick_image_url_custom(item, cfg)
                link = (
                    _as_str(_dig(item, cfg["custom_link_field"])).strip()
                    if cfg["custom_link_field"]
                    else ""
                )

            if cfg["safe_filter"] and self._is_blocked(title, cfg["safe_words"]):
                skipped_unsafe += 1
                continue
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)

            entry = {"title": title, "image_url": image_url, "source_url": link}
            lowered = title.lower()
            if tokens and any(tok in lowered for tok in tokens):
                picked.append(entry)
            else:
                others.append(entry)

        return (picked + others)[:count], skipped_unsafe

    async def _search(self, query: str, count: int, cfg: dict) -> tuple[list[dict], str, int]:
        """搜索图片。返回 (结果, 实际生效查询词, 被安全过滤跳过条数)。"""
        total_skipped = 0
        last_error = ""

        for variant in _query_variants(query):
            try:
                url, headers = self._url_and_headers(cfg, variant, count)
            except ValueError as exc:
                logger.error("搜图配置错误: %s", exc)
                raise

            try:
                if cfg["source"] == "bing":
                    raw_text = await asyncio.to_thread(
                        lambda: _http_get(url, cfg["request_timeout"], headers).decode(
                            "utf-8", "replace"
                        )
                    )
                    items = _parse_bing_items(raw_text)
                else:
                    data = await asyncio.to_thread(
                        _fetch_json, url, cfg["request_timeout"], headers
                    )
                    items = self._extract_items(data, cfg)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("搜图请求失败 query=%r url=%s: %s", variant, url, exc)
                continue
            results, skipped = self._collect(items, count, cfg, variant)
            total_skipped += skipped
            if results:
                if variant != query:
                    logger.info("搜图查询降级: %r → %r", query, variant)
                return results, variant, total_skipped

        if last_error:
            logger.warning("搜图所有查询变体均失败，最后错误: %s", last_error)
        return [], query, total_skipped

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    async def _download(self, results: list[dict], timeout: float, cfg: dict) -> list[dict]:
        """把图片下载到临时目录，返回带 local_path 的条目（失败的直接丢掉）。"""
        temp_dir = os.path.join(get_astrbot_temp_path(), "image_search")
        os.makedirs(temp_dir, exist_ok=True)

        referer = cfg["custom_referer"] or (
            BUILTIN_360_REFERER if cfg["source"] == "360" else ""
        )
        dl_headers = {"Accept": "image/*"}
        if referer:
            dl_headers["Referer"] = referer

        downloaded: list[dict] = []
        for entry in results:
            url = entry["image_url"]
            try:
                raw = await asyncio.to_thread(
                    _http_get, url, timeout, dl_headers
                )
            except Exception as exc:
                logger.warning("图片下载失败 %s: %s", url, exc)
                continue

            if len(raw) < MIN_IMAGE_BYTES:
                logger.warning("图片太小，跳过 %s (%d 字节)", url, len(raw))
                continue

            suffix = ""
            if raw[:2] == b"\xff\xd8":
                suffix = ".jpg"
            elif raw[:8] == b"\x89PNG\r\n\x1a\n":
                suffix = ".png"
            elif raw[:4] == b"GIF8":
                suffix = ".gif"
            elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                suffix = ".webp"
            if not suffix:
                logger.warning("不是图片格式，跳过 %s (%s)", url, raw[:8])
                continue

            name = hashlib.md5(url.encode("utf-8")).hexdigest()[:12] + suffix
            path = os.path.join(temp_dir, name)
            try:
                with open(path, "wb") as fp:
                    fp.write(raw)
            except OSError as exc:
                logger.warning("写入临时文件失败 %s: %s", path, exc)
                continue

            downloaded.append({**entry, "local_path": path})
            if len(downloaded) >= len(results):
                break
        return downloaded

    # ------------------------------------------------------------------
    # LLM 工具
    # ------------------------------------------------------------------
    @filter.llm_tool(name="search_image")
    async def search_image(
        self,
        event: AstrMessageEvent,
        query: str,
        count: int = 0,
    ) -> str:
        """联网搜索图片并直接发送给用户。

        当用户想"看看某物的图片""来几张 xx 图""搜一张 xx"之类的请求时使用。
        图片发送成功后不需要你再用文字描述图片内容。

        Args:
            query(string): 搜索关键词。必须用**中文**的简短名词，2-6 个字为宜，例如"布偶猫""红烧肉""埃菲尔铁塔"。不要翻译成英文，也不要把多个修饰词堆在一起（图源对长查询和英文查询效果很差）。
            count(number): 发送几张图片，1 到上限，默认 0 表示使用插件配置的默认张数。
        """
        query = (query or "").strip()
        if not query:
            return "搜索关键词为空，请提供要搜索的内容。"

        cfg = self._cfg()
        try:
            requested = int(count or 0)
        except (TypeError, ValueError):
            requested = 0
        if requested <= 0:
            requested = cfg["default_count"]
        requested = max(1, min(requested, cfg["max_count"]))

        try:
            results, used_query, skipped_unsafe = await self._search(query, requested, cfg)
        except ValueError as exc:
            return f"搜图配置有问题：{exc}"
        except Exception as exc:
            logger.error("搜图失败 query=%r: %s", query, exc)
            return (
                f"搜索「{query}」时出错：{type(exc).__name__}，"
                "可能是网络问题，换个关键词或稍后再试。"
            )

        if not results:
            hint = "（部分结果可能被安全过滤拦下了）" if skipped_unsafe else ""
            return (
                f"没有搜到「{query}」的可用图片{hint}。"
                "请改用更短的中文关键词再试，例如把「爱莉希雅 崩坏3 粉色妖精小姐」改成「爱莉希雅」。"
            )

        downloaded = await self._download(results, cfg["request_timeout"], cfg)
        if not downloaded:
            return (
                f"搜到了「{used_query}」的图片，但图片下载失败（图源可能限制了访问），"
                "换个关键词或稍后再试。"
            )

        chain = [Image.fromFileSystem(item["local_path"]) for item in downloaded]
        try:
            await event.send(MessageChain(chain))
        except Exception as exc:
            logger.error("发送搜图结果失败 query=%r: %s", query, exc)
            return (
                f"已下载 {len(downloaded)} 张「{used_query}」的图片，"
                f"但发送失败：{type(exc).__name__}。"
            )

        titles = "、".join(t for t in (r["title"] for r in downloaded) if t)
        suffix = f"（{titles}）" if titles else ""
        note = f"（关键词已简化为「{used_query}」）" if used_query != query else ""
        return f"已发送 {len(downloaded)} 张「{used_query}」的图片{note}{suffix}。"
