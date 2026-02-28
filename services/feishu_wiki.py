import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

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

    async def push_note(
        self,
        note_text: str,
        video_url: str = "",
        screenshot_paths: list[str] | None = None,
        mindmap_mermaid: str = "",
    ) -> tuple[bool, str, dict]:
        """
        推送总结到飞书知识库

        :return: (是否成功, 信息)
        """
        if not self.is_config_ready():
            return (
                False,
                "飞书配置不完整（缺少 app_id/app_secret/space_id）",
                {
                    "success": False,
                    "error": "config_incomplete",
                },
            )

        if not note_text or not note_text.strip():
            return (
                False,
                "总结内容为空",
                {
                    "success": False,
                    "error": "empty_note",
                },
            )

        token = await self._get_tenant_access_token()
        if not token:
            return (
                False,
                "获取 tenant_access_token 失败",
                {
                    "success": False,
                    "error": "token_failed",
                },
            )

        title = self._build_title(note_text, video_url)
        doc_id, node_token = await self._create_wiki_doc(token, title)
        if not doc_id:
            return (
                False,
                "创建飞书知识库文档失败",
                {
                    "success": False,
                    "error": "create_doc_failed",
                },
            )

        root_block_id = await self._get_document_root_block_id(token, doc_id)
        if not root_block_id:
            return (
                False,
                "获取飞书文档根块失败",
                {
                    "success": False,
                    "error": "get_root_block_failed",
                    "doc_id": doc_id,
                    "node_token": node_token,
                    "doc_url": self._build_doc_url(node_token),
                },
            )

        blocks, image_tasks = self._build_blocks_from_markdown(
            note_text=note_text,
            video_url=video_url,
            screenshot_paths=screenshot_paths or [],
        )
        if not blocks:
            return (
                False,
                "生成飞书块内容失败",
                {
                    "success": False,
                    "error": "build_blocks_failed",
                    "doc_id": doc_id,
                    "node_token": node_token,
                    "doc_url": self._build_doc_url(node_token),
                },
            )

        ok, block_id_relations, appended_image_block_ids = await self._append_blocks(
            token, doc_id, root_block_id, blocks
        )
        if not ok:
            return (
                False,
                "写入飞书文档失败",
                {
                    "success": False,
                    "error": "append_blocks_failed",
                    "doc_id": doc_id,
                    "node_token": node_token,
                    "doc_url": self._build_doc_url(node_token),
                },
            )

        image_ok_count, image_fail_count = await self._bind_images(
            token=token,
            doc_id=doc_id,
            block_id_relations=block_id_relations,
            image_tasks=image_tasks,
            appended_image_block_ids=appended_image_block_ids,
        )

        mindmap_result = await self._try_insert_mindmap_whiteboard(
            token=token,
            doc_id=doc_id,
            mindmap_mermaid=mindmap_mermaid,
        )

        extra = ""
        if image_tasks:
            extra = f", images_ok={image_ok_count}, images_fail={image_fail_count}"
        if mindmap_result.get("attempted"):
            extra += (
                f", mindmap={'ok' if mindmap_result.get('success') else 'failed'}"
                if not mindmap_result.get("skipped")
                else ", mindmap=skipped"
            )
        doc_url = self._build_doc_url(node_token)
        return (
            True,
            f"推送成功，doc_id={doc_id}, node_token={node_token}{extra}",
            {
                "success": True,
                "doc_id": doc_id,
                "node_token": node_token,
                "doc_url": doc_url,
                "images_ok": image_ok_count,
                "images_fail": image_fail_count,
                "image_tasks": len(image_tasks),
                "mindmap": mindmap_result,
            },
        )

    def _build_doc_url(self, node_token: str | None) -> str:
        if not node_token:
            return ""
        if self.domain == "lark":
            return f"https://{self.domain}.com/wiki/{node_token}"
        return f"https://{self.domain}.cn/wiki/{node_token}"

    async def _get_tenant_access_token(self) -> str | None:
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
                        logger.warning(
                            f"[FeishuWiki] 获取 token HTTP 异常: {resp.status}"
                        )
                        return None
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 获取 token 异常: {e}")
            return None

        if data.get("code") != 0:
            logger.warning(
                f"[FeishuWiki] 获取 token 失败: code={data.get('code')}, msg={data.get('msg')}"
            )
            return None

        token = data.get("tenant_access_token", "")
        expire = int(data.get("expire", 7200))
        if not token:
            return None

        self._token = token
        self._token_expire_at = now + max(60, expire - 120)
        return token

    async def _create_wiki_doc(
        self, token: str, title: str
    ) -> tuple[str | None, str | None]:
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

    async def _get_document_root_block_id(self, token: str, doc_id: str) -> str | None:
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
            logger.warning(
                f"[FeishuWiki] 获取根块失败: code={data.get('code')}, msg={data.get('msg')}"
            )
            return None

        items = ((data.get("data") or {}).get("items")) or []
        if not items:
            return None

        for item in items:
            if item.get("block_type") == 1:
                return item.get("block_id")
        return items[0].get("block_id")

    async def _append_blocks(
        self, token: str, doc_id: str, parent_block_id: str, blocks: list[dict]
    ) -> tuple[bool, dict[str, str], list[str]]:
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/"
            f"{parent_block_id}/children?document_revision_id=-1"
        )
        relations: dict[str, str] = {}
        appended_image_block_ids: list[str] = []

        chunk_size = 30
        for i in range(0, len(blocks), chunk_size):
            chunk = blocks[i : i + chunk_size]
            payload = {"children": chunk, "index": i}
            try:
                async with aiohttp.ClientSession(timeout=self._timeout) as session:
                    async with session.post(url, headers=headers, json=payload) as resp:
                        data = await resp.json()
            except Exception as e:
                logger.warning(f"[FeishuWiki] 追加块异常: {e}")
                return False, relations, appended_image_block_ids

            if data.get("code") != 0:
                logger.warning(
                    f"[FeishuWiki] 追加块失败: code={data.get('code')}, msg={data.get('msg')}"
                )
                return False, relations, appended_image_block_ids

            for item in (data.get("data") or {}).get("block_id_relations", []) or []:
                temporary_id = item.get("temporary_block_id")
                real_id = item.get("block_id")
                if temporary_id and real_id:
                    relations[temporary_id] = real_id

            for child in (data.get("data") or {}).get("children", []) or []:
                if int(child.get("block_type") or 0) == 27 and child.get("block_id"):
                    appended_image_block_ids.append(str(child["block_id"]))

        return True, relations, appended_image_block_ids

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

    def _build_blocks_from_markdown(
        self,
        note_text: str,
        video_url: str,
        screenshot_paths: list[str] | None = None,
    ) -> tuple[list[dict], dict[str, dict[str, str]]]:
        blocks: list[dict] = []
        image_tasks: dict[str, dict[str, str]] = {}
        screenshot_insert_map = self._build_screenshot_insert_map(
            note_text=note_text, screenshot_paths=screenshot_paths or []
        )
        screenshot_global_idx = 0

        if video_url:
            blocks.append(self._text_block(f"原视频链接：{video_url}"))

        lines = note_text.splitlines()
        in_code_block = False
        code_lang = ""
        code_lines: list[str] = []
        in_formula_block = False
        formula_lines: list[str] = []

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
            if (
                stripped.startswith("$$")
                and stripped.endswith("$$")
                and len(stripped) > 4
            ):
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
                image_tasks[temp_id] = {"type": "url", "value": img_url}
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
                    bound_paths = screenshot_insert_map.get(screenshot_global_idx, [])
                    if bound_paths:
                        for local_path in bound_paths:
                            p = Path(local_path)
                            if not p.exists() or not p.is_file():
                                continue
                            temp_id = f"simg_{uuid.uuid4().hex[:12]}"
                            blocks.append(self._image_block(temp_id))
                            image_tasks[temp_id] = {"type": "local", "value": str(p)}
                    screenshot_global_idx += 1
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
                blocks.append(
                    self._list_block_with_inline(bullet.group(1).strip(), ordered=False)
                )
                continue

            # 有序列表
            ordered = re.match(r"^\d+\.\s+(.*)$", stripped)
            if ordered:
                blocks.append(
                    self._list_block_with_inline(ordered.group(1).strip(), ordered=True)
                )
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

        # 没有可插入标题时兜底追加到文末
        consumed_paths = {
            item for group in screenshot_insert_map.values() for item in group
        }
        remaining_paths = [p for p in (screenshot_paths or []) if p not in consumed_paths]
        for screenshot_path in remaining_paths:
            p = Path(str(screenshot_path))
            if not p.exists() or not p.is_file():
                continue
            temp_id = f"simg_{uuid.uuid4().hex[:12]}"
            blocks.append(self._image_block(temp_id))
            image_tasks[temp_id] = {"type": "local", "value": str(p)}

        return blocks, image_tasks

    @staticmethod
    def _build_screenshot_insert_map(
        note_text: str, screenshot_paths: list[str]
    ) -> dict[int, list[str]]:
        if not screenshot_paths:
            return {}
        lines = note_text.splitlines()
        heading_indexes: list[int] = []
        heading_seen = 0
        for raw in lines:
            stripped = raw.strip()
            heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if not heading:
                continue
            level = len(heading.group(1))
            if level >= 2:
                heading_indexes.append(heading_seen)
            heading_seen += 1

        if not heading_indexes:
            return {}

        mapping: dict[int, list[str]] = {idx: [] for idx in heading_indexes}
        if len(heading_indexes) == 1:
            mapping[heading_indexes[0]] = list(screenshot_paths)
            return mapping

        n = len(screenshot_paths)
        m = len(heading_indexes)
        for i, path in enumerate(screenshot_paths):
            pos = round(i * (m - 1) / max(1, n - 1))
            heading_idx = heading_indexes[pos]
            mapping.setdefault(heading_idx, []).append(path)
        return mapping

    @staticmethod
    def _split_text(text: str, max_len: int = 900) -> list[str]:
        if len(text) <= max_len:
            return [text]
        parts = []
        start = 0
        while start < len(text):
            parts.append(text[start : start + max_len])
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
                "elements": [
                    {
                        "text_run": {
                            "content": content,
                            "text_element_style": self._default_text_style(),
                        }
                    }
                ],
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
                "elements": [
                    {
                        "text_run": {
                            "content": content,
                            "text_element_style": self._default_text_style(),
                        }
                    }
                ],
                "style": {"align": 1},
            },
        }

    def _code_block(self, code: str, lang: str = "") -> dict:
        return {
            "block_type": 14,
            "code": {
                "elements": [
                    {
                        "text_run": {
                            "content": code,
                            "text_element_style": self._default_text_style(),
                        }
                    }
                ],
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
                "elements": [
                    {
                        "equation": {
                            "content": expr,
                            "text_element_style": self._default_text_style(),
                        }
                    }
                ],
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

    def _parse_inline_elements(self, text: str) -> list[dict]:
        """
        解析行内 Markdown 样式（bold/italic/inline_code/strikethrough/link降级）
        """
        # 链接降级: [text](url) -> text (url)
        normalized = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1 (\2)", text)
        tokens = self._tokenize_inline(normalized)
        elements: list[dict] = []
        for token in tokens:
            style = self._default_text_style()
            style.update(token.get("style", {}))
            if "equation" in token:
                elements.append(
                    {
                        "equation": {
                            "content": token["equation"],
                            "text_element_style": style,
                        }
                    }
                )
            else:
                elements.append(
                    {
                        "text_run": {
                            "content": token.get("text", ""),
                            "text_element_style": style,
                        }
                    }
                )
        return elements or [
            {
                "text_run": {
                    "content": text,
                    "text_element_style": self._default_text_style(),
                }
            }
        ]

    def _tokenize_inline(self, text: str) -> list[dict]:
        pattern = re.compile(
            r"(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|~~[^~]+~~|\$[^$]+\$)"
        )
        pos = 0
        out: list[dict] = []

        for m in pattern.finditer(text):
            if m.start() > pos:
                out.append({"text": text[pos : m.start()], "style": {}})
            token = m.group(0)
            parsed = self._parse_inline_token(token)
            out.append(parsed)
            pos = m.end()

        if pos < len(text):
            out.append({"text": text[pos:], "style": {}})

        merged: list[dict] = []
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
    def _parse_inline_token(token: str) -> dict:
        if token.startswith("`") and token.endswith("`"):
            return {"text": token[1:-1], "style": {"inline_code": True}}
        if token.startswith("$") and token.endswith("$"):
            return {"equation": token[1:-1], "style": {}}
        if (token.startswith("**") and token.endswith("**")) or (
            token.startswith("__") and token.endswith("__")
        ):
            return {"text": token[2:-2], "style": {"bold": True}}
        if (token.startswith("*") and token.endswith("*")) or (
            token.startswith("_") and token.endswith("_")
        ):
            return {"text": token[1:-1], "style": {"italic": True}}
        if token.startswith("~~") and token.endswith("~~"):
            return {"text": token[2:-2], "style": {"strikethrough": True}}
        return {"text": token, "style": {}}

    async def _bind_images(
        self,
        token: str,
        doc_id: str,
        block_id_relations: dict[str, str],
        image_tasks: dict[str, dict[str, str]],
        appended_image_block_ids: list[str] | None = None,
    ) -> tuple[int, int]:
        if not image_tasks:
            return 0, 0

        ok_count = 0
        fail_count = 0
        appended_image_block_ids = appended_image_block_ids or []
        used_block_ids: set[str] = set()
        unresolved: list[tuple[str, dict[str, str]]] = []

        for temp_id, task in image_tasks.items():
            block_id = block_id_relations.get(temp_id)
            if not block_id:
                unresolved.append((temp_id, task))
                continue
            used_block_ids.add(block_id)
            try:
                task_type = str((task or {}).get("type") or "url")
                task_value = str((task or {}).get("value") or "").strip()
                if not task_value:
                    fail_count += 1
                    continue

                if task_type == "local":
                    file_token = await self._upload_image_media_from_local(
                        token, task_value, block_id
                    )
                else:
                    file_token = await self._upload_image_media_from_url(
                        token, task_value, block_id
                    )
                if not file_token:
                    fail_count += 1
                    continue
                replaced = await self._replace_image_block(
                    token, doc_id, block_id, file_token
                )
                if replaced:
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                logger.warning(f"[FeishuWiki] 绑定图片失败: {e}")
                fail_count += 1

        fallback_block_ids = [bid for bid in appended_image_block_ids if bid not in used_block_ids]
        for (temp_id, task), block_id in zip(unresolved, fallback_block_ids):
            try:
                task_type = str((task or {}).get("type") or "url")
                task_value = str((task or {}).get("value") or "").strip()
                if not task_value:
                    fail_count += 1
                    continue
                if task_type == "local":
                    file_token = await self._upload_image_media_from_local(
                        token, task_value, block_id
                    )
                else:
                    file_token = await self._upload_image_media_from_url(
                        token, task_value, block_id
                    )
                if not file_token:
                    fail_count += 1
                    continue
                replaced = await self._replace_image_block(
                    token, doc_id, block_id, file_token
                )
                if replaced:
                    ok_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                logger.warning(f"[FeishuWiki] 回退绑定图片失败: {temp_id}, err={e}")
                fail_count += 1

        if len(unresolved) > len(fallback_block_ids):
            for temp_id, _ in unresolved[len(fallback_block_ids) :]:
                logger.warning(f"[FeishuWiki] 图片块映射缺失: {temp_id}")
                fail_count += 1

        return ok_count, fail_count

    async def _upload_image_media_from_local(
        self, token: str, image_path: str, parent_block_id: str
    ) -> str | None:
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            logger.warning(f"[FeishuWiki] 本地图片不存在: {image_path}")
            return None
        try:
            image_bytes = path.read_bytes()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 读取本地图片失败: {e}, path={image_path}")
            return None

        if not image_bytes:
            return None

        upload_url = "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"
        headers = {"Authorization": f"Bearer {token}"}
        file_name = path.name

        form = aiohttp.FormData()
        form.add_field(
            "file",
            image_bytes,
            filename=file_name,
            content_type=self._guess_mime(file_name),
        )
        form.add_field("file_name", file_name)
        form.add_field("parent_type", "docx_image")
        form.add_field("parent_node", parent_block_id)
        form.add_field("size", str(len(image_bytes)))

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(upload_url, headers=headers, data=form) as resp:
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[FeishuWiki] 上传本地图片异常: {e}")
            return None

        if data.get("code") != 0:
            logger.warning(
                f"[FeishuWiki] 上传本地图片失败: code={data.get('code')}, msg={data.get('msg')}"
            )
            return None

        payload = data.get("data") or {}
        return payload.get("file_token") or data.get("file_token")

    async def _try_insert_mindmap_whiteboard(
        self,
        token: str,
        doc_id: str,
        mindmap_mermaid: str,
    ) -> dict[str, Any]:
        mermaid = (mindmap_mermaid or "").strip()
        if not mermaid:
            return {
                "attempted": True,
                "success": False,
                "skipped": True,
                "reason": "mindmap_empty",
            }

        align = self._normalize_int(
            self._safe_config_get("feishu_mindmap_align"), default=2, min_v=1, max_v=3
        )
        style_type = self._normalize_int(
            self._safe_config_get("feishu_mindmap_style_type"),
            default=1,
            min_v=1,
            max_v=2,
        )

        try:
            whiteboard = await self._create_whiteboard_block(
                token=token, doc_id=doc_id, align=align
            )
            board_token = str(
                ((whiteboard or {}).get("board") or {}).get("token") or ""
            ).strip()
            block_id = str((whiteboard or {}).get("block_id") or "").strip()
            if not board_token:
                return {
                    "attempted": True,
                    "success": False,
                    "skipped": False,
                    "reason": "whiteboard_token_missing",
                    "block_id": block_id,
                }
            diagram = await self._create_mermaid_node(
                token=token,
                whiteboard_token=board_token,
                mermaid_code=mermaid,
                style_type=style_type,
            )
            return {
                "attempted": True,
                "success": True,
                "skipped": False,
                "block_id": block_id,
                "whiteboard_id": board_token,
                "diagram": diagram,
            }
        except Exception as e:
            logger.warning(f"[FeishuWiki] 插入思维导图白板失败: {e}")
            return {
                "attempted": True,
                "success": False,
                "skipped": False,
                "reason": "whiteboard_exception",
                "error": str(e),
            }

    async def _create_whiteboard_block(
        self, token: str, doc_id: str, align: int = 2
    ) -> dict[str, Any]:
        root_block_id = await self._get_document_root_block_id(token, doc_id)
        if not root_block_id:
            raise RuntimeError("root_block_not_found")
        url = (
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/"
            f"{root_block_id}/children?document_revision_id=-1"
        )
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "children": [
                {
                    "block_type": 43,
                    "board": {"align": align},
                }
            ]
        }

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()
        if data.get("code") != 0:
            raise RuntimeError(
                f"create_whiteboard_failed:{data.get('code')}:{data.get('msg')}"
            )
        children = (data.get("data") or {}).get("children") or []
        for child in children:
            if int(child.get("block_type") or 0) == 43:
                return child
        raise RuntimeError("whiteboard_block_not_found")

    async def _create_mermaid_node(
        self,
        token: str,
        whiteboard_token: str,
        mermaid_code: str,
        style_type: int = 1,
    ) -> dict[str, Any]:
        url = f"https://open.feishu.cn/open-apis/board/v1/whiteboards/{whiteboard_token}/nodes/plantuml"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "plant_uml_code": mermaid_code,
            "style_type": 1 if style_type not in (1, 2) else style_type,
            "syntax_type": 2,
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()
        if data.get("code") != 0:
            raise RuntimeError(
                f"create_mermaid_failed:{data.get('code')}:{data.get('msg')}"
            )
        return data.get("data") or {}

    def _safe_config_get(self, key: str):
        return os.getenv(key.upper(), "")

    @staticmethod
    def _normalize_int(value: Any, default: int, min_v: int, max_v: int) -> int:
        try:
            parsed = int(str(value).strip())
        except Exception:
            parsed = default
        return max(min_v, min(max_v, parsed))

    async def _upload_image_media_from_url(
        self, token: str, image_url: str, parent_block_id: str
    ) -> str | None:
        # 1) 下载图片
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"[FeishuWiki] 下载图片失败 HTTP {resp.status}: {image_url}"
                        )
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
        form.add_field(
            "file",
            image_bytes,
            filename=parsed_name,
            content_type=self._guess_mime(parsed_name),
        )
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
            logger.warning(
                f"[FeishuWiki] 上传图片失败: code={data.get('code')}, msg={data.get('msg')}"
            )
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
            logger.warning(
                f"[FeishuWiki] 替换图片块失败: code={data.get('code')}, msg={data.get('msg')}"
            )
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
                "elements": [
                    {
                        "text_run": {
                            "content": content,
                            "text_element_style": self._default_text_style(),
                        }
                    }
                ],
                "style": {"align": 1, "folded": False},
            },
        }
