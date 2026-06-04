from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import requests
import time
import threading
import json
from typing import Dict, Set, List, Optional

@register("EveKillmail", "YourName", "EVE Online 击杀邮件订阅插件", "1.0.0")
class KillmailPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        
        # 存储订阅数据
        self.subscriptions: Dict[str, Set[str]] = {}
        
        # 已推送过的击杀ID
        self.pushed_kills: Set[str] = set()
        
        # 监控线程控制
        self.monitoring = False
        self.monitor_thread = None
        
        # API配置
        self.ZKILL_API = "https://zkillboard.com/api"
        self.ZKILL_URL = "https://zkillboard.com/kill"
        
        # 使用 fuzzwork 作为备选搜索API
        self.FUZZWORK_API = "https://www.fuzzwork.co.uk/api"
        
        # 高价值击杀阈值 (10B ISK)
        self.HIGH_VALUE_THRESHOLD = 10000000000

    async def initialize(self):
        logger.info("击杀邮件订阅插件已加载")
        self.start_monitoring()

    async def terminate(self):
        self.stop_monitoring()
        logger.info("击杀邮件订阅插件已卸载")

    # ==================== 监控线程 ====================
    
    def start_monitoring(self):
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
                time.sleep(30)
        
        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()
    
    def stop_monitoring(self):
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)

    # ==================== API 请求 ====================
    
    def fetch_killmails(self, limit: int = 30) -> List[dict]:
        """获取最近的击杀邮件"""
        url = f"{self.ZKILL_API}/killmails/"
        params = {"limit": limit}
        headers = {"User-Agent": "AstrBot-EVE-Killmail-Plugin/1.0"}
        
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"获取到 {len(data)} 条击杀邮件")
                return data
            else:
                logger.error(f"获取击杀邮件失败: HTTP {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return []
    
    def search_corporation(self, name: str) -> Optional[dict]:
        """通过 fuzzwork 搜索军团"""
        try:
            # 方法1: 使用 fuzzwork 搜索
            url = f"{self.FUZZWORK_API}/corporationID.php"
            params = {"corporation": name}
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200 and resp.text.strip():
                corp_id = resp.text.strip()
                if corp_id.isdigit():
                    return {"id": int(corp_id), "name": name, "type": "corporation"}
        except Exception as e:
            logger.debug(f"fuzzwork 搜索失败: {e}")
        
        # 方法2: 使用 zKillboard 搜索
        try:
            url = f"{self.ZKILL_API}/corporationID/{name}/"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0:
                    return {"id": data[0].get("corporationID"), "name": name, "type": "corporation"}
        except Exception as e:
            logger.debug(f"zKillboard 搜索失败: {e}")
        
        return None
    
    def search_alliance(self, name: str) -> Optional[dict]:
        """搜索联盟"""
        try:
            url = f"{self.FUZZWORK_API}/allianceID.php"
            params = {"alliance": name}
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200 and resp.text.strip():
                ally_id = resp.text.strip()
                if ally_id.isdigit():
                    return {"id": int(ally_id), "name": name, "type": "alliance"}
        except Exception as e:
            logger.debug(f"fuzzwork 搜索联盟失败: {e}")
        return None
    
    def search_character(self, name: str) -> Optional[dict]:
        """搜索玩家角色"""
        try:
            url = f"{self.FUZZWORK_API}/characterID.php"
            params = {"character": name}
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200 and resp.text.strip():
                char_id = resp.text.strip()
                if char_id.isdigit():
                    return {"id": int(char_id), "name": name, "type": "character"}
        except Exception as e:
            logger.debug(f"fuzzwork 搜索角色失败: {e}")
        return None

    # ==================== 击杀邮件处理 ====================
    
    def check_new_killmails(self):
        if not self.subscriptions:
            return
        
        killmails = self.fetch_killmails(limit=30)
        if not killmails:
            return
        
        for killmail in killmails:
            kill_id = str(killmail.get("killmail_id"))
            
            if kill_id in self.pushed_kills:
                continue
            
            matched_groups = self.match_subscriptions(killmail)
            
            if matched_groups:
                logger.info(f"击杀 {kill_id} 匹配到群组 {matched_groups}")
                self.pushed_kills.add(kill_id)
                
                if len(self.pushed_kills) > 5000:
                    self.pushed_kills.clear()
                
                for group_id in matched_groups:
                    self.send_notification(group_id, killmail)
    
    def match_subscriptions(self, killmail: dict) -> Set[str]:
        matched_groups = set()
        
        victim = killmail.get("victim", {})
        victim_char_id = victim.get("character_id")
        victim_corp_id = victim.get("corporation_id")
        victim_alliance_id = victim.get("alliance_id")
        
        attackers = killmail.get("attackers", [])
        attacker_char_ids = set()
        attacker_corp_ids = set()
        attacker_alliance_ids = set()
        
        for attacker in attackers:
            if attacker.get("character_id"):
                attacker_char_ids.add(attacker.get("character_id"))
            if attacker.get("corporation_id"):
                attacker_corp_ids.add(attacker.get("corporation_id"))
            if attacker.get("alliance_id"):
                attacker_alliance_ids.add(attacker.get("alliance_id"))
        
        kill_value = killmail.get("zkb", {}).get("totalValue", 0)
        
        for group_id, subs in self.subscriptions.items():
            matched = False
            
            for sub in subs:
                if sub == "high_value" and kill_value >= self.HIGH_VALUE_THRESHOLD:
                    matched = True
                    break
                
                if ":" not in sub:
                    continue
                
                sub_type, sub_id = sub.split(":", 1)
                sub_id_int = int(sub_id)
                
                if sub_type == "corp":
                    if victim_corp_id == sub_id_int or sub_id_int in attacker_corp_ids:
                        matched = True
                        break
                elif sub_type == "alliance":
                    if victim_alliance_id == sub_id_int or sub_id_int in attacker_alliance_ids:
                        matched = True
                        break
                elif sub_type == "player":
                    if victim_char_id == sub_id_int or sub_id_int in attacker_char_ids:
                        matched = True
                        break
                elif sub_type == "player_kill":
                    if sub_id_int in attacker_char_ids:
                        matched = True
                        break
                elif sub_type == "player_loss":
                    if victim_char_id == sub_id_int:
                        matched = True
                        break
            
            if matched:
                matched_groups.add(group_id)
        
        return matched_groups
    
    def send_notification(self, group_id: str, killmail: dict):
        """发送击杀通知"""
        kill_id = killmail.get("killmail_id")
        victim = killmail.get("victim", {})
        
        victim_name = victim.get("character_name", "未知")
        victim_ship = victim.get("ship_type_name", "未知")
        victim_corp = victim.get("corporation_name", "未知")
        victim_alliance = victim.get("alliance_name")
        
        kill_value = killmail.get("zkb", {}).get("totalValue", 0)
        
        attackers = killmail.get("attackers", [])
        main_killer = attackers[0] if attackers else {}
        killer_name = main_killer.get("character_name", "未知")
        killer_ship = main_killer.get("ship_type_name", "未知")
        
        solar_system = killmail.get("solar_system", {})
        system_name = solar_system.get("name", "未知")
        sec_status = solar_system.get("security_status", 0)
        
        if sec_status >= 0.5:
            sec_type = "高安"
        elif sec_status > 0:
            sec_type = "低安"
        else:
            sec_type = "00区"
        
        message_lines = []
        
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
            f"💰 **价值**: {kill_value:,.2f} ISK",
            "",
            f"📍 **星系**: {system_name} ({sec_type})",
            "",
            f"🔗 **详情**: {self.ZKILL_URL}/{kill_id}/"
        ])
        
        message = "\n".join(message_lines)
        
        logger.info(f"准备发送消息到群组 {group_id}")
        
        import asyncio
        asyncio.create_task(self._send_message(group_id, message))
    
    async def _send_message(self, group_id: str, message: str):
        try:
            group_id_int = int(group_id) if group_id != "default" else None
            
            if group_id_int:
                await self.context.send_group_message(group_id_int, message)
                logger.info(f"消息已发送到群组 {group_id_int}")
            else:
                logger.info(f"[模拟发送] {message[:100]}...")
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    # ==================== 命令实现 ====================
    
    @filter.command(".sub")
    async def subscribe(self, event: AstrMessageEvent):
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
                ".sub high_value       - 订阅高价值击杀(10B+)\n"
                ".sub list             - 查看订阅\n"
                ".sub clear            - 清空订阅\n\n"
                "💡 使用 .search [名称] 搜索获取ID"
            )
            return
        
        group_id = str(event.group_id) if event.group_id else str(event.get_session_id())
        
        if group_id not in self.subscriptions:
            self.subscriptions[group_id] = set()
        
        command = parts[1].lower()
        subs = self.subscriptions[group_id]
        
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
        
        if command == "clear":
            self.subscriptions[group_id].clear()
            yield event.plain_result("✅ 已清空所有订阅")
            return
        
        if command == "high_value":
            subs.add("high_value")
            yield event.plain_result(f"✅ 已订阅高价值击杀 (≥ {self.HIGH_VALUE_THRESHOLD:,} ISK)")
            return
        
        if len(parts) < 3:
            yield event.plain_result(f"用法: .sub {command} [ID]")
            return
        
        entity_id = parts[2]
        
        if not entity_id.isdigit():
            yield event.plain_result("❌ ID必须是数字，使用 .search [名称] 搜索获取ID")
            return
        
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
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .unsub corp/alliance/player/player_kill/player_loss/high_value [ID]")
            return
        
        group_id = str(event.group_id) if event.group_id else str(event.get_session_id())
        
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
        """搜索军团/联盟/玩家
        
        用法: .search [名称]
        示例: .search Goonswarm
        """
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .search [名称]\n例如: .search Goonswarm\n\n💡 支持中文名称搜索，如 .search 熊猫人")
            return
        
        search_name = " ".join(parts[1:])
        yield event.plain_result(f"🔍 正在搜索: {search_name}...")
        
        results = []
        
        # 1. 搜索军团
        corp = self.search_corporation(search_name)
        if corp:
            results.append(corp)
        
        # 2. 搜索联盟
        alliance = self.search_alliance(search_name)
        if alliance:
            results.append(alliance)
        
        # 3. 搜索角色
        character = self.search_character(search_name)
        if character:
            results.append(character)
        
        if not results:
            yield event.plain_result(
                f"❌ 未找到: {search_name}\n\n"
                "可能的原因:\n"
                "1. 名称拼写错误\n"
                "2. 使用英文名称搜索\n"
                "3. 尝试搜索知名军团如: Goonswarm, Pandemic, Fraternity\n\n"
                "💡 提示: 也可以从 https://zkillboard.com 搜索获取ID"
            )
            return
        
        result_lines = [f"🔍 搜索结果: {search_name}", ""]
        
        for r in results:
            if r["type"] == "corporation":
                result_lines.append(f"🏢 **军团**: {r['name']}")
                result_lines.append(f"   ID: {r['id']}")
                result_lines.append(f"   订阅: .sub corp {r['id']}")
            elif r["type"] == "alliance":
                result_lines.append(f"🌟 **联盟**: {r['name']}")
                result_lines.append(f"   ID: {r['id']}")
                result_lines.append(f"   订阅: .sub alliance {r['id']}")
            elif r["type"] == "character":
                result_lines.append(f"👤 **角色**: {r['name']}")
                result_lines.append(f"   ID: {r['id']}")
                result_lines.append(f"   订阅: .sub player {r['id']}")
            result_lines.append("")
        
        result_lines.append("💡 使用 .sub [类型] [ID] 开始订阅")
        
        yield event.plain_result("\n".join(result_lines))
    
    @filter.command(".killinfo")
    async def kill_info(self, event: AstrMessageEvent):
        content = event.message_str.strip()
        parts = content.split()
        
        if len(parts) < 2:
            yield event.plain_result("用法: .killinfo [击杀ID]\n例如: .killinfo 12345678")
            return
        
        kill_id = parts[1]
        yield event.plain_result(f"🔗 查看详情: {self.ZKILL_URL}/{kill_id}/")

    @filter.command(".test")
    async def test(self, event: AstrMessageEvent):
        """测试插件是否正常工作"""
        yield event.plain_result(
            "✅ 击杀邮件插件正常运行！\n\n"
            "📋 可用命令:\n"
            ".search [名称] - 搜索军团/联盟/玩家\n"
            ".sub corp/alliance/player [ID] - 订阅\n"
            ".sub list - 查看订阅\n"
            ".sub clear - 清空订阅\n"
            ".killinfo [ID] - 查看击杀详情\n\n"
            "💡 快速开始: .search Goonswarm"
        )
