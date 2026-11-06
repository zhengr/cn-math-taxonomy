#!/usr/bin/env python3
"""从体庄(tizhuang)题库服务导入 K12 真题，扩充 data/questions.json。

题源后端与 skills/question-bank 相同(见 weishao2/tizhuang-agent-skills):
  基址 QUESTION_SERVICE_URL 默认 https://tizhuang.qcscience.cc/api
  凭证 X-API-Key，取自环境变量 TIZHUANG_API_KEY 或 QUESTION_SERVICE_LICENSE(不打印、不落盘)
  配额 GET /v1/quota:免费注册档每天 200 题;元数据(/v1/meta/*)免费;列表每次消耗返回的题目数

子命令:
  map     免费元数据解析每个 cnm_ 知识点 -> 体庄 grade_id / knowledge_id / keyword,
          写 data/_tizhuang_import/topic_map.json
  fetch   按年级轮询拉题 -> 转换成 questions.json 格式 -> 按 content_hash 去重 -> 追加
          --per-topic 每点本次最多导入几题(默认 2)  --budget 本次消耗上限(默认=当日剩余额度-5)
          --grades 逗号分隔的 cnm 年级 1-14  --target 每点目标题数(默认 10, 达标即跳过)
          --dry-run 只拉取转换、不写 questions.json
  status  配额 + 已导入统计

只导入 has_images=false 且 auto_gradable=true 的题;另跳过:
  含子题的综合题 / 任一字段带 <img> 的题 / 题干写了"如图、图中、见图"的无图题 /
  选项数不在 3-4 或选项文字重复或答案不是单个字母的选择题 / 答案过长(>60字)的题。
真题来源、年份、试卷、content_hash 等落在 data/_tizhuang_import/log.json(questions.json
的字段白名单不允许带来源字段)。
"""
import argparse, json, os, re, sys, time, unicodedata
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
IMP = os.path.join(DATA, "_tizhuang_import")
RAW = os.path.join(IMP, "raw")
QUESTIONS = os.path.join(DATA, "questions.json")
TOPICS = os.path.join(DATA, "topics.json")
BASE = os.environ.get("QUESTION_SERVICE_URL", "https://tizhuang.qcscience.cc/api").rstrip("/")

SUBJECT_MATH = 2
NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?$")
SRC_TAG_RE = re.compile(r"^\(\d{4}[^()]*\)")
FIG_REF_RE = re.compile(r"如图|图中|见图|图象如图")


def load_key():
    key = os.environ.get("TIZHUANG_API_KEY") or os.environ.get("QUESTION_SERVICE_LICENSE")
    if not key:
        sys.exit("需要环境变量 TIZHUANG_API_KEY 或 QUESTION_SERVICE_LICENSE")
    return key


def api_get(path, params=None, tries=5):
    h = {"X-API-Key": load_key()}
    for i in range(tries):
        try:
            r = requests.get(BASE + path, params=params or {}, headers=h, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (500, 502, 503, 504):
                time.sleep(1.5 * (i + 1)); continue
            sys.exit(f"{path} -> HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    sys.exit(f"{path}: 多次重试后仍失败")


# ---------------------------------------------------------------- 文本清洗

SYMBOLS = {
    r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\pm": "±", r"\mp": "∓",
    r"\neq": "≠", r"\ne": "≠", r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\approx": "≈", r"\equiv": "≡", r"\infty": "∞", r"\pi": "π", r"\triangle": "△",
    r"\vartriangle": "△", r"\angle": "∠", r"\circ": "°", r"\perp": "⊥", r"\parallel": "∥",
    r"\in": "∈", r"\notin": "∉", r"\cup": "∪", r"\cap": "∩", r"\subset": "⊂",
    r"\subseteq": "⊆", r"\supset": "⊃", r"\emptyset": "∅", r"\ldots": "…", r"\cdots": "…",
    r"\left": "", r"\right": "", r"\,": "", r"\;": "", r"\!": "", r"\ ": " ", r"\quad": " ",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ",
    r"\degree": "°", r"\%": "%",
}
SUP_KEYS = "0123456789-+()"
SUP = str.maketrans(SUP_KEYS, "⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺⁽⁾")


def _read_group(s, i):
    """读取 {..} 或单字符参数, 返回 (内容, 下标); 无括号时取一个字符。"""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] == "{":
        depth, j = 1, i + 1
        while j < len(s) and depth:
            if s[j] == "{": depth += 1
            elif s[j] == "}": depth -= 1
            j += 1
        return s[i + 1:j - 1], j
    return s[i], i + 1


def latex_to_text(s):
    s = s.replace(r"\(", " ").replace(r"\)", " ").replace(r"\[", " ").replace(r"\]", " ").replace("$", "")
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            m = re.match(r"\\[a-zA-Z]+", s[i:])
            if m:
                cmd = m.group(0)
                if cmd in (r"\frac", r"\dfrac", r"\tfrac"):
                    a, j = _read_group(s, i + len(cmd))
                    b, j = _read_group(s, j)
                    out.append(f"({latex_to_text(a).strip()}/{latex_to_text(b).strip()})"); i = j; continue
                if cmd == r"\sqrt":
                    j = i + len(cmd)
                    opt = ""
                    if j < len(s) and s[j] == "[":
                        k = s.index("]", j)
                        opt, j = s[j + 1:k], k + 1
                    x, j = _read_group(s, j)
                    xt = latex_to_text(x)
                    out.append(f"{opt}√({xt})" if opt else f"√({xt})"); i = j; continue
                if cmd == r"\overrightarrow":
                    x, j = _read_group(s, i + len(cmd))
                    out.append(latex_to_text(x) + "\u20d7"); i = j; continue
                if cmd in (r"\text", r"\mathrm", r"\mbox", r"\textbf", r"\textrm"):
                    x, j = _read_group(s, i + len(cmd))
                    out.append(latex_to_text(x)); i = j; continue
                if cmd in SYMBOLS:
                    out.append(SYMBOLS[cmd]); i += len(cmd); continue
                out.append(cmd[1:]); i += len(cmd); continue
            out.append(s[i + 1] if i + 1 < len(s) else ""); i += 2; continue
        if c == "^" or c == "_":
            body, j = _read_group(s, i + 1)
            bt = latex_to_text(body)
            if c == "^" and all(ch in SUP_KEYS for ch in bt):
                out.append(bt.translate(SUP))
            else:
                out.append(("^" if c == "^" else "_") + bt)
            i = j; continue
        out.append(c); i += 1
    return "".join(out)


def html_to_text(s):
    if not s:
        return ""
    soup = BeautifulSoup(s, "html.parser")
    for tag in soup.find_all("input"):
        tag.replace_with("____")
    for tag in soup.find_all("img"):
        tag.decompose()
    for tag in soup.find_all("br"):
        tag.replace_with(" ")
    text = soup.get_text(" ")
    text = unicodedata.normalize("NFKC", text)
    text = latex_to_text(text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\r", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*", " ", text)
    return text.strip()


def clean_prompt(title):
    t = html_to_text(title)
    t = SRC_TAG_RE.sub("", t).strip()
    return t


def clean_answer(ans):
    return html_to_text(ans).strip().strip("。．.")


def numeric_parts(ans):
    """若答案整体是数值(允许每段外层括号、全角已被 NFKC 规整), 返回规范化段列表, 否则 None。"""
    parts = [p.strip() for p in re.split(r"[，,]", ans) if p.strip()]
    if not parts:
        return None
    out = []
    for p in parts:
        while len(p) >= 2 and p[0] == "(" and p[-1] == ")":
            p = p[1:-1].strip()
        if not NUM_RE.match(p):
            return None
        out.append(p)
    return out


def map_difficulty(d):
    try:
        d = float(d)
    except (TypeError, ValueError):
        return "standard"
    return "basic" if d <= 1.5 else ("standard" if d <= 3.5 else "challenge")


# ---------------------------------------------------------------- 转换

CHOICE_TYPES = {"单选题", "选择题"}
FILL_TYPES = {"填空题", "口算题", "计算题", "应用题", "解答题", "综合题", "证明题", "判断题"}


INLINE_OPT_RE = re.compile(r"([A-D])[\.、．]\s*")


def split_inline_options(text):
    """题干里内嵌 "A.xxx B.xxx C.xxx D.xxx" 形式的选项时, 拆成 (prompt, {key: text})。"""
    ms = list(INLINE_OPT_RE.finditer(text))
    if len(ms) < 3 or len(ms) > 4:
        return None
    keys = [m.group(1) for m in ms]
    if keys != list("ABCD")[:len(ms)]:
        return None
    first = ms[0].start()
    if first < 0.3 * len(text):  # 选项应在题干尾部
        return None
    prompt = re.sub(r"\[\s*\]\s*$", "", text[:first]).strip()
    opts = {}
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        opts[m.group(1)] = text[m.end():end].strip()
    if any(not v for v in opts.values()):
        return None
    return prompt, opts


def convert_item(it, topic_id, letter_cycle=0):
    """把体庄题目转成 questions.json 条目; 不合格返回 (None, 原因)。"""
    if it.get("subquestions"):
        return None, "compound"
    title = it.get("title") or ""
    options = it.get("options") or {}
    analysis = it.get("analysis") or ""
    if "<img" in title or any("<img" in str(v) for v in options.values()):
        return None, "has-img-field"
    prompt = clean_prompt(title)
    if not prompt or FIG_REF_RE.search(prompt):
        return None, "fig-ref"
    expl = re.sub(r"【[^】]*】", "", html_to_text(analysis or it.get("answer_html") or "")).strip()
    expl = re.sub(r"^(解|解答|分析|证明)[：:]", "", expl).strip()
    if len(expl) < 4:  # 太短的"解析"只有答案字母之类, 没有参考价值
        return None, "no-explanation"
    if "begin{" in prompt or "begin{" in expl:
        return None, "latex-array"
    if len(expl) > 300:
        expl = expl[:300].rstrip() + "…"
    qtype = (it.get("question_type") or "").strip()
    diff = map_difficulty(it.get("difficulty"))
    ans_raw = clean_answer(it.get("answer") or "")
    base = {"topicId": topic_id, "difficulty": diff, "explanation": expl}
    if qtype in CHOICE_TYPES:
        if not options:
            parsed = split_inline_options(prompt)
            if parsed:
                prompt, options = parsed
        keys = sorted(k for k in options.keys() if re.fullmatch(r"[A-D]", str(k)))
        if not (3 <= len(keys) <= 4):
            return None, "choice-arity"
        ans = (it.get("answer") or "").strip()
        if len(ans) != 1 or ans not in keys:
            return None, "choice-answer"
        texts = [html_to_text(str(options[k])) for k in keys]
        if any("begin{" in t for t in texts):
            return None, "latex-array"
        if len(set(texts)) != len(texts) or any(not t for t in texts):
            return None, "choice-dup"
        # 保持题源选项顺序和答案字母不变(不改写、不重排)
        base.update({
            "type": "choice",
            "prompt": prompt,
            "choices": [{"key": k, "text": t} for k, t in zip(keys, texts)],
            "answer": ans,
        })
        return base, None
    if qtype in FILL_TYPES:
        if not ans_raw:
            return None, "no-answer"
        np_ = numeric_parts(ans_raw)
        if np_:
            base.update({"type": "numeric", "prompt": prompt, "answer": "，".join(np_)})
            return base, None
        if len(ans_raw) <= 60:
            base.update({"type": "short", "prompt": prompt, "answer": ans_raw})
            return base, None
        return None, "answer-too-long"
    return None, f"type:{qtype}"


# ---------------------------------------------------------------- 映射

def norm(s):
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", s or "")).lower()


def grade_id_for(t):
    g, sem = t["grade"], t.get("semester", "")
    if g <= 6:
        b = 100 + 10 * g
    elif g <= 9:
        b = 100 + 100 * (g - 6)
    elif g == 10:
        b = 500
    elif g == 11:
        b = 600
    elif g == 12:
        return 30100
    elif g == 13:
        return 30200
    elif g == 14:
        return 30300
    else:
        return None
    return b + (1 if sem == "上" else 2 if sem == "下" else 0)


def build_map():
    topics = [t for t in json.load(open(TOPICS, encoding="utf-8")) if t["id"].startswith("cnm_")]
    kp = api_get("/v1/meta/knowledge-points", {"subject_id": SUBJECT_MATH, "limit": 5000})
    by_name, by_norm = {}, {}
    for k in kp:
        by_name.setdefault(k["name"], []).append(k)
        by_norm.setdefault(norm(k["name"]), []).append(k)
    mapping, unmatched = {}, []
    for t in topics:
        name = t["name"]
        cands = by_name.get(name) or by_norm.get(norm(name))
        if not cands:
            n = norm(name)
            cands = [k for k in kp if n and (n in norm(k["name"]) or norm(k["name"]) in n)]
            cands.sort(key=lambda k: (k.get("has_children"), abs(len(norm(k["name"])) - len(n))))
        gid = grade_id_for(t)
        if cands:
            best = cands[0]
            mapping[t["id"]] = {"name": name, "grade": t["grade"], "grade_id": gid,
                                "knowledge_id": best.get("knowledge_id"), "tz_name": best["name"]}
        else:
            unmatched.append({"topicId": t["id"], "name": name, "grade": t["grade"], "grade_id": gid})
    os.makedirs(IMP, exist_ok=True)
    json.dump({"mapping": mapping, "unmatched": unmatched},
              open(os.path.join(IMP, "topic_map.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"map: {len(mapping)} 个知识点匹配到体庄知识点, {len(unmatched)} 个未匹配(走 keyword 回退)")


# ---------------------------------------------------------------- fetch

def load_state():
    os.makedirs(IMP, exist_ok=True)
    os.makedirs(RAW, exist_ok=True)
    log_path = os.path.join(IMP, "log.json")
    log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else {"fetched": {}, "runs": []}
    questions = json.load(open(QUESTIONS, encoding="utf-8"))
    tmap = json.load(open(os.path.join(IMP, "topic_map.json"), encoding="utf-8"))
    return log, questions, tmap


def max_q_per_topic(questions):
    mx = {}
    for q in questions:
        m = re.match(r"(.+)_q(\d+)$", q.get("id", ""))
        if m:
            mx[m.group(1)] = max(mx.get(m.group(1), 0), int(m.group(2)))
    return mx


def cmd_fetch(args):
    log, questions, tmap = load_state()
    mapping = tmap["mapping"]
    topics = [t for t in json.load(open(TOPICS, encoding="utf-8"))
              if t["id"].startswith("cnm_") and t["id"] in mapping]
    if args.grades:
        gs = {int(x) for x in args.grades.split(",")}
        topics = [t for t in topics if t["grade"] in gs]
    quota = api_get("/v1/quota")
    budget = min(args.budget, max(0, quota.get("remaining", 0) - 5))
    print(f"quota: {quota.get('used')}/{quota.get('daily_limit')} used, remaining={quota.get('remaining')}, 本次预算={budget}")
    if budget <= 0:
        print("今日配额不足, 明天再跑"); return
    mx = max_q_per_topic(questions)
    seen_hash = set(log["fetched"].keys())
    per_topic_done = {}
    for h, rec in log["fetched"].items():
        per_topic_done[rec["topicId"]] = per_topic_done.get(rec["topicId"], 0) + 1
    # 按年级轮询, 保证各学段均衡
    by_grade = {}
    for t in topics:
        if mx.get(t["id"], 0) + per_topic_done.get(t["id"], 0) < args.target:
            by_grade.setdefault(t["grade"], []).append(t)
    order = []
    lists = [sorted(v, key=lambda t: t["id"]) for v in by_grade.values()]
    while any(lists):
        for l in lists:
            if l:
                order.append(l.pop(0))
    new_items, skipped = [], {}
    spent = 0
    raw_batch = []
    letter_counter = {}
    for t in order:
        if spent >= budget:
            break
        tid = t["id"]
        want = min(args.per_topic, args.target - mx.get(tid, 0) - per_topic_done.get(tid, 0))
        if want <= 0:
            continue
        m = mapping[tid]
        params = {"subject_id": SUBJECT_MATH, "has_images": "false", "auto_gradable": "true",
                  "limit": want + 2, "random_order": "true"}
        if m.get("grade_id"):
            params["grade_id"] = m["grade_id"]
        if m.get("knowledge_id"):
            params["knowledge_id"] = m["knowledge_id"]
        else:
            params["keyword"] = m["name"]
        data = api_get("/v1/questions", params)
        items = data.get("items", [])
        spent += len(items)
        raw_batch.extend(items)
        got = 0
        for it in items:
            ch = it.get("content_hash") or str(it.get("id"))
            if ch in seen_hash or got >= want:
                continue
            q, why = convert_item(it, tid, letter_counter.get(tid, 0))
            letter_counter[tid] = letter_counter.get(tid, 0) + 1
            if not q:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            mx[tid] = mx.get(tid, 0) + 1
            q["id"] = f"{tid}_q{mx[tid]}"
            field_order = ["id", "topicId", "difficulty", "type", "prompt", "choices", "answer", "explanation"]
            q = {k: q[k] for k in field_order if k in q}
            new_items.append(q)
            seen_hash.add(ch)
            log["fetched"][ch] = {"tz_id": it.get("id"), "topicId": tid, "qid": q["id"],
                                   "source": it.get("source"), "year": it.get("year"),
                                   "paper_type": it.get("paper_type"),
                                   "tz_question_type": it.get("question_type"),
                                   "tz_difficulty": it.get("difficulty"),
                                   "grade_id": m.get("grade_id"), "at": datetime.now(timezone.utc).isoformat()}
            per_topic_done[tid] = per_topic_done.get(tid, 0) + 1
            got += 1
        if data.get("quota_remaining_today", 0) <= 5:
            print("配额即将用尽, 停止")
            break
        time.sleep(0.15)
    run = {"at": datetime.now(timezone.utc).isoformat(), "budget": budget, "spent": spent,
           "imported": len(new_items), "skipped": skipped,
           "quota_remaining": data.get("quota_remaining_today") if 'data' in dir() else None}
    log["runs"].append(run)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json.dump(raw_batch, open(os.path.join(RAW, f"{ts}.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(f"本次消耗 {spent} 题, 可导入 {len(new_items)} 题, 跳过 {skipped}")
    if args.dry_run:
        print("dry-run: 未写入 questions.json / log.json; 样例:")
        for q in new_items[:3]:
            print(json.dumps(q, ensure_ascii=False)[:400])
        return
    json.dump(log, open(os.path.join(IMP, "log.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if new_items:
        questions.extend(new_items)
        json.dump(questions, open(QUESTIONS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"已追加到 data/questions.json, 现共 {len(questions)} 题")


def cmd_status(args):
    quota = api_get("/v1/quota")
    print("quota:", json.dumps(quota, ensure_ascii=False))
    log_path = os.path.join(IMP, "log.json")
    if os.path.exists(log_path):
        log = json.load(open(log_path, encoding="utf-8"))
        per = {}
        for rec in log["fetched"].values():
            per[rec["topicId"]] = per.get(rec["topicId"], 0) + 1
        import collections
        print(f"已导入 {len(log['fetched'])} 题, 覆盖 {len(per)} 个知识点; 按年级:",
              dict(collections.Counter(t["grade"] for t in json.load(open(TOPICS, encoding='utf-8'))
                                         if t["id"].startswith('cnm_') and t["id"] in per)))
        for r in log["runs"][-5:]:
            print(" run:", r["at"][:19], "spent", r["spent"], "imported", r["imported"], r["skipped"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("map")
    f = sub.add_parser("fetch")
    f.add_argument("--per-topic", type=int, default=2)
    f.add_argument("--budget", type=int, default=10**9)
    f.add_argument("--grades", default="")
    f.add_argument("--target", type=int, default=10)
    f.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "map":
        build_map()
    elif args.cmd == "fetch":
        cmd_fetch(args)
    else:
        cmd_status(args)


if __name__ == "__main__":
    main()
