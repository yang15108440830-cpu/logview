# -*- coding: utf-8 -*-
"""
LogView - 日志查看分析工具（免重启）
====================================================
通过 Spring Boot Actuator 对远程服务做：
  1. 日志级别热调（查 / 改 / 复原）         —— /actuator/loggers
  2. 原始日志文件实时查看（等价 tail -f）    —— /actuator/logfile（HTTP Range 增量拉取）
  3. AI 辅助日志分析（OpenAI 兼容接口，SSE 流式）

前提：目标服务已暴露 actuator 端点（本项目 bs:9528 / manage:9527 均为
management.endpoints.web.exposure.include: '*'，且配置了 logging.file.name）。

外部调用三要素：
  - 超时：每次 HTTP 请求 5 秒
  - 重试：不自动重试（幂等操作，失败由用户手动重按；日志跟随轮询例外——持续轮询天然容错）
  - 兜底：所有异常捕获后展示在界面状态区，绝不让界面崩溃
"""

import collections
import datetime
import difflib
import json
import os
import socket
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "LogView - 日志查看分析（调级 + 实时日志 + AI，免重启）"


def app_dir():
    """配置文件所在目录：PyInstaller onefile 打包后 __file__ 指向临时解压目录，
    必须改用 exe 自身所在目录，否则 services.json 随临时目录销毁而丢失"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = os.path.join(app_dir(), "data")  # 配置与用户偏好统一放 data/，与源码分离
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "services.json")
PREFS_FILE = os.path.join(DATA_DIR, "prefs.json")      # 屏蔽词等用户偏好，跨启动保留
AI_CONFIG_FILE = os.path.join(DATA_DIR, "ai_config.json")  # AI 分析配置（含 api_key，已 gitignore 不入库）
AI_HISTORY_FILE = os.path.join(DATA_DIR, "ai_history.json")  # 历史分析记录（含日志分析全文，已 gitignore）
HTTP_TIMEOUT = 5          # 单次 HTTP 请求超时（秒）
POLL_INTERVAL = 2.0       # 日志跟随轮询间隔（秒）
TAIL_CHUNK = 1024 * 1024  # 全量追赶阶段的分块大小（字节）：积压超过此值走 bulk 模式（不逐块渲染 UI）
RAW_LIMIT = 800 * 1024    # 原始行缓存上限（字节），过滤重渲染用

# 历史日志落盘（跟随自动保存）：按 服务/日期 分文件，超期自动清理
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_KEEP_DAYS = 7                     # 保留最近 N 天（含今天），启动时清理超期文件
LOG_VIEW_MAX_BYTES = 20 * 1024 * 1024  # 历史查看单次加载默认上限（可在弹窗就地改并持久化 prefs.view_max_mb）

# AI 日志分析（OpenAI 兼容接口，任意厂商均可）
AI_HTTP_TIMEOUT = 120     # 大模型分析 600 行日志是长响应，超时放宽到 120 秒（流式下为 socket 读超时）
AI_TEST_TIMEOUT = 20      # 「测试连通」小请求超时（秒）
AI_ANALYZE_LINES = 600    # 每次分析取原始行缓存的最近 N 行（与屏蔽/过滤无关，屏蔽只影响显示）
AI_MAX_BYTES = 128 * 1024     # 送模型正文字节上限：600 行超长行（大 JSON/SQL）最坏 800KB 会超模型上下文且成本失控
AI_MAX_OUTPUT_TOKENS = 4096   # 输出 token 上限：防模型超长输出（重复循环）烧钱久等
AI_STACK_FRAMES = 5           # 低档过滤时每个异常保留的 at 堆栈帧数（定位"哪一层抛的"必需）
AI_FLUSH_MS = 80          # 流式增量刷 UI 的节流间隔（毫秒）：攒批写入，避免逐 token 刷新卡顿
AI_DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"  # 预填：智谱开放平台（OpenAI 兼容）
AI_DEFAULT_MODEL = "glm-5.3"

# AI 提供商预置：选中自动带入 base_url 与默认模型；"自定义"= 全手填（Ollama 等）
AI_PROVIDERS = {
    "智谱 GLM": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-5.3"},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "自定义": None,
}

# 分析强度三档：值为"送模型前的行过滤关键字"（None=不过滤，全量送）
AI_ANALYZE_MODES = {
    "只分析异常": ("ERROR", "Exception", "Caused by"),
    "异常和告警": ("ERROR", "WARN", "Exception", "Caused by"),
    "全量分析": None,
}
AI_MODE_DEFAULT = "异常和告警"
AI_HISTORY_KEEP = 20       # 历史分析保留条数（超出丢最旧）

# system 提示词 = 公共骨架（含中文思考要求）+ 档位专属的分析要求
AI_SYSTEM_PROMPT_COMMON = (
    "你是资深 Java/Spring Boot 运维排障专家。用户会给你一段正在运行的服务的日志尾部。\n"
    "请只基于日志中可见的事实分析，不要臆造日志里不存在的信息。\n"
    "【重要】你的思考过程（reasoning）请全程使用中文。\n"
    "输出要求：\n%s\n"
    "用中文分条输出，引用关键日志行时带上时间戳，便于人工复核。"
)
AI_SYSTEM_PROMPT_BY_MODE = {
    "只分析异常": (
        "1. 异常清单：列出所有 ERROR / Exception / 堆栈，逐条给出根因推断与影响面；\n"
        "2. 处置建议：按严重程度排序，给出具体可执行的下一步动作。\n"
        "若给出的日志中没有 ERROR 级问题，直接说明未发现异常即可。"
    ),
    "异常和告警": (
        "1. 异常清单：列出所有 ERROR / Exception / 堆栈，逐条给出根因推断与影响面；\n"
        "2. 告警线索：分析 WARN 级日志（重复告警、超时、重试、降级等），指出可能演变为故障的信号；\n"
        "3. 处置建议：按严重程度排序，给出具体可执行的下一步动作。\n"
        "若日志整体健康无异常，明确说明，并列出仍值得留意的告警。"
    ),
    "全量分析": (
        "1. 异常清单：列出所有 ERROR / Exception / 堆栈，逐条给出根因推断与影响面；\n"
        "2. 可疑模式：重复报错、超时、重试风暴、资源泄漏征兆、业务流程卡住等 WARN 级线索；\n"
        "3. 运行画像：基于 INFO 级日志概述当前业务流（正在执行什么、进度是否正常、有无长时间无输出）；\n"
        "4. 处置建议：按严重程度排序，给出具体可执行的下一步动作。\n"
        "若日志整体健康无异常，明确说明，并列出仍值得留意的信息。"
    ),
}


def build_system_prompt(mode):
    """按分析强度拼 system 提示词（公共骨架 + 档位要求）"""
    return AI_SYSTEM_PROMPT_COMMON % AI_SYSTEM_PROMPT_BY_MODE[mode]


def filter_lines_for_mode(lines, mode):
    """按分析强度过滤要送模型的行：None 档全量保留；
    其余保留含任一关键字的行（ERROR/WARN 大写精确匹配级别，Exception/Caused by 捕获异常与根因行），
    并保留异常行之后连续的 at 堆栈帧（前 AI_STACK_FRAMES 帧，含 "... N more"）——定位哪一层抛的必需"""
    keywords = AI_ANALYZE_MODES.get(mode)
    if keywords is None:
        return list(lines)
    out = []
    stack_left = 0
    for ln in lines:
        if any(k in ln for k in keywords):
            out.append(ln)
            stack_left = AI_STACK_FRAMES
            continue
        s = ln.lstrip()
        if stack_left > 0 and (s.startswith("at ") or s.startswith("... ")):
            out.append(ln)
            stack_left -= 1
        else:
            stack_left = 0  # 堆栈序列被打断（来了普通行），后续 at 不再跟随保留
    return out


def clip_lines_by_bytes(lines, limit=AI_MAX_BYTES):
    """字节双限：从尾部往前累计，超 limit 即停（尾部是最新的，最有分析价值）；
    单行就超 limit 的巨行（超大 JSON 报文等）直接跳过——它没有分析价值还会撑爆上下文。
    返回 (截断后行列表, 是否发生了截断)"""
    total, out = 0, []
    for ln in reversed(lines):
        n = len(ln.encode("utf-8")) + 1
        if n > limit:
            continue  # 巨行跳过（继续收更早的行）
        if total + n > limit:
            break
        total += n
        out.append(ln)
    out.reverse()
    return out, len(out) < len(lines)

# 本项目高频排查目标（irfyiplatform 专用书签）
PRESETS = [
    ("业务包(DEBUG主力)", "com.yunda.module.irfyiplatform"),
    ("SQL包(排查时临时开)", "com.yunda.module.irfyiplatform.dal.mysql"),
    ("定时任务包", "com.yunda.module.irfyiplatform.autotaskmanage"),
    ("API日志拦截器(preHandle刷屏源)", "com.cloud.yunda.framework.apilog.core.interceptor.ApiAccessLogInterceptor"),
    ("ROOT(全局)", "ROOT"),
]

DEFAULT_SERVICES = [
    {"name": "bs-9528(巡检执行)", "base": "http://192.168.10.20:9528"},
    {"name": "manage-9527(管理平台)", "base": "http://192.168.10.20:9527"},
    {"name": "carrying-9529(机器人执行)", "base": "http://192.168.123.162:9529"},
]

# 使用说明弹窗内容：目标服务接入要求 + 配置 demo（以本项目 carrying 真实配置为蓝本）
HELP_TEXT = """\
============================================================
LogView 使用说明 —— 目标服务需要集成什么？
============================================================

【工作原理】
本工具完全基于 Spring Boot Actuator 的 3 类 HTTP 端点工作，
目标服务只需是 Spring Boot 项目并按下文配置，无需安装任何 agent：

  端点                            用途
  GET  /actuator/health          测试连接（顶部「测试连接」按钮）
  GET  /actuator/loggers         查询单个/全部 logger 的当前级别
  POST /actuator/loggers/{名}    动态修改/复原级别，请求体：
        {"configuredLevel":"DEBUG"}  设为 DEBUG（OFF/ERROR/WARN/INFO/TRACE 同理）
        {"configuredLevel":null}     复原（回到继承 / yaml 配置值）
  GET  /actuator/logfile         拉取日志文件内容，支持 HTTP Range 增量
                                 （「实时日志」页签 = tail -f，2 秒轮询）

【接入四要素】（缺一不可，逐项核对）
 (1) Maven 依赖：spring-boot-starter-actuator
 (2) 暴露端点：management.endpoints.web.exposure.include 显式加 loggers,logfile
     —— Spring Boot 默认只暴露 health/info，不配就是 404
 (3) 日志落盘：必须配置 logging.file.name，否则 /actuator/logfile 端点
     根本不会注册，「实时日志」页签不可用（调级功能不受影响）
 (4) 认证放行：有登录拦截的服务须放行 /actuator/**，
     本项目（ydcloudplus 框架）在 yunda.security.permit-all_urls 中配置

【Demo —— 复制即用（以本项目 carrying 服务真实配置为蓝本）】

--- pom.xml -------------------------------------------------
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-actuator</artifactId>
</dependency>

--- application.yaml ----------------------------------------
# (1) 暴露端点（内网可信环境也可用 '*' 全暴露；公网环境严禁 '*'）
management:
  endpoints:
    web:
      exposure:
        include: health,info,loggers,logfile

# (2) 日志落盘（不配则 logfile 端点不存在）
logging:
  file:
    name: ${user.home}/logs/${spring.application.name}.log

# (3) 安全放行（本项目写法；其他项目对应各自的放行机制）
yunda:
  security:
    permit-all_urls:
      - /actuator/**

--- 部署后自检（curl 三连，全过即可用本工具连接）-------------
curl http://<host>:<port>/actuator/health         # 期望 {"status":"UP"}
curl http://<host>:<port>/actuator/loggers/ROOT   # 期望返回 effectiveLevel 字段
curl -I http://<host>:<port>/actuator/logfile     # 期望 HTTP 200 + Content-Length

【注意事项】
 * 动态改级不持久：服务重启后自动回到 yaml 配置值（排查期开、重启自复位，
   属有意设计；但建议排查完点「复原」压回日志量）
 * logfile 端点原生支持 Range：工具增量拉取，日志文件滚动（按天/按大小）
   时自动重置并重拉尾部，无需人工干预
 * 暴露面控制：loggers 可改任意 logger 级别、logfile 可读全量日志，
   请只在可信内网开放；有条件的服务建议用独立管理端口
   （management.server.port）把监控流量与业务流量隔离开
"""


# ---------------------------------------------------------------- HTTP 层
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向：POST 被 302 到登录页时会以 200 假成功，必须原地暴露"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 返回 None → urlopen 抛 HTTPError(30x)，真实原因可见


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _check_framework_error(status, body_bytes, what):
    """框架（ydcloudplus）安全层拒绝时返回 HTTP 200 + {"code":401/403,...} 的 JSON 体，
    状态码层面是"成功"的假象——按 body 特征识别并抛出真实原因"""
    if status == 204 or not body_bytes:
        return None
    text = body_bytes.decode("utf-8", errors="replace").strip()
    if not text.startswith("{"):
        return None  # 非 JSON（如 302 后的 HTML 已被 _NoRedirect 拦截，不会到这里）
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict) and "code" in data and data.get("code") not in (0, 200):
        raise RuntimeError("%s被服务端拒绝：HTTP 200 但业务码 code=%s msg=%s（典型原因：actuator 未加入 "
                           "yunda.security.permit-all_urls 放行清单）" % (what, data.get("code"), data.get("msg")))
    return data


def http_get(base, path):
    """GET 并解析 JSON，失败抛异常（由调用方展示）"""
    url = base.rstrip("/") + path
    with _NO_REDIRECT_OPENER.open(url, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read()
    data = _check_framework_error(resp.status, body, "GET %s " % path)
    return data if data is not None else {}


def http_post_level(base, logger_name, level):
    """POST 修改日志级别。level=None 表示复原（清除显式配置，回到继承/yaml 态）。
    成功=204 或 2xx 空体；2xx 但带框架错误 JSON 体视为失败（安全层 200 假成功）"""
    url = base.rstrip("/") + "/actuator/loggers/" + urllib.parse.quote(logger_name, safe="")
    body = json.dumps({"configuredLevel": level}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with _NO_REDIRECT_OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
        resp_body = resp.read()
    _check_framework_error(resp.status, resp_body, "设置日志级别 ")
    return resp.status  # 204 无正文；非 2xx 抛 HTTPError；2xx+错误体已在上面抛 RuntimeError


def fetch_logger(base, name):
    data = http_get(base, "/actuator/loggers/" + urllib.parse.quote(name, safe=""))
    return data.get("configuredLevel"), data.get("effectiveLevel")


def check_logger_exists(base, name):
    """写操作前的存在性校验。返回 None=名字真实存在；
    返回 list（可能为空）=不存在 + 拼写相近候选名。
    背景：logback 对任意名字惰性接受 POST（204 假成功）且事后查询该名字也能查到值，
    拼错的 logger 名会形成「设置成功+复核通过」的双重假象，只有全量列表能识别。"""
    data = http_get(base, "/actuator/loggers")
    names = set(data.get("loggers", {}))
    if name in names:
        return None
    return difflib.get_close_matches(name, names, n=3, cutoff=0.6)


def search_loggers(base, keyword):
    data = http_get(base, "/actuator/loggers")
    kw = (keyword or "").lower()
    out = []
    for name, info in data.get("loggers", {}).items():
        if kw and kw not in name.lower():
            continue
        out.append((name, info.get("effectiveLevel", "?")))
    out.sort()
    return out


def head_size(base):
    """HEAD /actuator/logfile 拿日志文件总字节数"""
    req = urllib.request.Request(base.rstrip("/") + "/actuator/logfile", method="HEAD")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return int(resp.headers.get("Content-Length", 0))


def range_get_span(base, start, end):
    """Range 闭区间分块拉取 [start, end)：大文件全量追赶时按块传输，
    避免单请求拉整个文件撑爆超时与内存"""
    req = urllib.request.Request(base.rstrip("/") + "/actuator/logfile",
                                 headers={"Range": "bytes=%d-%d" % (start, end - 1)})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        if resp.status == 206:
            return resp.read()
        return b""  # 某些实现不支持 Range 时静默跳过本轮


def chat_completions(base, api_key, model, system, user, max_tokens=None, timeout=None):
    """调 OpenAI 兼容接口 POST /chat/completions（非流式），返回 (回复文本, usage字典)。
    max_tokens/timeout 供「测试连通」类小请求收窄使用。
    HTTP 错误时读出响应体里的错误详情（key 无效 / 模型名错误 / 额度不足等一目了然）"""
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST", headers={
                                     "Content-Type": "application/json",
                                     "Authorization": "Bearer " + api_key,
                                 })
    try:
        with urllib.request.urlopen(req, timeout=timeout or AI_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s %s：%s" % (e.code, e.reason, _http_err_detail(e)))
    content = data["choices"][0]["message"]["content"]
    return content, data.get("usage") or {}


def chat_completions_stream(base, api_key, model, system, user, on_delta,
                            max_tokens=None, cancel_event=None, resp_ref=None):
    """流式版（stream=true, SSE）：逐块回调 on_delta(kind, text)，
    kind="reasoning"（思考过程，GLM/DeepSeek 系的 reasoning_content）或 "content"（最终结论）。
    cancel_event 被置位（或 resp_ref 里的连接被 close）时抛 AiCancelledError 中断。
    返回 usage 字典（流式通常在最后一个 chunk 携带，取不到则为空）。"""
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    })
    try:
        resp = urllib.request.urlopen(req, timeout=AI_HTTP_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s %s：%s" % (e.code, e.reason, _http_err_detail(e)))
    if resp_ref is not None:
        resp_ref["resp"] = resp  # 供取消方直接 close socket（服务端不吐块时 Event 感知不到）
    usage = {}
    try:
        for raw_line in resp:  # SSE：每个 data: 行一个 JSON chunk
            if cancel_event is not None and cancel_event.is_set():
                raise AiCancelledError()
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            if delta.get("reasoning_content"):
                on_delta("reasoning", delta["reasoning_content"])
            if delta.get("content"):
                on_delta("content", delta["content"])
    finally:
        try:
            resp.close()  # 正常结束/取消/异常都断开连接，不泄漏 socket
        except Exception:
            pass
    return usage


def _http_err_detail(e):
    """读 HTTPError 响应体里的错误详情（截断），读不出则给空串"""
    try:
        return e.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        return ""


class AiCancelledError(Exception):
    """用户主动取消 AI 分析（Event 置位或连接被关闭）"""


def force_close_http_response(resp):
    """强制断开流式响应连接：resp.close() 只关 BufferedReader，不能中断另一线程
    阻塞中的 recv（服务端挂住不吐块时取消要等满 120s 超时）；必须对底层 socket
    shutdown+close 才能让阻塞读立即抛错返回"""
    try:
        fp = getattr(resp, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            return
    except Exception:
        pass
    try:
        resp.close()
    except Exception:
        pass


# ---------------------------------------------------------------- 日志跟随线程
class LogTailThread(threading.Thread):
    """后台轮询 /actuator/logfile：开始跟随从头全量加载，追上后转增量跟随。
    - 积压超过 TAIL_CHUNK 走 bulk 模式：数据照常入缓存+落盘，但跳过 UI 增量渲染，
      追赶完成后 on_data(None, True) 通知一次性渲染（防几 MB 数据逐块 insert 卡界面）
    - on_progress(offset) 每块回调，供调用方持久化落盘进度（重开不重复落盘）
    - 文件滚动（total < offset）重置为 0 全量重拉：滚动后的当前文件是全新内容，
      追加落盘天然不与已有部分重叠"""

    def __init__(self, base, on_data, on_status, on_progress=None, start_offset=0):
        super().__init__(daemon=True)
        self.base = base
        self.on_data = on_data      # 回 UI 线程（调用方负责 root.after）
        self.on_status = on_status
        self.on_progress = on_progress
        self.stop_event = threading.Event()
        self.offset = start_offset  # 从上次进度继续（0=从头全量）

    def run(self):
        while not self.stop_event.is_set():
            try:
                # 先 HEAD 总长再决策：
                #   total < offset -> 文件被滚动/清空，重置为 0 全量重拉
                #   total > offset -> Range 分块拉取到本轮 HEAD 的 total（期间新增留待下轮）
                #   total == offset -> 无新数据，静默
                # （注意：直接盲发 bytes=offset- 在无新数据时服务端返回 416，
                #   不能用 416 区分"无数据"和"文件滚动"）
                total = head_size(self.base)
                if total < self.offset:
                    self.offset = 0
                    self._status("检测到日志文件滚动，从头重新加载")
                elif total > self.offset:
                    bulk = (total - self.offset) > TAIL_CHUNK  # 大量积压：只入缓存/落盘不渲染
                    while not self.stop_event.is_set() and self.offset < total:
                        end = min(self.offset + TAIL_CHUNK, total)
                        data = range_get_span(self.base, self.offset, end)
                        if not data:
                            break
                        self.offset += len(data)
                        self._emit(data, bulk)
                        self._progress()
                        if bulk:
                            self._status("加载历史日志中：%.1f / %.1f MB"
                                         % (self.offset / 1048576, total / 1048576))
                    if self.stop_event.is_set():
                        break
                    if bulk:
                        self._emit(None, True)  # 追赶完成：通知 UI 一次性渲染
                    self._status("跟随中：文件 %.1f MB，已读到 %.1f MB"
                                 % (total / 1048576, self.offset / 1048576))
            except urllib.error.HTTPError as e:
                if e.code == 416:  # HEAD 与 GET 之间文件被滚动：兜底重置
                    self.offset = 0
                else:
                    self._status("✗ " + App.friendly_err(e))
            except Exception as e:
                self._status("✗ " + App.friendly_err(e))  # 轮询容错：下一轮重试
            self.stop_event.wait(POLL_INTERVAL)

    def _emit(self, data, bulk=False):
        try:
            self.on_data(data, bulk)
        except Exception:
            pass  # UI 已关闭等场景

    def _progress(self):
        if self.on_progress:
            try:
                self.on_progress(self.offset)
            except Exception:
                pass

    def _status(self, msg):
        try:
            self.on_status(msg)
        except Exception:
            pass


# ---------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        self.services = self.load_config()
        self.tail_thread = None
        self.raw_lines = collections.deque()   # 原始行缓存（含时间戳全行），过滤重渲染用
        self.block_words = self.load_prefs().get("block_words", [])  # 屏蔽词（小写黑名单，持久化）
        self.ai_mode = self.load_prefs().get("ai_mode", AI_MODE_DEFAULT)  # AI 分析强度（持久化）
        self.view_max_mb = self.load_prefs().get("view_max_mb", LOG_VIEW_MAX_BYTES // 1048576)  # 历史日志加载上限（MB，可配）
        self.raw_bytes = 0
        self.pending = ""                      # 跨轮次的不完整行
        self.ai_rs_expanded = False            # 思考过程框展开态（默认收起）
        self._ai_buf = {}                      # 流式增量缓冲 {kind: [text,...]}，攒批刷 UI
        self._ai_flush_after = None            # 节流定时器 id
        self._log_fp = None                    # 历史日志落盘句柄（懒开，随 服务/日期 翻转换文件）
        self._log_fp_key = None                # 当前句柄对应的 (服务名, 日期)
        self._build_ui()
        self._cleanup_old_logs()               # 启动清理超期历史日志
        self.root.after(24 * 3600 * 1000, self._daily_log_cleanup)  # 长开场景每日再清一次

    # ---------- 配置持久化 ----------
    def load_config(self):
        services = []
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    services = json.load(f)
            except Exception:
                services = []
        # 合并默认服务：工具升级新增的默认服务自动补入（按名称去重），用户自定义项保留
        known = {s["name"] for s in services}
        merged = False
        for d in DEFAULT_SERVICES:
            if d["name"] not in known:
                services.append(dict(d))
                merged = True
        if merged:
            self.services = services
            self.save_config()
        return services

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.services, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log("⚠ 配置保存失败：%s" % e)

    # ---------- 界面 ----------
    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("1180x720")
        self.root.minsize(1000, 640)
        # 关窗时优雅收尾：停跟随 + 关落盘句柄（否则日志尾部可能滞留缓冲未刷盘）
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # === 公共顶部：服务选择 ===
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="目标服务：").pack(side="left")
        self.svc_var = tk.StringVar()
        self.svc_box = ttk.Combobox(top, textvariable=self.svc_var, state="readonly", width=32)
        self.svc_box["values"] = [s["name"] for s in self.services]
        self.svc_box.current(0)
        self.svc_box.pack(side="left", padx=(0, 8))
        self.svc_box.bind("<<ComboboxSelected>>", lambda e: self.stop_tail())
        ttk.Button(top, text="测试连接", command=self.on_ping).pack(side="left", padx=2)
        ttk.Button(top, text="＋添加", command=self.on_add_service).pack(side="left", padx=2)
        ttk.Button(top, text="编辑", command=self.on_edit_service).pack(side="left", padx=2)
        ttk.Button(top, text="－删除", command=self.on_del_service).pack(side="left", padx=2)
        ttk.Button(top, text="? 使用说明", command=self.show_help).pack(side="right", padx=2)

        # === 双页签 ===（实时日志为核心功能，放首位且默认选中）
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)
        self.tab_level = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        self.nb.add(self.tab_log, text=" 实时日志(tail) ")
        self.nb.add(self.tab_level, text=" 日志级别调级 ")
        self._build_level_tab()
        self._build_log_tab()

    # ---------- Tab1：调级 ----------
    def _build_level_tab(self):
        pad = {"padx": 8, "pady": 4}
        t = self.tab_level

        row2 = ttk.LabelFrame(t, text="常用目标（点击自动填入并查询）")
        row2.pack(fill="x", **pad)
        for label, logger in PRESETS:
            ttk.Button(row2, text=label, width=12,
                       command=lambda l=logger: self.preset(l)).pack(side="left", padx=3, pady=3)

        row3 = ttk.Frame(t)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="Logger：").pack(side="left")
        self.name_var = tk.StringVar(value=PRESETS[0][1])
        entry = ttk.Entry(row3, textvariable=self.name_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.on_query())
        ttk.Button(row3, text="查询", command=self.on_query).pack(side="left")
        ttk.Button(row3, text="浏览全部(搜索)", command=self.on_browse).pack(side="left", padx=6)

        row4 = ttk.LabelFrame(t, text="当前状态")
        row4.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="尚未查询。configuredLevel 为空 = 未显式配置（继承父包 / yaml 值）")
        ttk.Label(row4, textvariable=self.status_var, foreground="#0055aa").pack(anchor="w", padx=6, pady=4)

        row5 = ttk.LabelFrame(t, text="级别操作（立即生效，无需重启；重启后回到 yaml 配置值）")
        row5.pack(fill="x", **pad)
        self.level_var = tk.StringVar(value="DEBUG")
        for lv in ("OFF", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"):
            ttk.Radiobutton(row5, text=lv, value=lv, variable=self.level_var).pack(side="left", padx=6)
        btns = ttk.Frame(row5)
        btns.pack(side="right", padx=6)
        ttk.Button(btns, text="应用级别", command=self.on_apply).pack(side="left", padx=3)
        ttk.Button(btns, text="复原(清除显式配置)", command=self.on_reset).pack(side="left", padx=3)

        lf = ttk.LabelFrame(t, text="操作记录")
        lf.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(lf, height=8, state="disabled", font=("Consolas", 9), wrap="none")
        scroll = ttk.Scrollbar(lf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        self.log("提示：改完记得「复原」。SQL 包临时开 DEBUG 排查完务必压回 INFO。")

    # ---------- Tab2：实时日志 ----------
    def _build_log_tab(self):
        t = self.tab_log

        # === 左右分栏：左=日志跟随区，右=AI 分析栏（PanedWindow，分隔条可拖动调宽） ===
        pw = ttk.PanedWindow(t, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        left = ttk.Frame(pw, width=700)
        right = ttk.Frame(pw, width=430)
        pw.add(left, weight=1)
        pw.add(right, weight=0)

        # ---- 左栏第一行：跟随控制 + 过滤（短标签+弹性输入框，缩放时按钮不会被挤掉） ----
        bar = ttk.Frame(left)
        bar.pack(fill="x")
        self.tail_btn_var = tk.StringVar(value="开始跟随")
        ttk.Button(bar, textvariable=self.tail_btn_var, width=10, command=self.toggle_tail).pack(side="left")
        ttk.Button(bar, text="清屏", width=6, command=self.clear_log_view).pack(side="left", padx=4)
        ttk.Button(bar, text="历史日志", width=9, command=self.show_log_history).pack(side="left", padx=4)
        ttk.Label(bar, text="过滤：").pack(side="left", padx=(8, 2))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.render_log_view())
        ttk.Entry(bar, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动滚动", variable=self.autoscroll_var).pack(side="left")

        # ---- 左栏第二行：屏蔽词操作（短标签 + 弹性输入框 + 按钮组，缩放时按钮不会被挤掉） ----
        bar2 = ttk.Frame(left)
        bar2.pack(fill="x", pady=(0, 2))
        ttk.Label(bar2, text="屏蔽词：").pack(side="left")
        self.block_var = tk.StringVar()
        block_entry = ttk.Entry(bar2, textvariable=self.block_var)  # 弹性伸缩，窗口变窄时先压它
        block_entry.pack(side="left", fill="x", expand=True, padx=(0, 3))
        block_entry.bind("<Return>", lambda e: self.on_block_add())
        ttk.Button(bar2, text="＋屏蔽", width=7, command=self.on_block_add).pack(side="left", padx=2)
        ttk.Button(bar2, text="删选中", width=7, command=self.on_block_del).pack(side="left")
        ttk.Button(bar2, text="清空屏蔽", width=9, command=self.on_block_clear).pack(side="left", padx=2)
        # 屏蔽词列表独占一行（选中某词后点「删选中」移除；多词时可滚动）
        self.block_list = tk.Listbox(left, height=2, exportselection=False, font=("Consolas", 9))
        self.block_list.pack(fill="x", pady=(0, 2))

        self.tail_status_var = tk.StringVar(value="未开始。点「开始跟随」= 从日志文件尾部开始实时拉取（等价 tail -f）")
        ttk.Label(left, textvariable=self.tail_status_var, foreground="#0055aa").pack(anchor="w")

        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True, pady=(2, 0))
        self.view_text = tk.Text(lf, state="disabled", width=40, font=("Consolas", 9), wrap="none",
                                 background="#101418", foreground="#d0d7de")
        vs = ttk.Scrollbar(lf, command=self.view_text.yview)
        hs = ttk.Scrollbar(lf, orient="horizontal", command=self.view_text.xview)
        self.view_text.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.view_text.pack(fill="both", expand=True)
        self._refresh_block_list()  # 显示已持久化的屏蔽词

        # ---- 右栏：AI 分析结果（上=思考过程可收起，下=最终结论） ----
        self._build_ai_pane(right)

    def _build_ai_pane(self, parent):
        # 取消分析所需状态（on_ai_analyze 开始时 clear / 置位）
        self._ai_cancel_evt = threading.Event()
        self._ai_resp_ref = {"resp": None}
        self._ai_gen = 0

        # 操作行：强度下拉 + 分析 + 设置 + 历史，独立整行（在左右分栏内的右栏顶部，
        # 不与左栏跟随控件同行，窗口缩放时不会被挤出可视区）
        ops = ttk.Frame(parent)
        ops.pack(fill="x", pady=(0, 2))
        self.ai_mode_var = tk.StringVar(value=self.ai_mode)
        mode_box = ttk.Combobox(ops, textvariable=self.ai_mode_var, state="readonly",
                                width=10, values=list(AI_ANALYZE_MODES.keys()))
        mode_box.pack(side="left")
        mode_box.bind("<<ComboboxSelected>>", self._on_ai_mode_change)
        self.ai_btn = ttk.Button(ops, text="AI 分析(600行)", command=self.on_ai_analyze)
        self.ai_btn.pack(side="left", padx=4)
        ttk.Button(ops, text="AI 设置", command=self.show_ai_settings).pack(side="left")
        ttk.Button(ops, text="历史", width=6, command=self.show_ai_history).pack(side="left", padx=4)

        # 状态行：状态文字（左，弹性）+ 复制结论/导出（右）
        state_row = ttk.Frame(parent)
        state_row.pack(fill="x", pady=(0, 2))
        self.ai_state_var = tk.StringVar(value="未开始（点「AI 分析」）")
        ttk.Label(state_row, textvariable=self.ai_state_var,
                  foreground="#0055aa", wraplength=250).pack(side="left", fill="x", expand=True)
        self.ai_copy_btn = ttk.Button(state_row, text="复制结论", width=8, command=self.on_copy_result)
        self.ai_copy_btn.pack(side="right", padx=2)
        ttk.Button(state_row, text="导出txt", width=8, command=self.on_export_result).pack(side="right", padx=2)

        # 思考过程：默认收起（只显示标题按钮），点按钮展开/收起；收起期间内容照常后台累积
        self.ai_rs_btn = ttk.Button(parent, text="▶ 思考过程（默认收起）", width=26,
                                    command=self._toggle_reasoning)
        self.ai_rs_btn.pack(anchor="w", pady=(0, 2))
        # 注意：Text 默认请求宽 80 字符（≈600px）会把右 pane 撑得极宽并挤压左栏，
        # 必须显式给小请求宽，实际显示宽由 pack 的 fill/expand 决定
        self.ai_rs_text = tk.Text(parent, wrap="word", height=12, width=44, state="disabled",
                                  font=("Consolas", 9), background="#151b22",
                                  foreground="#9ab0c4")  # 暗蓝色调与结论区分

        self.result_lf = ttk.LabelFrame(parent, text="分析结论")
        self.result_lf.pack(fill="both", expand=True)
        self.ai_text = tk.Text(self.result_lf, wrap="word", width=42, state="disabled",
                               font=("Microsoft YaHei UI", 10),
                               background="#101418", foreground="#d0d7de")
        rvs = ttk.Scrollbar(self.result_lf, orient="vertical", command=self.ai_text.yview)
        self.ai_text.configure(yscrollcommand=rvs.set)
        rvs.pack(side="right", fill="y")
        self.ai_text.pack(fill="both", expand=True, padx=2, pady=2)

    # ---------- 工具方法 ----------
    @staticmethod
    def load_prefs():
        if os.path.exists(PREFS_FILE):
            try:
                with open(PREFS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
        return {}

    def save_prefs(self):
        try:
            with open(PREFS_FILE, "w", encoding="utf-8") as f:
                json.dump({"block_words": self.block_words, "ai_mode": self.ai_mode,
                           "view_max_mb": self.view_max_mb},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 持久化失败不影响当次使用

    def _line_visible(self, ln):
        """统一行显示判定：含过滤词(若设) 且 不含任何屏蔽词"""
        low = ln.lower()
        kw = self.filter_var.get().strip().lower()
        if kw and kw not in low:
            return False
        for b in self.block_words:
            if b in low:
                return False
        return True

    def current_base(self):
        for s in self.services:
            if s["name"] == self.svc_var.get():
                return s["base"]
        return None

    def log(self, msg):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "[%s] %s\n" % (stamp, msg))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def run_bg(self, fn, on_ok, on_err=None):
        def worker():
            try:
                result = fn()
                self.root.after(0, lambda: on_ok(result))
            except Exception as e:
                err = self.friendly_err(e)
                self.root.after(0, lambda: (on_err(err) if on_err else self.log("✗ " + err)))
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def friendly_err(e):
        # 注意顺序：HTTPError 是 URLError 的子类，必须先判 HTTPError，
        # 否则 404/401 会被误报成"连接失败（网络不通）"
        if isinstance(e, urllib.error.HTTPError):
            return "HTTP %s %s（检查端点是否暴露 / 路径是否正确）" % (e.code, e.reason)
        if isinstance(e, urllib.error.URLError):
            return "连接失败（服务不可达 / 端口未开 / 网络不通）：%s" % e.reason
        if isinstance(e, RuntimeError):
            return str(e)  # chat 系列主动抛的 HTTP 详情错误，直接给原文
        return "异常：%r" % e

    # ---------- 调级事件 ----------
    def preset(self, logger):
        self.name_var.set(logger)
        self.on_query()

    def on_ping(self):
        base = self.current_base()
        if not base:
            return
        self.log("→ 测试连接 %s" % base)

        def ok(d):
            status = d.get("status", "?")
            self.log("✓ 连接正常，服务状态：%s" % status)
            messagebox.showinfo(
                "测试连接",
                "✓ 连接成功\n\n服务：%s\n地址：%s\n健康状态：%s" % (self.svc_var.get(), base, status))

        def err(e):
            self.log("✗ " + e)
            messagebox.showerror("测试连接", "✗ 连接失败\n\n服务：%s\n地址：%s\n\n%s" % (
                self.svc_var.get(), base, e))

        self.run_bg(lambda: http_get(base, "/actuator/health"), ok, err)

    def show_help(self):
        """使用说明弹窗：目标服务接入四要素 + 配置 demo，可一键复制发给服务负责人"""
        win = tk.Toplevel(self.root)
        win.title("使用说明 - 目标服务接入要求")
        win.geometry("840x680")

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=6, pady=4)
        copy_btn = ttk.Button(bar, text="复制全部内容")

        def copy_all():
            self.root.clipboard_clear()
            self.root.clipboard_append(txt.get("1.0", "end-1c"))
            copy_btn.configure(text="✓ 已复制")
            win.after(2000, lambda: copy_btn.configure(text="复制全部内容"))

        copy_btn.configure(command=copy_all)
        copy_btn.pack(side="left")
        ttk.Label(bar, text="（复制后可直接发给目标服务的负责人照做）").pack(side="left", padx=8)

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=6, pady=4)
        txt = tk.Text(body, wrap="none", font=("Consolas", 9),
                      background="#101418", foreground="#d0d7de")
        vs = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        hs = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")

    def on_add_service(self):
        self._service_dialog(mode="add")

    def on_edit_service(self):
        """编辑当前选中服务的名称/地址：预填现值，保存后刷新下拉并停跟随（地址已变）"""
        if not any(s["name"] == self.svc_var.get() for s in self.services):
            messagebox.showwarning("提示", "当前服务不在清单中，无法编辑")
            return
        self._service_dialog(mode="edit")

    def _service_dialog(self, mode):
        """服务新增/编辑共用弹窗"""
        old = next((s for s in self.services if s["name"] == self.svc_var.get()), None) \
            if mode == "edit" else None
        win = tk.Toplevel(self.root)
        win.title("编辑服务" if mode == "edit" else "添加服务")
        win.geometry("380x150")
        ttk.Label(win, text="名称：").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        name_e = ttk.Entry(win, width=30)
        name_e.grid(row=0, column=1, padx=6, pady=6)
        ttk.Label(win, text="地址(host:port)：").grid(row=1, column=0, padx=6, pady=6, sticky="e")
        base_e = ttk.Entry(win, width=30)
        base_e.grid(row=1, column=1, padx=6, pady=6)
        if old:
            name_e.insert(0, old["name"])
            base_e.insert(0, old["base"])
        else:
            base_e.insert(0, "http://")

        def save():
            name, base = name_e.get().strip(), base_e.get().strip()
            if not name or not base:
                messagebox.showwarning("提示", "名称和地址都要填", parent=win)
                return
            if not base.startswith("http"):
                base = "http://" + base
            if mode == "edit":
                if any(s["name"] == name and s is not old for s in self.services):
                    messagebox.showwarning("提示", "名称「%s」已被其他服务占用" % name, parent=win)
                    return
                old_name = old["name"]
                old.update(name=name, base=base)
                if name != old_name:
                    self.log("（服务更名 %s -> %s；历史日志目录仍按旧名存放，可在「历史日志」里查看）"
                             % (old_name, name))
                self.stop_tail()  # 地址可能已变，旧跟随连接作废
            else:
                self.services.append({"name": name, "base": base})
            self.save_config()
            self.svc_box["values"] = [s["name"] for s in self.services]
            self.svc_box.set(name)
            win.destroy()
            self.log("✓ 已%s服务 %s -> %s" % ("编辑" if mode == "edit" else "添加", name, base))

        ttk.Button(win, text="保存", command=save).grid(row=2, column=0, columnspan=2, pady=8)

    def on_del_service(self):
        name = self.svc_var.get()
        if len(self.services) <= 1:
            messagebox.showwarning("提示", "至少保留一个服务")
            return
        if not messagebox.askyesno("确认", "删除服务「%s」？" % name):
            return
        self.stop_tail()
        self.services = [s for s in self.services if s["name"] != name]
        self.save_config()
        self.svc_box["values"] = [s["name"] for s in self.services]
        self.svc_box.current(0)
        self.log("✓ 已删除服务 %s" % name)

    def on_query(self, expected=None):
        base, name = self.current_base(), self.name_var.get().strip()
        if not base or not name:
            messagebox.showwarning("提示", "请先填写 Logger 名")
            return
        self.log("→ 查询 %s  %s" % (self.svc_var.get(), name))
        self.run_bg(lambda: fetch_logger(base, name),
                    lambda pair: self._show_level(pair, expected))

    def _show_level(self, pair, expected=None):
        configured, effective = pair
        conf = configured if configured is not None else "(空=继承)"
        self.status_var.set("configuredLevel = %s    effectiveLevel = %s" % (conf, effective))
        self.log("✓ configured=%s effective=%s" % (conf, effective))
        if expected is not None and configured != expected:
            self.log("⚠ 服务端应答成功，但复核 configuredLevel=%s ≠ 所设 %s——调级未真正生效" % (conf, expected))

    def on_apply(self):
        base, name = self.current_base(), self.name_var.get().strip()
        if not base or not name:
            messagebox.showwarning("提示", "请先填写 Logger 名")
            return
        level = self.level_var.get()

        def ok(_):
            self.log("✓ 已设为 %s（立即生效，重启后回到 yaml 值）" % level)
            self.on_query(expected=level)  # 复核：configured 不等于所设值时显式告警

        def do_post():
            cands = check_logger_exists(base, name)
            if cands is not None:  # 名字不存在：拼错名 POST 会 204 假成功，直接拦截并给候选
                hint = ("，你是不是要找：%s" % " / ".join(cands)) if cands else ""
                raise RuntimeError(
                    "Logger 名「%s」在该服务的 logger 列表中不存在%s。\n"
                    "服务端对不存在的名字也会应答成功（logback 惰性创建，查询也查得到），"
                    "但不会影响任何真实日志——这就是「显示成功但级别没生效」的根因。\n"
                    "请核对名称，或点「浏览全部(搜索)」选取。" % (name, hint))
            return http_post_level(base, name, level)

        self.log("→ 设置 %s  %s -> %s" % (self.svc_var.get(), name, level))
        self.run_bg(do_post, ok)

    def on_reset(self):
        base, name = self.current_base(), self.name_var.get().strip()
        if not base or not name:
            messagebox.showwarning("提示", "请先填写 Logger 名")
            return

        def ok(_):
            self.log("✓ 已复原（清除显式配置，回到继承 / yaml 态）")
            self.on_query()

        def do_reset():
            cands = check_logger_exists(base, name)
            if cands is not None:  # 复原一个不存在的名字同样无意义，拦截提示
                hint = ("，你是不是要找：%s" % " / ".join(cands)) if cands else ""
                raise RuntimeError("Logger 名「%s」在该服务的 logger 列表中不存在%s，复原无意义。" % (name, hint))
            return http_post_level(base, name, None)

        self.log("→ 复原 %s  %s（POST configuredLevel=null）" % (self.svc_var.get(), name))
        self.run_bg(do_reset, ok)

    def on_browse(self):
        base = self.current_base()
        if not base:
            return
        win = tk.Toplevel(self.root)
        win.title("浏览全部 loggers - %s" % self.svc_var.get())
        win.geometry("640x520")

        top = ttk.Frame(win)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="过滤：").pack(side="left")
        kw_var = tk.StringVar()

        cols = ("name", "level")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        tree.heading("name", text="Logger 名")
        tree.heading("level", text="effective")
        tree.column("name", width=480)
        tree.column("level", width=90, anchor="center")
        scroll = ttk.Scrollbar(win, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", padx=(0, 6))
        tree.pack(fill="both", expand=True, padx=6, pady=4)

        def do_filter():
            kw = kw_var.get().strip()
            tree.delete(*tree.get_children())
            for n, lv in search_loggers(base, kw):
                tree.insert("", "end", values=(n, lv))

        ttk.Entry(top, textvariable=kw_var, width=40).pack(side="left", padx=4)
        kw_var.trace_add("write", lambda *_: self.run_bg(do_filter, lambda _r: None))
        ttk.Button(top, text="刷新", command=lambda: self.run_bg(do_filter, lambda _r: None)).pack(side="left")

        def pick(_event):
            sel = tree.selection()
            if sel:
                self.name_var.set(tree.item(sel[0], "values")[0])
                win.destroy()
                self.on_query()

        tree.bind("<Double-1>", pick)
        self.log("→ 拉取 %s 全量 loggers" % self.svc_var.get())
        self.run_bg(do_filter, lambda _r: None)

    # ---------- 实时日志 ----------
    # ---------- 屏蔽词事件 ----------
    def _refresh_block_list(self):
        self.block_list.delete(0, "end")
        for w in self.block_words:
            self.block_list.insert("end", w)

    def on_block_add(self):
        word = self.block_var.get().strip().lower()
        if not word:
            return
        if word not in self.block_words:  # 去重
            self.block_words.append(word)
        self.block_var.set("")
        self._refresh_block_list()
        self.save_prefs()
        self.render_log_view()  # 立即按新屏蔽词重渲染

    def on_block_del(self):
        for idx in reversed(self.block_list.curselection()):
            del self.block_words[idx]
        self._refresh_block_list()
        self.save_prefs()
        self.render_log_view()

    def on_block_clear(self):
        if not self.block_words:
            return
        self.block_words.clear()
        self._refresh_block_list()
        self.save_prefs()
        self.render_log_view()

    def toggle_tail(self):
        # 已请求停止（stop_event 置位）的旧线程即使还没退出也可直接开新的：
        # 否则"停止后立刻再开始"会被误判为"再停一次"，跟随开不起来
        if (self.tail_thread and self.tail_thread.is_alive()
                and not self.tail_thread.stop_event.is_set()):
            self.stop_tail()
            return
        base = self.current_base()
        if not base:
            return
        self.pending = ""
        start_offset = self._load_tail_progress()   # 上次落盘进度：重开不重复落盘/重读
        self.tail_thread = LogTailThread(
            base,
            on_data=lambda data, bulk: self.root.after(0, lambda: self.append_log_data(data, bulk)),
            on_status=lambda msg: self.root.after(0, lambda: self.tail_status_var.set(msg)),
            on_progress=lambda off: self.root.after(0, lambda: self._save_tail_progress(off)),
            start_offset=start_offset,
        )
        self.tail_thread.start()
        self.tail_btn_var.set("停止跟随")
        self.log("→ 开始跟随 %s 的日志文件（从头全量加载后实时追加）" % self.svc_var.get())

    def stop_tail(self):
        if self.tail_thread and self.tail_thread.is_alive():
            self.tail_thread.stop_event.set()
            self.tail_status_var.set("已停止跟随")
            self.tail_btn_var.set("开始跟随")
            self.log("→ 停止跟随")
        self._log_close()  # 落盘句柄随停跟随关闭（下次开始跟随重新懒开）

    def append_log_data(self, data, bulk=False):
        """新数据到达：解码、按行补齐、入缓存、落盘；bulk=False 时增量渲染。
        data=None 是"全量追赶完成"信号：按当前过滤/屏蔽一次性渲染缓存"""
        if data is None:
            self.render_log_view()
            return
        text = self.pending + data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self.pending = lines.pop()  # 最后一段可能不完整，留待下轮
        chunk = []
        for ln in lines:
            self.raw_lines.append(ln)
            self.raw_bytes += len(ln) + 1
            if not bulk and self._line_visible(ln):
                chunk.append(ln)
        self._trim_raw()
        if lines:
            self._log_write(lines)  # 完整行落盘（不受过滤/屏蔽/bulk 影响，存原始全量）
        if chunk:
            self._view_insert("\n".join(chunk) + "\n")

    def _tail_progress_path(self):
        return os.path.join(LOG_DIR, self._safe_filename(self.svc_var.get()), "progress.json")

    def _load_tail_progress(self):
        """读取上次跟随的落盘进度（同一天才有效）；无进度返回 0=从头全量"""
        try:
            with open(self._tail_progress_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == datetime.date.today().strftime("%Y-%m-%d"):
                return int(d.get("offset", 0))
        except Exception:
            pass
        return 0

    def _save_tail_progress(self, offset):
        """持久化落盘进度（每分块一次，文件极小，直接写）"""
        try:
            os.makedirs(os.path.dirname(self._tail_progress_path()), exist_ok=True)
            with open(self._tail_progress_path(), "w", encoding="utf-8") as f:
                json.dump({"date": datetime.date.today().strftime("%Y-%m-%d"),
                           "offset": offset}, f)
        except Exception:
            pass  # 进度丢失的代价只是下次全量重拉，不影响正确性

    # ---------- 历史日志落盘 / 查看 / 清理 ----------
    def _log_write(self, lines):
        """跟随日志追加落盘：data/logs/{服务}/{yyyy-MM-dd}.log。
        句柄懒开，服务切换或跨天自动换文件；失败记日志并关闭，下轮重开重试"""
        today = datetime.date.today().strftime("%Y-%m-%d")
        key = (self.svc_var.get(), today)
        if key != self._log_fp_key:
            self._log_close()
            try:
                svc_dir = os.path.join(LOG_DIR, self._safe_filename(key[0]))
                os.makedirs(svc_dir, exist_ok=True)
                self._log_fp = open(os.path.join(svc_dir, key[1] + ".log"), "a", encoding="utf-8")
                self._log_fp_key = key
            except Exception as e:
                self.log("⚠ 历史日志落盘失败：%s" % e)
                self._log_fp_key = None
                return
        try:
            self._log_fp.write("\n".join(lines) + "\n")
            self._log_fp.flush()
        except Exception as e:
            self.log("⚠ 历史日志落盘失败：%s" % e)
            self._log_close()

    def _log_close(self):
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None
            self._log_fp_key = None

    def _on_close(self):
        """主窗口关闭：停跟随、刷盘、退出"""
        self.stop_tail()
        self._log_close()
        self.root.destroy()

    def _cleanup_old_logs(self):
        """删除超过保留天数的历史日志（文件名 yyyy-MM-dd.log，字符串比较即日期比较）。
        保留最近 LOG_KEEP_DAYS 天（含今天），即删除日期 < 今天-(N-1) 的文件"""
        if not os.path.isdir(LOG_DIR):
            return
        cutoff = (datetime.date.today()
                  - datetime.timedelta(days=LOG_KEEP_DAYS - 1)).strftime("%Y-%m-%d")
        removed = 0
        try:
            for svc in os.listdir(LOG_DIR):
                svc_dir = os.path.join(LOG_DIR, svc)
                if not os.path.isdir(svc_dir):
                    continue
                for fn in os.listdir(svc_dir):
                    if fn.endswith(".log") and fn[:-4] < cutoff:
                        try:
                            os.remove(os.path.join(svc_dir, fn))
                            removed += 1
                        except Exception:
                            pass
        except Exception:
            pass
        if removed:
            self.log("已自动清理 %d 个超过 %d 天的历史日志文件" % (removed, LOG_KEEP_DAYS))

    def _daily_log_cleanup(self):
        """长开场景：每 24 小时再清一次（跨天不重启也能清）"""
        self._cleanup_old_logs()
        self.root.after(24 * 3600 * 1000, self._daily_log_cleanup)

    def show_log_history(self):
        """历史日志查看：左树（服务→日期）右内容；大文件只载尾部 LOG_VIEW_MAX_BYTES"""
        win = tk.Toplevel(self.root)
        win.title("历史日志（跟随自动保存，保留 %d 天）" % LOG_KEEP_DAYS)
        win.geometry("1020x640")

        paned = ttk.PanedWindow(win, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6, pady=6)

        # 左：服务→日期 树（values 存文件完整路径，服务父节点 values 为空）
        tree = ttk.Treeview(paned, show="tree", selectmode="browse")
        tree.column("#0", width=240)
        paned.add(tree, weight=1)

        info_var = tk.StringVar(value="选中左侧日期查看（双击也可）")
        body = ttk.Frame(paned)
        paned.add(body, weight=3)

        # 筛选行：过滤（白名单）+ 排除（黑名单）+ 加载上限（MB，就地配置并持久化）
        flt = ttk.Frame(body)
        flt.pack(fill="x", pady=(0, 2))
        ttk.Label(flt, text="过滤：").pack(side="left")
        kw_var = tk.StringVar()
        ttk.Entry(flt, textvariable=kw_var, width=24).pack(side="left", padx=(0, 10))
        ttk.Label(flt, text="排除：").pack(side="left")
        ex_var = tk.StringVar()
        ttk.Entry(flt, textvariable=ex_var, width=24).pack(side="left", padx=(0, 10))
        ttk.Label(flt, text="上限MB：").pack(side="left")
        mb_var = tk.StringVar(value=str(self.view_max_mb))  # 初始为上次确认过的值（prefs 持久化）
        mb_e = ttk.Entry(flt, textvariable=mb_var, width=5)
        mb_e.pack(side="left")
        mb_e.bind("<Return>", lambda e: apply_mb())  # 回车等价确认（需在 apply_mb 定义后重绑，见下）
        mb_btn = ttk.Button(flt, text="确认", width=5)
        mb_btn.pack(side="left", padx=3)
        ttk.Label(body, textvariable=info_var, foreground="#0055aa", wraplength=640).pack(anchor="w")
        txt = tk.Text(body, wrap="none", state="disabled", font=("Consolas", 9),
                      background="#101418", foreground="#d0d7de")
        vs = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        hs = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        txt.pack(fill="both", expand=True)

        loaded = {"lines": [], "path": "", "note": ""}  # 已加载原始行（内存），筛选变化不重读文件
        debounce = {"after": None}

        def on_flt_change(*_):
            if debounce["after"] is not None:
                win.after_cancel(debounce["after"])
            debounce["after"] = win.after(200, lambda: (debounce.update(after=None), render()))

        kw_var.trace_add("write", on_flt_change)
        ex_var.trace_add("write", on_flt_change)

        def apply_mb():
            """点「确认」才生效：校验 → 持久化（记住本次值）→ 按新上限重载当前文件"""
            try:
                mb = int(mb_var.get().strip())
            except ValueError:
                info_var.set("✗ 上限需为 1~2048 的整数，未生效")
                return
            if not 1 <= mb <= 2048:
                info_var.set("✗ 上限需为 1~2048 的整数，未生效")
                return
            self.view_max_mb = mb
            self.save_prefs()
            mb_btn.configure(text="✓")
            win.after(1500, lambda: mb_btn.configure(text="确认"))
            load()

        mb_btn.configure(command=apply_mb)

        def render():
            """按 过滤/排除 条件渲染已加载内容（含过滤词且不含排除词，大小写不敏感）"""
            kw, ex = kw_var.get().strip().lower(), ex_var.get().strip().lower()
            lines = loaded["lines"]
            if kw or ex:
                shown = [ln for ln in lines
                         if (not kw or kw in ln.lower()) and (not ex or ex not in ln.lower())]
            else:
                shown = lines  # 快路径：无条件时跳过 10 万级逐行扫描
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            if shown:
                txt.insert("1.0", "\n".join(shown) + "\n")
            txt.configure(state="disabled")
            if loaded["path"]:
                extra = "，筛选后 %d / %d 行" % (len(shown), len(lines)) if (kw or ex) else ""
                info_var.set("%s  %d 行%s %s" % (loaded["path"], len(lines), extra, loaded["note"]))

        def reload_tree():
            tree.delete(*tree.get_children())
            if not os.path.isdir(LOG_DIR):
                return
            for svc in sorted(os.listdir(LOG_DIR)):
                svc_dir = os.path.join(LOG_DIR, svc)
                if not os.path.isdir(svc_dir):
                    continue
                parent = tree.insert("", "end", text=svc, open=False)
                for fn in sorted(os.listdir(svc_dir), reverse=True):  # 新日期在前
                    if not fn.endswith(".log"):
                        continue
                    path = os.path.join(svc_dir, fn)
                    size_kb = max(1, os.path.getsize(path) // 1024)
                    tree.insert(parent, "end", text="%s（%dKB）" % (fn[:-4], size_kb),
                                values=(path,))

        def load(_event=None):
            sel = tree.selection()
            if not sel:
                return
            path = tree.item(sel[0], "values")
            if not path:
                return  # 服务父节点无文件
            path = path[0]
            try:
                size = os.path.getsize(path)
                limit = self.view_max_mb * 1048576
                with open(path, "rb") as f:
                    if size > limit:
                        f.seek(-limit, 2)  # 只载尾部，跳过首个不完整行
                        raw = f.read()
                        nl = raw.find(b"\n")
                        if nl >= 0:
                            raw = raw[nl + 1:]
                        note = "（文件 %.1fMB，仅显示尾部 %dMB）" % (
                            size / 1048576, self.view_max_mb)
                    else:
                        raw = f.read()
                        note = ""
                loaded.update(lines=raw.decode("utf-8", errors="replace").splitlines(),
                              path=path, note=note)
                render()
            except Exception as e:
                info_var.set("✗ 读取失败：%s" % e)

        tree.bind("<<TreeviewSelect>>", load)
        tree.bind("<Double-1>", load)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="刷新列表", command=reload_tree).pack(side="left")
        copy_btn = ttk.Button(bar, text="复制当前内容")
        copy_btn.pack(side="left", padx=6)

        def copy_all():
            content = txt.get("1.0", "end-1c")
            if not content:
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            copy_btn.configure(text="✓ 已复制")
            win.after(2000, lambda: copy_btn.configure(text="复制当前内容"))

        copy_btn.configure(command=copy_all)
        reload_tree()

    def _trim_raw(self):
        while self.raw_bytes > RAW_LIMIT and len(self.raw_lines) > 1:
            self.raw_bytes -= len(self.raw_lines.popleft()) + 1

    def render_log_view(self):
        """过滤/屏蔽条件变化时：从原始行缓存全量重渲染"""
        self.view_text.configure(state="normal")
        self.view_text.delete("1.0", "end")
        shown = [ln for ln in self.raw_lines if self._line_visible(ln)]
        if shown:
            self.view_text.insert("end", "\n".join(shown) + "\n")
        self.view_text.see("end")
        self.view_text.configure(state="disabled")

    def _view_insert(self, text):
        self.view_text.configure(state="normal")
        self.view_text.insert("end", text)
        # 视图缓冲保护：超过 2 倍原始缓存时删头部
        if float(self.view_text.index("end-1c").split(".")[0]) > 12000:
            self.view_text.delete("1.0", "2000.0")
        if self.autoscroll_var.get():
            self.view_text.see("end")
        self.view_text.configure(state="disabled")

    def clear_log_view(self):
        self.view_text.configure(state="normal")
        self.view_text.delete("1.0", "end")
        self.view_text.configure(state="disabled")
        self.tail_status_var.set("已清屏（继续跟随中，只清显示不清缓存）")

    # ---------- AI 日志分析 ----------
    def load_ai_config(self):
        if os.path.exists(AI_CONFIG_FILE):
            try:
                with open(AI_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
        return {}

    def save_ai_config(self, cfg):
        try:
            with open(AI_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showwarning("提示", "AI 配置保存失败：%s" % e)

    def show_ai_settings(self):
        """AI 设置弹窗：提供商（智谱 GLM / DeepSeek / 自定义）选中自动带入地址与默认模型，
        三要素（base_url/model/api_key）+ 测试连通，持久化"""
        cfg = self.load_ai_config()
        win = tk.Toplevel(self.root)
        win.title("AI 设置（OpenAI 兼容接口）")
        win.geometry("620x360")

        provider = cfg.get("provider")
        if provider not in AI_PROVIDERS:  # 老配置无 provider 字段 → 按自定义处理（原有值保留可改）
            provider = "自定义"

        ttk.Label(win, text="提供商：").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        provider_var = tk.StringVar(value=provider)
        provider_box = ttk.Combobox(win, textvariable=provider_var, state="readonly",
                                    width=20, values=list(AI_PROVIDERS.keys()))
        provider_box.grid(row=0, column=1, padx=6, sticky="w")

        ttk.Label(win, text="Base URL：").grid(row=1, column=0, sticky="e", padx=6, pady=6)
        base_e = ttk.Entry(win, width=56)
        base_e.insert(0, cfg.get("base_url", AI_DEFAULT_BASE))
        base_e.grid(row=1, column=1, padx=6)

        ttk.Label(win, text="模型名：").grid(row=2, column=0, sticky="e", padx=6, pady=6)
        model_e = ttk.Entry(win, width=56)
        model_e.insert(0, cfg.get("model", AI_DEFAULT_MODEL))
        model_e.grid(row=2, column=1, padx=6)

        ttk.Label(win, text="API Key：").grid(row=3, column=0, sticky="e", padx=6, pady=6)
        key_e = ttk.Entry(win, width=56, show="*")
        key_e.insert(0, cfg.get("api_key", ""))
        key_e.grid(row=3, column=1, padx=6)

        def on_provider_change(_event=None):
            """选中预置提供商 → 自动带入 base_url 与默认模型；「自定义」保留现值手改"""
            preset = AI_PROVIDERS.get(provider_var.get())
            if preset:
                base_e.delete(0, "end")
                base_e.insert(0, preset["base_url"])
                model_e.delete(0, "end")
                model_e.insert(0, preset["model"])

        provider_box.bind("<<ComboboxSelected>>", on_provider_change)

        hint = ("选择提供商自动带入地址与默认模型；想看 DeepSeek 思考过程可把模型改为 deepseek-reasoner。\n"
                "自定义适用于其他 OpenAI 兼容接口（通义 / 本地 Ollama 等）：Ollama 填 http://localhost:11434/v1，Key 随便填。\n"
                "⚠ 分析时日志内容将明文发送至所配 API 服务，请确认合规后使用。\n"
                "配置保存于本机 data/ai_config.json（已 gitignore，不会随仓库分发）。")
        ttk.Label(win, text=hint, foreground="#0055aa", justify="left").grid(
            row=4, column=0, columnspan=2, padx=10, pady=(10, 0), sticky="w")

        self.test_result_var = tk.StringVar(value="（测试连通 = 用当前填写的三项发一个最小对话请求，验证地址/模型/Key 是否可用）")
        ttk.Label(win, textvariable=self.test_result_var, foreground="#0055aa",
                  justify="left", wraplength=560).grid(row=6, column=0, columnspan=2,
                                                       padx=10, pady=(4, 0), sticky="w")

        def collect():
            return (base_e.get().strip(), model_e.get().strip(), key_e.get().strip())

        def do_test():
            base, model, key = collect()
            if not all((base, model, key)):
                messagebox.showwarning("提示", "Base URL / 模型名 / API Key 三项都要填才能测试", parent=win)
                return
            test_btn.configure(state="disabled", text="测试中…")
            self.test_result_var.set("→ 测试中（最长等 %d 秒）…" % AI_TEST_TIMEOUT)
            t0 = time.monotonic()

            def ok(pair):
                if not _win_alive():
                    return
                test_btn.configure(state="normal", text="测试连通")
                reply = (pair[0] or "").strip().replace("\n", " ")[:40]
                self.test_result_var.set("✓ 连通正常，模型回复「%s」（耗时 %.1f 秒）"
                                         % (reply or "(空)", time.monotonic() - t0))

            def fail(err):
                if not _win_alive():
                    return
                test_btn.configure(state="normal", text="测试连通")
                self.test_result_var.set("✗ 失败：%s" % err)

            self.run_bg(lambda: chat_completions(base, key, model,
                                                 "你是连通性测试助手。", "连通性测试，请只回复：OK",
                                                 max_tokens=256, timeout=AI_TEST_TIMEOUT),
                        ok, fail)

        def save():
            base, model, key = collect()
            new_cfg = {"provider": provider_var.get(), "base_url": base,
                       "model": model, "api_key": key}
            if not all((base, model, key)):
                messagebox.showwarning("提示", "Base URL / 模型名 / API Key 三项都要填", parent=win)
                return
            self.save_ai_config(new_cfg)
            win.destroy()
            self.tail_status_var.set("AI 配置已保存")
            self.log("✓ AI 配置已保存（%s  模型 %s）" % (new_cfg["base_url"], new_cfg["model"]))

        def _win_alive():
            try:
                return bool(win.winfo_exists())
            except Exception:
                return False  # 弹窗已关：测试结果无处可显，静默丢弃

        test_btn = ttk.Button(win, text="测试连通", command=do_test)
        test_btn.grid(row=5, column=0, pady=(10, 0), sticky="e", padx=6)
        ttk.Button(win, text="保存", command=save).grid(row=5, column=1, pady=(10, 0), sticky="w", padx=6)

    def _ai_btn_reset(self):
        """分析结束（完成/失败/取消）后恢复按钮为可发起分析态"""
        self.ai_btn.configure(state="normal", text="AI 分析(600行)", command=self.on_ai_analyze)

    def _ai_cancel_current(self):
        """分析中点「取消分析」：置 Event + 断开连接 + **立即**作废当前代次并恢复 UI。
        （Windows 上无法唤醒阻塞中的 recv，僵尸 worker 由代次令牌兜底，最迟 120s 后自灭）"""
        self._ai_cancel_evt.set()
        resp = self._ai_resp_ref.get("resp")
        if resp is not None:
            force_close_http_response(resp)
        self._ai_gen += 1              # 作废当前分析：僵尸 worker 的回写按代次丢弃
        self._ai_cancel_evt.clear()    # 复位，供下一轮分析使用
        self._ai_cancelled()

    def _ai_cancelled(self):
        self._ai_flush()  # 已流式收到的部分结果保留展示
        self.ai_state_var.set("已取消（部分结果已保留）")
        self.tail_status_var.set("AI 分析已取消")
        self.log("→ AI 分析已取消")
        self._ai_btn_reset()

    def _on_ai_mode_change(self, _event=None):
        """切换分析强度：立即持久化（下次启动保持），下次点「AI 分析」生效"""
        self.ai_mode = self.ai_mode_var.get()
        self.save_prefs()
        self.log("分析强度已切换为「%s」%s" % (
            self.ai_mode, "（送模型前过滤行，更省 token）" if AI_ANALYZE_MODES[self.ai_mode] else "（全量送）"))

    def on_ai_analyze(self):
        """AI 分析：按强度过滤 → 自动停止跟随（保证日志快照稳定）→ 流式调大模型（SSE）
        → 右栏实时展示思考过程与结论。超时 120s，失败手动重点。"""
        cfg = self.load_ai_config()
        if not (cfg.get("base_url") and cfg.get("model") and cfg.get("api_key")):
            messagebox.showwarning("提示", "请先点「AI 设置」配置 Base URL / 模型名 / API Key")
            return
        if not self.raw_lines:
            messagebox.showwarning("提示", "日志缓存为空：请先「开始跟随」采集一段日志再分析")
            return

        mode = self.ai_mode
        raw_n = min(len(self.raw_lines), AI_ANALYZE_LINES)
        lines = filter_lines_for_mode(list(self.raw_lines)[-AI_ANALYZE_LINES:], mode)
        if not lines:
            messagebox.showwarning(
                "提示", "强度「%s」下最近 %d 行中没有匹配的日志行（无 ERROR/WARN 等），\n"
                        "请切换更高强度（全量分析）或先采集日志。" % (mode, raw_n))
            return
        # 字节双限：行数限完再限字节（尾部最新优先），防超长行撑爆模型上下文/成本失控
        sent_n_before = len(lines)
        lines, clipped = clip_lines_by_bytes(lines)
        if not lines:
            messagebox.showwarning(
                "提示", "强度「%s」下匹配到的 %d 行全部是超长行（单行 > %dKB），无可送内容。" % (
                    mode, sent_n_before, AI_MAX_BYTES // 1024))
            return

        if self.tail_thread and self.tail_thread.is_alive():
            self.stop_tail()
            self.tail_status_var.set("已自动停止跟随（分析要求日志快照稳定）")

        scope_note = ("原始未过滤" if AI_ANALYZE_MODES[mode] is None
                      else "已按强度只保留 %s 相关行" % "/".join(AI_ANALYZE_MODES[mode]))
        if clipped:
            scope_note += "；超过 %dKB 已从尾部截留" % (AI_MAX_BYTES // 1024)
        user_content = "目标服务：%s\n分析强度：%s\n以下是该服务日志（%s，本次送 %d 行）：\n\n%s" % (
            self.svc_var.get(), mode, scope_note, len(lines), "\n".join(lines))
        system_prompt = build_system_prompt(mode)

        self._ai_reset_view()
        self.ai_state_var.set("思考中…（%s）" % mode)
        # 分析中按钮转义为「取消分析」，可随时中断
        self.ai_btn.configure(text="取消分析", command=self._ai_cancel_current)
        self.tail_status_var.set("AI 分析中…（%s，右栏实时展示，最长等 %d 秒）" % (mode, AI_HTTP_TIMEOUT))
        self.log("→ AI 分析：%s 最近 %d 行，强度「%s」送 %d 行%s（%s @ %s）" % (
            self.svc_var.get(), raw_n, mode, len(lines),
            "（已按 128KB 截断）" if clipped else "", cfg["model"], cfg["base_url"]))
        # 本次分析元信息（完成/失败时写入历史记录，导出 txt 也用它）
        self._ai_meta = {"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "service": self.svc_var.get(), "mode": mode,
                         "model": cfg["model"], "base_url": cfg["base_url"],
                         "tokens": "", "status": "分析中"}

        cancel_evt = self._ai_cancel_evt
        cancel_evt.clear()
        resp_ref = self._ai_resp_ref
        resp_ref["resp"] = None
        # 代次令牌：取消/新一轮分析会使 gen+1，旧 worker 的任何 UI 回写按 gen 失效丢弃
        # （Windows 上 shutdown/close 都无法唤醒阻塞中的 recv，僵尸 worker 可能最迟 120s 后
        #  才醒来，令牌保证它不会覆盖"已取消/新一轮"的状态与内容）
        self._ai_gen += 1
        gen = self._ai_gen

        def worker():
            def post(fn):
                def run():
                    if gen == self._ai_gen:
                        fn()
                self.root.after(0, run)

            try:
                usage = chat_completions_stream(
                    cfg["base_url"], cfg["api_key"], cfg["model"],
                    system_prompt, user_content,
                    on_delta=lambda kind, text: gen == self._ai_gen and self._ai_append(kind, text),
                    max_tokens=AI_MAX_OUTPUT_TOKENS,
                    cancel_event=cancel_evt, resp_ref=resp_ref)
                pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
                note = "" if pt is None else "（tokens：输入 %s / 输出 %s）" % (pt, ct)
                post(lambda: self._ai_finish(note))
            except AiCancelledError:
                post(self._ai_cancelled)
            except Exception as e:
                if cancel_evt.is_set():
                    # 取消方断开了连接，read 抛的是连接类异常而非 AiCancelledError
                    post(self._ai_cancelled)
                    return
                err = self.friendly_err(e)
                post(lambda: self._ai_fail(err))

        threading.Thread(target=worker, daemon=True).start()

    # ---- AI 右栏流式渲染 ----
    def _ai_reset_view(self):
        """开始新一次分析：取消未刷的节流任务、清空缓冲与两个文本框"""
        if self._ai_flush_after is not None:
            self.root.after_cancel(self._ai_flush_after)
            self._ai_flush_after = None
        self._ai_buf = {}
        for w in (self.ai_rs_text, self.ai_text):
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.configure(state="disabled")
        self._refresh_rs_btn()

    def _ai_append(self, kind, text):
        """流式增量（工作线程调用）：攒 AI_FLUSH_MS 毫秒批量刷 UI，避免逐 token 刷新卡顿"""
        self._ai_buf.setdefault(kind, []).append(text)
        if self._ai_flush_after is None:
            self._ai_flush_after = self.root.after(AI_FLUSH_MS, self._ai_flush)

    def _ai_flush(self):
        """把缓冲的增量写入右栏文本框（UI 线程）"""
        self._ai_flush_after = None
        buf, self._ai_buf = self._ai_buf, {}
        for kind in ("reasoning", "content"):
            parts = buf.get(kind)
            if not parts:
                continue
            w = self.ai_rs_text if kind == "reasoning" else self.ai_text
            w.configure(state="normal")
            w.insert("end", "".join(parts))
            w.see("end")
            w.configure(state="disabled")
            if kind == "reasoning":
                self._refresh_rs_btn()  # 收起态也能在按钮上看到思考字数在涨
            elif self.ai_state_var.get().startswith("思考中"):
                self.ai_state_var.set("输出结论中…")

    def _ai_finish(self, token_note):
        self._ai_flush()  # 刷掉残余缓冲
        self.ai_state_var.set("✓ 完成 %s" % token_note)
        self.tail_status_var.set("✓ AI 分析完成 %s，结论见右栏" % token_note)
        self.log("✓ AI 分析完成 %s" % token_note)
        self._ai_btn_reset()
        self._refresh_rs_btn(done=True)
        if getattr(self, "_ai_meta", None):
            self._ai_meta.update(status="完成", tokens=token_note)
            self._ai_record_history()

    def _ai_fail(self, err):
        self._ai_flush()
        self.ai_state_var.set("✗ 失败（重按可再试）")
        self.tail_status_var.set("✗ AI 分析失败：%s" % err)
        self.log("✗ AI 分析失败：%s" % err)
        self._ai_btn_reset()
        self._refresh_rs_btn()
        if getattr(self, "_ai_meta", None):
            self._ai_meta.update(status="失败：%s" % err[:80])
            self._ai_record_history()

    # ---------- 复制 / 导出 / 历史 ----------
    @staticmethod
    def _ai_record_text(rec):
        """把一条分析记录拼成可读文本（复制/导出/历史查看共用）"""
        return ("LogView AI 分析结果\n"
                "时间：%s\n服务：%s\n强度：%s\n模型：%s\ntokens：%s\n状态：%s\n"
                "\n===== 思考过程 =====\n%s\n\n===== 分析结论 =====\n%s\n" % (
                    rec.get("time", ""), rec.get("service", ""), rec.get("mode", ""),
                    rec.get("model", ""), rec.get("tokens") or "-",
                    rec.get("status", ""),
                    rec.get("reasoning") or "（无）",
                    rec.get("content") or "（无）"))

    def _collect_current_rec(self):
        """收集当前右栏内容为一条记录（复制/导出用）；无分析记录返回 None"""
        if not getattr(self, "_ai_meta", None):
            messagebox.showinfo("提示", "还没有分析结果")
            return None
        rec = dict(self._ai_meta)
        rec["reasoning"] = self.ai_rs_text.get("1.0", "end-1c")
        rec["content"] = self.ai_text.get("1.0", "end-1c")
        return rec

    def on_copy_result(self):
        """复制当前结论（含元信息头），一键贴进群/工单"""
        rec = self._collect_current_rec()
        if not rec:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self._ai_record_text(rec))
        self.ai_copy_btn.configure(text="✓ 已复制")
        self.root.after(2000, lambda: self.ai_copy_btn.configure(text="复制结论"))

    @staticmethod
    def _safe_filename(name):
        return "".join(c for c in name if c not in '\\/:*?"<>|')

    def on_export_result(self):
        """导出当前分析（元信息+思考+结论）为 txt"""
        rec = self._collect_current_rec()
        if not rec:
            return
        fname = "AI分析_%s_%s.txt" % (self._safe_filename(rec["service"]),
                                      rec["time"].replace(":", "").replace(" ", "_"))
        path = filedialog.asksaveasfilename(
            title="导出 AI 分析结果", defaultextension=".txt", initialfile=fname,
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._ai_record_text(rec))
            self.log("✓ 已导出 %s" % path)
        except Exception as e:
            messagebox.showwarning("提示", "导出失败：%s" % e)

    def _load_ai_history(self):
        if os.path.exists(AI_HISTORY_FILE):
            try:
                with open(AI_HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception:
                pass
        return []

    def _ai_record_history(self):
        """完成/失败后写入历史（取消不记），最新在前，超 AI_HISTORY_KEEP 丢最旧"""
        rec = dict(self._ai_meta)
        rec["reasoning"] = self.ai_rs_text.get("1.0", "end-1c")
        rec["content"] = self.ai_text.get("1.0", "end-1c")
        records = self._load_ai_history()
        records.insert(0, rec)
        del records[AI_HISTORY_KEEP:]
        try:
            with open(AI_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log("⚠ 历史保存失败：%s" % e)

    def show_ai_history(self):
        """历史分析记录列表：双击或「查看」回看详情（含思考与结论全文）"""
        records = self._load_ai_history()
        win = tk.Toplevel(self.root)
        win.title("AI 分析历史（共 %d 条）" % len(records))
        win.geometry("800x480")

        cols = ("time", "service", "mode", "model", "tokens", "status")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for key, text, width in [("time", "时间", 130), ("service", "服务", 150),
                                 ("mode", "强度", 90), ("model", "模型", 100),
                                 ("tokens", "tokens", 180), ("status", "状态", 110)]:
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor="w")
        vs = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True, padx=6, pady=6)
        for i, rec in enumerate(records):
            tree.insert("", "end", iid=str(i), values=tuple(rec.get(k, "") for k in cols))

        def selected_rec():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "先选中一条记录", parent=win)
                return None
            return records[int(sel[0])]

        def view():
            rec = selected_rec()
            if rec:
                self._show_ai_history_detail(rec)

        def copy_one():
            rec = selected_rec()
            if rec:
                self.root.clipboard_clear()
                self.root.clipboard_append(self._ai_record_text(rec))
                messagebox.showinfo("提示", "已复制到剪贴板", parent=win)

        def export_one():
            rec = selected_rec()
            if not rec:
                return
            fname = "AI分析_%s_%s.txt" % (self._safe_filename(rec.get("service", "")),
                                          rec.get("time", "").replace(":", "").replace(" ", "_"))
            path = filedialog.asksaveasfilename(
                title="导出分析记录", defaultextension=".txt", initialfile=fname,
                filetypes=[("文本文件", "*.txt")], parent=win)
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._ai_record_text(rec))
            except Exception as e:
                messagebox.showwarning("提示", "导出失败：%s" % e, parent=win)

        def clear_all():
            if not records:
                return
            if not messagebox.askyesno("确认", "清空全部 %d 条历史记录？" % len(records), parent=win):
                return
            try:
                with open(AI_HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump([], f)
            except Exception:
                pass
            tree.delete(*tree.get_children())
            records.clear()
            win.title("AI 分析历史（共 0 条）")

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="查看", command=view).pack(side="left", padx=2)
        ttk.Button(bar, text="复制", command=copy_one).pack(side="left", padx=2)
        ttk.Button(bar, text="导出", command=export_one).pack(side="left", padx=2)
        ttk.Button(bar, text="清空历史", command=clear_all).pack(side="right", padx=2)
        tree.bind("<Double-1>", lambda e: view())

    def _show_ai_history_detail(self, rec):
        win = tk.Toplevel(self.root)
        win.title("分析记录 - %s %s" % (rec.get("service", ""), rec.get("time", "")))
        win.geometry("760x560")
        copy_btn = ttk.Button(win, text="复制全文")
        copy_btn.pack(anchor="e", padx=6)
        txt = tk.Text(win, wrap="word", font=("Microsoft YaHei UI", 10))
        vs = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True, padx=6, pady=4)
        txt.insert("1.0", self._ai_record_text(rec))
        txt.configure(state="disabled")

        def cp():
            self.root.clipboard_clear()
            self.root.clipboard_append(txt.get("1.0", "end-1c"))
            copy_btn.configure(text="✓ 已复制")
            win.after(2000, lambda: copy_btn.configure(text="复制全文"))

        copy_btn.configure(command=cp)

    def _toggle_reasoning(self):
        """思考过程框展开/收起（默认收起；收起期间内容照常累积，展开即见全量）"""
        self.ai_rs_expanded = not self.ai_rs_expanded
        if self.ai_rs_expanded:
            self.ai_rs_text.pack(before=self.result_lf, fill="both", expand=True, pady=(0, 2))
        else:
            self.ai_rs_text.pack_forget()
        self._refresh_rs_btn()

    def _refresh_rs_btn(self, done=False):
        n = len(self.ai_rs_text.get("1.0", "end-1c"))
        arrow = "▼" if self.ai_rs_expanded else "▶"
        if done and n == 0:
            text = "%s 思考过程（本模型无思考输出）" % arrow
        elif done:
            text = "%s 思考过程 ✓（%d 字）" % (arrow, n)
        else:
            text = "%s 思考过程（%d 字）" % (arrow, n)
        self.ai_rs_btn.configure(text=text)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
