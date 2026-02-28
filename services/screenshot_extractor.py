import subprocess
from pathlib import Path

from astrbot.api import logger


class ScreenshotExtractor:
    """按时间戳从视频提取截图"""

    def extract(
        self, video_path: str, timestamps: list[float], output_dir: str
    ) -> list[str]:
        video_file = Path(video_path)
        if not video_file.exists() or not timestamps:
            return []

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        result_paths: list[str] = []
        for timestamp in timestamps:
            path = self._extract_single(
                video_path=video_path,
                timestamp=float(timestamp),
                output_dir=output_path,
            )
            if path:
                result_paths.append(path)
        return result_paths

    def _extract_single(
        self, video_path: str, timestamp: float, output_dir: Path
    ) -> str:
        file_name = f"screenshot_{timestamp:.1f}s.jpg"
        output_file = output_dir / file_name
        cmd = [
            "ffmpeg",
            "-ss",
            str(max(0.0, timestamp)),
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(output_file),
        ]
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                check=True,
                text=True,
                timeout=40,
            )
            if output_file.exists():
                return str(output_file)
        except Exception as e:
            logger.warning(f"[Screenshot] 提取失败: ts={timestamp}, err={e}")
        return ""
