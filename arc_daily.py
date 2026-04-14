"""
Arc Network 每日积分自动化脚本（多账号版）
任务：
  1. Content 阅读 5 篇文章   +25 分
  2. Content 观看 1 个短视频  +5 分
  3. Events 注册新活动        +5 分/个
  4. Discussions 发 1 篇帖子  +10 分
  5. Discussions 评论 2 条帖子 +10 分

账号配置文件 accounts.txt（与本脚本同目录），每行一个账号：
  格式：邮箱----应用专用密码
  示例：
    alice@gmail.com----abcd efgh ijkl mnop
    bob@gmail.com----wxyz abcd efgh ijkl

代理配置文件 proxies.txt（与本脚本同目录），每行一个代理，与账号按行对应：
  支持格式：
    http://user:pass@host:port
    socks5://user:pass@host:port
    http://host:port            （无认证）
  示例：
    http://user1:pass1@1.2.3.4:8080
    socks5://user2:pass2@5.6.7.8:1080
    http://9.10.11.12:3128
  注意：
    - 行数必须与 accounts.txt 一致（一一对应）
    - 如果某行写 none 或留空，该账号不使用代理（不推荐）

Gmail 准备步骤（每个账号）：
  1. Google 账户 → 安全 → 两步验证（必须开启）
  2. Google 账户 → 安全 → 应用专用密码 → 生成 16 位密码
  3. Gmail 设置 → 转发和 POP/IMAP → 启用 IMAP

用法：
  # 首次部署（安装依赖 + Chromium + 配置 cron）：
  python arc_daily.py --setup

  # 每日执行（无头浏览器，cron 自动调用）：
  python arc_daily.py
"""

import asyncio
import sys
import os
import random
import imaplib
import email as email_lib
import re
import json
import logging
import time
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from playwright.async_api import async_playwright, Page, BrowserContext, Browser

# ─── 常量 ────────────────────────────────────────────────────────────────────
BASE_URL      = "https://community.arc.network"
SCRIPT_DIR    = Path(__file__).parent
LOG_DIR       = SCRIPT_DIR.parent / "security-reports"
ACCOUNTS_FILE     = SCRIPT_DIR / "accounts.txt"
GMAIL_PASSES_FILE = SCRIPT_DIR / "gmail_passes.txt"
PROXIES_FILE      = SCRIPT_DIR / "proxies.txt"
STATE_FILE    = SCRIPT_DIR / "arc_state.json"
SESSIONS_DIR  = SCRIPT_DIR / "sessions"   # 存放各账号的浏览器 session

LOG_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# ─── 发帖/评论文案库 ─────────────────────────────────────────────────────────
POST_TEMPLATES = [
    "What are the most exciting use cases you've seen being built on Arc recently? Would love to hear what the community is working on!",
    "For those building with Arc App Kits — what has your experience been like so far? Any tips for getting started quickly?",
    "Curious about the community's thoughts on stablecoin adoption trends. Are we seeing more real-world usage than last year?",
    "Has anyone attended recent Arc Office Hours? What topics came up that were most useful for builders?",
    "What tooling or documentation improvements would most help you as an Arc developer? Open to discussing pain points.",
    "How are developers handling cross-chain UX challenges when building on Arc? Would love to compare approaches.",
    "Are there any Arc community projects looking for contributors? Happy to help with testing or docs.",
    "What's your take on the role of stablecoins in DeFi liquidity? Curious how Arc builders are thinking about this.",
]

COMMENT_TEMPLATES = [
    "Great perspective, thanks for sharing this!",
    "Really useful to hear — appreciate you writing this up.",
    "This matches my experience too. Good to know others are thinking along the same lines.",
    "Interesting take. Have you explored how this might work at scale?",
    "Thanks for the detailed write-up — bookmarking this for reference.",
    "Solid question. I've been wondering about this as well.",
    "Appreciate the insight — this gives me a new angle to consider.",
    "Well said. The ecosystem definitely benefits from more discussions like this.",
]

# ─── 数据结构 ─────────────────────────────────────────────────────────────────
@dataclass
class Account:
    email: str
    app_pass: str
    proxy: str | None = None  # e.g. "http://user:pass@host:port" or "socks5://..."

@dataclass
class AccountResult:
    email: str
    score_before: int | None = None
    score_after: int | None = None
    tasks_done: dict = field(default_factory=dict)
    error: str | None = None

    def gained(self) -> int | None:
        if self.score_before is not None and self.score_after is not None:
            return self.score_after - self.score_before
        return None

# ─── 日志 ────────────────────────────────────────────────────────────────────
log_file = LOG_DIR / f"arc_daily_{date.today()}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("arc")


# ─── 通用：读取配置文件中的有效行（忽略空行和 # 注释）──────────────────────────
def _read_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# ─── 账号文件读取 ─────────────────────────────────────────────────────────────
def load_accounts() -> list[Account]:
    if not ACCOUNTS_FILE.exists():
        ACCOUNTS_FILE.write_text(
            "# Arc 登录邮箱列表，每行一个，与 gmail_passes.txt / proxies.txt 按行对应\n"
            "# 示例：\n"
            "# alice@gmail.com\n"
            "# bob@gmail.com\n",
            encoding="utf-8",
        )
        log.error(f"accounts.txt 不存在，已创建示例，请填写后重新运行。")
        sys.exit(1)

    emails = _read_lines(ACCOUNTS_FILE)
    if not emails:
        log.error("accounts.txt 中没有有效邮箱，请检查。")
        sys.exit(1)

    log.info(f"加载 {len(emails)} 个账号")
    return [Account(email=e, app_pass="") for e in emails]


# ─── Gmail 应用专用密码读取 ───────────────────────────────────────────────────
def load_gmail_passes(count: int) -> list[str]:
    if not GMAIL_PASSES_FILE.exists():
        GMAIL_PASSES_FILE.write_text(
            "# Gmail 应用专用密码，每行一个，与 accounts.txt 按行一一对应\n"
            "# 在 Google 账户 → 安全 → 应用专用密码 中生成（16位，空格可保留）\n"
            "# 示例：\n"
            "# abcd efgh ijkl mnop\n"
            "# wxyz abcd efgh ijkl\n",
            encoding="utf-8",
        )
        log.error(f"gmail_passes.txt 不存在，已创建示例，请填写后重新运行。")
        sys.exit(1)

    passes = _read_lines(GMAIL_PASSES_FILE)

    if len(passes) < count:
        log.error(
            f"gmail_passes.txt 只有 {len(passes)} 条，但有 {count} 个账号，请补全。"
        )
        sys.exit(1)
    if len(passes) > count:
        log.warning(f"gmail_passes.txt 有 {len(passes)} 条，多于账号数 {count}，多余行已忽略。")

    log.info(f"加载 {count} 条 Gmail 应用密码")
    return passes[:count]


# ─── 代理文件读取 ─────────────────────────────────────────────────────────────
def load_proxies(count: int) -> list[str | None]:
    """读取 proxies.txt，返回与账号等长的代理列表。缺少行时报错退出。"""
    if not PROXIES_FILE.exists():
        PROXIES_FILE.write_text(
            "# 代理配置文件，每行一个代理，与 accounts.txt 按行一一对应\n"
            "# 支持格式：\n"
            "#   http://user:pass@host:port\n"
            "#   socks5://user:pass@host:port\n"
            "#   http://host:port  （无认证）\n"
            "#   none              （该账号不使用代理，不推荐）\n"
            "# 示例：\n"
            "# http://user1:pass1@1.2.3.4:8080\n"
            "# socks5://user2:pass2@5.6.7.8:1080\n",
            encoding="utf-8",
        )
        log.error(f"代理文件不存在，已创建示例：{PROXIES_FILE}\n请填写代理后重新运行。")
        sys.exit(1)

    lines = []
    for line in PROXIES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(None if line.lower() == "none" else line)

    if len(lines) < count:
        log.error(
            f"proxies.txt 只有 {len(lines)} 条代理，但有 {count} 个账号。"
            f"请补全代理（每个账号必须有独立代理）。"
        )
        sys.exit(1)

    if len(lines) > count:
        log.warning(f"proxies.txt 有 {len(lines)} 条，账号有 {count} 个，多余代理已忽略。")

    # 校验格式
    for i, proxy in enumerate(lines[:count], 1):
        if proxy is None:
            log.warning(f"proxies.txt 第{i}行为 none，账号 {i} 将不走代理（风险较高）。")
            continue
        if not re.match(r'^(http|https|socks5)://', proxy):
            log.error(f"proxies.txt 第{i}行格式不正确（需以 http:// 或 socks5:// 开头）: {proxy}")
            sys.exit(1)

    log.info(f"加载 {count} 条代理")
    return lines[:count]


def parse_proxy(proxy_url: str) -> dict:
    """
    将代理 URL 解析为 Playwright context 所需的 proxy dict。
    Playwright 格式：{"server": "...", "username": "...", "password": "..."}
    """
    # 提取认证信息
    m = re.match(
        r'^((?:http|https|socks5)://)(?:([^:@]+):([^@]+)@)?(.+)$',
        proxy_url,
    )
    if not m:
        return {"server": proxy_url}

    scheme, username, password, hostport = m.groups()
    result: dict = {"server": f"{scheme}{hostport}"}
    if username:
        result["username"] = username
    if password:
        result["password"] = password
    return result


# ─── 全局状态（记录已注册活动，按邮箱区分）────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def get_account_state(state: dict, email: str) -> dict:
    if email not in state:
        state[email] = {"registered_events": [], "read_articles": [], "last_run": None}
    # 兼容旧版没有 read_articles 字段的情况
    if "read_articles" not in state[email]:
        state[email]["read_articles"] = []
    return state[email]


# ─── 辅助工具 ─────────────────────────────────────────────────────────────────
async def human_delay(min_s: float = 1.5, max_s: float = 4.0):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def scroll_slowly(page: Page, steps: int = 5):
    for _ in range(steps):
        await page.mouse.wheel(0, random.randint(200, 500))
        await asyncio.sleep(random.uniform(0.4, 0.9))


# ─── IMAP：从 Gmail 取 Magic Link ─────────────────────────────────────────────
def fetch_magic_link(email: str, app_pass: str, timeout_sec: int = 90) -> str | None:
    log.info(f"[{email}] IMAP: 等待 magic link（最多 {timeout_sec} 秒）...")
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_sec)

    while datetime.now(timezone.utc) < deadline:
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(email, app_pass)
            mail.select("INBOX")

            since = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%d-%b-%Y")

            # 精确搜索
            _, data = mail.search(None, f'(UNSEEN SINCE "{since}" FROM "circle")')
            if not data or not data[0]:
                _, data = mail.search(None, f'(UNSEEN SINCE "{since}")')

            msg_ids = data[0].split() if data and data[0] else []

            for msg_id in reversed(msg_ids):
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                sender  = msg.get("From", "").lower()
                subject = msg.get("Subject", "").lower()

                if not any(kw in sender or kw in subject
                           for kw in ["arc", "circle", "sign in", "login", "magic", "confirm"]):
                    continue

                # 提取正文
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            charset = part.get_content_charset() or "utf-8"
                            body += part.get_payload(decode=True).decode(charset, errors="replace")
                else:
                    charset = msg.get_content_charset() or "utf-8"
                    body = msg.get_payload(decode=True).decode(charset, errors="replace")

                # 找 magic link
                urls = re.findall(
                    r'https?://[^\s"\'<>]+(?:magic|token|sign_in|confirm|auth)[^\s"\'<>]*',
                    body,
                )
                if not urls:
                    urls = re.findall(
                        r'https?://(?:[^\s"\'<>]*arc\.network|[^\s"\'<>]*circle)[^\s"\'<>]*',
                        body,
                    )

                if urls:
                    link = urls[0].rstrip(".")
                    log.info(f"[{email}] 找到 magic link")
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                    mail.logout()
                    return link

            mail.logout()

        except Exception as e:
            log.warning(f"[{email}] IMAP 读取失败: {e}")

        remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
        log.info(f"[{email}] 未找到邮件，8秒后重试（剩余 {remaining}s）...")
        time.sleep(8)

    log.error(f"[{email}] 超时，未收到 magic link")
    return None


# ─── 读取积分 ─────────────────────────────────────────────────────────────────
async def get_score(page: Page, email: str) -> int | None:
    try:
        # 尝试几个常见 profile 路径
        profile_paths = ["/home/profile", "/home/member/profile", "/profile", "/home/account"]
        navigated = False
        for path in profile_paths:
            try:
                resp = await page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded", timeout=30000)
                if resp and resp.status != 404:
                    navigated = True
                    break
            except Exception:
                continue
        if not navigated:
            log.warning(f"[{email}] 所有 profile 路径均 404，跳过积分读取")
            return None
        await human_delay(2, 4)

        # 尝试多种积分选择器
        score_selectors = [
            # 常见积分/点数显示
            "[class*='point' i]",
            "[class*='score' i]",
            "[class*='credit' i]",
            "[class*='reward' i]",
            # 数字+文字组合
            "span:has-text('points')",
            "span:has-text('Points')",
            "div:has-text('points')",
            # 通用数字展示
            "[class*='stat'] [class*='number']",
            "[class*='badge'] [class*='count']",
        ]

        for sel in score_selectors:
            try:
                els = page.locator(sel)
                count = await els.count()
                for i in range(min(count, 5)):
                    text = (await els.nth(i).text_content() or "").strip()
                    # 提取数字
                    nums = re.findall(r'\d[\d,]*', text.replace(",", ""))
                    if nums:
                        score = int(nums[0].replace(",", ""))
                        if 0 < score < 1_000_000:
                            log.info(f"[{email}] 当前积分: {score}")
                            return score
            except Exception:
                continue

        # 截图方便人工检查
        await page.screenshot(path=str(LOG_DIR / f"profile_{email.split('@')[0]}.png"))
        log.warning(f"[{email}] 未能自动读取积分，已截图")
        return None

    except Exception as e:
        log.warning(f"[{email}] 读取积分失败: {e}")
        return None


# ─── 辅助：判断当前页面是否已登录 ───────────────────────────────────────────
async def is_logged_in(page: Page) -> bool:
    """检查当前页面是否处于登录态（不在 sign_in / 404 页）"""
    url = page.url
    if "sign_in" in url or "login" in url.lower():
        return False
    # 检查是否存在登出按钮 / 用户头像 / 用户菜单（表示已登录）
    logged_in_selectors = [
        "[class*='avatar' i]",
        "[class*='user-menu' i]",
        "button:has-text('Sign out')",
        "a:has-text('Sign out')",
        "button:has-text('Log out')",
        "[data-testid*='user']",
        "[aria-label*='profile' i]",
        "[class*='profile' i]",
    ]
    for sel in logged_in_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                return True
        except Exception:
            continue
    # 如果 URL 不含 sign_in 且不是 404，也认为登录成功
    if "sign_in" not in url and "404" not in url and url != BASE_URL + "/":
        return True
    return False


# ─── 登录 ─────────────────────────────────────────────────────────────────────
async def login(page: Page, account: Account) -> bool:
    log.info(f"[{account.email}] 开始登录（Magic Link）...")
    await page.goto(f"{BASE_URL}/home/sign_in", wait_until="domcontentloaded", timeout=60000)
    await human_delay(3, 5)

    email_input = page.locator(
        "input[type='email'], input[name='email'], input[placeholder*='email' i]"
    ).first
    await email_input.wait_for(state="visible", timeout=60000)
    await email_input.fill(account.email)
    await human_delay(0.8, 1.5)

    submit = page.locator(
        "button[type='submit'], button:has-text('Sign in'), "
        "button:has-text('Log in'), button:has-text('Continue'), button:has-text('Send')"
    ).first
    await submit.click()
    log.info(f"[{account.email}] 已提交邮箱，等待确认邮件...")
    await human_delay(3, 5)

    magic_link = await asyncio.get_event_loop().run_in_executor(
        None, lambda: fetch_magic_link(account.email, account.app_pass, timeout_sec=90)
    )

    if not magic_link:
        await page.screenshot(path=str(LOG_DIR / f"login_failed_{account.email.split('@')[0]}.png"))
        return False

    log.info(f"[{account.email}] magic link: {magic_link}")
    resp = await page.goto(magic_link, wait_until="domcontentloaded", timeout=60000)
    # 等待 cookie / session 写入稳定
    await human_delay(3, 5)

    # magic link 落地页可能是 404（token 已消耗，但 cookie 已写入）
    # 无论 404 与否，都跳转到 /home 继续
    if resp and resp.status == 404:
        log.warning(f"[{account.email}] magic link 落地页 404（正常），等待 session 写入后跳转...")
        await asyncio.sleep(3)  # 额外等待确保 cookie 写入
        await page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded", timeout=60000)
        await human_delay(3, 5)
    else:
        # 落地页正常，等待可能的自动跳转
        try:
            await page.wait_for_url(
                lambda u: "sign_in" not in u and "magic" not in u.lower(),
                timeout=10000,
            )
        except Exception:
            pass
        await human_delay(2, 3)

    # 页面上可能有一个"确认登录"按钮需要点击
    confirm_selectors = [
        "button:has-text('Confirm')",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
        "button:has-text('Continue')",
        "a:has-text('Confirm')",
        "a:has-text('Sign in')",
    ]
    for sel in confirm_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3000):
                log.info(f"[{account.email}] 点击确认按钮: {sel}")
                await btn.click()
                await human_delay(3, 5)
                break
        except Exception:
            continue

    # 如果还在登录页，主动跳转到 /home
    current_url = page.url
    if "sign_in" in current_url or "magic" in current_url.lower():
        log.warning(f"[{account.email}] 仍在登录页，主动跳转 /home...")
        await page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded", timeout=60000)
        await human_delay(3, 5)

    # 截图记录当前页面状态
    screenshot_path = str(SCRIPT_DIR / f"login_result_{account.email.split('@')[0]}.png")
    await page.screenshot(path=screenshot_path)

    current_url = page.url
    log.info(f"[{account.email}] 登录后 URL: {current_url}")

    # 验证登录态
    logged_in = await is_logged_in(page)
    if logged_in:
        log.info(f"[{account.email}] 登录成功 ✓")
        return True
    else:
        log.error(f"[{account.email}] 登录失败，已截图: {screenshot_path}")
        return False


# ─── 任务 1 & 2：阅读文章 + 观看视频 ─────────────────────────────────────────
async def read_content(page: Page, email: str, acct_state: dict) -> dict:
    log.info(f"[{email}] === 任务1&2：Content 阅读文章 + 视频 ===")
    await page.goto(f"{BASE_URL}/home/content", wait_until="domcontentloaded")
    await human_delay(3, 5)

    articles_read = 0
    videos_watched = 0
    TARGET_ARTICLES = 5
    TARGET_VIDEOS   = 1

    # 已读记录（按邮箱隔离，持久化在 arc_state.json）
    read_history: list = acct_state.get("read_articles", [])

    # 直接获取内容链接，不等待 visible（导航栏也有 /home/ 链接会导致 wait_for_selector 超时）
    links = await page.locator(
        "a[href*='/home/blogs/'], a[href*='/home/externals/'], a[href*='/home/videos/'], "
        "a[href*='/home/content/'], a[href*='/home/posts/'], a[href*='/home/articles/']"
    ).all()

    # 如果专用选择器没找到内容，降级用所有 /home/ 下的链接（过滤掉导航项）
    if len(links) == 0:
        all_links = await page.locator("a[href*='/home/']").all()
        nav_keywords = ["sign_in", "sign_out", "profile", "settings", "events", "forum",
                        "content", "notifications", "members", "leaderboard"]
        links = [
            lnk for lnk in all_links
            if not any(kw in (await lnk.get_attribute("href") or "") for kw in nav_keywords)
        ]
        log.info(f"[{email}] 降级模式：过滤后得到 {len(links)} 个内容链接")

    hrefs = []
    for lnk in links:
        href = await lnk.get_attribute("href")
        if href and href not in hrefs:
            hrefs.append(href)

    log.info(f"[{email}] 发现 {len(hrefs)} 个内容链接")

    # 过滤掉已读过的文章（视频不做去重，数量少）
    new_hrefs  = [h for h in hrefs if h not in read_history or "/videos/" in h]
    skip_count = len(hrefs) - len(new_hrefs)
    if skip_count > 0:
        log.info(f"[{email}] 跳过已读文章 {skip_count} 篇，剩余新内容 {len(new_hrefs)} 篇")

    # 如果新内容不够 5 篇，把已读的也补进来（保证任务能完成）
    if len(new_hrefs) < TARGET_ARTICLES:
        already_read = [h for h in hrefs if h in read_history and "/videos/" not in h]
        needed = TARGET_ARTICLES - len(new_hrefs)
        new_hrefs += already_read[:needed]
        if already_read:
            log.info(f"[{email}] 新内容不足，补充 {min(needed, len(already_read))} 篇旧文章凑够任务数")

    random.shuffle(new_hrefs)

    for href in new_hrefs:
        if articles_read >= TARGET_ARTICLES and videos_watched >= TARGET_VIDEOS:
            break

        is_video = "/videos/" in href
        if is_video and videos_watched >= TARGET_VIDEOS:
            continue
        if not is_video and articles_read >= TARGET_ARTICLES:
            continue

        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await human_delay(1, 2)
            await scroll_slowly(page, steps=random.randint(4, 8))
            read_time = random.uniform(15, 30)
            log.info(f"[{email}]   阅读中（{read_time:.0f}s）: {url.split('/')[-1][:50]}")
            await asyncio.sleep(read_time)

            if is_video:
                videos_watched += 1
            else:
                articles_read += 1
                # 记录已读（用规范化的 href 存储，避免 http/https 差异）
                if href not in read_history:
                    read_history.append(href)
                    acct_state["read_articles"] = read_history

            await page.go_back()
            await human_delay(2, 4)
        except Exception as e:
            log.warning(f"[{email}]   跳过: {e}")

    log.info(f"[{email}] Content 完成：文章 {articles_read}/{TARGET_ARTICLES}，视频 {videos_watched}/{TARGET_VIDEOS}，累计已读 {len(read_history)} 篇")
    return {"articles": articles_read, "videos": videos_watched}


# ─── 任务 3：Events 注册 ──────────────────────────────────────────────────────
async def register_events(page: Page, email: str, acct_state: dict) -> int:
    log.info(f"[{email}] === 任务3：Events 注册新活动 ===")
    await page.goto(f"{BASE_URL}/home/events", wait_until="domcontentloaded")
    await human_delay(2, 3)

    registered_count = 0

    try:
        upcoming_btn = page.locator("button:has-text('Upcoming')").first
        if await upcoming_btn.is_visible():
            await upcoming_btn.click()
            await human_delay(1, 2)
    except Exception:
        pass

    register_btns = page.locator("button:has-text('Register')")
    count = await register_btns.count()
    log.info(f"[{email}] 发现 {count} 个 Register 按钮")

    for i in range(count):
        btn = register_btns.nth(i)
        try:
            card = btn.locator("xpath=ancestor::div[contains(@class,'CardContainer') or contains(@class,'card')]").first
            title_el = card.locator("h3, h2").first
            title = (await title_el.text_content() if await title_el.count() > 0 else f"Event_{i}").strip()

            if title in acct_state["registered_events"]:
                log.info(f"[{email}]   跳过（已注册）: {title}")
                continue

            log.info(f"[{email}]   注册: {title}")
            await btn.scroll_into_view_if_needed()
            await human_delay(1, 2)
            await btn.click()
            await human_delay(2, 4)

            for sel in ["button:has-text('Confirm')", "button:has-text('Submit')", "button:has-text('OK')"]:
                try:
                    cb = page.locator(sel).last
                    if await cb.is_visible(timeout=3000):
                        await cb.click()
                        await human_delay(1, 2)
                        break
                except Exception:
                    pass

            try:
                close_btn = page.locator("button[aria-label='Close'], [class*='close']").first
                if await close_btn.is_visible(timeout=2000):
                    await close_btn.click()
                    await human_delay(1, 2)
            except Exception:
                pass

            acct_state["registered_events"].append(title)
            registered_count += 1
            log.info(f"[{email}]   注册成功 (+5分): {title}")
            await page.keyboard.press("Escape")
            await human_delay(2, 3)

        except Exception as e:
            log.warning(f"[{email}]   注册失败 index={i}: {e}")

    log.info(f"[{email}] Events 完成：注册 {registered_count} 个活动 (+{registered_count*5}分)")
    return registered_count


# ─── 任务 4：发帖 ─────────────────────────────────────────────────────────────
async def find_forum_url(page: Page, email: str) -> str:
    """尝试从导航栏找到 forum/discussions 的真实路径"""
    forum_url = f"{BASE_URL}/home/forum"  # 默认猜测
    try:
        nav_links = await page.locator("nav a, aside a, [class*='sidebar'] a, [class*='nav'] a").all()
        for lnk in nav_links:
            href = (await lnk.get_attribute("href") or "").lower()
            text = (await lnk.text_content() or "").lower().strip()
            if any(kw in text or kw in href for kw in ["forum", "discussion", "discuss", "community", "post"]):
                full = href if href.startswith("http") else f"{BASE_URL}{href}"
                log.info(f"[{email}] 找到 forum 链接: {full}")
                return full
    except Exception as e:
        log.warning(f"[{email}] 自动查找 forum 链接失败: {e}")
    log.info(f"[{email}] 使用默认 forum URL: {forum_url}")
    return forum_url


async def create_post(page: Page, email: str) -> bool:
    log.info(f"[{email}] === 任务4：Discussions 发帖 ===")
    forum_url = await find_forum_url(page, email)
    await page.goto(forum_url, wait_until="domcontentloaded")
    await human_delay(3, 5)

    try:
        await page.wait_for_selector(
            "button:has-text('Create a post'), button:has-text('New post')",
            timeout=10000,
        )
    except Exception:
        log.warning(f"[{email}] 未找到发帖按钮（继续尝试）")

    try:
        create_btn = page.locator(
            "button:has-text('Create a post'), button:has-text('New post'), a:has-text('Create a post')"
        ).first
        await create_btn.scroll_into_view_if_needed()
        await human_delay(1, 2)
        await create_btn.click()
        await human_delay(2, 3)

        # 标题
        title_input = page.locator("input[placeholder*='title' i], input[name='title']").first
        if await title_input.is_visible(timeout=5000):
            today_str = datetime.now().strftime("%B %d")
            await title_input.fill(f"Daily Discussion – {today_str}")
            await human_delay(0.5, 1.5)

        # 正文（每个账号用不同模板）
        post_text = random.choice(POST_TEMPLATES)
        body_filled = False
        for sel in ["div[contenteditable='true']", "textarea", ".ql-editor", "div[role='textbox']"]:
            try:
                body = page.locator(sel).first
                if await body.is_visible(timeout=4000):
                    await body.click()
                    await human_delay(0.5, 1)
                    await body.fill(post_text)
                    body_filled = True
                    break
            except Exception:
                continue

        if not body_filled:
            log.warning(f"[{email}] 未能填写正文，跳过发帖")
            await page.keyboard.press("Escape")
            return False

        await human_delay(1, 2)

        submitted = False
        for sel in ["button:has-text('Post')", "button:has-text('Publish')", "button:has-text('Submit')", "button[type='submit']"]:
            try:
                sb = page.locator(sel).last
                if await sb.is_visible(timeout=3000):
                    await sb.click()
                    submitted = True
                    break
            except Exception:
                continue

        if submitted:
            await human_delay(3, 5)
            log.info(f"[{email}] 发帖成功 (+10分)")
            return True
        else:
            log.warning(f"[{email}] 未找到提交按钮")
            await page.keyboard.press("Escape")
            return False

    except Exception as e:
        log.error(f"[{email}] 发帖失败: {e}")
        await page.screenshot(path=str(LOG_DIR / f"post_failed_{email.split('@')[0]}.png"))
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False


# ─── 任务 5：评论 ─────────────────────────────────────────────────────────────
async def comment_on_posts(page: Page, email: str) -> int:
    log.info(f"[{email}] === 任务5：Discussions 评论 2 条帖子 ===")
    forum_url = await find_forum_url(page, email)
    await page.goto(forum_url, wait_until="domcontentloaded")
    await human_delay(3, 5)

    commented    = 0
    TARGET       = 2
    post_link_sel = "a[href*='/home/forum/'], a[href*='/home/post/'], a[href*='/home/discussion']"

    try:
        await page.wait_for_selector(post_link_sel, timeout=10000)
    except Exception:
        log.warning(f"[{email}] 帖子列表未加载（继续尝试）")

    post_links = await page.locator(post_link_sel).all()
    hrefs = []
    for lnk in post_links:
        href = await lnk.get_attribute("href")
        if href and href not in hrefs:
            hrefs.append(href)

    log.info(f"[{email}] 发现 {len(hrefs)} 个帖子")
    # 跳过前1个（可能是自己刚发的），多取几个备用
    target_posts = hrefs[1:TARGET + 4] if len(hrefs) > 1 else hrefs

    for href in target_posts:
        if commented >= TARGET:
            break

        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await human_delay(2, 4)
            await scroll_slowly(page, steps=3)

            comment_text = random.choice(COMMENT_TEMPLATES)
            comment_filled = False

            comment_sels = [
                "div[contenteditable='true']",
                "textarea[placeholder*='comment' i]",
                ".ql-editor",
                "div[role='textbox']",
            ]

            # 先尝试直接找输入框
            for sel in comment_sels:
                try:
                    cb = page.locator(sel).first
                    if await cb.is_visible(timeout=4000):
                        await cb.click()
                        await human_delay(0.5, 1)
                        await cb.fill(comment_text)
                        comment_filled = True
                        break
                except Exception:
                    continue

            # 若没找到，尝试点击触发按钮
            if not comment_filled:
                try:
                    trigger = page.locator(
                        "button:has-text('Add a comment'), button:has-text('Comment'), button:has-text('Reply')"
                    ).first
                    if await trigger.is_visible(timeout=4000):
                        await trigger.click()
                        await human_delay(1, 2)
                        for sel in comment_sels:
                            try:
                                cb = page.locator(sel).first
                                if await cb.is_visible(timeout=3000):
                                    await cb.fill(comment_text)
                                    comment_filled = True
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass

            if not comment_filled:
                log.warning(f"[{email}]   未找到评论框，跳过")
                await page.go_back()
                await human_delay(2, 3)
                continue

            await human_delay(1, 2)

            submitted = False
            for sel in ["button:has-text('Post')", "button:has-text('Submit')", "button:has-text('Reply')", "button:has-text('Send')", "button[type='submit']"]:
                try:
                    sb = page.locator(sel).last
                    if await sb.is_visible(timeout=3000):
                        await sb.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if submitted:
                await human_delay(3, 5)
                commented += 1
                log.info(f"[{email}]   评论 {commented}/{TARGET} 成功")
            else:
                log.warning(f"[{email}]   未找到提交按钮")

            await page.go_back()
            await human_delay(2, 4)

        except Exception as e:
            log.error(f"[{email}] 评论失败 {url}: {e}")
            try:
                await page.go_back()
            except Exception:
                pass
            await human_delay(2, 3)

    log.info(f"[{email}] 评论完成：{commented}/{TARGET} 条 (+{commented*5}分)")
    return commented


# ─── 单账号完整流程 ────────────────────────────────────────────────────────────
async def run_account(account: Account, browser: Browser, state: dict) -> AccountResult:
    result = AccountResult(email=account.email)
    acct_state = get_account_state(state, account.email)

    # session 文件路径（按邮箱前缀命名）
    session_file = SESSIONS_DIR / f"{account.email.split('@')[0]}.json"

    ctx_kwargs: dict = dict(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    if account.proxy:
        ctx_kwargs["proxy"] = parse_proxy(account.proxy)
        log.info(f"[{account.email}] 使用代理: {account.proxy.split('@')[-1]}")

    # 如果有保存的 session，先尝试直接加载
    session_ok = False
    if session_file.exists():
        log.info(f"[{account.email}] 发现已保存的 session，尝试直接使用...")
        try:
            ctx_kwargs["storage_state"] = str(session_file)
            context: BrowserContext = await browser.new_context(**ctx_kwargs)
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            await page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded", timeout=30000)
            await human_delay(2, 3)

            if await is_logged_in(page):
                log.info(f"[{account.email}] Session 有效，跳过登录 ✓")
                session_ok = True
            else:
                log.warning(f"[{account.email}] Session 已过期，需要重新登录")
                await context.close()
                del ctx_kwargs["storage_state"]
                session_file.unlink(missing_ok=True)
        except Exception as e:
            log.warning(f"[{account.email}] 加载 session 失败: {e}，重新登录")
            try:
                await context.close()
            except Exception:
                pass
            ctx_kwargs.pop("storage_state", None)
            session_file.unlink(missing_ok=True)
            session_ok = False

    # 没有有效 session，走正常登录流程
    if not session_ok:
        ctx_kwargs.pop("storage_state", None)
        context: BrowserContext = await browser.new_context(**ctx_kwargs)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        if not await login(page, account):
            result.error = "登录失败"
            await context.close()
            return result

        # 登录成功后保存 session
        try:
            await context.storage_state(path=str(session_file))
            log.info(f"[{account.email}] Session 已保存 → {session_file.name}")
        except Exception as e:
            log.warning(f"[{account.email}] 保存 session 失败: {e}")

    try:
        # 读取任务前积分
        result.score_before = await get_score(page, account.email)

        # 执行任务
        content_result   = await read_content(page, account.email, acct_state)
        await human_delay(3, 6)

        events_count     = await register_events(page, account.email, acct_state)
        await human_delay(3, 6)

        post_ok          = await create_post(page, account.email)
        await human_delay(3, 6)

        comment_count    = await comment_on_posts(page, account.email)

        result.tasks_done = {
            "articles":  content_result["articles"],
            "videos":    content_result["videos"],
            "events":    events_count,
            "post":      post_ok,
            "comments":  comment_count,
        }

        # 读取任务后积分
        await human_delay(3, 6)
        result.score_after = await get_score(page, account.email)

        acct_state["last_run"] = datetime.now().isoformat()

        # 任务完成后刷新保存 session（保持最新 cookie）
        try:
            await context.storage_state(path=str(session_file))
            log.info(f"[{account.email}] Session 已更新")
        except Exception as e:
            log.warning(f"[{account.email}] 更新 session 失败: {e}")

    except Exception as e:
        result.error = str(e)
        log.error(f"[{account.email}] 账号异常: {e}", exc_info=True)
        await page.screenshot(path=str(LOG_DIR / f"error_{account.email.split('@')[0]}.png"))
    finally:
        await context.close()

    return result


# ─── 汇总报告 ─────────────────────────────────────────────────────────────────
def print_summary(results: list[AccountResult]):
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  Arc Network 每日积分汇总  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(sep)

    total_gained = 0
    for r in results:
        status = "✓" if not r.error else "✗"
        before = f"{r.score_before:,}" if r.score_before is not None else "N/A"
        after  = f"{r.score_after:,}"  if r.score_after  is not None else "N/A"
        gained = r.gained()
        gained_str = f"+{gained}" if gained is not None else "N/A"
        if gained:
            total_gained += gained

        print(f"\n  [{status}] {r.email}")
        if r.error:
            print(f"      错误: {r.error}")
        else:
            print(f"      积分: {before} → {after}  （{gained_str}）")
            td = r.tasks_done
            print(f"      任务: 文章 {td.get('articles',0)}/5  "
                  f"视频 {td.get('videos',0)}/1  "
                  f"活动 {td.get('events',0)}个  "
                  f"发帖 {'✓' if td.get('post') else '✗'}  "
                  f"评论 {td.get('comments',0)}/2")

    print(f"\n{sep}")
    print(f"  共 {len(results)} 个账号  |  本次合计积分: +{total_gained}")
    print(sep)

    # 同时写入日志
    log.info(f"汇总：{len(results)} 个账号，合计 +{total_gained} 积分")


# ─── 单次执行所有账号 ──────────────────────────────────────────────────────────
async def run_once():
    log.info("=" * 60)
    log.info(f"Arc Network 每日任务开始  {datetime.now()}")
    log.info("=" * 60)

    accounts = load_accounts()
    passes   = load_gmail_passes(len(accounts))
    proxies  = load_proxies(len(accounts))
    for account, app_pass, proxy in zip(accounts, passes, proxies):
        account.app_pass = app_pass
        account.proxy    = proxy

    state   = load_state()
    results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )

        # 账号依次串行执行（避免同时多个浏览器触发反爬）
        for i, account in enumerate(accounts, 1):
            log.info(f"\n{'─'*60}")
            log.info(f"账号 {i}/{len(accounts)}: {account.email}")
            log.info(f"{'─'*60}")

            result = await run_account(account, browser, state)
            results.append(result)
            save_state(state)

            # 账号之间随机间隔 30-90 秒
            if i < len(accounts):
                wait = random.randint(30, 90)
                log.info(f"等待 {wait} 秒后处理下一个账号...")
                await asyncio.sleep(wait)

        await browser.close()

    print_summary(results)


# ─── 主循环：每隔 24 小时在同一时间点重复执行 ──────────────────────────────────
async def main():
    log.info("=" * 60)
    log.info(f"Arc Network 守护进程启动  {datetime.now()}")
    log.info(f"将在每次执行完成后等待 24 小时再次运行")
    log.info("=" * 60)

    run_count = 0
    INTERVAL  = 24 * 60 * 60  # 24 小时（秒）

    while True:
        run_count += 1
        start_time = datetime.now()
        log.info(f"\n{'━'*60}")
        log.info(f"第 {run_count} 次执行  {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"{'━'*60}")

        try:
            await run_once()
        except Exception as e:
            log.error(f"本轮执行异常: {e}", exc_info=True)

        end_time  = datetime.now()
        elapsed   = (end_time - start_time).total_seconds()
        wait_secs = max(0, INTERVAL - elapsed)

        next_run  = end_time + timedelta(seconds=wait_secs)
        log.info(f"\n本轮耗时 {elapsed/60:.1f} 分钟")
        log.info(f"下次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"等待 {wait_secs/3600:.2f} 小时...")

        await asyncio.sleep(wait_secs)


# ─── 首次部署：安装依赖 + Chromium + 配置 cron ───────────────────────────────
def setup():
    import subprocess
    import platform

    print("=" * 60)
    print("  Arc Network 脚本 — 首次部署向导")
    print("=" * 60)

    # 1. 安装 playwright
    print("\n[1/3] 安装 playwright...")
    subprocess.run([sys.executable, "-m", "pip", "install", "playwright", "-q"], check=True)

    # 2. 安装 Chromium
    print("[2/3] 安装 Chromium 浏览器...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    # Linux 上额外安装系统依赖
    if platform.system() == "Linux":
        subprocess.run([sys.executable, "-m", "playwright", "install-deps", "chromium"], check=True)

    # 3. 校验配置文件存在
    print("[3/3] 检查配置文件...")
    for fname, hint in [
        (ACCOUNTS_FILE,     "填写每行一个 Arc 登录邮箱"),
        (GMAIL_PASSES_FILE, "填写每行一个 Gmail 应用专用密码（与邮箱行对应）"),
        (PROXIES_FILE,      "填写每行一个代理（与邮箱行对应）"),
    ]:
        if not fname.exists() or all(
            l.strip().startswith("#") or not l.strip()
            for l in fname.read_text(encoding="utf-8").splitlines()
        ):
            print(f"  ⚠  {fname.name} 尚未填写有效内容 — 请{hint}")
        else:
            lines = [l for l in fname.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.strip().startswith("#")]
            print(f"  ✓  {fname.name} — {len(lines)} 条记录")

    # 4. 配置 cron（仅 Linux/macOS）
    if platform.system() in ("Linux", "Darwin"):
        python_bin = sys.executable
        script_path = Path(__file__).resolve()
        log_path = LOG_DIR / "arc_cron.log"
        cron_job = f"0 9 * * * {python_bin} {script_path} >> {log_path} 2>&1"

        try:
            result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            existing = result.stdout if result.returncode == 0 else ""
            if str(script_path) in existing:
                print(f"\n  ✓  Cron 任务已存在，跳过")
            else:
                new_crontab = existing.rstrip("\n") + "\n" + cron_job + "\n"
                subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
                print(f"\n  ✓  Cron 已配置（每天 UTC 09:00）: {cron_job}")
        except FileNotFoundError:
            print("\n  ℹ  未找到 crontab 命令，请手动配置定时任务")
    else:
        print(
            f"\n  ℹ  Windows 请使用「任务计划程序」设置每日定时运行：\n"
            f"     {sys.executable} {Path(__file__).resolve()}"
        )

    print("\n" + "=" * 60)
    print("  部署完成！确认配置文件填写正确后运行：")
    print(f"  python {Path(__file__).name}")
    print("=" * 60)


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup()
    else:
        asyncio.run(main())
