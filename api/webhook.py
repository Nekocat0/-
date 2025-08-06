from http.server import BaseHTTPRequestHandler
import json
import hmac
import hashlib
import os
import requests
import re
import time
import traceback

# 环境变量检查
SECRET_TOKEN = os.environ.get('GITHUB_WEBHOOK_SECRET')
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

if not (SECRET_TOKEN and BOT_TOKEN and CHAT_ID):
    raise RuntimeError("关键环境变量缺失或为空值")

MAX_CONTENT_LENGTH = 1024 * 1024  # 1MB
ANY_KERNEL_PATTERN = re.compile(r'any[\s_-]?kernel3?', re.IGNORECASE)
TELEGRAM_API_DELAY = 1  # 文件发送间隔(秒)

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # 增强路径验证 (支持/api/webhook和/webhook)
        valid_paths = ["/api/webhook", "/webhook"]
        if self.path not in valid_paths:
            error_msg = f"路径无效: {self.path}。期望路径: {', '.join(valid_paths)}"
            self.send_error(404, error_msg)
            print(f"❌ {error_msg}")
            return

        # 安全验证
        signature_header = self.headers.get('X-Hub-Signature-256')
        if not signature_header:
            self.send_error(403, "缺少签名")
            return

        # 内容长度检查
        content_length = int(self.headers['Content-Length'])
        if content_length > MAX_CONTENT_LENGTH:
            self.send_error(413, "请求体过大")
            return
            
        body = self.rfile.read(content_length)
        expected_signature = 'sha256=' + hmac.new(
            SECRET_TOKEN.encode(), body, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature_header, expected_signature):
            print(f"❌ 签名无效. 收到: {signature_header} 预期: {expected_signature}")
            self.send_error(403, "签名无效")
            return

        try:
            data = json.loads(body)
            
            if data.get('action') == 'published':
                repo = data['repository']
                release = data['release']
                sender = data['sender']
                assets = release.get('assets', [])
                
                print(f"📦 收到Release事件: {repo['full_name']} v{release['tag_name']}")

                # 发送基础通知
                message = (
                    f"🔔 **新版本发布通知**\n\n"
                    f"📦 仓库: [{repo['full_name']}]({repo['html_url']})\n"
                    f"🏷 版本: [{release['tag_name']}]({release['html_url']}) - {release.get('name', '')}\n"
                    f"👤 发布者: [{sender['login']}]({sender['html_url']})\n"
                    f"📅 发布时间: {release['published_at']}\n\n"
                    f"{release.get('body', '')[:300]}..."
                )
                self.send_telegram_message(message)
                
                # 处理附件
                anykernel_assets = [
                    asset for asset in assets 
                    if asset.get('name') and ANY_KERNEL_PATTERN.search(asset['name'])
                ]
                
                print(f"🔍 发现{len(anykernel_assets)}个匹配附件")
                
                if anykernel_assets:
                    small_files = []
                    large_files = []
                    
                    for asset in anykernel_assets:
                        asset_url = asset['browser_download_url']
                        asset_name = asset['name']
                        asset_size = asset.get('size')
                        
                        if asset_size and asset_size <= 20 * 1024 * 1024:  # 20MB限制
                            small_files.append((asset_url, asset_name))
                            print(f"📦 准备发送小文件: {asset_name}")
                        else:
                            size_desc = f"{asset_size/(1024*1024):.1f}MB" if asset_size else "大小未知"
                            print(f"⚠️ 文件过大({size_desc}): {asset_name}")
                            large_files.append(asset)
                    
                    # 同步发送小文件 (Vercel环境适配)
                    for file_url, file_name in small_files:
                        try:
                            print(f"🚀 发送文件中: {file_name}")
                            self.send_telegram_document(file_url, file_name)
                            time.sleep(TELEGRAM_API_DELAY)  # 避免速率限制
                        except Exception as e:
                            print(f"❌ 文件发送失败: {file_name} - {str(e)}")
                    
                    # 处理大文件
                    if large_files:
                        large_files_msg = "📦 大文件下载:\n" + "\n".join(
                            f"- [`{self.safe_markdown(f['name'])}`]({f['browser_download_url']})"
                            for f in large_files
                        )
                        self.send_telegram_message(large_files_msg)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        except Exception as e:
            print(f"❌ 处理错误: {str(e)}")
            traceback.print_exc()
            self.send_error(500, f"服务器内部错误: {str(e)}")

    def safe_markdown(self, text):
        """安全处理Markdown特殊字符"""
        return (
            text.replace('`', "'")
                .replace('*', '×')
                .replace('[', '(')
                .replace(']', ')')
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
            print("✅ 消息发送成功")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ 消息发送失败: {str(e)}")
            if hasattr(e, 'response'):
                print(f"📄 响应详情: {e.response.status_code} {e.response.text}")
            return False

    def send_telegram_document(self, file_url, file_name):
        """发送文件到Telegram"""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        safe_name = self.safe_markdown(file_name)
        
        payload = {
            'chat_id': CHAT_ID,
            'document': file_url,
            'caption': f"`{safe_name}`",
            'parse_mode': 'Markdown',
            'disable_notification': True
        }
        
        try:
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            print(f"✅ 文件发送成功: {file_name}")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ 文件发送失败: {file_name} - {str(e)}")
            if hasattr(e, 'response'):
                print(f"📄 响应详情: {e.response.status_code} {e.response.text}")
            
            # 降级方案：发送下载链接
            fallback_msg = (
                f"⚠️ 文件上传失败，请手动下载:\n"
                f"`{safe_name}`\n"
                f"[下载链接]({file_url})"
            )
            return self.send_telegram_message(fallback_msg)
