# 导入我们需要的工具
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

# 这是我们处理请求的核心类
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # --- 安全检查部分 ---
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
                repo_name = data['repository']['full_name']
                release_tag = data['release']['tag_name']
                release_name = data['release']['name'] or 'N/A'
                release_url = data['release']['html_url']
                releaser_name = data['sender']['login']

                # 构造并发送文本消息
                message = (
                    f"🔔 **叮咚！主人，项目有新动态啦！**\n\n"
                    f"🐾 **仓库:** `{repo_name}`\n"
                    f"✨ **版本:** `{release_tag}` - {release_name}\n"
                    f"👤 **发布者:** {releaser_name}\n\n"
                    f"快去看看有什么新内容吧：\n[点我直达]({release_url})"
                )
                self.send_telegram_message(message)

                # 检查并发送附件
                assets = data['release'].get('assets', [])
                if assets:
                    print(f"Found {len(assets)} assets. Checking for 'Anykernel3'...")
                    for asset in assets:
                        file_name = asset['name']
                        
                        # ✨✨✨ 重点在这里！添加文件名检查守卫 ✨✨✨
                        if 'anykernel3' in file_name.lower():
                            print(f"Found matching asset: {file_name}")
                            file_url = asset['browser_download_url']
                            file_size_mb = asset['size'] / (1024 * 1024)
                            caption = (
                                f"📄 **附件:** `{file_name}`\n"
                                f"📦 **大小:** `{file_size_mb:.2f} MB`\n"
                                f"🔑 **类型:** `Anykernel3`"
                            )
                            
                            # 检查文件大小
                            if asset['size'] > 50 * 1024 * 1024:
                                print(f"Skipping asset {file_name} due to size limit.")
                                self.send_telegram_message(f"🥺 文件 `{file_name}` 太大了（超过50MB），无法直接推送，请主人手动下载哦。")
                                continue

                            # 发送文件
                            self.send_telegram_document(file_url, caption)
                        else:
                            # 如果文件名不匹配，就在日志里说一声，然后跳过
                            print(f"Skipping asset: {file_name} (does not contain 'Anykernel3')")

                else:
                    print("No assets found in this release.")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        except Exception as e:
            print(f"Error: {e}")
            self.send_error(500, "Internal Server Error")

    def send_telegram_message(self, text):
        # (此函数保持不变)
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            print("Telegram text message sent successfully!")
        except requests.exceptions.RequestException as e:
            print(f"Failed to send Telegram message: {e}")

    def send_telegram_document(self, document_url, caption):
        # (此函数保持不变)
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        payload = {'chat_id': CHAT_ID, 'document': document_url, 'caption': caption, 'parse_mode': 'Markdown'}
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            print(f"Telegram document sent successfully: {document_url}")
        except requests.exceptions.RequestException as e:
            print(f"Failed to send Telegram document: {e}")
