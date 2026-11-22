#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox / OK影视 配置聚合器（国内净化 + 慢源治理）

每天做的事：
  1. 拉上游配置，校验 JSON 与片源数量
  2. 剔除需要翻墙的站点（YouTube / googlevideo / github.io / netlify / vercel 等）
  3. 把 ./xxx 相对路径改写成上游绝对地址（否则镜像后 spider、ext 全断）
  4. 测速：能直连的源测接口响应；域名解析不了的直接删；超过阈值的删
  5. 排序：快的排前，测不动的排后，让电视上第一眼看到的就是能用的
  6. 生成多仓 tvbox.json + status.md

关于测速的诚实边界：
  161 个源里只有十几个是「直连 HTTP API」类型，可以直接测；
  其余走 jar / js / python 解析器，服务器端无法模拟完整播放链路，
  只能标注"未测"，不能凭猜测删。播放中卡顿只能在电视上实测，
  遇到就写进 blocklist.txt，下一轮自动剔除。
"""

import concurrent.futures
import datetime
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request

PROXY = "https://gh-proxy.com/"
TIMEOUT = 20            # 接口超时
PLAY_MIN = 0.35         # 实测播放速率底线（MB/s≈2.8Mbps），低于它就转圈
RESOURCE = re.compile(r"gh-proxy\.com|\.js$|\.py$", re.I)  # 解析器资源，速度不代表源质量
SLOW_SEC = 3.0          # 接口响应超过这个秒数视为慢源，直接删除（电视端转圈都在这）
OUT_DIR = "sources"
BLOCK_FILE = "blocklist.txt"

SOURCES = [
    ("qist-jsm", "https://raw.githubusercontent.com/qist/tvbox/master/jsm.json",
     "家庭电视版（无直播·推荐）"),
    ("lightconer", "https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/单仓聚合.json",
     "聚合大仓（片源最多）"),
    ("qist-js", "https://raw.githubusercontent.com/qist/tvbox/master/js.json",
     "大合集"),
    ("niubihu1", "https://raw.githubusercontent.com/niubihu1/tvbox/main/1.json",
     "牛虎"),
    ("hd9211", "https://raw.githubusercontent.com/hd9211/Tvbox1/main/zy.json",
     "采集站合集"),
    ("qist-dianshi", "https://raw.githubusercontent.com/qist/tvbox/master/dianshi.json",
     "电视自用"),
    ("qist-0825", "https://raw.githubusercontent.com/qist/tvbox/master/0825.json",
     "小而精"),
    ("qist-9918", "https://raw.githubusercontent.com/qist/tvbox/master/9918.json",
     "极简"),
]

UA = "Mozilla/5.0 (Linux; Android 11; TV) AppleWebKit/537.36 Chrome/120 Safari/537.36"

BAD = re.compile(
    r"youtube|youtu\.be|googlevideo|ytimg|ggpht|gstatic|"
    r"github\.io|netlify\.app|vercel\.app|herokuapp|glitch\.me|"
    r"telegram|t\.me|discord|twitter|x\.com",
    re.I,
)

# 这些是 TVBox 内置功能项，不是真实片源，保持原位不参与测速
SPECIAL = re.compile(r"配置|本地|搜索|设置", re.I)

# 成人内容：家庭电视上不该出现
ADULT = re.compile(r"成人|黄AV|AV资源|porn|约炮|福利姬|三级片|av在线|涩涩", re.I)

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def fetch(url, timeout=TIMEOUT):
    url = urllib.parse.quote(url, safe=":/?&=%")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        return r.read()


def load(raw_url):
    last = ""
    for _ in range(2):
        for u in (raw_url, PROXY + raw_url):
            try:
                return json.loads(fetch(u).decode("utf-8", "ignore"))
            except Exception as e:  # noqa: BLE001
                last = "%s: %s" % (type(e).__name__, str(e)[:50])
    raise RuntimeError(last)


def fix_rel(obj, base):
    """把 ./xxx 相对路径改成上游绝对地址（走代理）"""
    if isinstance(obj, dict):
        return {k: fix_rel(v, base) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fix_rel(v, base) for v in obj]
    if isinstance(obj, str) and obj.startswith("./"):
        return PROXY + base + obj[2:]
    return obj


def clean(d, raw_url):
    base = raw_url.rsplit("/", 1)[0] + "/"
    removed = 0
    for key in ("sites", "lives", "parses"):
        items = d.get(key)
        if not isinstance(items, list):
            continue
        keep = []
        for it in items:
            if BAD.search(json.dumps(it, ensure_ascii=False)):
                removed += 1
            else:
                keep.append(it)
        d[key] = keep
    return fix_rel(d, base), removed


def clean_host(h):
    """从杂乱字符串里提取纯域名，不合法返回 None"""
    h = h.split("|")[0].split("$")[0].split(",")[0].split("#")[0].strip().lower()
    if ":" in h:
        h = h.split(":")[0]
    if not h or h in ("127.0.0.1", "localhost", "0.0.0.0"):
        return None
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
        return None
    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", h):
        return None
    return h if len(h) <= 100 else None


def primary_host(site):
    """取这个源的主域名：http 接口取 api 的域名；解析器源取 ext 里第一个 URL"""
    api = site.get("api") or ""
    if api.startswith("http"):
        m = re.match(r"https?://([^/\s\"']+)", api)
        return clean_host(m.group(1)) if m else None
    found = []

    def walk(o):
        if len(found) >= 1:
            return
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            m = re.search(r"https?://([^/\s\"']+)", o)
            if m:
                c = clean_host(m.group(1))
                if c:
                    found.append(c)

    walk(site.get("ext"))
    return found[0] if found else None


IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"


def net_check(host):
    try:
        socket.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001
        return "dns"
    if IN_CI:
        # GitHub Actions 在美国，连国内源站的 TCP 大量误判，只做 DNS 判断
        return "ok"
    for port in (443, 80):
        try:
            s = socket.create_connection((host, port), timeout=3)
            s.close()
            return "ok"
        except Exception:  # noqa: BLE001
            pass
    return "tcp"


def probe(api):
    """测接口响应。返回 (状态, 秒数)；状态 dead=dns/网络失败, slow=超阈值, ok=可用"""
    host = re.sub(r"^https?://", "", api).split("/")[0].split(":")[0]
    try:
        socket.gethostbyname(host)
    except Exception:  # noqa: BLE001
        return "dead", None

    sep = "&" if "?" in api else "?"
    url = api + sep + "ac=videolist&pg=1"
    t0 = time.time()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}),
            timeout=TIMEOUT, context=_ctx,
        ) as r:
            r.read(4096)
        dt = time.time() - t0
    except Exception:  # noqa: BLE001
        # 连不上 ≠ 源废（可能是封 UA/封 IP），只降到队尾，不删
        return "unreachable", None
    return ("slow", dt) if dt > SLOW_SEC else ("ok", dt)


def bump(state, key, ok, reason=""):
    """成功清零；失败累加。返回是否达到「连续 3 天失败 → 永久删除」阈值"""
    if ok:
        state.pop(key, None)
        return False
    e = state.setdefault(key, {"fail": 0})
    e["fail"] = e.get("fail", 0) + 1
    if reason:
        e["reason"] = reason
    e["last"] = datetime.date.today().isoformat()
    return e["fail"] >= 3


def load_blocklist():
    try:
        with open(BLOCK_FILE, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    except FileNotFoundError:
        return []


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    blocked = load_blocklist()
    try:
        state = json.load(open("state.json", encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    try:
        speed = json.load(open("speed.json", encoding="utf-8"))
    except FileNotFoundError:
        speed = {}
    # 两轮实测都连不上的源（DNS 失败/连接被重置/超时/404）
    try:
        dead = json.load(open("dead.json", encoding="utf-8"))
    except FileNotFoundError:
        dead = {}
    # 昨天那份死源名单：接口死要连续两天都测不通才删。
    # 单日网络抖动会让大片源假超时（实测误判率过半），域名判定则可靠得多。
    try:
        dead_prev = json.load(open("dead_prev.json", encoding="utf-8"))
    except FileNotFoundError:
        dead_prev = {}
    # 接口活着但内容已不是采集数据（卖域名页/授权到期页），连同播放太慢的源
    def _load(name):
        try:
            return json.load(open(name, encoding="utf-8"))
        except FileNotFoundError:
            return {}
    notapi = _load("notapi.json")
    playslow = _load("playslow.json")
    playslow_prev = _load("playslow_prev.json")

    # 汇总所有源的 api，去重后统一测一次
    all_apis, all_hosts, datasets = set(), set(), []
    for slug, raw_url, name in SOURCES:
        try:
            d = load(raw_url)
        except Exception as e:  # noqa: BLE001
            print("%-14s DEAD %s" % (slug, e))
            datasets.append((slug, name, None, 0, str(e)[:40]))
            continue
        if len(d.get("sites") or []) < 3:
            datasets.append((slug, name, None, 0, "片源太少"))
            continue
        d, n_bad = clean(d, raw_url)
        for s in d["sites"]:
            api = s.get("api") or ""
            if api.startswith("http"):
                all_apis.add(api)
            h = primary_host(s)
            if h:
                all_hosts.add(h)
        datasets.append((slug, name, (d, raw_url), n_bad, "ok"))

    print("域名检测 %d 个；接口速度用本地快照 speed.json（%d 条）" % (
        len(all_hosts), len(speed)))

    # 域名连通性用本地快照 badhost.json：GitHub runner 在美国测国内域名不准，
    # 所有实测都在沙箱侧做（见 tools/probe_all.py），update.py 只做过滤。
    try:
        host_res = json.load(open("badhost.json", encoding="utf-8"))
    except FileNotFoundError:
        host_res = {}
    print("域名快照：%d 个不通（本次上游 %d 个域名）" % (len(host_res), len(all_hosts)))

    seen_ok, seen_fail = set(), {}
    rows, urls = [], []
    for slug, name, payload, n_bad, note in datasets:
        if payload is None:
            rows.append((slug, name + "（" + note + "）", False, 0, 0, 0, 0))
            continue
        d, raw_url = payload
        sites = d["sites"]

        kept, dropped_slow, dropped_block, dropped_perm, untested = [], 0, 0, 0, 0
        dropped_dead = 0
        dropped_host = 0
        dropped_fake = 0
        dropped_pslow = 0
        front, mid, back = [], [], []
        for s in sites:
            sname = s.get("name") or ""
            if any(b.lower() in sname.lower() for b in blocked):
                dropped_block += 1
                continue
            api = s.get("api") or ""
            if SPECIAL.search(sname):
                mid.append(s)          # 配置中心/本地/搜索等内置项
                continue

            h = primary_host(s)
            # 状态键：优先用接口，没有接口用域名。连续 3 天失败才永久删除
            key = ("api:" + api) if api.startswith("http") else ("host:" + (h or sname))

            if h and host_res.get(h) in ("dns", "tcp"):
                seen_fail[key] = host_res.get(h)
                dropped_host += 1
                continue

            if not api.startswith("http"):
                seen_ok.add(key)
                mid.append(s)          # jar / js / py 解析器源：不测，居中
                untested += 1
                continue

            if api in dead and (not dead_prev or api in dead_prev):
                dropped_dead += 1
                continue

            # 假活源：接口 200 但返回的不是采集数据，对电视来说等于废源
            if api in notapi:
                dropped_fake += 1
                continue

            # 播放速率太慢：直链实测低于 PLAY_MIN，播起来必然转圈（两天确认）
            if api in playslow and (not playslow_prev or api in playslow_prev):
                dropped_pslow += 1
                continue

            if api in speed and speed[api] > SLOW_SEC and not RESOURCE.search(api):
                seen_fail[key] = "slow"
                dropped_slow += 1
                continue

            seen_ok.add(key)
            if api in speed:
                s["_dt"] = speed[api]
                front.append(s)        # 有实测速度：按速度排前面
            else:
                mid.append(s)          # 没测过：居中保留

        front.sort(key=lambda x: x.get("_dt", 999.0))   # 实测快的排最前
        for s in front + mid + back:
            s.pop("_dt", None)
        kept = front + mid + back

        # 仓内去重：接口和名字完全一致才算重复（同接口不同名是分类入口，保留）
        seen, dedup, dup = set(), [], 0
        for s in kept:
            k = (s.get("api") or "") + "||" + (s.get("name") or "")
            if k in seen:
                dup += 1
                continue
            seen.add(k)
            dedup.append(s)
        kept = dedup
        if dup:
            print("  去重 %d 个" % dup)
        d["sites"] = kept

        path = os.path.join(OUT_DIR, slug + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))

        urls.append({
            "name": name,
            "url": PROXY + "https://raw.githubusercontent.com/hesanyue50-lang/tvbox-config/main/" + path,
        })
        drop_all = (dropped_slow + dropped_host + dropped_dead + dropped_fake
                    + dropped_pslow + dropped_block + dropped_perm)
        rows.append((slug, name, True, len(kept), drop_all, len(front), untested))
        print("%-14s 保留%-4d 删%-3d(接口慢%d/域名死%d/连不上%d/假活%d/播放慢%d/黑名单%d) 快源%-3d 未测%-3d" % (
            slug, len(kept), drop_all, dropped_slow, dropped_host, dropped_dead,
            dropped_fake, dropped_pslow, dropped_block + dropped_perm,
            len(front), untested))

    # 每轮每个源只记一次，避免被多个仓重复计数
    today = datetime.date.today().isoformat()
    for k in seen_ok - set(seen_fail):
        state.pop(k, None)
    for k, reason in seen_fail.items():
        e = state.setdefault(k, {"fail": 0})
        e["fail"] = e.get("fail", 0) + 1
        e["reason"] = reason
        e["last"] = today
    # 已有 3 次失败记录的保留，成功过的不再出现
    state = {k: v for k, v in state.items() if v.get("fail", 0) > 0}
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("状态记录：%d 个源待观察" % len(state))

    if not urls:
        print("!! 全部上游不可用，保留旧文件", file=sys.stderr)
        return 1

    with open("tvbox.json", "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, ensure_ascii=False, indent=2)
        f.write("\n")

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# 源检测状态", "",
        "最近更新：%s" % now, "",
        "| 仓 | 状态 | 保留 | 删除 | 已测快源 | 解析器源（未测） |",
        "|---|---|---|---|---|---|",
    ]
    for slug, name, ok, n, dropped, fast, untested in rows:
        lines.append("| `%s` | %s | %d | %d | %d | %d |" % (
            slug, "✅ " + name if ok else "❌ " + "不可用", n, dropped, fast, untested))
    lines += [
        "",
        "## 筛选口径", "",
        "- **域名连通性**：每个源提取主域名，解析失败或 80/443 两轮都连不上的直接删除（多 DNS 复核，避免误判）",
        "- **接口测速**：直连 HTTP API 实测响应时间，超过 %.1f 秒直接删除（每个接口测两轮取优值）" % SLOW_SEC,
        "- **连不上就删**：DNS 失败、连接被重置、8 秒超时、接口 404 的源，两轮实测都失败即删除",
        "- **假活源删**：接口返回 200 但内容已不是采集数据（卖域名页、授权到期页）的直接删除",
        "- **播放速率**：能拿到直链的源实测分片下载速率，低于 %.2f MB/s（≈%.0f Mbps）播 720p 都会转圈，连续两天不达标即删除" % (PLAY_MIN, PLAY_MIN * 8),
        "- **可自愈**：每天从上游重新拉取再过滤，被删的源如果哪天恢复，第二天会自动回来",
        "- **排序**：实测快的排最前；jar/js/py 解析器源无法在服务端模拟播放链路，居中标注「未测」",
        "- **国外源**：YouTube、googlevideo、github.io、netlify、vercel、telegram 等命中即删",
        "- 播放中卡顿只能在电视上实测：遇到卡的就往 `blocklist.txt` 加一行源名，第二天自动剔除",
        "",
        "## 电视上填这个地址", "",
        "```",
        PROXY + "https://raw.githubusercontent.com/hesanyue50-lang/tvbox-config/main/tvbox.json",
        "```", "",
        "主代理不通时把开头的 `gh-proxy.com` 换成 `ghproxy.net`。",
        "`gh-proxy` 是 GitHub 国内加速镜像，不是 VPN，国内直连即可。", "",
    ]
    with open("status.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("完成，生成 %d 个仓" % len(urls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
