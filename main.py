"""
BiliBrief 视频纪要插件

订阅 B站 UP主，定时/手动生成 AI 视频纪要并推送到聊天
"""

import asyncio
import os
import uuid

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import Plain, Image
from astrbot.api import logger

from .services.subscription import SubscriptionManager
from .services.bilibili_api import get_up_info, get_latest_videos, search_up_by_name
from .services.bilibili_login import BilibiliLogin
from .services.note_service import NoteService
from .services.feishu_wiki import FeishuWikiPusher
from .utils.url_parser import detect_platform, extract_bilibili_mid
from .utils.md_to_image import render_note_image


class BiliBriefPlugin(Star):
    """BiliBrief 视频纪要插件"""

    def __init__(self, context: Context):
        super().__init__(context)

        # 数据目录（使用框架规范 API）
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_bilibrief"))
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "images"), exist_ok=True)

        # 读取配置
        self.config = self.context.get_config() or {}

        # Debug 模式 —— 在其他所有初始化之前设置
        self._debug_mode = bool(self.config.get("debug_mode", False))
        if self._debug_mode:
            logger.info("═══════════ [BiliBrief] Debug 模式已启用 ═══════════")

        self._log("══════ [BiliBrief] 插件初始化开始 ══════")
        self._log(f"配置内容: { {k: v for k, v in self.config.items() if k not in ('cookies',)} }")

        # B站扫码登录服务
        self.bili_login = BilibiliLogin(self.data_dir)
        self.bili_cookies = self.bili_login.get_cookies()
        self._log(f"Cookie 状态: {'已加载, keys=' + str(list(self.bili_cookies.keys())) if self.bili_cookies else '无'}")

        # 解析群聊访问控制
        self.access_mode = self.config.get("access_mode", "blacklist")
        self.group_list = self._parse_list(
            str(self.config.get("group_list", ""))
        )
        self._log(f"访问控制: mode={self.access_mode}, group_list={self.group_list}")

        # 初始化服务
        self.subscription_mgr = SubscriptionManager(self.data_dir)
        self.note_service = NoteService(
            data_dir=self.data_dir,
            cookies=self.bili_cookies if self.bili_cookies else None,
        )
        self.feishu_wiki_pusher = FeishuWikiPusher(
            app_id=str(self.config.get("feishu_app_id", "")),
            app_secret=str(self.config.get("feishu_app_secret", "")),
            space_id=str(self.config.get("feishu_wiki_space_id", "")),
            parent_node_token=str(self.config.get("feishu_parent_node_token", "")),
            title_prefix=str(self.config.get("feishu_title_prefix", "BiliBrief纪要")),
            domain=str(self.config.get("feishu_domain", "feishu")),
        )
        self._last_feishu_publish_result = {}

        # 从配置加载推送目标（与命令添加的合并，不重复）
        self._load_push_targets_from_config()

        # 定时任务
        self._check_task = None
        self._running = False

        # 启动定时检查
        if self.config.get("enable_auto_push", True):
            self._running = True
            self._check_task = asyncio.create_task(self._scheduled_check_loop())
            self._log("定时检查任务已启动")
        else:
            self._log("定时推送已禁用")

        self._log("══════ [BiliBrief] 插件初始化完成 ══════")

        if self.bili_login.is_logged_in():
            logger.info("BiliBrief 插件已加载（B站已登录）")
        else:
            logger.info("BiliBrief 插件已加载（B站未登录，请发送 /B站登录 扫码）")

    # ==================== 工具方法 ====================

    def _log(self, msg: str):
        """Debug 日志输出 —— 使用 logger.info 确保始终可见"""
        if self._debug_mode:
            logger.info(f"[BiliBrief/DBG] {msg}")

    def _load_push_targets_from_config(self):
        """从配置文件加载推送目标到 SubscriptionManager"""
        prefix = self.config.get("platform_prefix", "aiocqhttp")
        push_groups = str(self.config.get("push_groups", "")).strip()
        push_users = str(self.config.get("push_users", "")).strip()

        if push_groups:
            for gid in push_groups.split(","):
                gid = gid.strip()
                if gid and gid.isdigit():
                    origin = f"{prefix}:GroupMessage:{gid}"
                    self.subscription_mgr.add_push_target(origin, f"群{gid}")

        if push_users:
            for uid in push_users.split(","):
                uid = uid.strip()
                if uid and uid.isdigit():
                    origin = f"{prefix}:FriendMessage:{uid}"
                    self.subscription_mgr.add_push_target(origin, f"QQ{uid}")

    @staticmethod
    def _parse_list(text: str) -> set:
        """解析逗号分隔的列表为 set"""
        if not text or not text.strip():
            return set()
        return {item.strip() for item in text.split(',') if item.strip()}

    def _check_access(self, event: AstrMessageEvent) -> bool:
        """检查群是否有权使用插件（仅群维度，不看个人）"""
        try:
            origin = getattr(event, 'unified_msg_origin', '') or ''
            self._log(f"[AccessCheck] mode={self.access_mode}, origin={origin}, group_list={self.group_list}")

            if self.access_mode == 'all':
                self._log("[AccessCheck] 模式=all, 放行")
                return True

            if not self.group_list:
                self._log("[AccessCheck] group_list 为空, 放行")
                return True

            if self.access_mode == 'whitelist':
                for gid in self.group_list:
                    if f':{gid}' in origin or origin.endswith(gid):
                        self._log(f"[AccessCheck] 白名单命中: {gid}")
                        return True
                self._log("[AccessCheck] 白名单未命中, 拒绝")
                return False

            elif self.access_mode == 'blacklist':
                for gid in self.group_list:
                    if f':{gid}' in origin or origin.endswith(gid):
                        self._log(f"[AccessCheck] 黑名单命中: {gid}, 拒绝")
                        return False
                self._log("[AccessCheck] 黑名单未命中, 放行")
                return True

        except Exception as e:
            logger.warning(f"访问控制检查异常: {e}")

        return True

    @staticmethod
    def _parse_args(message_str) -> str:
        """从完整消息中提取命令后的参数"""
        if not message_str:
            return ""
        parts = str(message_str).strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _extract_clean_bilibili_url(text: str) -> str:
        """
        从输入文本中提取并清洗 B站 URL。
        兼容 Markdown 链接: [title](https://www.bilibili.com/video/xxx)
        """
        if not text:
            return ""
        import re

        raw = str(text).strip().strip("<>").strip()

        # Markdown 链接
        md_link = re.search(r"\[[^\]]+]\((https?://[^\s)]+)\)", raw)
        if md_link:
            return md_link.group(1).strip()

        # 直接链接（B站长链 / b23短链）
        direct = re.search(
            r"https?://(?:www\.)?(?:bilibili\.com/video/[^\s)>]+|b23\.tv/[^\s)>]+)",
            raw
        )
        if direct:
            return direct.group(0).strip()

        # 纯 BV 号
        bv = re.search(r"(BV[0-9A-Za-z]{10})", raw)
        if bv:
            return f"https://www.bilibili.com/video/{bv.group(1)}"

        return raw

    def _render_and_get_chain(self, note_text: str):
        """
        将总结渲染为图片并返回消息链组件，或回退到纯文本。

        :return: list[Image] (图片模式) 或 str (文本模式)
        """
        if not self.config.get("output_image", True):
            self._log("[Render] output_image=False, 使用纯文本")
            return note_text or "❌ 总结为空"

        # 生成唯一文件名
        import time
        img_filename = f"note_{int(time.time() * 1000)}.png"
        img_path = os.path.join(self.data_dir, "images", img_filename)

        self._log(f"[Render] 开始渲染图片: {img_path}")
        result = render_note_image(note_text, img_path)

        if result and os.path.exists(result):
            self._log(f"[Render] 图片渲染成功: {os.path.getsize(result)} bytes")
            return [Image.fromFileSystem(result)]
        else:
            self._log("[Render] 图片渲染失败, 回退到纯文本")
            return note_text or "❌ 总结为空"

    async def _try_push_note_to_feishu(self, note_text: str, video_url: str, source: str):
        """
        尝试推送总结到飞书知识库（软失败，不影响主流程）

        :param note_text: 总结内容
        :param video_url: 视频链接
        :param source: 触发来源 manual/auto
        """
        if not self.config.get("enable_feishu_wiki_push", True):
            self._log("[FeishuPush] 配置关闭，跳过")
            return {"attempted": False, "reason": "disabled"}

        if source == "manual" and not self.config.get("feishu_push_on_manual", True):
            self._log("[FeishuPush] manual 触发已关闭，跳过")
            return {"attempted": False, "reason": "manual_disabled"}
        if source == "auto" and not self.config.get("feishu_push_on_auto", True):
            self._log("[FeishuPush] auto 触发已关闭，跳过")
            return {"attempted": False, "reason": "auto_disabled"}

        if not note_text or str(note_text).strip().startswith("❌"):
            self._log("[FeishuPush] 总结为空或失败结果，跳过")
            return {"attempted": False, "reason": "invalid_note"}

        if not self.feishu_wiki_pusher.is_config_ready():
            self._log("[FeishuPush] 配置未就绪（app_id/app_secret/space_id），跳过")
            return {"attempted": False, "reason": "config_not_ready"}

        ok, message, detail = await self.feishu_wiki_pusher.push_note(note_text=note_text, video_url=video_url)
        result = {
            "attempted": True,
            "success": ok,
            "message": message,
            "detail": detail or {},
        }
        self._last_feishu_publish_result = result
        if ok:
            logger.info(f"[FeishuPush] {message}")
        else:
            logger.warning(f"[FeishuPush] 推送失败: {message}")
        return result

    # ==================== 命令处理 ====================

    @filter.command("总结帮助", alias={"BiliBrief help", "总结help", "总结帮助"})
    async def show_help(self, event: AstrMessageEvent):
        """显示插件帮助信息"""
        login_status = "✅ 已登录" if self.bili_login.is_logged_in() else "❌ 未登录"
        help_text = (
            "📝 BiliBrief 视频纪要助手 v1.0.1\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🔐 B站登录状态: {login_status}\n"
            "\n"
            "📌 登录命令:\n"
            "  /B站登录 → 扫码登录B站\n"
            "  /B站登出 → 退出B站登录\n"
            "\n"
            "📌 基本命令:\n"
            "  /总结 <B站视频链接或BV号>\n"
            "    → 为指定视频生成AI总结\n"
            "  /最新视频 <UP主UID、空间链接或昵称>\n"
            "    → 获取UP主最新视频并生成总结\n"
            "\n"
            "📌 订阅管理:\n"
            "  /订阅 <UP主UID、空间链接或昵称>\n"
            "    → 订阅UP主，有新视频自动推送总结\n"
            "  /取消订阅 <UP主UID、空间链接或昵称>\n"
            "    → 取消订阅\n"
            "  /订阅列表\n"
            "    → 查看当前订阅的UP主\n"
            "  /检查更新\n"
            "    → 手动检查订阅UP主的新视频\n"
            "\n"
            "📌 推送目标:\n"
            "  /添加推送群 <群号>\n"
            "    → 将QQ群加入推送列表\n"
            "  /添加推送号 <QQ号>\n"
            "    → 将QQ号加入推送列表\n"
            "  /推送列表\n"
            "    → 查看当前推送目标\n"
            "  /移除推送 <群号或QQ号>\n"
            "    → 移除推送目标\n"
            "\n"
            "💡 示例:\n"
            "  /总结 https://www.bilibili.com/video/BV1xx...\n"
            "  /总结 BV1xx411c7mD\n"
            "  /订阅 123456789\n"
            "  /添加推送群 123456789\n"
            "\n"
            "ℹ️ 总结默认以图片形式发送，可在配置中切换\n"
        )
        yield event.plain_result(help_text)

    @filter.command("B站登录", alias={"bili_login", "哔哩登录", "B站扫码登录", "扫码登录"})
    async def bili_login_cmd(self, event: AstrMessageEvent):
        """B站扫码登录"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return

        if self.bili_login.is_logged_in():
            yield event.plain_result("✅ B站已登录！如需重新登录请先 /B站登出")
            return

        yield event.plain_result("🔄 正在生成B站登录二维码...")

        # 申请二维码
        qr_data = await self.bili_login.generate_qrcode()
        if not qr_data:
            yield event.plain_result("❌ 生成二维码失败，请稍后重试")
            return

        qr_url = qr_data.get("url", "")
        qrcode_key = qr_data.get("qrcode_key", "")

        if not qr_url or not qrcode_key:
            yield event.plain_result("❌ 获取二维码数据失败")
            return

        # 本地生成二维码图片
        try:
            try:
                import segno
            except ImportError:
                yield event.plain_result("❌ 缺少 segno 依赖，请运行: pip install segno")
                return

            qr_filename = f"login_qr_{uuid.uuid4().hex[:8]}.png"
            qr_path = os.path.join(self.data_dir, qr_filename)
            qr = segno.make(qr_url)
            qr.save(qr_path, scale=10, border=4)
        except Exception as e:
            logger.error(f"生成二维码图片失败: {e}")
            yield event.plain_result(f"❌ 生成二维码图片失败: {e}")
            return

        # 发送二维码图片
        chain = [
            Plain("📱 请使用B站App扫描下方二维码登录\n⏳ 二维码有效期3分钟\n"),
            Image.fromFileSystem(qr_path),
        ]
        yield event.chain_result(chain)

        # 轮询登录结果
        result = await self.bili_login.do_login_flow(qrcode_key, timeout=180)

        if result["status"] == "success":
            # 更新 cookies
            self.bili_cookies = self.bili_login.get_cookies()
            # 重新初始化 NoteService
            self.note_service = NoteService(
                data_dir=self.data_dir,
                cookies=self.bili_cookies,
            )
            yield event.plain_result("✅ B站登录成功！现在可以使用所有功能了。")
        elif result["status"] == "expired":
            yield event.plain_result("⏰ 二维码已过期，请重新发送 /B站登录")
        elif result["status"] == "timeout":
            yield event.plain_result("⏰ 登录超时，请重新发送 /B站登录")
        else:
            yield event.plain_result("❌ 登录失败，请重新发送 /B站登录")

        # 清理二维码图片
        try:
            os.remove(qr_path)
        except Exception:
            pass

    @filter.command("B站登出", alias={"bili_logout", "哔哩登出"})
    async def bili_logout_cmd(self, event: AstrMessageEvent):
        """退出B站登录"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return

        if not self.bili_login.is_logged_in():
            yield event.plain_result("ℹ️ 当前未登录B站")
            return

        self.bili_login.logout()
        self.bili_cookies = {}
        yield event.plain_result("✅ 已退出B站登录")

    @filter.command("总结", alias={"BiliBrief", "视频总结", "总结"})
    async def generate_note_cmd(self, event: AstrMessageEvent):
        """手动为视频生成总结"""
        self._log("═══════ [总结命令] 开始处理 ═══════")

        if not self._check_access(event):
            self._log("[总结命令] 访问控制不通过, 结束")
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return

        # 从消息中提取 URL
        import re
        raw_msg = event.message_str or ""
        self._log(f"[总结命令] event.message_str = '{raw_msg}'")
        self._log(f"[总结命令] event.message_str type = {type(raw_msg)}")
        self._log(f"[总结命令] event.message_str repr = {repr(raw_msg)}")

        # 也尝试从 message_obj 中获取完整消息
        full_text = raw_msg
        try:
            if hasattr(event, 'message_obj') and event.message_obj:
                chain = event.message_obj.message
                self._log(f"[总结命令] message_obj.message 链长度 = {len(chain) if chain else 0}")
                for i, comp in enumerate(chain or []):
                    self._log(f"[总结命令] 消息组件[{i}]: type={type(comp).__name__}, str={str(comp)[:200]}")
                # 拼接所有 Plain 文本
                plain_texts = []
                for comp in (chain or []):
                    if hasattr(comp, 'text'):
                        plain_texts.append(comp.text)
                    elif isinstance(comp, str):
                        plain_texts.append(comp)
                if plain_texts:
                    full_text = " ".join(plain_texts)
                    self._log(f"[总结命令] 从 message_obj 拼接文本: '{full_text}'")
        except Exception as e:
            self._log(f"[总结命令] 解析 message_obj 异常: {e}")

        logger.info(f"总结命令收到消息: {raw_msg}")

        video_url = ""

        # 方式1: 从命令参数中取
        args = self._parse_args(raw_msg)
        self._log(f"[总结命令] 方式1 _parse_args 结果: '{args}'")
        if args:
            candidate = self._extract_clean_bilibili_url(args)
            self._log(f"[总结命令] 方式1 清洗后参数: '{candidate}'")
            if detect_platform(candidate) == "bilibili":
                video_url = candidate
                self._log(f"[总结命令] 方式1 命中URL: '{video_url}'")

        # 方式2: 用正则从 raw_msg 中找 bilibili URL
        if not video_url:
            url_match = re.search(
                r'https?://(?:www\.)?bilibili\.com/video/[A-Za-z0-9/?=&_.]+',
                raw_msg
            )
            if url_match:
                video_url = url_match.group(0)
                self._log(f"[总结命令] 方式2 从raw_msg正则匹配: '{video_url}'")
            else:
                self._log("[总结命令] 方式2 raw_msg中未匹配到bilibili URL")

        # 方式3: 从 full_text (message_obj) 中找
        if not video_url and full_text != raw_msg:
            url_match = re.search(
                r'https?://(?:www\.)?bilibili\.com/video/[A-Za-z0-9/?=&_.]+',
                full_text
            )
            if url_match:
                video_url = url_match.group(0)
                self._log(f"[总结命令] 方式3 从full_text正则匹配: '{video_url}'")
            else:
                self._log("[总结命令] 方式3 full_text中未匹配到bilibili URL")

        # 方式4: 找 b23.tv 短链
        if not video_url:
            for text_src in [raw_msg, full_text]:
                short_match = re.search(r'https?://b23\.tv/\S+', text_src)
                if short_match:
                    video_url = short_match.group(0)
                    self._log(f"[总结命令] 方式4 短链匹配: '{video_url}'")
                    break
            if not video_url:
                self._log("[总结命令] 方式4 未匹配到 b23.tv 短链")

        # 方式5: 尝试从整条消息中找 BV 号
        if not video_url:
            bv_match = re.search(r'(BV[0-9A-Za-z]{10})', raw_msg + " " + full_text)
            if bv_match:
                video_url = f"https://www.bilibili.com/video/{bv_match.group(1)}"
                self._log(f"[总结命令] 方式5 从BV号构建URL: '{video_url}'")
            else:
                self._log("[总结命令] 方式5 未找到BV号")

        if not video_url:
            self._log("[总结命令] 所有方式均未提取到URL, 返回错误")
            self._log("═══════ [总结命令] 结束(无URL) ═══════")
            yield event.plain_result(
                "❌ 请提供视频链接\n用法: /总结 <B站视频链接>\n"
                "示例: /总结 https://www.bilibili.com/video/BV1xx..."
            )
            return

        video_url = self._extract_clean_bilibili_url(video_url).rstrip('>')
        platform = detect_platform(video_url)
        self._log(f"[总结命令] 最终URL='{video_url}', platform='{platform}'")
        if platform != "bilibili":
            self._log("═══════ [总结命令] 结束(非B站) ═══════")
            yield event.plain_result("❌ 目前仅支持B站视频链接")
            return

        yield event.plain_result("⏳ 正在生成总结，请稍候（可能需要1-3分钟）...")

        self._log(f"[总结命令] 调用 _generate_note: {video_url}")
        note = await self._generate_note(video_url)
        if not isinstance(note, str) or not note.strip():
            note = "❌ 总结生成结果为空"
        self._log(f"[总结命令] 总结生成完成, 长度={len(note) if note else 0}")
        feishu_result = await self._try_push_note_to_feishu(note, video_url, source="manual")

        # 发送总结（图片或文本）
        result = self._render_and_get_chain(note)
        self._log(f"[总结命令] 输出模式: {'图片' if isinstance(result, list) else '文本'}")
        self._log("═══════ [总结命令] 结束(成功) ═══════")
        if isinstance(result, list):
            yield event.chain_result(result)
        else:
            safe_text = result if isinstance(result, str) and result else "❌ 总结发送内容为空"
            yield event.plain_result(safe_text)
        if feishu_result.get("attempted"):
            if feishu_result.get("success"):
                doc_url = (feishu_result.get("detail") or {}).get("doc_url", "")
                if doc_url:
                    yield event.plain_result(f"📚 飞书发布成功：{doc_url}")
                else:
                    yield event.plain_result("📚 飞书发布成功")
            else:
                yield event.plain_result(f"⚠️ 飞书发布失败：{feishu_result.get('message', '未知错误')}")

    @filter.command("最新视频", alias={"latest"})
    async def latest_video_cmd(self, event: AstrMessageEvent):
        """获取UP主最新视频并生成总结"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return
        args = self._parse_args(event.message_str)
        if not args:
            yield event.plain_result("❌ 请提供UP主UID、空间链接或昵称\n用法: /最新视频 <UP主UID或昵称>")
            return

        mid = extract_bilibili_mid(args)
        if not mid:
            # 尝试按名称搜索UP主
            yield event.plain_result(f"🔍 正在搜索UP主: {args}...")
            search_result = await search_up_by_name(args, cookies=self.bili_cookies)
            if search_result:
                mid = search_result["mid"]
                yield event.plain_result(f"✅ 找到UP主【{search_result['name']}】(UID:{mid})")
            else:
                yield event.plain_result(
                    "❌ 无法识别UP主\n"
                    "支持: 纯数字UID、空间链接、或UP主昵称"
                )
                return

        yield event.plain_result(f"⏳ 正在获取UP主 (UID:{mid}) 的最新视频...")

        videos = await get_latest_videos(mid, count=1, cookies=self.bili_cookies)
        if not videos:
            yield event.plain_result("❌ 未找到该UP主的视频")
            return

        video = videos[0]
        video_url = f"https://www.bilibili.com/video/{video['bvid']}"

        yield event.plain_result(
            f"📺 找到最新视频: {video['title']}\n⏳ 正在生成总结..."
        )

        note = await self._generate_note(video_url)
        if not isinstance(note, str) or not note.strip():
            note = "❌ 总结生成结果为空"
        feishu_result = await self._try_push_note_to_feishu(note, video_url, source="manual")
        result = self._render_and_get_chain(note)
        if isinstance(result, list):
            yield event.chain_result(result)
        else:
            safe_text = result if isinstance(result, str) and result else "❌ 总结发送内容为空"
            yield event.plain_result(safe_text)
        if feishu_result.get("attempted") and feishu_result.get("success"):
            doc_url = (feishu_result.get("detail") or {}).get("doc_url", "")
            if doc_url:
                yield event.plain_result(f"📚 飞书发布成功：{doc_url}")

    @filter.command("订阅", alias={"subscribe", "关注UP"})
    async def subscribe_cmd(self, event: AstrMessageEvent):
        """订阅UP主"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return
        args = self._parse_args(event.message_str)
        if not args:
            yield event.plain_result("❌ 请提供UP主UID、空间链接或昵称\n用法: /订阅 <UP主UID或昵称>")
            return

        mid = extract_bilibili_mid(args)
        if not mid:
            # 尝试按名称搜索UP主
            yield event.plain_result(f"🔍 正在搜索UP主: {args}...")
            search_result = await search_up_by_name(args, cookies=self.bili_cookies)
            if search_result:
                mid = search_result["mid"]
                yield event.plain_result(f"✅ 找到UP主【{search_result['name']}】(UID:{mid})")
            else:
                yield event.plain_result(
                    "❌ 无法识别UP主\n"
                    "支持: 纯数字UID、空间链接、或UP主昵称"
                )
                return

        # 检查订阅上限
        max_subs = self.config.get("max_subscriptions", 20)
        origin = event.unified_msg_origin
        current_count = self.subscription_mgr.get_subscription_count(origin)
        if current_count >= max_subs:
            yield event.plain_result(f"❌ 已达到最大订阅数 ({max_subs})")
            return

        # 获取 UP主 信息
        up_info = await get_up_info(mid, cookies=self.bili_cookies)
        if not up_info:
            yield event.plain_result(f"❌ 无法获取UP主信息 (UID:{mid})，请检查UID是否正确")
            return

        name = up_info["name"]

        # 添加订阅
        success = self.subscription_mgr.add_subscription(origin, mid, name)
        if success:
            # 记录最新视频 BVID，避免重复推送已有视频
            videos = await get_latest_videos(mid, count=1, cookies=self.bili_cookies)
            if videos:
                self.subscription_mgr.update_last_video(origin, mid, videos[0]["bvid"])

            yield event.plain_result(
                f"✅ 已订阅 UP主【{name}】(UID:{mid})\n"
                f"有新视频时将自动推送总结"
            )
        else:
            yield event.plain_result(f"⚠️ 已经订阅了 UP主【{name}】(UID:{mid})")

    @filter.command("取消订阅", alias={"unsubscribe", "取关UP"})
    async def unsubscribe_cmd(self, event: AstrMessageEvent):
        """取消订阅UP主"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return
        args = self._parse_args(event.message_str)
        if not args:
            yield event.plain_result("❌ 请提供UP主UID、空间链接或昵称\n用法: /取消订阅 <UP主UID或昵称>")
            return

        mid = extract_bilibili_mid(args)
        if not mid:
            # 尝试按名称搜索UP主
            yield event.plain_result(f"🔍 正在搜索UP主: {args}...")
            search_result = await search_up_by_name(args, cookies=self.bili_cookies)
            if search_result:
                mid = search_result["mid"]
                yield event.plain_result(f"✅ 找到UP主【{search_result['name']}】(UID:{mid})")
            else:
                yield event.plain_result(
                    "❌ 无法识别UP主\n"
                    "支持: 纯数字UID、空间链接、或UP主昵称"
                )
                return

        origin = event.unified_msg_origin
        success = self.subscription_mgr.remove_subscription(origin, mid)

        if success:
            yield event.plain_result(f"✅ 已取消订阅 (UID:{mid})")
        else:
            yield event.plain_result(f"⚠️ 未找到该订阅 (UID:{mid})")

    @filter.command("订阅列表", alias={"sublist", "订阅列表查看"})
    async def list_subscriptions_cmd(self, event: AstrMessageEvent):
        """查看订阅列表"""
        origin = event.unified_msg_origin
        subs = self.subscription_mgr.get_subscriptions(origin)

        if not subs:
            yield event.plain_result("📋 当前没有订阅任何UP主\n使用 /订阅 <UID或昵称> 添加订阅")
            return

        lines = ["📋 当前订阅列表:"]
        lines.append("━━━━━━━━━━━━━━━━━━━")
        for i, up in enumerate(subs, 1):
            lines.append(f"  {i}. {up['name']} (UID:{up['mid']})")

        lines.append(f"\n共 {len(subs)} 个订阅")
        yield event.plain_result("\n".join(lines))

    @filter.command("检查更新", alias={"check", "手动检查"})
    async def manual_check_cmd(self, event: AstrMessageEvent):
        """手动触发一次订阅检查"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return

        origin = event.unified_msg_origin
        subs = self.subscription_mgr.get_subscriptions(origin)

        if not subs:
            yield event.plain_result("📋 当前没有订阅任何UP主，无法检查更新")
            return

        yield event.plain_result(
            f"🔍 正在检查 {len(subs)} 个UP主的更新...\n"
            f"这可能需要一些时间，请耐心等待"
        )

        found_new = 0
        for up in subs:
            try:
                mid = up["mid"]
                last_bvid = up.get("last_bvid", "")

                videos = await get_latest_videos(mid, count=1, cookies=self.bili_cookies)
                if not videos:
                    continue

                latest = videos[0]
                latest_bvid = latest["bvid"]

                if latest_bvid == last_bvid:
                    continue  # 没有新视频

                if not last_bvid:
                    # 首次检查，只记录不推送
                    self.subscription_mgr.update_last_video(origin, mid, latest_bvid)
                    continue

                # 有新视频！
                found_new += 1
                yield event.plain_result(
                    f"🔔 UP主【{up['name']}】有新视频!\n"
                    f"📺 {latest['title']}\n"
                    f"⏳ 正在生成总结..."
                )

                video_url = f"https://www.bilibili.com/video/{latest_bvid}"
                note = await self._generate_note(video_url)
                if not isinstance(note, str) or not note.strip():
                    note = "❌ 总结生成结果为空"
                feishu_result = await self._try_push_note_to_feishu(note, video_url, source="manual")
                result = self._render_and_get_chain(note)
                if isinstance(result, list):
                    yield event.chain_result(result)
                else:
                    safe_text = result if isinstance(result, str) and result else "❌ 总结发送内容为空"
                    yield event.plain_result(safe_text)
                if feishu_result.get("attempted") and feishu_result.get("success"):
                    doc_url = (feishu_result.get("detail") or {}).get("doc_url", "")
                    if doc_url:
                        yield event.plain_result(f"📚 飞书发布成功：{doc_url}")

                # 更新已推送记录
                self.subscription_mgr.update_last_video(origin, mid, latest_bvid)

                await asyncio.sleep(2)  # 避免请求过快
            except Exception as e:
                logger.error(f"手动检查UP主 {up.get('name', '?')} 失败: {e}")

        if found_new == 0:
            yield event.plain_result("✅ 检查完成，所有订阅的UP主暂无新视频")
        else:
            yield event.plain_result(f"✅ 检查完成，共发现 {found_new} 个新视频")

    # ==================== 推送目标管理 ====================

    def _detect_platform_prefix(self, origin: str) -> str:
        """
        从 unified_msg_origin 中提取平台前缀
        例如 'aiocqhttp:GroupMessage:123' -> 'aiocqhttp'
        """
        parts = origin.split(':')
        return parts[0] if parts else ''

    def _build_group_origin(self, origin: str, group_id: str) -> str:
        """根据当前平台构建群消息 origin"""
        prefix = self._detect_platform_prefix(origin)
        return f"{prefix}:GroupMessage:{group_id}"

    def _build_user_origin(self, origin: str, user_id: str) -> str:
        """根据当前平台构建私聊 origin"""
        prefix = self._detect_platform_prefix(origin)
        return f"{prefix}:FriendMessage:{user_id}"

    @filter.command("添加推送群", alias={"add_push_group"})
    async def add_push_group_cmd(self, event: AstrMessageEvent):
        """添加QQ群到推送列表"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return
        args = self._parse_args(event.message_str)
        if not args or not args.strip().isdigit():
            yield event.plain_result("❌ 请提供QQ群号\n用法: /添加推送群 <群号>")
            return

        group_id = args.strip()
        target_origin = self._build_group_origin(event.unified_msg_origin, group_id)
        success = self.subscription_mgr.add_push_target(target_origin, f"群{group_id}")
        if success:
            yield event.plain_result(f"✅ 已添加推送目标: 群 {group_id}")
        else:
            yield event.plain_result(f"⚠️ 群 {group_id} 已在推送列表中")

    @filter.command("添加推送号", alias={"add_push_user"})
    async def add_push_user_cmd(self, event: AstrMessageEvent):
        """添加QQ号到推送列表"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return
        args = self._parse_args(event.message_str)
        if not args or not args.strip().isdigit():
            yield event.plain_result("❌ 请提供QQ号\n用法: /添加推送号 <QQ号>")
            return

        user_id = args.strip()
        target_origin = self._build_user_origin(event.unified_msg_origin, user_id)
        success = self.subscription_mgr.add_push_target(target_origin, f"QQ{user_id}")
        if success:
            yield event.plain_result(f"✅ 已添加推送目标: QQ {user_id}")
        else:
            yield event.plain_result(f"⚠️ QQ {user_id} 已在推送列表中")

    @filter.command("推送列表", alias={"push_list", "推送目标"})
    async def push_list_cmd(self, event: AstrMessageEvent):
        """查看推送目标列表"""
        targets = self.subscription_mgr.get_push_targets()
        if not targets:
            yield event.plain_result(
                "📋 当前没有配置推送目标\n"
                "使用 /添加推送群 <群号> 或 /添加推送号 <QQ号> 添加\n"
                "⚠️ 未配置推送目标时，总结将推送到发起订阅的群"
            )
            return

        lines = ["📋 当前推送目标:"]
        lines.append("━━━━━━━━━━━━━━━━━━━")
        for i, t in enumerate(targets, 1):
            lines.append(f"  {i}. {t['label']}")
        lines.append(f"\n共 {len(targets)} 个推送目标")
        yield event.plain_result("\n".join(lines))

    @filter.command("移除推送", alias={"remove_push", "删除推送"})
    async def remove_push_cmd(self, event: AstrMessageEvent):
        """移除推送目标"""
        if not self._check_access(event):
            yield event.plain_result("⛔ 你没有权限使用此插件")
            return
        args = self._parse_args(event.message_str)
        if not args:
            yield event.plain_result("❌ 请提供要移除的群号或QQ号\n用法: /移除推送 <群号或QQ号>")
            return

        target_id = args.strip()
        # 尝试按 label 匹配
        label_group = f"群{target_id}"
        label_user = f"QQ{target_id}"
        success = self.subscription_mgr.remove_push_target(label_group)
        if not success:
            success = self.subscription_mgr.remove_push_target(label_user)
        if not success:
            success = self.subscription_mgr.remove_push_target(target_id)

        if success:
            yield event.plain_result(f"✅ 已移除推送目标: {target_id}")
        else:
            yield event.plain_result(f"⚠️ 未找到该推送目标: {target_id}")

    @filter.command("飞书发布状态", alias={"feishu_status", "发布状态"})
    async def feishu_publish_status_cmd(self, event: AstrMessageEvent):
        """查看最近一次飞书发布结果"""
        result = self._last_feishu_publish_result or {}
        if not result:
            yield event.plain_result("ℹ️ 暂无飞书发布记录")
            return

        if not result.get("attempted"):
            yield event.plain_result(f"ℹ️ 最近一次未尝试飞书发布: {result.get('reason', 'unknown')}")
            return

        detail = result.get("detail") or {}
        if result.get("success"):
            doc_url = detail.get("doc_url", "")
            msg = "✅ 最近一次飞书发布成功"
            if doc_url:
                msg += f"\n📚 {doc_url}"
            if "images_ok" in detail:
                msg += f"\n🖼️ 图片绑定: 成功 {detail.get('images_ok', 0)} / 失败 {detail.get('images_fail', 0)}"
            yield event.plain_result(msg)
        else:
            yield event.plain_result(f"❌ 最近一次飞书发布失败\n原因: {result.get('message', '未知错误')}")

    # ==================== 核心逻辑 ====================

    async def _generate_note(self, video_url: str) -> str:
        """生成总结的统一调用入口"""
        self._log("═══════ [生成总结] 开始 ═══════")
        style = self.config.get("note_style", "detailed")
        enable_link = self.config.get("enable_link", True)
        enable_summary = self.config.get("enable_summary", True)
        quality = self.config.get("download_quality", "fast")
        max_length = self.config.get("max_note_length", 3000)
        self._log(
            f"[生成总结] 参数: url={video_url}, style={style}, "
            f"enable_link={enable_link}, enable_summary={enable_summary}, "
            f"quality={quality}, max_length={max_length}"
        )

        try:
            result = await self.note_service.generate_note(
                video_url=video_url,
                llm_ask_func=self._ask_llm,
                style=style,
                enable_link=enable_link,
                enable_summary=enable_summary,
                quality=quality,
                max_length=max_length,
            )
            self._log(f"[生成总结] 完成, 结果长度={len(result) if result else 0}")
            self._log("═══════ [生成总结] 结束 ═══════")
            return result
        except Exception as e:
            self._log(f"[生成总结] 异常: {e}")
            self._log("═══════ [生成总结] 结束(异常) ═══════")
            logger.error(f"总结生成异常: {e}", exc_info=True)
            return f"❌ 总结生成失败: {str(e)}"

    async def _ask_llm(self, prompt: str) -> str:
        """调用 AstrBot 内置 LLM"""
        try:
            self._log(f"[AskLLM] prompt 长度={len(prompt)}, 前100字: {prompt[:100]}...")
            provider = self.context.get_using_provider()
            self._log(f"[AskLLM] provider={type(provider).__name__ if provider else 'None'}")
            if not provider:
                return "❌ 未配置 LLM Provider，请在 AstrBot 设置中配置"

            response = await provider.text_chat(
                prompt=prompt,
                session_id="BiliBrief_plugin",
            )
            self._log(f"[AskLLM] response type={type(response).__name__}")

            if hasattr(response, 'completion_text'):
                result = response.completion_text
                self._log(f"[AskLLM] 使用 completion_text, 长度={len(result) if result else 0}")
                return result
            elif isinstance(response, str):
                self._log(f"[AskLLM] response 是 str, 长度={len(response)}")
                return response
            else:
                self._log(f"[AskLLM] response 转 str")
                return str(response)

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}", exc_info=True)
            return f"❌ LLM 调用失败: {str(e)}"

    # ==================== 定时任务 ====================

    async def _scheduled_check_loop(self):
        """定时检查订阅UP主的新视频"""
        await asyncio.sleep(10)  # 启动后等待10秒再开始

        while self._running:
            try:
                await self._check_new_videos()
            except Exception as e:
                logger.error(f"定时检查异常: {e}", exc_info=True)

            interval = self.config.get("check_interval_minutes", 30)
            await asyncio.sleep(interval * 60)

    async def _check_new_videos(self):
        """检查所有订阅是否有新视频"""
        all_subs = self.subscription_mgr.get_all_subscriptions()

        if not all_subs:
            return

        logger.info(f"开始定时检查，共 {len(all_subs)} 个会话的订阅")

        for origin, up_list in all_subs.items():
            for up in up_list:
                try:
                    await self._check_up_new_video(origin, up)
                    await asyncio.sleep(2)  # 避免请求过快
                except Exception as e:
                    logger.error(f"检查UP主 {up['name']} 新视频失败: {e}")

    async def _check_up_new_video(self, origin: str, up: dict):
        """检查单个UP主是否有新视频"""
        mid = up["mid"]
        last_bvid = up.get("last_bvid", "")

        videos = await get_latest_videos(mid, count=1, cookies=self.bili_cookies)
        if not videos:
            return

        latest = videos[0]
        latest_bvid = latest["bvid"]

        if latest_bvid == last_bvid:
            return  # 没有新视频

        if not last_bvid:
            # 首次检查，只记录不推送
            self.subscription_mgr.update_last_video(origin, mid, latest_bvid)
            return

        # 有新视频！
        logger.info(f"UP主 {up['name']} 有新视频: {latest['title']}")

        video_url = f"https://www.bilibili.com/video/{latest_bvid}"

        # 生成总结
        note = await self._generate_note(video_url)
        await self._try_push_note_to_feishu(note, video_url, source="auto")

        # 推送消息
        push_header = f"🔔 UP主【{up['name']}】发布了新视频!\n"
        result = self._render_and_get_chain(note)
        if isinstance(result, list):
            chain = [Plain(push_header)] + result
        else:
            chain = [Plain(push_header + "━━━━━━━━━━━━━━━━━━━\n\n" + result)]

        # 获取推送目标：优先使用配置的推送目标，否则推到订阅来源
        push_origins = self.subscription_mgr.get_push_origins()
        if not push_origins:
            push_origins = [origin]

        for target in push_origins:
            try:
                await self.context.send_message(target, chain)
                logger.info(f"已推送新视频总结给 {target}")
            except Exception as e:
                logger.error(f"推送消息到 {target} 失败: {e}")

        # 更新已推送记录
        self.subscription_mgr.update_last_video(origin, mid, latest_bvid)

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件卸载时停止定时任务"""
        self._running = False
        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        logger.info("BiliBrief 视频纪要插件已卸载")
