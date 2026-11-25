"""
Chinese versions of the API's error messages.

Every endpoint raises its errors in English. The console sends `X-Lang: zh`
when the merchant is using the Chinese interface, and main.py's HTTP-error
handler passes the message through `localise()` on the way out — so the
endpoints stay simple and a Chinese-speaking merchant never gets an English
toast in the middle of a Chinese page.

Each entry is (regex over the whole English message, Chinese template).
`{1}`, `{2}` … in the template are the regex groups. A message with no entry
is returned unchanged, which is safe: it is still a correct message.
"""
import re

_PLAN = {"Free": "免费版", "Pro": "专业版", "Team": "团队版"}
_STATUS = {"pending": "待付款", "submitted": "审核中", "paid": "已付款", "rejected": "未确认",
           "expired": "已过期", "cancelled": "已取消"}

_RULES = [
    # ── sign-in & account ──
    (r"Not authenticated", "请先登录。"),
    (r"Invalid or expired token|Invalid token", "登录已失效，请重新登录。"),
    (r"Invalid email or password", "邮箱或密码不正确。"),
    (r"Please enter a valid email address|Invalid email address", "请输入有效的邮箱地址。"),
    (r"An account with this email already exists", "这个邮箱已经注册过了。"),
    (r"Password must be at least (\d+) characters", "密码至少需要 {1} 位。"),
    (r"Password must be at most (\d+) characters", "密码不能超过 {1} 位。"),
    (r"Current password is incorrect", "当前密码不正确。"),
    (r"Password is incorrect", "密码不正确。"),
    (r"New password must be different from the current one", "新密码不能和当前密码相同。"),
    (r"This link is invalid or has expired — please request a new one\.", "链接无效或已过期，请重新获取。"),
    (r"Too many sign-ups from this network — please try again later\.", "当前网络注册次数过多，请稍后再试。"),
    (r"Too many sign-in attempts — please wait a few minutes and try again\.", "登录尝试次数过多，请几分钟后再试。"),
    (r"Too many demo sessions from this network — create a free account to keep going\.",
     "当前网络的演示次数已用完，注册一个免费账号即可继续使用。"),
    (r"Too many reset requests — please wait a while and try again\.", "重置请求过于频繁，请稍后再试。"),
    (r"Only the workspace owner can do this\.", "只有工作区所有者可以执行这个操作。"),
    (r"Demo sessions have no account to manage — create a free account first\.",
     "演示账号无法管理账号信息，请先注册一个免费账号。"),
    (r"Email is not set up on this server yet\.", "服务器尚未配置邮件功能。"),
    (r"Email is not set up on this server — contact us to reset your password\.",
     "服务器尚未配置邮件功能，请联系我们重置密码。"),
    (r"We just sent you a link — check your inbox \(and spam\) before asking again\.",
     "验证邮件刚刚发出，请先查看收件箱（包括垃圾邮件）。"),
    (r"This workspace's plan no longer covers its team seats — ask the workspace owner to renew the plan\.",
     "这个工作区的套餐已不包含团队席位，请联系工作区所有者续费。"),
    # ── plans & payment ──
    (r"Monthly optimization limit reached \((\d+) runs on your plan\)\. Upgrade for more optimization runs\.",
     "本月优化次数已用完（当前套餐每月 {1} 次）。升级套餐可获得更多次数。"),
    (r"Monthly AI Copilot limit reached \((\d+) chats on your plan\)\. Upgrade for more copilot conversations\.",
     "本月 AI 助手对话次数已用完（当前套餐每月 {1} 次）。升级套餐可获得更多次数。"),
    (r"Creator library limit reached \((.+?) on your plan.*\)\. Upgrade for a larger library\.",
     "达人库已达上限（{1}）。升级套餐可获得更大的达人库。"),
    (r"You are already on this plan", "你已经在使用这个套餐了。"),
    (r"The (\w+) plan is activated after payment — contact us to upgrade and we will switch your plan\.",
     "{plan1}需要付款后开通，请联系我们升级。"),
    (r"Your library has (\d+) creators but the (\w+) plan allows (\d+)\. Remove creators first or pick a bigger plan\.",
     "你的达人库有 {1} 位达人，而{plan2}最多 {3} 位。请先删除部分达人，或选择更高的套餐。"),
    (r"Unknown plan: (.+)|Unknown paid plan: (.+)", "未知的套餐。"),
    (r"months must be one of (.+)", "购买时长只能是 {1} 个月。"),
    (r"Too many payments are in progress — please try again in a few minutes\.", "当前待付款订单过多，请几分钟后再试。"),
    (r"Order not found", "找不到这个订单。"),
    (r"QR payment is not available right now — contact us to upgrade\.", "扫码支付暂时不可用，请联系我们升级。"),
    (r"Create a free account first — demo sessions can't buy a plan\.", "演示账号无法购买套餐，请先注册一个免费账号。"),
    (r"You already have payments in progress — finish or cancel one first\.", "你有尚未完成的付款，请先完成或取消。"),
    (r"Enter the transaction reference from your payment receipt \(4–64 letters or digits\)\.",
     "请填写付款凭证上的交易参考号（4–64 位字母或数字）。"),
    (r"This transaction reference was already used for another order\.", "这个交易参考号已用于其他订单。"),
    (r"This order is already (\w+)\.", "这个订单已是「{status1}」状态。"),
    (r"This order can't be cancelled — it is already being reviewed or finished\.", "订单正在审核或已完成，无法取消。"),
    # ── team ──
    (r"This invitation is invalid, expired or was withdrawn — ask the workspace owner for a new one\.",
     "邀请链接无效、已过期或已被撤回，请让工作区所有者重新邀请。"),
    (r"Create a free account first — demo sessions can't invite a team\.", "演示账号无法邀请团队成员，请先注册一个免费账号。"),
    (r"That person is already in this workspace\.", "对方已经在这个工作区里了。"),
    (r"This email already has its own Lumio account, so it can't join another workspace\.",
     "这个邮箱已有自己的 Lumio 账号，无法加入其他工作区。"),
    (r"Your plan includes (\d+) seats? and they are all taken — upgrade to add more people\.",
     "当前套餐包含 {1} 个席位，已全部占用。升级套餐后可邀请更多人。"),
    (r"Invitation not found", "找不到这个邀请。"),
    (r"No such member in this workspace", "工作区里没有这位成员。"),
    (r"The workspace owner can't be removed\.", "不能移除工作区所有者。"),
    (r"This email already has a Lumio account — sign in instead\.", "这个邮箱已有 Lumio 账号，请直接登录。"),
    (r"This workspace no longer exists\.", "这个工作区已不存在。"),
    (r"This workspace has no free seat right now — ask the owner to upgrade or free one up\.",
     "这个工作区暂时没有空余席位，请让所有者升级套餐或腾出席位。"),
    (r"This invitation was already used\.", "这个邀请已被使用。"),
    # ── creators, campaigns, optimizer ──
    (r"KOL with id (\d+) not found|Creator not found", "找不到这位达人。"),
    (r"Campaign (?:\d+ )?not found", "找不到这次投放。"),
    (r"That creator is not part of this campaign", "这位达人不在这次投放里。"),
    (r"You're running optimizations very quickly — give it a few seconds\.", "优化运行得太频繁了，请稍等几秒。"),
    (r"budget must be a positive number", "预算必须是正数。"),
    (r"invalid countries: (.+)", "无效的国家：{1}"),
    (r"country must be one of: (.+)", "国家只能是：{1}"),
    (r"category must be one of: (.+)", "不支持这个品类。"),
    (r"tier must be one of: (.+)", "达人层级只能是：{1}"),
    (r"content_type must be one of: (.+)", "投放形式只能是：{1}"),
    (r"File too large \(limit (\d+) MB\)", "文件过大（上限 {1} MB）。"),
    (r"Could not read Excel file: (.+)", "无法读取这个 Excel 文件：{1}"),
    (r"Legacy \.xls is not supported — please re-save as \.xlsx or \.csv\.", "不支持旧版 .xls 格式，请另存为 .xlsx 或 .csv。"),
    (r"File must be a \.csv or \.xlsx file", "只支持 .csv 或 .xlsx 文件。"),
    (r"No data rows found in the file\.", "文件里没有数据行。"),
    (r"Unknown guide topic: (.+)", "找不到这个指南主题。"),
    (r"Unknown integration type: (.+)", "未知的集成类型。"),
    # ── collaborations, templates, contracts ──
    (r"status must be one of: (.+)", "不支持这个状态。"),
    (r"The content link must start with http:// or https://", "内容链接必须以 http:// 或 https:// 开头。"),
    (r"You can only assign someone in this workspace", "只能指派给这个工作区里的成员。"),
    (r"kind must be message or contract", "模板类型只能是消息或合同。"),
    (r"Template limit reached — delete some you no longer use\.", "模板数量已达上限，请先删除不用的模板。"),
    (r"Template not found|Contract template not found", "找不到这个模板。"),
    (r"channel must be one of: (.+)", "不支持这个发送渠道。"),
    (r"Contract limit reached\.", "合同数量已达上限。"),
    (r"Contract not found", "找不到这份合同。"),
    (r"A signed contract can't be edited\.", "已签署的合同不能修改。"),
    (r"A signed contract can't be deleted — cancel it instead\.", "已签署的合同不能删除，可以改为「已取消」。"),
    # ── explore & extension ──
    (r"Create a free account first — demo sessions can't connect the extension\.", "演示账号无法连接浏览器扩展，请先注册一个免费账号。"),
    (r"This connection code is invalid or has expired — create a new one under Explore\.", "连接码无效或已过期，请在「发现达人」里重新生成。"),
    (r"The extension is not bundled with this server\.", "这台服务器没有附带浏览器扩展。"),
    (r"Capturing very quickly — the extension will try again later\.", "采集过于频繁，扩展稍后会自动重试。"),
    (r"platform must be one of: (.+)", "不支持这个平台。"),
    (r"That does not look like a creator handle\.", "这看起来不是一个达人账号名。"),
    (r"This creator is already in your library\.", "这位达人已经在你的达人库里了。"),
    (r"This account has been suspended — contact us if you think this is a mistake\.", "这个账号已被停用。如果你认为这是误操作，请联系我们。"),
    (r"Saved content limit reached — delete some you no longer need\.", "保存的内容已达上限，请先删除不需要的。"),
    (r"Saved item not found", "找不到这条保存的内容。"),
    (r"AI video is not set up on this server yet\.", "服务器尚未开启 AI 视频功能。"),
    (r"duration must be one of (.+)", "时长只能是 {1} 秒。"),
    (r"aspect must be one of (.+)", "不支持这个画面比例。"),
    (r"The image must be a JPEG, PNG or WebP picture\.", "图片必须是 JPEG、PNG 或 WebP 格式。"),
    (r"Monthly AI video limit reached \((\d+) clips on your plan\)\. Upgrade for more AI video clips\.",
     "本月 AI 视频额度已用完（当前套餐每月 {1} 条）。升级套餐可获得更多额度。"),
    (r"You're starting videos very quickly — wait for the current ones to finish\.", "生成得太频繁了，请等当前的视频完成。"),
    (r"Three videos are already being generated — wait for one to finish\.", "已有 3 条视频正在生成，请等其中一条完成。"),
    (r"The AI video service is temporarily unavailable — please try again shortly\.", "AI 视频服务暂时不可用，请稍后再试。"),
    (r"Video not found", "找不到这条视频。"),
    # ── AI ──
    (r"The AI copilot is temporarily unavailable — please try again shortly\.", "AI 助手暂时不可用，请稍后再试。"),
    (r"The AI copilot returned an unreadable answer — please try again\.|The AI returned an unreadable answer — please try again\.",
     "AI 返回的内容无法识别，请重试。"),
    (r"You're sending messages very quickly — give the copilot a moment\.|You're asking very quickly — give the AI a moment\.",
     "操作太频繁了，请稍等片刻。"),
    (r"Agent not configured.*|AI features are not set up on this server yet\.", "服务器尚未开启 AI 功能。"),
    (r"Save at least one campaign first — there is nothing to analyse yet\.", "请先保存至少一次投放，目前还没有可分析的数据。"),
    (r"format must be video or live", "形式只能是短视频或直播。"),
    # ── generic ──
    (r"Too many requests.*", "请求过于频繁，请稍后再试。"),
    (r"Request body too large.*", "提交的内容过大。"),
    (r"Not Found", "找不到请求的内容。"),
    (r"Method Not Allowed", "不支持这个操作。"),
    (r"Internal server error.*", "服务器出了点问题，请稍后再试。"),
]
_COMPILED = [(re.compile(r"^(?:" + pat + r")$", re.S), tpl) for pat, tpl in _RULES]


def is_zh(lang) -> bool:
    return (lang or "").lower().startswith("zh")


def localise(message, lang):
    """`message` in Chinese when `lang` is zh and we have a translation."""
    if not is_zh(lang) or not isinstance(message, str):
        return message
    for rx, tpl in _COMPILED:
        m = rx.match(message)
        if not m:
            continue
        groups = [g or "" for g in m.groups()]
        out = tpl
        for i, g in enumerate(groups, 1):
            out = (out.replace("{plan%d}" % i, _PLAN.get(g, g) if g else "")
                      .replace("{status%d}" % i, _STATUS.get(g, g))
                      .replace("{%d}" % i, g))
        return out
    return message
