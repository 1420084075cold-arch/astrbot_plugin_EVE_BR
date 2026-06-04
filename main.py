from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import requests
import re
import time
import threading
import base64
from typing import Dict, Set, List, Optional, Tuple
from datetime import datetime, timedelta
from io import BytesIO

@register("EveKillmail", "YourName", "EVE Online 击杀邮件订阅插件，支持军团、联盟、玩家订阅和截图", "1.0.0")
class KillmailPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        
        # 存储订阅数据: {group_id: {"corp:123", "alliance:456", "player:789", "high_value"}}
        self.subscriptions: Dict[str, Set[str]] = {}
        
        # 已推送过的击杀ID，避免重复
        self.pushed_kills: Set[str] = set()
        
        # 监控线程控制
        self.monitoring = False
        self.monitor_thread = None
        
        # API配置
        self.ZKILL_API = "https://zkillboard.com/api"
        self.ZKILL_URL = "https://zkillboard.com/kill"
        self.ESI_API = "https://esi.evetech.net/latest"
        
        # 高价值击杀阈值 (10B ISK)
        self.HIGH_VALUE_THRESHOLD = 10000000000  # 10B
        
        # 截图配置
        self.SCREENSHOT_ENABLED = True  # 是否启用截图
        self.SCREENSHOT_ATTEMPTS = 3   # 截图重试次数

    async def initialize(self):
        """插件初始化时启动监控"""
        logger.info("击杀邮件订阅插件已加载（带截图功能）")
        self.start_monitoring()

    async def terminate(self):
        """插件卸载时停止监控"""
        self.stop_monitoring()
        logger.info("击杀邮件订阅插件已卸载")

    # ==================== 截图功能 ====================
    
    def get_killmail_screenshot(self, kill_id: int, max_retries: int = 3) -> Optional[bytes]:
        """
        获取击杀邮件的截图
        从 zKillboard 获取击杀界面的截图
        """
        # 尝试多个可能的截图URL
        urls = [
            f"https://zkillboard.com/kill/{kill_id}/screenshot/",
            f"https://zkillboard.com/kill/{kill_id}/screenshot.png",
            f"https://zkillboard.com/kill/{kill_id}/render/",
            f"https://zkillboard.com/kill/{kill_id}/render.png",
        ]
        
        headers = {
            "User-Agent": "AstrBot-EVE-Killmail-Plugin/1.0",
            "Accept": "image/webp,image/apng,image/png,image/jpeg,*/*"
        }
        
        for attempt in range(max_retries):
            for url in urls:
                try:
                    resp = requests.get(url, headers=headers, timeout=15)
                    
                    # 检查返回的是否是图片
                    content_type = resp.headers.get("Content-Type", "")
                    if resp.status_code == 200 and ("image" in content_type or len(resp.content) > 1000):
                        logger.info(f"成功获取击杀 {kill_id} 截图: {url}")
                        return resp.content
                    
                except Exception as e:
                    logger.debug(f"尝试截图失败 {url}: {e}")
                    continue
            
            if attempt < max_retries - 1:
                time.sleep(1)  # 重试前等待
        
        logger.warning(f"无法获取击杀 {kill_id} 的截图")
        return None
    
    def get_character_portrait(self, character_id: int, size: int = 64) -> Optional[bytes]:
        """获取角色肖像"""
        url = f"https://images.eveonline.com/Character/{character_id}_{size}.jpg"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.debug(f"获取角色肖像失败: {e}")
        return None
    
    def get_corporation_logo(self, corporation_id: int, size: int = 64) -> Optional[bytes]:
        """获取军团标志"""
        url = f"https://images.eveonline.com/Corporation/{corporation_id}_{size}.png"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.debug(f"获取军团标志失败: {e}")
        return None
    
    def get_ship_image(self, ship_type_id: int, size: int = 256) -> Optional[bytes]:
        """获取舰船图片"""
        url = f"https://images.eveonline.com/types/{ship_type_id}_{size}.png"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.debug(f"获取舰船图片失败: {e}")
        return None
    
    def get_ship_type_id_by_name(self, ship_name: str) -> Optional[int]:
        """根据舰船名称获取 type_id"""
        # 使用 fuzzwork API
        url = "https://www.fuzzwork.co.uk/api/typeid.php"
        params = {"typename": ship_name}
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return data[0].get("typeID")
                elif isinstance(data, dict) and "typeID" in data:
                    return data["typeID"]
        except Exception as e:
            logger.debug(f"获取舰船ID失败: {e}")
        return None

    # ==================== 监控线程管理 ====================
    
    def start_monitoring(self):
        """启动后台监控线程"""
        if self.monitoring:
            return
        
        self.monitoring = True
        
        def monitor_loop():
            logger.info("击杀邮件监控线程已启动")
            while self.monitoring:
                try:
                    self.check_new_killmails()
                except Exception as e:
                    logger.error(f"监控异常: {e}")
                time.sleep(30)  # 每30秒检查一次
        
        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()
    
    def stop_monitoring(self):
        """停止监控线程"""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)

    # ==================== API 请求 ====================
    
    def fetch_killmails(self, limit: int = 50) -> List[dict]:
        """获取最近的击杀邮件"""
        url = f"{self.ZKILL_API}/killmails/"
        params = {"limit": limit}
        headers = {"User-Agent": "AstrBot-EVE-Killmail-Plugin/1.0"}
        
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(f"获取击杀邮件失败: HTTP {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return []
    
    def fetch_killmail_detail(self, kill_id: int) -> Optional[dict]:
        """获取击杀邮件详情"""
        url = f"{self.ZKILL_API}/killmail/{kill_id}/"
        headers = {"User-Agent": "AstrBot-EVE-Killmail-Plugin/1.0"}
        
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error(f"获取击杀详情失败: {e}")
        return None
    
    def search_entity(self, name: str) -> dict:
        """搜索角色、军团、联盟"""
        url = f"{self.ESI_API}/universe/ids/"
        headers = {"Accept-Language": "zh"}
        
        try:
            resp = requests.post(url, headers=headers, json=[name], timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error(f"搜索失败: {e}")
        return {}

    # ==================== 击杀邮件处理 ====================
    
    def check_new_killmails(self):
        """检查新击杀邮件并推送"""
        if not self.subscriptions:
            return
        
        killmails = self.fetch_killmails(limit=50)
        if not killmails:
            return
        
        # 按群组收集需要推送的击杀
        group_killmails: Dict[str, List[dict]] = {}
        
        for killmail in killmails:
            kill_id = str(killmail.get("killmail_id"))
            
            # 跳过已推送的
            if kill_id in self.pushed_kills:
                continue
            
            # 检查哪些群组订阅了这个击杀
            matched_groups = self.match_subscriptions(killmail)
            
            if matched_groups:
                # 标记为已推送
                self.pushed_kills.add(kill_id)
                
                # 限制历史记录大小
                if len(self.pushed_kills) > 5000:
                    self.pushed_kills.clear()
                
                # 按群组分组
                for group_id in matched_groups:
                    if group_id not in group_killmails:
                        group_killmails[group_id] = []
                    group_killmails[group_id].append(killmail)
        
        # 发送通知
        for group_id, mails in group_killmails.items():
            for killmail in mails:
                self.send_notification(group_id, killmail)
    
    def match_subscriptions(self, killmail: dict) -> Set[str]:
        """检查击杀邮件匹配哪些群组的订阅"""
        matched_groups = set()
        
        # 提取击杀信息
        victim = killmail.get("victim", {})
        victim_char_id = victim.get("character_id")
        victim_corp_id = victim.get("corporation_id")
        victim_alliance_id = victim.get("alliance_id")
        
        attackers = killmail.get("attackers", [])
        attacker_char_ids = {a.get("character_id") for a in attackers if a.get("character_id")}
        attacker_corp_ids = {a.get("corporation_id") for a in attackers if a.get("corporation_id")}
        attacker_alliance_ids = {a.get("alliance_id") for a in attackers if a.get("alliance_id")}
        
        # 击杀价值
        kill_value = killmail.get("zkb", {}).get("totalValue", 0)
        
        # 检查每个群组的订阅
        for group_id, subs in self.subscriptions.items():
            matched = False
            
            for sub in subs:
                # 高价值击杀
                if sub == "high_value" and kill_value >= self.HIGH_VALUE_THRESHOLD:
                    matched = True
                    break
                
                # 解析订阅类型和ID
                if ":" not in sub:
                    continue
                
                sub_type, sub_id = sub.split(":", 1)
                sub_id = int(sub_id)
                
                if sub_type == "corp":
                    if victim_corp_id == sub_id or sub_id in attacker_corp_ids:
                        matched = True
                        break
                
                elif sub_type == "alliance":
                    if victim_alliance_id == sub_id or sub_id in attacker_alliance_ids:
                        matched = True
                        break
                
                elif sub_type == "player":
                    if victim_char_id == sub_id or sub_id in attacker_char_ids:
                        matched = True
                        break
                
                elif sub_type == "player_kill":
                    if sub_id in attacker_char_ids:
                        matched = True
                        break
                
                elif sub_type == "player_loss":
                    if victim_char_id == sub_id:
                        matched = True
                        break
            
            if matched:
                matched_groups.add(group_id)
        
        return matched_groups

    # ==================== 消息推送（带截图） ====================
    
    def send_notification(self, group_id: str, killmail: dict):
        """发送击杀通知到群组（包含截图）"""
        kill_id = killmail.get("killmail_id")
        victim = killmail.get("victim", {})
        
        # 受害者信息
        victim_name = victim.get("character_name", "未知")
        victim_ship = victim.get("ship_type_name", "未知")
        victim_corp = victim.get("corporation_name", "未知")
        victim_alliance = victim.get("alliance_name")
        victim_char_id = victim.get("character_id")
        victim_corp_id = victim.get("corporation_id")
        
        # 击杀价值
        kill_value = killmail.get("zkb", {}).get("totalValue", 0)
        dropped_value = killmail.get("zkb", {}).get("droppedValue", 0)
        destroyed_value = killmail.get("zkb", {}).get("destroyedValue", 0)
        
        # 击杀者信息（取第一个）
        attackers = killmail.get("attackers", [])
        main_killer = attackers[0] if attackers else {}
        killer_name = main_killer.get("character_name", "未知")
        killer_ship = main_killer.get("ship_type_name", "未知")
        killer_char_id = main_killer.get("character_id")
        
        # 星系信息
        solar_system = killmail.get("solar_system", {})
        system_name = solar_system.get("name", "未知")
        sec_status = solar_system.get("security_status", 0)
        
        # 判断安全区类型
        if sec_status >= 0.5:
            sec_type = "🛡️ 高安"
            sec_emoji = "🛡️"
        elif sec_status > 0:
            sec_type = "⚠️ 低安"
            sec_emoji = "⚠️"
        else:
            sec_type = "💀 00区"
            sec_emoji = "💀"
        
        # 时间
        kill_time = killmail.get("killmail_time", "")
        if kill_time:
            try:
                dt = datetime.fromisoformat(kill_time.replace('Z', '+00:00'))
                kill_time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                kill_time_str = kill_time
        else:
            kill_time_str = "未知"
        
        # 构建文本消息
        message_lines = []
        
        # 高价值标记
        if kill_value >= self.HIGH_VALUE_THRESHOLD:
            message_lines.append("🌟✨ 高价值击杀！ ✨🌟")
            message_lines.append("")
        
        message_lines.extend([
            f"⚔️ **击杀邮件** ⚔️",
            "",
            f"💀 **受害者**: {victim_name}",
            f"🚀 **舰船**: {victim_ship}",
            f"🏢 **军团**: {victim_corp}",
        ])
        
        if victim_alliance:
            message_lines.append(f"🌟 **联盟**: {victim_alliance}")
        
        message_lines.extend([
            "",
            f"🎯 **击杀者**: {killer_name} ({killer_ship})",
            f"💰 **总价值**: {kill_value:,.2f} ISK",
            f"💎 **掉落**: {dropped_value:,.2f} ISK",
            f"🔨 **摧毁**: {destroyed_value:,.2f} ISK",
            "",
            f"📍 **星系**: {system_name}",
            f"⭐ **安全等级**: {sec_type} ({sec_status:.1f})",
            f"🕐 **时间**: {kill_time_str}",
            "",
            f"🔗 **详情**: {self.ZKILL_URL}/{kill_id}/"
        ])
        
        text_message = "\n".join(message_lines)
        
        # 记录日志
        logger.info(f"[群组 {group_id}] 击杀 {kill_id}: {victim_name} 被 {killer_name} 击杀，价值 {kill_value:,.2f} ISK")
        
        # 发送文本消息
        # TODO: 根据实际 AstrBot API 发送消息
        # await self.context.send_group_message(int(group_id), text_message)
        
        # 尝试获取并发送截图
        if self.SCREENSHOT_ENABLED:
            screenshot = self.get_killmail_screenshot(kill_id)
            if screenshot:
                # 发送截图
                try:
                    # 方式1: 发送 base64 图片
                    image_base64 = base64.b64encode(screenshot).decode('utf-8')
                    # await self.context.send_group_image(int(group_id), image_base64)
                    logger.info(f"已获取击杀 {kill_id} 截图，大小: {len(screenshot)} bytes")
                except Exception as e:
                    logger.error(f"发送截图失败: {e}")
            else:
                logger.debug(f"击杀 {kill_id} 无可用截图")
    
    # ==================== 手动命令 ====================
    
    @filter.command(".killimg")
    async def get_kill_image(self, event: AstrMessageEvent):
        """手动获取击杀邮件截图
        
        用法: .killimg [击杀ID]
        """
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .killimg [击杀ID]\n例如: .killimg 12345678")
            return
        
        kill_id = parts[1]
        
        if not kill_id.isdigit():
            yield event.plain_result("❌ 击杀ID必须是数字")
            return
        
        kill_id_int = int(kill_id)
        yield event.plain_result(f"📸 正在获取击杀 {kill_id} 的截图...")
        
        screenshot = self.get_killmail_screenshot(kill_id_int)
        
        if screenshot:
            # 发送截图
            try:
                image_base64 = base64.b64encode(screenshot).decode('utf-8')
                # await self.context.send_group_image(event.group_id, image_base64)
                yield event.plain_result(f"✅ 截图获取成功！大小: {len(screenshot)} bytes")
            except Exception as e:
                yield event.plain_result(f"❌ 发送截图失败: {e}")
        else:
            yield event.plain_result(f"❌ 无法获取击杀 {kill_id} 的截图\n可能原因：截图不存在或网络问题")
    
    @filter.command(".shipimg")
    async def get_ship_image_cmd(self, event: AstrMessageEvent):
        """获取舰船图片
        
        用法: .shipimg [舰船名称]
        """
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .shipimg [舰船名称]\n例如: .shipimg Rifter")
            return
        
        ship_name = " ".join(parts[1:])
        yield event.plain_result(f"🖼️ 正在获取 {ship_name} 的图片...")
        
        # 获取舰船 type_id
        ship_type_id = self.get_ship_type_id_by_name(ship_name)
        
        if not ship_type_id:
            yield event.plain_result(f"❌ 未找到舰船: {ship_name}")
            return
        
        ship_image = self.get_ship_image(ship_type_id)
        
        if ship_image:
            try:
                image_base64 = base64.b64encode(ship_image).decode('utf-8')
                # await self.context.send_group_image(event.group_id, image_base64)
                yield event.plain_result(f"✅ {ship_name} 图片获取成功！")
            except Exception as e:
                yield event.plain_result(f"❌ 发送图片失败: {e}")
        else:
            yield event.plain_result(f"❌ 无法获取 {ship_name} 的图片")
    
    @filter.command(".portrait")
    async def get_portrait(self, event: AstrMessageEvent):
        """获取角色肖像
        
        用法: .portrait [角色ID]
        """
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .portrait [角色ID]\n例如: .portrait 1234567890")
            return
        
        char_id = parts[1]
        
        if not char_id.isdigit():
            yield event.plain_result("❌ 角色ID必须是数字")
            return
        
        char_id_int = int(char_id)
        yield event.plain_result(f"👤 正在获取角色肖像...")
        
        portrait = self.get_character_portrait(char_id_int, 256)
        
        if portrait:
            try:
                image_base64 = base64.b64encode(portrait).decode('utf-8')
                # await self.context.send_group_image(event.group_id, image_base64)
                yield event.plain_result(f"✅ 角色肖像获取成功！")
            except Exception as e:
                yield event.plain_result(f"❌ 发送图片失败: {e}")
        else:
            yield event.plain_result(f"❌ 无法获取角色肖像")

    # ==================== 订阅命令 ====================
    
    @filter.command(".sub")
    async def subscribe(self, event: AstrMessageEvent):
        """订阅击杀邮件
        
        用法:
            .sub corp [军团ID]        - 订阅军团
            .sub alliance [联盟ID]    - 订阅联盟
            .sub player [角色ID]      - 订阅玩家
            .sub player_kill [角色ID] - 订阅玩家击杀
            .sub player_loss [角色ID] - 订阅玩家被击杀
            .sub high_value           - 订阅高价值击杀（10B+）
            .sub list                 - 查看当前订阅
            .sub clear                - 清空订阅
        """
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result(
                "📋 **订阅命令帮助**\n\n"
                ".sub corp [ID]        - 订阅军团\n"
                ".sub alliance [ID]    - 订阅联盟\n"
                ".sub player [ID]      - 订阅玩家\n"
                ".sub player_kill [ID] - 订阅玩家击杀\n"
                ".sub player_loss [ID] - 订阅玩家被击杀\n"
                ".sub high_value       - 订阅高价值击杀\n"
                ".sub list             - 查看订阅\n"
                ".sub clear            - 清空订阅"
            )
            return
        
        # 获取群组ID
        group_id = str(event.group_id) if hasattr(event, 'group_id') else "default"
        
        # 初始化群组订阅
        if group_id not in self.subscriptions:
            self.subscriptions[group_id] = set()
        
        command = parts[1].lower()
        subs = self.subscriptions[group_id]
        
        # 查看订阅列表
        if command == "list":
            if not subs:
                yield event.plain_result("当前群组没有订阅")
            else:
                lines = ["📋 **当前订阅列表**:", ""]
                for sub in subs:
                    if sub == "high_value":
                        lines.append("  💰 高价值击杀 (10B+)")
                    elif sub.startswith("corp:"):
                        lines.append(f"  🏢 军团 ID: {sub[5:]}")
                    elif sub.startswith("alliance:"):
                        lines.append(f"  🌟 联盟 ID: {sub[9:]}")
                    elif sub.startswith("player:"):
                        lines.append(f"  👤 玩家 ID: {sub[7:]}")
                    elif sub.startswith("player_kill:"):
                        lines.append(f"  🔫 玩家击杀 ID: {sub[12:]}")
                    elif sub.startswith("player_loss:"):
                        lines.append(f"  💀 玩家被击杀 ID: {sub[12:]}")
                yield event.plain_result("\n".join(lines))
            return
        
        # 清空订阅
        if command == "clear":
            self.subscriptions[group_id].clear()
            yield event.plain_result("✅ 已清空所有订阅")
            return
        
        # 高价值击杀订阅
        if command == "high_value":
            subs.add("high_value")
            yield event.plain_result(f"✅ 已订阅高价值击杀 (≥ {self.HIGH_VALUE_THRESHOLD:,} ISK)")
            return
        
        # 需要ID的命令
        if len(parts) < 3:
            yield event.plain_result(f"用法: .sub {command} [ID]\n例如: .sub corp 98654321")
            return
        
        entity_id = parts[2]
        
        # 验证ID格式
        if not entity_id.isdigit():
            yield event.plain_result("❌ ID必须是数字，使用 .search 搜索获取ID")
            return
        
        # 添加订阅
        if command == "corp":
            subs.add(f"corp:{entity_id}")
            yield event.plain_result(f"✅ 已订阅军团 ID: {entity_id}")
        elif command == "alliance":
            subs.add(f"alliance:{entity_id}")
            yield event.plain_result(f"✅ 已订阅联盟 ID: {entity_id}")
        elif command == "player":
            subs.add(f"player:{entity_id}")
            yield event.plain_result(f"✅ 已订阅玩家 ID: {entity_id}")
        elif command == "player_kill":
            subs.add(f"player_kill:{entity_id}")
            yield event.plain_result(f"✅ 已订阅玩家击杀 ID: {entity_id}")
        elif command == "player_loss":
            subs.add(f"player_loss:{entity_id}")
            yield event.plain_result(f"✅ 已订阅玩家被击杀 ID: {entity_id}")
        else:
            yield event.plain_result(f"❌ 未知命令: {command}")
    
    @filter.command(".unsub")
    async def unsubscribe(self, event: AstrMessageEvent):
        """取消订阅"""
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .unsub corp/alliance/player/player_kill/player_loss/high_value [ID]")
            return
        
        group_id = str(event.group_id) if hasattr(event, 'group_id') else "default"
        
        if group_id not in self.subscriptions:
            yield event.plain_result("当前群组没有订阅")
            return
        
        command = parts[1].lower()
        subs = self.subscriptions[group_id]
        
        if command == "high_value":
            if "high_value" in subs:
                subs.remove("high_value")
                yield event.plain_result("✅ 已取消高价值击杀订阅")
            else:
                yield event.plain_result("当前未订阅高价值击杀")
            return
        
        if len(parts) < 3:
            to_remove = [s for s in subs if s.startswith(f"{command}:")]
            for s in to_remove:
                subs.remove(s)
            if to_remove:
                yield event.plain_result(f"✅ 已取消所有 {command} 类型订阅 ({len(to_remove)}个)")
            else:
                yield event.plain_result(f"当前没有 {command} 类型订阅")
            return
        
        entity_id = parts[2]
        sub_key = f"{command}:{entity_id}"
        
        if sub_key in subs:
            subs.remove(sub_key)
            yield event.plain_result(f"✅ 已取消订阅 {command} ID: {entity_id}")
        else:
            yield event.plain_result(f"❌ 未找到订阅 {command} ID: {entity_id}")
    
    @filter.command(".search")
    async def search(self, event: AstrMessageEvent):
        """搜索角色、军团、联盟的ID"""
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .search [名称]\n例如: .search Goonswarm")
            return
        
        search_name = " ".join(parts[1:])
        yield event.plain_result(f"🔍 正在搜索: {search_name}...")
        
        data = self.search_entity(search_name)
        
        if not data:
            yield event.plain_result(f"❌ 未找到: {search_name}")
            return
        
        result_lines = [f"🔍 搜索结果: {search_name}", ""]
        
        characters = data.get("characters", [])
        if characters:
            result_lines.append("👤 **角色**:")
            for c in characters[:5]:
                result_lines.append(f"   {c.get('name')} (ID: {c.get('id')})")
                result_lines.append(f"     订阅: .sub player {c.get('id')}")
        
        corporations = data.get("corporations", [])
        if corporations:
            result_lines.append("\n🏢 **军团**:")
            for c in corporations[:5]:
                result_lines.append(f"   {c.get('name')} (ID: {c.get('id')})")
                result_lines.append(f"     订阅: .sub corp {c.get('id')}")
        
        alliances = data.get("alliances", [])
        if alliances:
            result_lines.append("\n🌟 **联盟**:")
            for a in alliances[:5]:
                result_lines.append(f"   {a.get('name')} (ID: {a.get('id')})")
                result_lines.append(f"     订阅: .sub alliance {a.get('id')}")
        
        result_lines.append("\n💡 使用 .sub [类型] [ID] 开始订阅")
        result_lines.append("📸 使用 .killimg [ID] 获取击杀截图")
        
        yield event.plain_result("\n".join(result_lines))
    
    @filter.command(".killinfo")
    async def kill_info(self, event: AstrMessageEvent):
        """查询击杀邮件详情"""
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .killinfo [击杀ID]\n例如: .killinfo 12345678")
            return
        
        kill_id = parts[1]
        
        if not kill_id.isdigit():
            yield event.plain_result("❌ 击杀ID必须是数字")
            return
        
        yield event.plain_result(f"🔗 查看详情: {self.ZKILL_URL}/{kill_id}/\n📸 查看截图: .killimg {kill_id}")
    
    @filter.command(".screenshot")
    async def screenshot_toggle(self, event: AstrMessageEvent):
        """切换截图功能开关"""
        self.SCREENSHOT_ENABLED = not self.SCREENSHOT_ENABLED
        status = "开启" if self.SCREENSHOT_ENABLED else "关闭"
        yield event.plain_result(f"📸 截图功能已{status}")
