# webhook.py
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import hmac
import hashlib
import os
import requests
import re
import time
import traceback
import io

# -------------------------
# 配置与环境变量
# -------------------------
SECRET_TOKEN = os.environ.get('GITHUB_WEBHOOK_SECRET')
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
# 可选：用于通过 GitHub API 查询与下载 assets（推荐在 Vercel 环境变量中设置）
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')  # 可以使用 GITHUB_TOKEN 或 PAT

if not (SECRET_TOKEN and BOT_TOKEN and CHAT_ID):
    raise RuntimeError("关键环境变量缺失: 请设置 GITHUB_WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")

# 常量
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB for webhook payload （防止超大）
ANY_KERNEL_PATTERN = re.compile(r'any.*kernel', re.IGNORECASE)
TELEGRAM_MAX_MESSAGE_LENGTH = 4000
MAX_RETRY_ATTEMPTS = 4
RETRY_DELAY = 2  # seconds
TELEGRAM_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB

# GitHub API headers (用于 fetch assets / 下载)
GITHUB_API_HEADERS = {
    'User-Agent': 'github-webhook-to-telegram',
    'Accept': 'application/vnd.github+json'
}
if GITHUB_TOKEN:
    GITHUB_API_HEADERS['Authorization'] = f'token {GITHUB_TOKEN}'


# -------------------------
# Handler
# -------------------------
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        valid_paths = ["/api/webhook", "/webhook"]
        if self.path not in valid_paths:
            self.send_error(404, f"路径无效: {self.path}")
            print(f"❌ 非法路径请求: {self.path}")
            return

        # 验证 X-Hub-Signature-256
        signature_header = self.headers.get('X-Hub-Signature-256')
        if not signature_header:
            self.send_error(403, "缺少签名")
            print("❌ 缺少 X-Hub-Signature-256 头")
            return

        # 检查长度
        try:
            content_length = int(self.headers.get('Content-Length', '0'))
        except:
            content_length = 0
        if content_length > MAX_CONTENT_LENGTH:
            self.send_error(413, "请求体过大")
            print("❌ webhook payload 过大")
            return

        body = self.rfile.read(content_length)
        expected_signature = 'sha256=' + hmac.new(SECRET_TOKEN.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature_header, expected_signature):
            print(f"❌ 签名无效. 收到: {signature_header}  预期: {expected_signature}")
            self.send_error(403, "签名无效")
            return

        try:
            data = json.loads(body)
        except Exception as e:
            print(f"❌ 无法解析 JSON: {e}")
            self.send_error(400, "无效的 JSON")
            return

        try:
            action = data.get('action')
            if action != 'published':
                # 仅关心 release published 事件（可根据需要拓展）
                print(f"ℹ️ 忽略 action: {action}")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'OK')
                return

            repo = data.get('repository', {})
            release = data.get('release', {})
            sender = data.get('sender', {})

            repo_full = repo.get('full_name', 'unknown/repo')
            tag_name = release.get('tag_name', 'unknown')
            release_id = release.get('id')
            print(f"📦 收到 Release published: {repo_full} {tag_name}")

            # 构建通知消息并发送（支持分段）
            message = (
                f"🔔 **新版本发布通知**\n\n"
                f"📦 仓库: [{repo_full}]({repo.get('html_url', '')})\n"
                f"🏷 版本: [{tag_name}]({release.get('html_url','')}) - {release.get('name','')}\n"
                f"👤 发布者: [{sender.get('login','')}]({sender.get('html_url','')})\n"
                f"📅 发布时间: {release.get('published_at','')}\n\n"
                f"{release.get('body','')}"
            )
            self.send_telegram_message_safe(message)

            # 关键改动：**不再读取 payload 中的 assets**，始终通过 GitHub API 查询 assets
            matched_asset = None
            assets = None

            for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
                print(f"🔄 (尝试 #{attempt}) 通过 GitHub API 查询 release assets...")
                api_assets = fetch_release_assets(repo_full, release_id=release_id, tag_name=tag_name)
                if api_assets is None:
                    print("⚠️ API 查询失败或无返回")
                    assets = []
                else:
                    assets = api_assets

                print(f"🔁 从 API 查询到 {len(assets)} 个 asset")

                # 打印调试信息
                for i, a in enumerate(assets):
                    an = a.get('name', '')
                    sz = a.get('size', 0)
                    print(f"  asset[{i}]: {an} ({sz//1024 if sz else '未知'} KB)")

                # 找到第一个匹配 ANY_KERNEL_PATTERN 的 asset
                for a in assets:
                    an = a.get('name', '')
                    if an and ANY_KERNEL_PATTERN.search(an):
                        matched_asset = a
                        print(f"🎯 匹配到 asset: {an}")
                        break

                if matched_asset:
                    break

                if attempt < MAX_RETRY_ATTEMPTS:
                    print(f"⏳ 未找到匹配 asset，等待 {RETRY_DELAY}s 后重试...")
                    time.sleep(RETRY_DELAY)

            # 处理匹配到的 asset
            if matched_asset:
                self.process_single_asset(matched_asset, repo_full, release)
            else:
                print("ℹ️ 最终未找到匹配附件")
                no_asset_msg = (
                    "⚠️ 未检测到内核刷机包附件\n\n"
                    "这可能是由于：\n"
                    "1. 附件尚未完成上传（GitHub 延迟）\n"
                    "2. 附件名称不符合模式\n"
                    "3. 发布未包含内核刷机包\n\n"
                    "请检查 GitHub 发布页面：\n"
                    f"[{tag_name} 发布页面]({release.get('html_url','')})"
                )
                self.send_telegram_message(no_asset_msg)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        except Exception as e:
            print(f"❌ 处理错误: {e}")
            traceback.print_exc()
            self.send_error(500, f"服务器内部错误: {e}")

    # -------------------------
    # 处理单个 asset
    # -------------------------
    def process_single_asset(self, asset, repo_full, release):
        asset_name = asset.get('name')
        asset_size = asset.get('size')
        asset_browser_url = asset.get('browser_download_url')
        asset_api_url = asset.get('url')  # API 下载地址： /repos/:owner/:repo/releases/assets/:asset_id

        print(f"🔍 处理匹配附件: {asset_name}")
        if asset_size:
            print(f"  大小: {asset_size/(1024*1024):.2f} MB")
        print(f"  浏览器下载链接: {asset_browser_url}")
        print(f"  API 链接: {asset_api_url}")

        # 尝试通过 API 下载（优先），若失败回退到 browser_download_url
        content_bytes = None
        try:
            content_bytes = download_asset_content(asset)
        except Exception as e:
            print(f"❌ 通过 API 下载 asset 失败: {e}")
            content_bytes = None

        # 如果用 API 下载失败但 browser_download_url 存在，则尝试直接下载
        if content_bytes is None and asset_browser_url:
            try:
                print("⬇️ 回退到 browser_download_url 下载（可能需要公有仓库或 token）")
                r = requests.get(asset_browser_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
                r.raise_for_status()
                content_bytes = r.content
                r.close()
            except Exception as e:
                print(f"❌ 回退下载失败: {e}")
                content_bytes = None

        # 检查大小并上传到 Telegram（并添加简单描述）
        if content_bytes:
            size_b = len(content_bytes)
            if size_b <= TELEGRAM_MAX_UPLOAD_BYTES:
                try:
                    # 简单描述：release tag + asset name
                    description = f"{release.get('tag_name','')} 的附件：{asset_name}"
                    ok = send_telegram_document_bytes(asset_name, content_bytes, description)
                    if ok:
                        print("✅ asset 已成功上传到 Telegram")
                        return
                    else:
                        print("⚠️ 上传到 Telegram 返回失败")
                except Exception as e:
                    print(f"❌ 上传到 Telegram 发生异常: {e}")
            else:
                print(f"⚠️ 文件过大，无法通过 Telegram 上传: {size_b/(1024*1024):.2f}MB")

        # 如果到这里仍然没有成功，发送 fallback 消息包含下载链接
        fallback_msg = (
            f"⚠️ 无法自动上传附件: `{self.safe_markdown(asset_name or '未知')}`\n\n"
            f"请手动下载：\n{asset_browser_url or asset.get('url')}"
        )
        self.send_telegram_message(fallback_msg)

    # -------------------------
    # 文本发送与分段
    # -------------------------
    def send_telegram_message_safe(self, text):
        if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return self.send_telegram_message(text)
        print(f"⚠️ 文本过长({len(text)}字符)，将分段发送")
        messages = []
        current = ""
        for line in text.splitlines():
            if len(current) + len(line) + 1 <= TELEGRAM_MAX_MESSAGE_LENGTH:
                current += line + "\n"
            else:
                if current:
                    messages.append(current.strip())
                if len(line) > TELEGRAM_MAX_MESSAGE_LENGTH:
                    # 强制切分
                    chunks = [line[i:i+TELEGRAM_MAX_MESSAGE_LENGTH] for i in range(0, len(line), TELEGRAM_MAX_MESSAGE_LENGTH)]
                    messages.extend(chunks)
                    current = ""
                else:
                    current = line + "\n"
        if current.strip():
            messages.append(current.strip())
        for i, msg in enumerate(messages):
            prefix = f"📄 消息分段 ({i+1}/{len(messages)})\n\n" if len(messages) > 1 else ""
            self.send_telegram_message(prefix + msg)
            time.sleep(0.3)
        return True

    def safe_markdown(self, text):
        if not text:
            return ""
        return text.replace('`', "'").replace('*', '×').replace('[', '(').replace(']', ')')

    def send_telegram_message(self, text):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': CHAT_ID,
            'text': text,
            'parse_mode': 'Markdown',
            'disable_web_page_preview': True
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
            print(f"✅ 发送消息成功 ({len(text)} 字符)")
            return True
        except requests.exceptions.RequestException as e:
            print(f"❌ 发送消息失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"  响应: {e.response.status_code} {e.response.text}")
            return False


# -------------------------
# 辅助函数（类外，以便复用）
# -------------------------
def fetch_release_assets(repo_full_name, release_id=None, tag_name=None):
    """
    使用 GitHub API 获取 release 的 assets 列表。
    优先使用 release_id，如果没有则使用 tag_name。
    返回 assets 列表（或空列表），出错时返回 None。
    """
    try:
        if release_id:
            url = f"https://api.github.com/repos/{repo_full_name}/releases/{release_id}"
        elif tag_name:
            url = f"https://api.github.com/repos/{repo_full_name}/releases/tags/{tag_name}"
        else:
            print("❌ fetch_release_assets: 既没有 release_id 也没有 tag_name")
            return None

        resp = requests.get(url, headers=GITHUB_API_HEADERS, timeout=10)
        resp.raise_for_status()
        rel = resp.json()
        return rel.get('assets', [])
    except Exception as e:
        print(f"❌ fetch_release_assets 异常: {e}")
        return None


def download_asset_content(asset):
    """
    优先使用 asset['url']（API 地址）并加 Accept: application/octet-stream 来下载二进制内容
    如果使用 API 下载需要 Authorization（如果仓库是私有或 token 必要）
    成功返回 bytes，失败抛出异常
    """
    asset_api_url = asset.get('url')
    asset_browser_url = asset.get('browser_download_url')
    headers = GITHUB_API_HEADERS.copy()
    # 当使用 assets API 直接获取二进制时需 Accept header
    headers['Accept'] = 'application/octet-stream'
    try:
        if asset_api_url:
            print(f"⬇️ 使用 API 下载 asset: {asset_api_url}")
            resp = requests.get(asset_api_url, headers=headers, timeout=60, stream=True)
            resp.raise_for_status()
            content = resp.content
            resp.close()
            return content
        elif asset_browser_url:
            print(f"⬇️ 使用 browser_download_url 下载 asset: {asset_browser_url}")
            resp = requests.get(asset_browser_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
            resp.raise_for_status()
            content = resp.content
            resp.close()
            return content
        else:
            raise RuntimeError("asset 不包含可下载的 url")
    except Exception as e:
        raise


def send_telegram_document_bytes(filename, content_bytes, caption):
    """
    把 bytes 内容作为文件上传到 Telegram（caption 为简短描述）
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    files = {
        'document': (filename or 'file.bin', content_bytes)
    }
    # caption 长度受 Telegram 限制（约 1024 字符），这里发送简单描述
    data = {
        'chat_id': CHAT_ID,
        'caption': caption or '',
        'parse_mode': 'Markdown',
        'disable_notification': True
    }
    try:
        r = requests.post(url, files=files, data=data, timeout=60)
        r.raise_for_status()
        print("✅ sendDocument 成功")
        return True
    except requests.exceptions.RequestException as e:
        print(f"❌ sendDocument 失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"  响应: {e.response.status_code} {e.response.text}")
        return False


# -------------------------
# 可选的本地测试/运行入口（在 Vercel/服务器上运行时可以注释或留空）
# -------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    print(f"启动本地测试服务器: 0.0.0.0:{port}")
    server = HTTPServer(('0.0.0.0', port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
        print("服务器已停止")
