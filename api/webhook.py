# 导入我们需要的魔法工具
from http.server import BaseHTTPRequestHandler
import json
import hmac
import hashlib
import os
import requests

# 从 Vercel 的环境变量里安全地取出我们的秘密信息
SECRET_TOKEN = os.environ.get('GITHUB_WEBHOOK_SECRET')
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# MarkdownV2 转义函数 (我们依然在文本消息中保留它，以防万一)
def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in escape_chars else char for char in str(text))

# 这是我们处理请求的核心类
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # --- 安全检查部分 (保持不变) ---
        # (这部分代码没有变化)
        signature_header = self.headers.get('X-Hub-Signature-256')
        if not signature_header:
            self.send_error(403, "Missing signature")
            return
        
        content_length = int(self.headers['Content-Length'])
        body = self.rfile.read(content_length)
        
        expected_signature = 'sha256=' + hmac.new(SECRET_TOKEN.encode(), body, hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(signature_header, expected_signature):
            self.send_error(403, "Invalid signature")
            return
            
        # --- 解析数据 & 发送通知 ---
        try:
            data = json.loads(body)

            if data.get('action') == 'published':
                # 提取信息
                release_url = data['release']['html_url']
                
                # 为了安全，我们对所有用于Markdown的文本进行转义
                repo_name = escape_markdown(data['repository']['full_name'])
                release_tag = escape_markdown(data['release']['tag_name'])
                release_name = escape_markdown(data['release']['name'] or 'N/A')
                releaser_name = escape_markdown(data['sender']['login'])

                # 构造并发送文本消息 (使用 MarkdownV2)
                message = (
                    f"🔔 *叮咚！主人，项目有新动态啦！*\n\n"
                    f"🐾 *仓库:* `{repo_name}`\n"
                    f"✨ *版本:* `{release_tag}` - {release_name}\n"
                    f"👤 *发布者:* {releaser_name}\n\n"
                    f"快去看看有什么新内容吧：\n[点我直达]({release_url})"
                )
                self.send_telegram_message(message)

                # 检查并发送附件
                assets = data['release'].get('assets', [])
                if assets:
                    for asset in assets:
                        file_name = asset['name']
                        
                        if 'anykernel3' in file_name.lower():
                            print(f"Found matching asset: {file_name}")
                            file_url = asset['browser_download_url']
                            file_size_mb = asset['size'] / (1024 * 1024)
                            
                            # ✨✨✨ 终极修复：为文件说明(caption)使用纯文本，不再进行Markdown格式化 ✨✨✨
                            caption = (
                                f"📄 附件: {file_name}\n"
                                f"📦 大小: {file_size_mb:.2f} MB\n"
                                f"🔑 类型: Anykernel3"
                            )
                            
                            if asset['size'] > 50 * 1024 * 1024:
                                self.send_telegram_message(f"🥺 文件 `{escape_markdown(file_name)}` 太大了（超过50MB），无法直接推送，请主人手动下载哦。")
                                continue

                            # 调用发送文件的函数
                            self.send_telegram_document(file_url, caption)
                        else:
                            print(f"Skipping asset: {file_name} (does not contain 'Anykernel3')")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        except Exception as e:
            print(f"Error: {e}")
            self.send_error(500, "Internal Server Error")

    def send_telegram_message(self, text):
        """发送带MarkdownV2格式的文本消息"""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'MarkdownV2'}
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            print("Telegram text message sent successfully!")
        except requests.exceptions.RequestException as e:
            print(f"Failed to send Telegram message: {e}")

    def send_telegram_document(self, document_url, caption):
        """发送文件，附带纯文本说明"""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        # ✨✨✨ 核心改动：移除了 'parse_mode' 参数 ✨✨✨
        payload = {'chat_id': CHAT_ID, 'document': document_url, 'caption': caption}
        try:
            response = requests.post(url, json=payload, timeout=60) # 延长文件发送的超时时间
            response.raise_for_status()
            print(f"Telegram document sent successfully: {document_url}")
        except requests.exceptions.RequestException as e:
            print(f"Failed to send Telegram document: {e}")
