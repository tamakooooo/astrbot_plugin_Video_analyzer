import os
import re
import time
import uuid
from typing import Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger


class FeishuWikiPusher:
    """飞书知识库推送服务（创建文档并写入总结）"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        space_id: str,
        parent_node_token: str = "",
        title_prefix: str = "BiliBrief纪要",
        domain: str = "feishu",
        timeout_seconds: int = 20,
    ):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.space_id = (space_id or "").strip()
        self.parent_node_token = (parent_node_token or "").strip()
        self.title_prefix = (title_prefix or "BiliBrief纪要").strip()
        self.domain = (domain or "feishu").strip().lower()
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

        self._token: str = ""
        self._token_expire_at: float = 0.0

    def is_config_ready(self) -> bool:
        return bool(self.app_id and self.app_secret and self.space_id)

    async def push_note(self, note_text: str, video_url: str = "") -> Tuple[bool, str, Dict]:
        """
        推送总结到飞书知识库

        :return: (是否成功, 信息)
        """
        if not self.is_config_ready():
            return False, "飞书配置不完整（缺少 app_id/app_secret/space_id）", {
                "success": False,
                "error": "config_incomplete",
            }

        if not note_text or not note_text.strip():
            return False, "总结内容为空", {
                "success": False,
                "error": "empty_note",
            }

        token = await self._get_tenant_access_token()
        if not token:
            return False, "获取 tenant_access_token 失败", {
                "success": False,
                "error": "token_failed",
            }

        title = self._build_title(note_text, video_url)
        doc_id, node_token = await self._create_wiki_doc(token, title)
        if not doc_id:
            return False, "创建飞书知识库文档失败", {
                "success": False,
                "error": "create_doc_failed",
            }

        root_block_id = await self._get_document_root_block_id(token, doc_id)
        if not root_block_id:
            return False, "获取飞书文档根块失败", {
                "success": False,
                "error": "get_root_block_failed",
                "doc_id": doc_id,
                "node_token": node_token,
                "doc_url": self._build_doc_url(node_token),
            }

        blocks, image_tasks = self._build_blocks_from_markdown(note_text, video_url)
        if not blocks:
            return False, "生成飞书块内容失败", {
                "success": False,
                "error": "build_blocks_failed",
                "doc_id": doc_id,
                "node_token": node_token,
                "doc_url": self._build_doc_url(node_token),
            }

        ok, block_id_relations = await self._append_blocks(token, doc_id, root_block_id, blocks)
        if not ok:
            return False, "写入飞书文档失败", {
                "success": False,
                "error": "append_blocks_failed",
                "doc_id": doc_id,
                "node_token": node_token,
                "doc_url": self._build_doc_url(node_token),
            }

        image_ok_count, image_fail_count = await self._bind_images(
            token=token,
            doc_id=doc_id,
            block_id_relations=block_id_relations,
            image_tasks=image_tasks,
        )

        extra = ""
        if image_tasks:
            extra = f", images_ok={image_ok_count}, images_fail={image_fail_count}"
        doc_url = self._build_doc_url(node_token)
        return True, f"推送成功，doc_id={doc_id}, node_token={node_token}{extra}", {
            "success": True,
            "doc_id": doc_id,
            "node_token": node_token,
            "doc_url": doc_url,
            "images_ok": image_ok_count,
            "images_fail": image_fail_count,
            "image_tasks": len(image_tasks),
        }

    def _build_doc_url(self, node_token: Optional[str]) -> str:
        if not node_token:
            return ""
        if self.domain == "lark":
            return f"https://{self.domain}.com/wiki/{node_token}"
        return f"https://{self.domain}.cn/wiki/{node_token}"

    async def _get_tenant_access_token(self) -> Optional[str]:
        now = time.time()
        if self._token and now < self._token_expire_at:
            return self._token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        logger.warning(f"[FeishuWiki] 获取 token HTTP 异常: {resp.status}")
                        return None
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 获取 token 异常: {e}")
            return None

        if data.get("code") != 0:
            logger.warning(f"[FeishuWiki] 获取 token 失败: code={data.get('code')}, msg={data.get('msg')}")
            return None

        token = data.get("tenant_access_token", "")
        expire = int(data.get("expire", 7200))
        if not token:
            return None

        self._token = token
        self._token_expire_at = now + max(60, expire - 120)
        return token

    async def _create_wiki_doc(self, token: str, title: str) -> Tuple[Optional[str], Optional[str]]:
        url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{self.space_id}/nodes"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "title": title[:100],
            "obj_type": "docx",
            "node_type": "origin",
        }
        if self.parent_node_token:
            payload["parent_node_token"] = self.parent_node_token

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 创建节点异常: {e}")
            return None, None

        if data.get("code") != 0:
            logger.warning(
                f"[FeishuWiki] 创建节点失败: code={data.get('code')}, msg={data.get('msg')}, data={data}"
            )
            return None, None

        node = (data.get("data") or {}).get("node") or {}
        return node.get("obj_token"), node.get("node_token")

    async def _get_document_root_block_id(self, token: str, doc_id: str) -> Optional[str]:
        url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"page_size": 200, "document_revision_id": -1}

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 获取根块异常: {e}")
            return None

        if data.get("code") != 0:
            logger.warning(f"[FeishuWiki] 获取根块失败: code={data.get('code')}, msg={data.get('msg')}")
            return None

        items = (((data.get("data") or {}).get("items")) or [])
        if not items:
            return None

        for item in items:
            if item.get("block_type") == 1:
                return item.get("block_id")
        return items[0].get("block_id")

    async def _append_blocks(
        self, token: str, doc_id: str, parent_block_id: str, blocks: List[dict]
    ) -> Tuple[bool, Dict[str, str]]:
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/"
            f"{parent_block_id}/children?document_revision_id=-1"
        )
        relations: Dict[str, str] = {}

        chunk_size = 30
        for i in range(0, len(blocks), chunk_size):
            chunk = blocks[i:i + chunk_size]
            payload = {"children": chunk, "index": i}
            try:
                async with aiohttp.ClientSession(timeout=self._timeout) as session:
                    async with session.post(url, headers=headers, json=payload) as resp:
                        data = await resp.json()
            except Exception as e:
                logger.warning(f"[FeishuWiki] 追加块异常: {e}")
                return False, relations

            if data.get("code") != 0:
                logger.warning(f"[FeishuWiki] 追加块失败: code={data.get('code')}, msg={data.get('msg')}")
                return False, relations

            for item in (data.get("data") or {}).get("block_id_relations", []) or []:
                temporary_id = item.get("temporary_block_id")
                real_id = item.get("block_id")
                if temporary_id and real_id:
                    relations[temporary_id] = real_id

        return True, relations

    def _build_title(self, note_text: str, video_url: str) -> str:
        first_non_empty = ""
        for line in note_text.splitlines():
            t = line.strip()
            if t:
                first_non_empty = t
                break

        if first_non_empty.startswith("#"):
            first_non_empty = re.sub(r"^#+\s*", "", first_non_empty).strip()

        if not first_non_empty:
            first_non_empty = "B站视频总结"

        if video_url and "BV" in video_url:
            m = re.search(r"(BV[0-9A-Za-z]+)", video_url)
            if m:
                first_non_empty = f"{first_non_empty} [{m.group(1)}]"

        return f"{self.title_prefix} - {first_non_empty}"[:100]

    def _build_blocks_from_markdown(self, note_text: str, video_url: str) -> Tuple[List[dict], Dict[str, str]]:
        blocks: List[dict] = []
        image_tasks: Dict[str, str] = {}

        if video_url:
            blocks.append(self._text_block(f"原视频链接：{video_url}"))

        lines = note_text.splitlines()
        in_code_block = False
        code_lang = ""
        code_lines: List[str] = []
        in_formula_block = False
        formula_lines: List[str] = []

        for raw in lines:
            line = raw.rstrip("\n")
            stripped = line.strip()

            # 围栏代码块
            fence = re.match(r"^```([a-zA-Z0-9_+-]*)\s*$", stripped)
            if fence:
                if not in_code_block:
                    in_code_block = True
                    code_lang = (fence.group(1) or "").lower().strip()
                    code_lines = []
                else:
                    code_text = "\n".join(code_lines).strip()
                    if code_text:
                        blocks.append(self._code_block(code_text, code_lang))
                    in_code_block = False
                    code_lang = ""
                    code_lines = []
                continue

            if in_code_block:
                code_lines.append(line)
                continue

            # 块级公式 $$...$$
            if stripped == "$$":
                if not in_formula_block:
                    in_formula_block = True
                    formula_lines = []
                else:
                    expr = "\n".join(formula_lines).strip()
                    if expr:
                        blocks.append(self._equation_block(expr))
                    in_formula_block = False
                    formula_lines = []
                continue
            if in_formula_block:
                formula_lines.append(line)
                continue
            if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
                expr = stripped[2:-2].strip()
                if expr:
                    blocks.append(self._equation_block(expr))
                continue

            # 空行
            if not stripped:
                continue

            # 独立图片行 ![alt](url)
            image_match = re.match(r"^!\[([^\]]*)\]\((https?://[^\)]+)\)$", stripped)
            if image_match:
                alt = (image_match.group(1) or "").strip()
                img_url = image_match.group(2).strip()
                temp_id = f"img_{uuid.uuid4().hex[:12]}"
                blocks.append(self._image_block(temp_id))
                image_tasks[temp_id] = img_url
                if alt:
                    blocks.append(self._text_block(f"图：{alt}"))
                continue

            # 标题
            heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if heading:
                level = len(heading.group(1))
                text = heading.group(2).strip()
                if text:
                    blocks.append(self._heading_block(level, text))
                continue

            # 分割线
            if re.match(r"^(-{3,}|_{3,}|\*{3,})$", stripped):
                blocks.append(self._text_block("──────────"))
                continue

            # 引用
            quote = re.match(r"^>\s?(.*)$", stripped)
            if quote:
                quote_text = quote.group(1).strip()
                if quote_text:
                    blocks.append(self._text_block(f"▌{quote_text}"))
                continue

            # 无序列表
            bullet = re.match(r"^[-*]\s+(.*)$", stripped)
            if bullet:
                blocks.append(self._list_block_with_inline(bullet.group(1).strip(), ordered=False))
                continue

            # 有序列表
            ordered = re.match(r"^\d+\.\s+(.*)$", stripped)
            if ordered:
                blocks.append(self._list_block_with_inline(ordered.group(1).strip(), ordered=True))
                continue

            # 表格行（降级为文本）
            if stripped.startswith("|") and "|" in stripped[1:]:
                normalized = re.sub(r"\s*\|\s*", " | ", stripped.strip("| "))
                blocks.append(self._text_block(normalized))
                continue

            # 普通段落（支持行内富文本）
            for part in self._split_text(stripped, max_len=900):
                blocks.append(self._text_block_with_inline(part))

        # 处理未闭合代码块
        if in_code_block and code_lines:
            code_text = "\n".join(code_lines).strip()
            if code_text:
                blocks.append(self._code_block(code_text, code_lang))
        # 处理未闭合公式块
        if in_formula_block and formula_lines:
            expr = "\n".join(formula_lines).strip()
            if expr:
                blocks.append(self._equation_block(expr))

        return blocks, image_tasks

    @staticmethod
    def _split_text(text: str, max_len: int = 900) -> List[str]:
        if len(text) <= max_len:
            return [text]
        parts = []
        start = 0
        while start < len(text):
            parts.append(text[start:start + max_len])
            start += max_len
        return parts

    @staticmethod
    def _default_text_style() -> dict:
        return {
            "bold": False,
            "inline_code": False,
            "italic": False,
            "strikethrough": False,
            "underline": False,
        }

    def _text_block_with_inline(self, content: str) -> dict:
        elements = self._parse_inline_elements(content)
        return {
            "block_type": 2,
            "text": {
                "elements": elements,
                "style": {"align": 1, "folded": False},
            },
        }

    def _text_block(self, content: str) -> dict:
        return {
            "block_type": 2,
            "text": {
                "elements": [{
                    "text_run": {
                        "content": content,
                        "text_element_style": self._default_text_style(),
                    }
                }],
                "style": {"align": 1, "folded": False},
            },
        }

    def _list_block_with_inline(self, content: str, ordered: bool = False) -> dict:
        key = "ordered" if ordered else "bullet"
        return {
            "block_type": 13 if ordered else 12,
            key: {
                "elements": self._parse_inline_elements(content),
                "style": {"align": 1},
            },
        }

    def _list_block(self, content: str, ordered: bool = False) -> dict:
        key = "ordered" if ordered else "bullet"
        return {
            "block_type": 13 if ordered else 12,
            key: {
                "elements": [{
                    "text_run": {
                        "content": content,
                        "text_element_style": self._default_text_style(),
                    }
                }],
                "style": {"align": 1},
            },
        }

    def _code_block(self, code: str, lang: str = "") -> dict:
        return {
            "block_type": 14,
            "code": {
                "elements": [{
                    "text_run": {
                        "content": code,
                        "text_element_style": self._default_text_style(),
                    }
                }],
                "style": {
                    "language": self._map_code_language(lang),
                    "wrap": True,
                },
            },
        }

    def _equation_block(self, expr: str) -> dict:
        return {
            "block_type": 2,
            "text": {
                "elements": [{
                    "equation": {
                        "content": expr,
                        "text_element_style": self._default_text_style(),
                    }
                }],
                "style": {"align": 1, "folded": False},
            },
        }

    @staticmethod
    def _image_block(temp_block_id: str, width: int = 640, height: int = 360) -> dict:
        return {
            "block_id": temp_block_id,
            "block_type": 27,
            "image": {
                "width": width,
                "height": height,
                "token": "",
            },
        }

    @staticmethod
    def _map_code_language(lang: str) -> int:
        # 常见语言映射，未知回退纯文本(1)
        mapping = {
            "plain": 1,
            "text": 1,
            "python": 49,
            "py": 49,
            "javascript": 34,
            "js": 34,
            "typescript": 73,
            "ts": 73,
            "java": 33,
            "go": 26,
            "bash": 3,
            "sh": 3,
            "shell": 3,
            "json": 35,
            "yaml": 74,
            "yml": 74,
            "sql": 62,
            "markdown": 40,
            "md": 40,
            "xml": 75,
            "html": 29,
            "css": 11,
            "cpp": 12,
            "c++": 12,
            "c": 10,
            "rust": 59,
        }
        return mapping.get((lang or "").strip().lower(), 1)

    def _parse_inline_elements(self, text: str) -> List[dict]:
        """
        解析行内 Markdown 样式（bold/italic/inline_code/strikethrough/link降级）
        """
        # 链接降级: [text](url) -> text (url)
        normalized = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1 (\2)", text)
        tokens = self._tokenize_inline(normalized)
        elements: List[dict] = []
        for token in tokens:
            style = self._default_text_style()
            style.update(token.get("style", {}))
            if "equation" in token:
                elements.append({
                    "equation": {
                        "content": token["equation"],
                        "text_element_style": style,
                    }
                })
            else:
                elements.append({
                    "text_run": {
                        "content": token.get("text", ""),
                        "text_element_style": style,
                    }
                })
        return elements or [{
            "text_run": {
                "content": text,
                "text_element_style": self._default_text_style(),
            }
        }]

    def _tokenize_inline(self, text: str) -> List[Dict]:
        pattern = re.compile(
            r"(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|~~[^~]+~~|\$[^$]+\$)"
        )
        pos = 0
        out: List[Dict] = []

        for m in pattern.finditer(text):
            if m.start() > pos:
                out.append({"text": text[pos:m.start()], "style": {}})
            token = m.group(0)
            parsed = self._parse_inline_token(token)
            out.append(parsed)
            pos = m.end()

        if pos < len(text):
            out.append({"text": text[pos:], "style": {}})

        merged: List[Dict] = []
        for item in out:
            if "equation" in item:
                merged.append(item)
                continue
            if not item.get("text"):
                continue
            if (
                merged
                and "text" in merged[-1]
                and merged[-1].get("style") == item.get("style")
            ):
                merged[-1]["text"] += item["text"]
            else:
                merged.append(item)
        return merged

    @staticmethod
    def _parse_inline_token(token: str) -> Dict:
        if token.startswith("`") and token.endswith("`"):
            return {"text": token[1:-1], "style": {"inline_code": True}}
        if token.startswith("$") and token.endswith("$"):
            return {"equation": token[1:-1], "style": {}}
        if (token.startswith("**") and token.endswith("**")) or (token.startswith("__") and token.endswith("__")):
            return {"text": token[2:-2], "style": {"bold": True}}
        if (token.startswith("*") and token.endswith("*")) or (token.startswith("_") and token.endswith("_")):
            return {"text": token[1:-1], "style": {"italic": True}}
        if token.startswith("~~") and token.endswith("~~"):
            return {"text": token[2:-2], "style": {"strikethrough": True}}
        return {"text": token, "style": {}}

    async def _bind_images(
        self,
        token: str,
        doc_id: str,
        block_id_relations: Dict[str, str],
        image_tasks: Dict[str, str],
    ) -> Tuple[int, int]:
        if not image_tasks:
            return 0, 0

        ok_count = 0
        fail_count = 0
        for temp_id, image_url in image_tasks.items():
            block_id = block_id_relations.get(temp_id)
            if not block_id:
                logger.warning(f"[FeishuWiki] 图片块映射缺失: {temp_id}")
                fail_count += 1
                continue
            try:
                file_token = await self._upload_image_media_from_url(token, image_url, block_id)
                if not file_token:
                    fail_count += 1
                    continue
                replaced = await self._replace_image_block(token, doc_id, block_id, file_token)
                if replaced:
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                logger.warning(f"[FeishuWiki] 绑定图片失败: {e}")
                fail_count += 1

        return ok_count, fail_count

    async def _upload_image_media_from_url(
        self, token: str, image_url: str, parent_block_id: str
    ) -> Optional[str]:
        # 1) 下载图片
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[FeishuWiki] 下载图片失败 HTTP {resp.status}: {image_url}")
                        return None
                    image_bytes = await resp.read()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 下载图片异常: {e}, url={image_url}")
            return None

        if not image_bytes:
            return None

        # 2) 上传图片素材
        upload_url = "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"
        headers = {"Authorization": f"Bearer {token}"}

        parsed_name = os.path.basename(image_url.split("?", 1)[0]) or "image.png"
        if "." not in parsed_name:
            parsed_name += ".png"

        form = aiohttp.FormData()
        form.add_field("file", image_bytes, filename=parsed_name, content_type=self._guess_mime(parsed_name))
        form.add_field("file_name", parsed_name)
        form.add_field("parent_type", "docx_image")
        form.add_field("parent_node", parent_block_id)
        form.add_field("size", str(len(image_bytes)))

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(upload_url, headers=headers, data=form) as resp:
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 上传图片异常: {e}")
            return None

        if data.get("code") != 0:
            logger.warning(f"[FeishuWiki] 上传图片失败: code={data.get('code')}, msg={data.get('msg')}")
            return None

        payload = data.get("data") or {}
        return payload.get("file_token") or data.get("file_token")

    async def _replace_image_block(
        self, token: str, doc_id: str, block_id: str, file_token: str
    ) -> bool:
        url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{block_id}?document_revision_id=-1"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"replace_image": {"token": file_token}}

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.patch(url, headers=headers, json=payload) as resp:
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 替换图片块异常: {e}")
            return False

        if data.get("code") != 0:
            logger.warning(f"[FeishuWiki] 替换图片块失败: code={data.get('code')}, msg={data.get('msg')}")
            return False
        return True

    @staticmethod
    def _guess_mime(file_name: str) -> str:
        lower = file_name.lower()
        if lower.endswith(".jpg") or lower.endswith(".jpeg"):
            return "image/jpeg"
        if lower.endswith(".gif"):
            return "image/gif"
        if lower.endswith(".webp"):
            return "image/webp"
        return "image/png"

    def _heading_block(self, level: int, content: str) -> dict:
        safe_level = max(1, min(6, level))
        block_type = 2 + safe_level
        key = f"heading{safe_level}"
        return {
            "block_type": block_type,
            key: {
                "elements": [{
                    "text_run": {
                        "content": content,
                        "text_element_style": self._default_text_style(),
                    }
                }],
                "style": {"align": 1, "folded": False},
            },
        }
