from http.server import BaseHTTPRequestHandler
import json
import hmac
import hashlib
import os
import requests
import re
import threading
import traceback  # 添加堆栈跟踪

# 严格的环境变量检查
SECRET_TOKEN = os.environ.get('GITHUB_WEBHOOK_SECRET')
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

if not (SECRET_TOKEN and BOT_TOKEN and CHAT_ID):
    raise RuntimeError("关键环境变量缺失或为空值")

MAX_CONTENT_LENGTH = 1024 * 1024  # 1MB
ANY_KERNEL_PATTERN = re.compile(r'any[\s_-]?kernel3?', re.IGNORECASE)

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # ========== 修复路径验证 ========== #
        # 允许 /api/webhook 和 /webhook 两种路径
        valid_paths = ["/api/webhook", "/webhook"]
        if self.path not in valid_paths:
            self.send_error(404, f"路径无效: {self.path}。期望路径: {', '.join(valid_paths)}")
            print(f"❌ 无效路径请求: {self.path}")
            return
        # ================================ #
        
        # 安全签名验证
        signature_header = self.headers.get('X-Hub-Signature-256')
        if not signature_header:
            self.send_error(403, "缺少签名")
            return

        # 内容长度限制
        content_length = int(self.headers['Content-Length'])
        if content_length > MAX_CONTENT_LENGTH:
            self.send_error(413, "请求体过大")
            return
            
        body = self.rfile.read(content_length)
        expected_signature = 'sha256=' + hmac.new(
            SECRET_TOKEN.encode(), body, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature_header, expected_signature):
            print(f"签名无效. 收到: {signature_header} 预期: {expected_signature}")
            self.send_error(403, "签名无效")
            return

        # 核心数据处理
        try:
            data = json.loads(body)
            
            if data.get('action') == 'published':
                repo = data['repository']
                release = data['release']
                sender = data['sender']
                assets = release.get('assets', [])
                
                # 详细日志记录
                print(f"📦 收到Release事件: {repo['full_name']} v{release['tag_name']}")
                
                # 构建基础通知消息
                message = (
                    f"🔔 **新版本发布通知**\n\n"
                    f"📦 仓库: [{repo['full_name']}]({repo['html_url']})\n"
                    f"🏷 版本: [{release['tag_name']}]({release['html_url']}) - {release.get('name', '')}\n"
                    f"👤 发布者: [{sender['login']}]({sender['html_url']})\n"
                    f"📅 发布时间: {release['published_at']}\n\n"
                    f"{release.get('body', '')[:300]}..."
                )
                
                # 发送基础通知
                self.send_telegram_message(message)
                
                # 智能匹配附件
                anykernel_assets = [
                    asset for asset in assets 
                    if asset.get('name') and ANY_KERNEL_PATTERN.search(asset['name'])
                ]
                
                print(f"🔍 发现{len(anykernel_assets)}个匹配附件")
                
                if anykernel_assets:
                    large_files = []
                    
                    for asset in anykernel_assets:
                        asset_url = asset['browser_download_url']
                        asset_name = asset['name']
                        asset_size = asset.get('size')
                        
                        # 大小判断处理
                        if asset_size and asset_size <= 20 * 1024 * 1024:  # 20MB限制
                            # 异步发送避免超时
                            thread = threading.Thread(
                                target=self.send_telegram_document,
                                args=(asset_url, asset_name)
                            )
                            thread.start()
                        else:
                            large_files.append(asset)
                    
                    # 智能聚合大文件消息
                    if large_files:
                        large_files_msg = "📦 大文件下载:\n" + "\n".join(
                            f"- [`{self.safe_markdown(f['name'])}`]({f['browser_download_url']})"
                            for f in large_files
                        )
                        self.send_telegram_message(large_files_msg)

            # 成功响应GitHub
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        except Exception as e:
            # 添加详细错误日志
            print(f"❌ 处理错误: {str(e)}")
            traceback.print_exc()  # 打印完整堆栈跟踪
            self.send_error(500, f"服务器内部错误: {str(e)}")

    def safe_markdown(self, text):
        """确保文本安全嵌入Markdown"""
        return (
            text.replace('`', "'")
                .replace('*', '×')
        )
    
    def send_telegram_message(self, text):
        """发送文本消息到Telegram"""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': CHAT_ID,
            'text': text,
            'parse_mode': 'Markdown',
            'disable_web_page_preview': True
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            print("✅ Telegram消息发送成功")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ Telegram消息发送失败: {str(e)}")
            if hasattr(e, 'response') and e.response:
                print(f"📄 API响应: {e.response.status_code} {e.response.text}")
            return False

    def send_telegram_document(self, file_url, file_name):
        """发送文件到Telegram"""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        
        # 安全处理特殊字符
        safe_name = self.safe_markdown(file_name)
        caption = f"`{safe_name}`"
        
        payload = {
            'chat_id': CHAT_ID,
            'document': file_url,
            'caption': caption,
            'parse_mode': 'Markdown',
            'disable_notification': True
        }
        
        try:
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            print(f"📤 文件发送成功: {file_name}")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ 文件发送失败: {file_name} - {str(e)}")
            if hasattr(e, 'response') and e.response:
                print(f"📄 API响应: {e.response.status_code} {e.response.text}")
                
            # 优雅降级
            fallback_msg = (
                f"⚠️ 文件上传失败，请手动下载:\n"
                f"`{safe_name}`\n"
                f"[下载链接]({file_url})"
            )
            return self.send_telegram_message(fallback_msg)
