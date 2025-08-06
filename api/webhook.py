# 导入我们需要的魔法工具
from http.server import BaseHTTPRequestHandler
import json
import hmac
import hashlib
import os
import requests # 用来发消息的工具

# 从 Vercel 的环境变量里安全地取出我们的秘密信息
# 这样就不会把 Token 直接写在代码里，非常安全！
SECRET_TOKEN = os.environ.get('GITHUB_WEBHOOK_SECRET')
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# 这是我们处理请求的核心类
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # --- 第一关：安全检查！---
        # 确保是 GitHub 官方发来的请求，而不是坏人伪造的
        signature_header = self.headers.get('X-Hub-Signature-256')
        if not signature_header:
            self.send_error(403, "Missing signature")
            return

        # 读取请求的内容
        content_length = int(self.headers['Content-Length'])
        body = self.rfile.read(content_length)

        # 用我们预设的 Secret Token 计算一个签名
        expected_signature = 'sha256=' + hmac.new(SECRET_TOKEN.encode(), body, hashlib.sha256).hexdigest()

        # 对比 GitHub 发来的签名和我们自己计算的是否一致
        if not hmac.compare_digest(signature_header, expected_signature):
            self.send_error(403, "Invalid signature")
            return

        # --- 第二关：解析数据 & 发送通知 ---
        try:
            data = json.loads(body)

            # 我们只关心“新版本发布(published)”这个动作
            if data.get('action') == 'published':
                # 从数据中提取我们需要的信息
                repo_name = data['repository']['full_name']
                release_tag = data['release']['tag_name']
                release_name = data['release']['name']
                release_url = data['release']['html_url']
                releaser_name = data['sender']['login']

                # 拼接一条可爱的通知消息！(主人可以随意修改这里的格式)
                message = (
                    f"🔔 **叮咚！主人，项目有新动态啦！**\n\n"
                    f"🐾 **仓库:** `{repo_name}`\n"
                    f"✨ **版本:** `{release_tag}` - {release_name}\n"
                    f"👤 **发布者:** {releaser_name}\n\n"
                    f"快去看看有什么新内容吧：\n[点我直达]({release_url})"
                )

                # 调用函数，把消息发送到 Telegram
                self.send_telegram_message(message)

            # 告诉 GitHub：“我收到啦，处理得很成功！”
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        except Exception as e:
            # 如果中间出了任何问题，记录下来并告诉 GitHub 处理失败
            print(f"Error: {e}")
            self.send_error(500, "Internal Server Error")

    def send_telegram_message(self, text):
        """专门负责发送消息到 Telegram 的函数"""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': CHAT_ID,
            'text': text,
            'parse_mode': 'Markdown' # 让消息支持加粗、链接等格式
        }
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status() # 如果发送失败会报错
            print("Telegram notification sent successfully!")
        except requests.exceptions.RequestException as e:
            print(f"Failed to send Telegram message: {e}")
