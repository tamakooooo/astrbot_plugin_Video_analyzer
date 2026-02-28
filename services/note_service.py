import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..downloaders.bilibili_downloader import BilibiliDownloader
from ..gpt.mindmap_prompt import MINDMAP_PROMPT_TEMPLATE
from ..gpt.prompt_builder import build_prompt
from ..transcriber.bcut import BcutTranscriber
from ..utils.note_helper import replace_content_markers
from ..utils.url_parser import extract_video_id
from .screenshot_extractor import ScreenshotExtractor


@dataclass
class NoteGenerationResult:
    note_text: str
    artifacts: dict[str, Any]


class NoteService:
    """
    总结生成服务

    流程: 下载音频 → 获取字幕/转写 → LLM 总结 → 后处理 → 返回 Markdown
    """

    def __init__(self, data_dir: str, cookies: dict | None = None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.downloader = BilibiliDownloader(
            data_dir=os.path.join(data_dir, "audio"),
            cookies=cookies,
        )
        self.transcriber = BcutTranscriber()
        self.screenshot_extractor = ScreenshotExtractor()

    async def generate_note(
        self,
        video_url: str,
        llm_ask_func,
        style: str = "detailed",
        enable_link: bool = True,
        enable_summary: bool = True,
        quality: str = "fast",
        max_length: int = 3000,
    ) -> str | None:
        """
        为单个视频生成总结

        :param video_url: B站视频链接
        :param llm_ask_func: 调用 AstrBot LLM 的异步函数, 签名: async (prompt: str) -> str
        :param style: 总结风格
        :param enable_link: 是否插入原片跳转
        :param enable_summary: 是否加 AI 总结
        :param quality: 音频下载质量
        :param max_length: 总结最大字符数
        :return: Markdown 总结文本
        """
        try:
            # 1. 下载音频
            logger.info(f"开始下载音频: {video_url}")
            audio_meta = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.downloader.download(video_url, quality=quality)
            )
            logger.info(f"音频下载完成: {audio_meta.title}")

            # 2. 获取字幕（优先平台字幕）
            logger.info("尝试获取平台字幕...")
            transcript = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.downloader.download_subtitles(video_url)
            )

            # 3. 如果没有字幕，使用 bcut 转写
            if not transcript or not transcript.segments:
                logger.info("无平台字幕，使用必剪转写...")
                transcript = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.transcriber.transcript(audio_meta.file_path)
                )

            if not transcript or not transcript.segments:
                return "❌ 无法获取视频内容（字幕和转写均失败）"

            logger.info(f"获取到 {len(transcript.segments)} 段转写内容")

            # 4. 构建 Prompt 并调用 LLM
            tags = ""
            raw_info = audio_meta.raw_info or {}
            if isinstance(raw_info.get("tags"), list):
                tags = ", ".join(raw_info["tags"])
            elif isinstance(raw_info.get("tags"), str):
                tags = raw_info["tags"]

            prompt = build_prompt(
                title=audio_meta.title,
                segments=transcript.segments,
                tags=tags,
                style=style,
                enable_link=enable_link,
                enable_summary=enable_summary,
            )

            logger.info("调用 LLM 生成总结...")
            markdown = await llm_ask_func(prompt)

            if not markdown:
                return "❌ LLM 生成总结失败"

            # 5. 后处理：替换链接标记
            if enable_link:
                video_id = extract_video_id(video_url, "bilibili")
                if video_id:
                    markdown = replace_content_markers(
                        markdown, video_id=video_id, platform="bilibili"
                    )

            # 6. 截断过长内容
            if len(markdown) > max_length:
                markdown = markdown[:max_length] + "\n\n...(内容过长，已截断)"

            # 标题已由 LLM 在 h1 中输出，无需额外添加

            # 8. 清理音频文件
            self._cleanup(audio_meta.file_path)

            return markdown

        except Exception as e:
            logger.error(f"总结生成失败: {e}", exc_info=True)
            return f"❌ 总结生成失败: {str(e)}"
        finally:
            # 清理音频文件（无论成功还是失败）
            try:
                if audio_meta and hasattr(audio_meta, "file_path"):
                    self._cleanup(audio_meta.file_path)
            except Exception:
                pass

    async def generate_note_with_artifacts(
        self,
        video_url: str,
        llm_ask_func,
        style: str = "detailed",
        enable_link: bool = True,
        enable_summary: bool = True,
        quality: str = "fast",
        max_length: int = 3000,
    ) -> NoteGenerationResult:
        note_text = await self.generate_note(
            video_url=video_url,
            llm_ask_func=llm_ask_func,
            style=style,
            enable_link=enable_link,
            enable_summary=enable_summary,
            quality=quality,
            max_length=max_length,
        )
        if not note_text:
            return NoteGenerationResult(note_text="❌ 总结生成结果为空", artifacts={})

        artifacts: dict[str, Any] = {}
        mindmap_mermaid = await self._generate_mindmap_mermaid(
            note_text=note_text, llm_ask_func=llm_ask_func
        )
        if mindmap_mermaid:
            artifacts["mindmap_mermaid"] = mindmap_mermaid

        screenshot_paths = await self._generate_screenshots(video_url=video_url)
        if screenshot_paths:
            artifacts["screenshot_paths"] = screenshot_paths

        return NoteGenerationResult(note_text=note_text, artifacts=artifacts)

    async def _generate_mindmap_mermaid(self, note_text: str, llm_ask_func) -> str:
        try:
            prompt = MINDMAP_PROMPT_TEMPLATE.format(note_text=note_text[:8000])
            result = await llm_ask_func(prompt)
            mermaid = str(result or "").strip()
            if mermaid.startswith("```"):
                mermaid = mermaid.strip("`").strip()
                if mermaid.lower().startswith("mermaid"):
                    mermaid = mermaid[7:].strip()
            if "mindmap" in mermaid.lower():
                return mermaid
        except Exception as e:
            logger.warning(f"[Mindmap] 生成失败: {e}")
        return ""

    async def _generate_screenshots(self, video_url: str) -> list[str]:
        video_meta = None
        screenshot_paths: list[str] = []
        try:
            logger.info(f"开始下载视频用于截图: {video_url}")
            video_meta = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.downloader.download_video(video_url),
            )
            duration = float(video_meta.duration or 0)
            if duration <= 0:
                return []

            timestamps = self._build_screenshot_timestamps(duration)
            if not timestamps:
                return []

            screenshots_dir = Path(self.data_dir) / "screenshots"
            screenshot_paths = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.screenshot_extractor.extract(
                    video_path=video_meta.file_path,
                    timestamps=timestamps,
                    output_dir=str(screenshots_dir),
                ),
            )
            return screenshot_paths
        except Exception as e:
            logger.warning(f"[Screenshot] 生成失败: {e}")
            return []
        finally:
            if video_meta and getattr(video_meta, "file_path", ""):
                self._cleanup(video_meta.file_path)

    @staticmethod
    def _build_screenshot_timestamps(duration_seconds: float) -> list[float]:
        if duration_seconds <= 60:
            return [max(1.0, duration_seconds / 2.0)]

        sample_count = 4
        start = max(1.0, duration_seconds * 0.1)
        end = max(start + 1.0, duration_seconds * 0.9)
        step = (end - start) / max(1, sample_count - 1)
        return [round(start + i * step, 1) for i in range(sample_count)]

    def _cleanup(self, file_path: str):
        """清理临时音频文件"""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"已清理临时文件: {file_path}")
        except Exception as e:
            logger.warning(f"清理文件失败: {e}")
