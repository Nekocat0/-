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
# 增强匹配模式 - 允许 "any" 和 "kernel" 之间有任意字符
ANY_KERNEL_PATTERN = re.compile(r'any.*kernel', re.IGNORECASE)
TELEGRAM_MAX_MESSAGE_LENGTH = 4000  # Telegram消息最大长度
MAX_RETRY_ATTEMPTS = 3  # 最大重试次数
RETRY_DELAY = 2  # 重试间隔（秒）

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
                
                print(f"📦 收到Release事件: {repo['full_name']} v{release['tag_name']}")
                
                # 发送基础通知
                message = (
                    f"🔔 **新版本发布通知**\n\n"
                    f"📦 仓库: [{repo['full_name']}]({repo['html_url']})\n"
                    f"🏷 版本: [{release['tag_name']}]({release['html_url']}) - {release.get('name', '')}\n"
                    f"👤 发布者: [{sender['login']}]({sender['html_url']})\n"
                    f"📅 发布时间: {release['published_at']}\n\n"
                    f"{release.get('body', '')}"  # 无字数限制
                )
                self.send_telegram_message_safe(message)
                
                # 智能附件检测（带重试机制）
                matched_asset = None
                assets = []
                
                for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
                    # 获取当前附件列表
                    assets = release.get('assets', [])
                    print(f"🔄 附件检测尝试 #{attempt}/{MAX_RETRY_ATTEMPTS}: 发现 {len(assets)} 个附件")
                    
                    # 打印所有附件详情（调试用）
                    for i, asset in enumerate(assets):
                        asset_name = asset.get('name', '')
                        asset_size = asset.get('size', 0)
                        print(f"  附件{i+1}: {asset_name} - {asset_size//1024}KB" if asset_size else f"  附件{i+1}: {asset_name} - 大小未知")
                    
                    # 查找匹配的附件 - 只取第一个匹配项
                    for asset in assets:
                        asset_name = asset.get('name', '')
                        if asset_name and ANY_KERNEL_PATTERN.search(asset_name):
                            print(f"🎯 匹配到附件: {asset_name}")
                            matched_asset = asset
                            break
                    
                    if matched_asset:
                        break  # 找到附件，退出重试循环
                    
                    # 如果还有重试机会，等待后重试
                    if attempt < MAX_RETRY_ATTEMPTS:
                        print(f"⏳ 未找到匹配附件，等待 {RETRY_DELAY} 秒后重试...")
                        time.sleep(RETRY_DELAY)
                
                if matched_asset:
                    self.process_single_asset(matched_asset)
                else:
                    print("ℹ️ 最终未找到匹配附件")
                    # 添加提示消息
                    no_asset_msg = (
                        "⚠️ 未检测到内核刷机包附件\n\n"
                        "这可能是由于：\n"
                        "1. 附件尚未完成上传（GitHub延迟）\n"
                        "2. 附件名称不符合模式\n"
                        "3. 发布未包含内核刷机包\n\n"
                        "请检查GitHub发布页面：\n"
                        f"[{release['tag_name']} 发布页面]({release['html_url']})"
                    )
                    self.send_telegram_message(no_asset_msg)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        except Exception as e:
            print(f"❌ 处理错误: {str(e)}")
            traceback.print_exc()
            self.send_error(500, f"服务器内部错误: {str(e)}")
    
    def process_single_asset(self, asset):
        """处理单个匹配的附件"""
        asset_url = asset['browser_download_url']
        asset_name = asset['name']
        asset_size = asset.get('size')
        
        # 详细日志
        print(f"🔍 处理附件: {asset_name}")
        print(f"  大小: {asset_size//1024}KB" if asset_size else "  大小: 未知")
        print(f"  下载链接: {asset_url}")
        
        # 检查文件大小是否在限制范围内
        if asset_size and asset_size <= 20 * 1024 * 1024:  # 20MB限制
            print(f"📦 准备发送文件: {asset_name}")
            try:
                # 使用简单描述而不是文件名
                description = "内核刷机包"
                self.send_telegram_document(asset_url, description)
            except Exception as e:
                print(f"❌ 文件发送失败: {asset_name} - {str(e)}")
                
                # 发送备用下载链接（仍然包含文件名）
                fallback_msg = (
                    f"⚠️ 文件上传失败，请手动下载:\n"
                    f"`{self.safe_markdown(asset_name)}`\n"
                    f"[下载链接]({asset_url})"
                )
                self.send_telegram_message(fallback_msg)
        else:
            size_desc = f"{asset_size/(1024*1024):.1f}MB" if asset_size else "大小未知"
            print(f"⚠️ 文件过大({size_desc}): {asset_name}")
            
            # 发送大文件下载链接（包含简单描述）
            large_file_msg = (
                f"📦 大文件下载 (内核刷机包):\n"
                f"[点击下载]({asset_url})"
            )
            self.send_telegram_message(large_file_msg)
    
    def send_telegram_message_safe(self, text):
        """智能处理超长消息的分段发送"""
        if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return self.send_telegram_message(text)
        
        print(f"⚠️ 消息过长({len(text)}字符)，将分段发送...")
        
        # 分段发送策略
        messages = []
        current_message = ""
        
        # 按行分割保持段落结构
        for line in text.split('\n'):
            # 如果当前行加入后不超过限制，添加该行
            if len(current_message) + len(line) + 1 <= TELEGRAM_MAX_MESSAGE_LENGTH:
                current_message += line + "\n"
            else:
                # 保存当前消息段
                if current_message:
                    messages.append(current_message.strip())
                
                # 如果单行就超过限制，进行强制分割
                if len(line) > TELEGRAM_MAX_MESSAGE_LENGTH:
                    chunks = [line[i:i+TELEGRAM_MAX_MESSAGE_LENGTH] for i in range(0, len(line), TELEGRAM_MAX_MESSAGE_LENGTH)]
                    messages.extend(chunks)
                    current_message = ""
                else:
                    current_message = line + "\n"
        
        # 添加最后一段
        if current_message.strip():
            messages.append(current_message.strip())
        
        # 发送所有分段
        for i, msg in enumerate(messages):
            prefix = f"📄 消息分段 ({i+1}/{len(messages)})\n\n" if len(messages) > 1 else ""
            self.send_telegram_message(prefix + msg)
            time.sleep(0.5)  # 短暂延迟避免速率限制

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
            print(f"✅ 消息发送成功 ({len(text)}字符)")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ 消息发送失败: {str(e)}")
            if hasattr(e, 'response'):
                print(f"📄 响应详情: {e.response.status_code} {e.response.text}")
            return False

    def send_telegram_document(self, file_url, description):
        """
        发送文件到Telegram - 使用简单描述
        """
        try:
            print(f"⬇️ 下载文件中...")
            
            # 设置浏览器User-Agent避免GitHub拦截
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            
            # 下载文件内容
            response = requests.get(file_url, headers=headers, timeout=20)
            response.raise_for_status()
            
            # 检查文件大小
            file_size = len(response.content)
            if file_size > 20 * 1024 * 1024:  # 20MB
                raise ValueError(f"文件大小超过20MB限制: {file_size/(1024*1024):.1f}MB")
            
            file_size_kb = file_size // 1024
            print(f"📥 下载完成 ({file_size_kb}KB)")
            
            # 准备上传到Telegram
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
            files = {'document': ('kernel_flash.zip', response.content)}  # 固定文件名
            data = {
                'chat_id': CHAT_ID,
                'caption': f"**{description}**",  # 使用简单描述
                'parse_mode': 'Markdown',
                'disable_notification': True
            }
            
            print(f"🚀 上传文件中...")
            upload_response = requests.post(url, files=files, data=data, timeout=30)
            upload_response.raise_for_status()
            
            print(f"✅ 文件发送成功")
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"❌ 文件发送失败 - {str(e)}")
            if hasattr(e, 'response') and e.response:
                print(f"📄 响应详情: {e.response.status_code} {e.response.text}")
            raise  # 抛出异常让上层处理
        
        except Exception as e:
            print(f"❌ 文件处理错误 - {str(e)}")
            raise  # 抛出异常让上层处理
        
        finally:
            # 确保资源释放
            if 'response' in locals():
                response.close()
