#!/usr/bin/env python3
# finance_bot_ai_v4.py
# 最终稳定版 — Қазақша жауап, data/ 默认存储, 支持上传原始Excel索引与按名称/日期检索并发送

import os
import re
import json
import time
import uuid
import traceback
from datetime import datetime, date, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

import requests
import telebot
import pandas as pd


BOT_TOKEN = ""  
OLLAMA_URL = ""         
MODEL_NAME = "mistral"
DATA_DIR = "data"
FILES_DIR = os.path.join(DATA_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

SAVE_MODE = "single"   # "single" 或 "daily"
DEFAULT_DATA_FILE = os.path.join(DATA_DIR, "finance_data.json")
OLLAMA_API_ENDPOINT = OLLAMA_URL.rstrip("/") + "/api/generate"

# -------------------- Қазақша мәтіндер --------------------
KZ = {
    "greeting": "Сәлем! Қалай көмектесейін? Мысал: «бүгін такси 2000 төледім және жерден 4000 таптым».",
    "saved_multi": "Жазбалар сақталды: {n} шт.\n{lines}\nЖиынтық: Кіріс={inc:.2f} KZT, Шығыс={exp:.2f} KZT, Таза={net:+.2f} KZT.",
    "saved_single": "Жазба сақталды: {type} — {amount:.2f} KZT ({date})\n{desc}",
    "ask_confirm_unknown": "Кейбір сандардың түрін анықтай алмадым: {items}\nТүзету үшін қысқа сөйлем жазыңыз (мысалы: 'сол 2-ні шығыс деп өзгерту').",
    "no_amount": "Сандар табылмады — нақты соманы жіберіңіз немесе Excel жіберіңіз.",
    "error": "Қате: {err}",
    "today_summary": "Бүгінгі есеп — Кіріс: {inc:.2f} KZT; Шығыс: {exp:.2f} KZT; Таза: {net:+.2f} KZT.",
    "file_saved": "Файл сақталды және өңделді: {count} жазба табылды.",
    "deleted_ok": "Жазба(лар) жойылды: {n}.",
    "edited_ok": "Жазба өзгертілді.",
    "export_ready": "Сұралған экспорт дайын — файл жіберілді.",
    "file_not_found": "Көрсетілген файл табылмады.",
    "no_transactions": "Осы күнге жазба табылмады."
}

# -------------------- JSON 存取 --------------------
def data_filepath(for_date: Optional[date] = None) -> str:
    if SAVE_MODE == "daily":
        d = for_date or date.today()
        return os.path.join(DATA_DIR, f"{d.isoformat()}.json")
    return DEFAULT_DATA_FILE

def load_data() -> Dict[str, Any]:
    fp = data_filepath()
    if not os.path.exists(fp):
        base = {"conversations": [], "transactions": [], "files": []}
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=2)
        return base
    with open(fp, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {"conversations": [], "transactions": [], "files": []}

def save_data(data: Dict[str, Any]) -> None:
    fp = data_filepath()
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


NUMBER_RE = re.compile(r'(?P<num>\d{1,3}(?:[ \u00A0,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*[kкKК]?)')

def normalize_number_token(tok: str) -> Optional[float]:
    if not tok:
        return None
    s = tok.strip().replace("\u00A0", "").replace(" ", "").replace(",", ".")
    mult = 1
    if s and s[-1].lower() in ("k", "к"):
        mult = 1000
        s = s[:-1]
    try:
        return float(s) * mult
    except:
        return None

def find_numbers_with_positions(text: str) -> List[Tuple[float,int,int,str]]:
    res = []
    for m in NUMBER_RE.finditer(text):
        raw = m.group("num")
        val = normalize_number_token(raw)
        if val is not None:
            res.append((val, m.start(), m.end(), raw))
    # fallback: try simple digits
    if not res:
        for m in re.finditer(r'(\d+(?:[.,]\d+)?)', text):
            try:
                res.append((float(m.group(1).replace(",", ".")), m.start(), m.end(), m.group(1)))
            except:
                continue
    return res


EXP_KW = ["жұмс","жұмса","шық","төл","төлед","spent","paid","pay","expense","потрат","платил","花了","支付","трат","кетті","шықты","шығыс"]
INC_KW = ["табу","табы","табып","алды","кірі","кіріс","пайда","получ","received","got","found","得","找到","нашел","таптым","кіру","кіріс"]

def nearest_keyword_type(text:str, pos:int) -> Optional[str]:
    window = 40
    start = max(0, pos - window)
    end = min(len(text), pos + window)
    seg = text[start:end].lower()
    for w in INC_KW:
        if w in seg:
            return "income"
    for w in EXP_KW:
        if w in seg:
            return "expense"
    # whole text fallback
    low = text.lower()
    for w in INC_KW:
        if w in low:
            return "income"
    for w in EXP_KW:
        if w in low:
            return "expense"
    return None


CLAUSE_SPLIT_RE = re.compile(r'[.,;!?\n]|(?:\b(?:және|и|and|мен|та|和|و)\b)', flags=re.IGNORECASE)
def split_clauses(text:str) -> List[Tuple[str,int]]:
    clauses=[]
    idx = 0
    for part in CLAUSE_SPLIT_RE.split(text):
        if not part:
            idx += 1
            continue
        start = text.find(part, idx)
        if start == -1: start = idx
        clauses.append((part.strip(), start))
        idx = start + len(part)
    if not clauses:
        clauses=[(text,0)]
    return clauses

def parse_message_to_transactions(text: str) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    """
    返回 (transactions, unknowns)
    transactions: [{'type','amount','date','currency','description'}...]
    unknowns: [{'amount','context','pos'}...] 需要确认的项
    """
    txs=[]
    unknowns=[]
    clauses = split_clauses(text)
    for clause, base_idx in clauses:
        nums = find_numbers_with_positions(clause)
        if not nums:
            continue
        for val, s, e, raw in nums:
            abs_pos = base_idx + s
            ttype = nearest_keyword_type(text, abs_pos)
            if ttype is None:
                # clause内查找关键词更精确
                candidate_types=[]
                low_clause = clause.lower()
                for w in INC_KW:
                    p = low_clause.find(w)
                    if p!=-1:
                        candidate_types.append(("income", abs(p - s)))
                for w in EXP_KW:
                    p = low_clause.find(w)
                    if p!=-1:
                        candidate_types.append(("expense", abs(p - s)))
                if candidate_types:
                    candidate_types.sort(key=lambda x: x[1])
                    ttype = candidate_types[0][0]
            if ttype is None:
                unknowns.append({"amount": val, "context": clause.strip(), "pos": abs_pos})
            else:
                txs.append({
                    "type": ttype,
                    "amount": float(val),
                    "currency": "KZT",
                    "date": date.today().isoformat(),
                    "description": clause.strip()[:240]
                })
    return txs, unknowns

# -------------------- Ollama 备援（仅在本地解析失败时调用） --------------------
def call_ollama_for_transaction(user_text: str, model: str = MODEL_NAME) -> Dict[str, Any]:
    system_prompt = (
        "You are a financial assistant. Respond ONLY with JSON array or JSON object.\n"
        "Fields: type (income|expense), amount (number), currency (string), date (YYYY-MM-DD), description (short)."
    )
    payload = {"model": model, "messages":[{"role":"system","content":system_prompt},{"role":"user","content":user_text}], "max_tokens":256, "temperature":0.0}
    try:
        resp = requests.post(OLLAMA_API_ENDPOINT, json=payload, timeout=6)
        resp.raise_for_status()
        j = resp.json()
        merged = json.dumps(j, ensure_ascii=False)
        js = extract_first_json_object(merged)
        if js:
            return {"json": json.loads(js), "raw": merged}
        return {"raw": merged}
    except Exception as e:
        return {"error": str(e)}

def extract_first_json_object(s: str) -> Optional[str]:
    # 尝试找第一个 JSON 对象或数组
    start = s.find("{")
    if start == -1:
        start = s.find("[")
        if start == -1:
            return None
        depth = 0; in_str = False; esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if ch == '"' and not esc:
                in_str = not in_str
            if in_str and ch == "\\" and not esc:
                esc = True; continue
            if not in_str:
                if ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        return s[start:i+1]
            if esc:
                esc = False
        return None
    depth = 0; in_str = False; esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if ch == '"' and not esc:
            in_str = not in_str
        if in_str and ch == "\\" and not esc:
            esc = True; continue
        if not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i+1]
        if esc:
            esc = False
    return None

# -------------------- 存储、检索、导出辅助 --------------------
def save_transactions(user_id:int, user_text:str, txs:List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    data = load_data()
    saved=[]
    ts = datetime.now(timezone.utc).isoformat()
    for t in txs:
        rec = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "timestamp": ts,
            "data": t,
            "source_text": user_text
        }
        data["transactions"].append(rec)
        saved.append(rec)
    data["conversations"].append({"id":str(uuid.uuid4()), "user_id":user_id, "timestamp":ts, "text":user_text, "tx_ids":[r["id"] for r in saved]})
    save_data(data)
    return saved

def totals_for_period(user_id:int, start_date:date, end_date:date) -> Tuple[float,float]:
    data = load_data()
    inc=0.0; exp=0.0
    for t in data.get("transactions",[]):
        if t.get("user_id")!=user_id: continue
        try:
            dt = datetime.fromisoformat(t.get("timestamp")).date()
            if not (start_date <= dt <= end_date): continue
            d = t.get("data",{})
            amt = float(d.get("amount",0))
            if d.get("type")=="income": inc+=amt
            else: exp+=amt
        except:
            continue
    return inc, exp

def list_transactions_for_date(user_id:int, target:date) -> List[Dict[str,Any]]:
    data = load_data()
    out=[]
    for t in data.get("transactions",[]):
        if t.get("user_id")!=user_id: continue
        try:
            dt = datetime.fromisoformat(t.get("timestamp")).date()
            if dt==target:
                out.append(t)
        except:
            continue
    return out

def export_transactions_to_csv(trans:List[Dict[str,Any]], filename:str) -> str:
    rows=[]
    for t in trans:
        d=t.get("data",{})
        rows.append({"id":t.get("id"), "type":d.get("type"), "amount":d.get("amount"), "currency":d.get("currency"), "date":d.get("date"), "description":d.get("description")})
    df = pd.DataFrame(rows)
    path = os.path.join(DATA_DIR, filename)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path

def index_uploaded_file(user_id:int, filename:str, path:str) -> None:
    data = load_data()
    data["files"].append({"id":str(uuid.uuid4()), "user_id":user_id, "timestamp":datetime.now(timezone.utc).isoformat(), "filename":filename, "path":path})
    save_data(data)

def find_file_by_name_or_date(user_id:int, text:str) -> Optional[Dict[str,Any]]:
    # 尝试按文件名关键词匹配
    data = load_data()
    low = text.lower()
    # 按文件名包含关键词搜索
    for f in reversed(data.get("files", [])):  # 最近上传优先
        if f.get("user_id")!=user_id: continue
        if f.get("filename") and f.get("filename").lower() in low:
            return f
    # 按出现的显式日期 YYYY-MM-DD 搜索
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        dstr = m.group(1)
        for f in reversed(data.get("files", [])):
            if f.get("user_id")!=user_id: continue
            try:
                if f.get("timestamp", "").startswith(dstr):
                    return f
            except:
                continue
    # 近似名字匹配（关键词）
    for f in reversed(data.get("files", [])):
        if f.get("user_id")!=user_id: continue
        fname = f.get("filename","").lower()
        for token in low.split():
            if token and token in fname:
                return f
    return None

# -------------------- Telegram 交互 --------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

def detect_intent(text:str) -> Optional[str]:
    low = text.lower()
    if any(w in low for w in ["удали последнее","удали последний","жой","удалить последний","delete last"]):
        return "delete_last"
    if any(w in low for w in ["экспорт","export","csv","excel","файл жібер","берші excel","отправь excel"]):
        return "export"
    if re.search(r'\b(қанша|сколько|how much|多少|жарат|жасады)\b', low):
        return "query"
    if any(w in low for w in ["исправ","измен","өңдеу","改变","改成","последний","change last","редакт"]):
        return "edit"
    if any(w in low for w in ["файл", "excel", "csv", ".xlsx", "берші", "жібер"]):
        return "file_request"
    return None

@bot.message_handler(commands=["start","help"])
def cmd_start(m):
    bot.reply_to(m, KZ["greeting"])

@bot.message_handler(content_types=["text"])
def handle_text(m):
    user_id = m.from_user.id
    text = m.text.strip()
    try:
        # 问候
        if any(g in text.lower() for g in ["привет","сәлем","hello","hi","салам"]):
            bot.reply_to(m, KZ["greeting"])
            return

        intent = detect_intent(text)

        # 删除最后 N 条
        if intent == "delete_last":
            nmatch = re.search(r'(\d+)', text)
            n = int(nmatch.group(1)) if nmatch else 1
            data = load_data()
            removed = 0
            for i in range(len(data["transactions"]) - 1, -1, -1):
                if removed >= n:
                    break
                if data["transactions"][i].get("user_id") == user_id:
                    data["transactions"].pop(i)
                    removed += 1
            save_data(data)
            bot.reply_to(m, KZ["deleted_ok"].format(n=removed))
            return

        # 导出 / 发送文件请求
        if intent == "file_request" or intent == "export":
            # 检查是否是“今天的”“指定日期的”或文件名
            if "бүгін" in text.lower() or "today" in text.lower():
                target = date.today()
                trans = list_transactions_for_date(user_id, target)
                if not trans:
                    bot.reply_to(m, KZ["no_transactions"])
                    return
                fname = f"export_{user_id}_{target.isoformat()}.csv"
                path = export_transactions_to_csv(trans, fname)
                bot.send_document(m.chat.id, open(path, "rb"))
                bot.reply_to(m, KZ["export_ready"])
                return
            # try find file by name or date tokens
            f = find_file_by_name_or_date(user_id, text)
            if f:
                try:
                    bot.send_document(m.chat.id, open(f["path"], "rb"))
                    return
                except Exception as e:
                    bot.reply_to(m, KZ["error"].format(err=str(e)))
                    return
            # fallback: export for date if specified
            mdate = re.search(r'(\d{4}-\d{2}-\d{2})', text)
            if mdate:
                target = datetime.fromisoformat(mdate.group(1)).date()
                trans = list_transactions_for_date(user_id, target)
                if not trans:
                    bot.reply_to(m, KZ["no_transactions"])
                    return
                fname = f"export_{user_id}_{target.isoformat()}.csv"
                path = export_transactions_to_csv(trans, fname)
                bot.send_document(m.chat.id, open(path, "rb"))
                bot.reply_to(m, KZ["export_ready"])
                return
            bot.reply_to(m, KZ["file_not_found"])
            return

        # 查询（今天/指定日期）
        if intent == "query":
            if re.search(r'\b(бүгін|today)\b', text.lower()):
                target = date.today()
            else:
                mdate = re.search(r'(\d{4}-\d{2}-\d{2})', text)
                if mdate:
                    target = datetime.fromisoformat(mdate.group(1)).date()
                else:
                    target = date.today()
            inc, exp = totals_for_period(user_id, target, target)
            bot.reply_to(m, KZ["today_summary"].format(inc=inc, exp=exp, net=inc-exp))
            return

        # 编辑（简单支持：修改最后一条金额 / 修改最后一条为支出/收入）
        if intent == "edit":
            # 修改最后金额 — 例 "change last to 3000" 或 "последний 3000"
            mnum = re.search(r'(\d+(?:[.,]\d+)?)(?!.*\d)', text.replace(",", "."))
            if mnum:
                val = float(mnum.group(1).replace(",", "."))
                data = load_data()
                for i in range(len(data["transactions"]) - 1, -1, -1):
                    if data["transactions"][i].get("user_id") == user_id:
                        data["transactions"][i]["data"]["amount"] = val
                        save_data(data)
                        bot.reply_to(m, KZ["edited_ok"])
                        return
            # 修改最后类型（"make last expense"）
            if any(w in text.lower() for w in ["expense","шығыс","шық","төл"]):
                data = load_data()
                for i in range(len(data["transactions"]) - 1, -1, -1):
                    if data["transactions"][i].get("user_id") == user_id:
                        data["transactions"][i]["data"]["type"] = "expense"
                        save_data(data)
                        bot.reply_to(m, KZ["edited_ok"])
                        return
            if any(w in text.lower() for w in ["income","кіріс","алды","табыс"]):
                data = load_data()
                for i in range(len(data["transactions"]) - 1, -1, -1):
                    if data["transactions"][i].get("user_id") == user_id:
                        data["transactions"][i]["data"]["type"] = "income"
                        save_data(data)
                        bot.reply_to(m, KZ["edited_ok"])
                        return
            bot.reply_to(m, "Өңдеу форматын түсінбедім. Мысал: 'change last to 3000' немесе 'последний 3000'.")
            return

        # 默认：尝试把消息解析为交易（可生成多笔）
        txs, unknowns = parse_message_to_transactions(text)
        # 如果本地解析为空，调用备援（Ollama）
        if not txs and not unknowns:
            model_resp = call_ollama_for_transaction(text)
            if "json" in model_resp:
                payload = model_resp["json"]
                if isinstance(payload, dict) and payload.get("amount") is not None:
                    txs = [{
                        "type": payload.get("type","expense"),
                        "amount": float(payload.get("amount")),
                        "currency": payload.get("currency","KZT"),
                        "date": payload.get("date", date.today().isoformat()),
                        "description": payload.get("description", text[:240])
                    }]
                elif isinstance(payload, list):
                    for obj in payload:
                        if obj.get("amount"):
                            txs.append({"type":obj.get("type","expense"), "amount":float(obj.get("amount")), "currency":obj.get("currency","KZT"), "date":obj.get("date", date.today().isoformat()), "description":obj.get("description", text[:240])})
            else:
                # fallback: first numeric token
                fb = find_numbers_with_positions(text)
                if fb:
                    txs = [{"type":"expense","amount":fb[0][0],"currency":"KZT","date":date.today().isoformat(),"description":text[:240]}]

        if txs:
            saved = save_transactions(user_id, text, txs)
            inc = sum([t["data"]["amount"] for t in saved if t["data"]["type"]=="income"])
            exp = sum([t["data"]["amount"] for t in saved if t["data"]["type"]!="income"])
            net = inc - exp
            lines=[]
            for i,t in enumerate(saved,1):
                typ = "Кіріс" if t["data"]["type"]=="income" else "Шығыс"
                lines.append(f"{i}) {typ} - {t['data']['amount']:.2f} KZT - {t['data']['description'][:60]}")
            # 如果有未确定项，先提示
            resp_text = KZ["saved_multi"].format(n=len(saved), lines="\n".join(lines), inc=inc, exp=exp, net=net)
            bot.reply_to(m, resp_text)
            if unknowns:
                items = "; ".join([f"{u['amount']} ({u['context'][:30]})" for u in unknowns])
                bot.reply_to(m, KZ["ask_confirm_unknown"].format(items=items))
            return
        else:
            bot.reply_to(m, KZ["no_amount"])
            return

    except Exception as e:
        traceback.print_exc()
        try:
            bot.reply_to(m, KZ["error"].format(err=str(e)))
        except:
            pass

@bot.message_handler(content_types=["document"])
def handle_document(m):
    try:
        file_info = bot.get_file(m.document.file_id)
        file_name = m.document.file_name or f"uploaded_{int(time.time())}"
        dest = os.path.join(FILES_DIR, f"{int(time.time())}_{file_name}")
        downloaded = bot.download_file(file_info.file_path)
        with open(dest, "wb") as f:
            f.write(downloaded)
        # 处理 Excel 文件：尝试从每行提取金额并保存为交易（作为默认行为）
        if file_name.lower().endswith((".xls", ".xlsx")):
            try:
                df = pd.read_excel(dest)
            except Exception:
                # 如果无法解析则只索引文件
                index_uploaded_file(m.from_user.id, file_name, dest)
                bot.reply_to(m, "Файл қабылданды, бірақ Excel оқу сәтсіз аяқталды — файл сақталды.")
                return
            extracted=[]
            for idx, row in df.iterrows():
                row_text = " ".join([str(x) for x in row.values if pd.notna(x)])
                nums = find_numbers_with_positions(row_text)
                if nums:
                    val = nums[0][0]
                    typ = "income" if any(w in row_text.lower() for w in INC_KW) else ("expense" if any(w in row_text.lower() for w in EXP_KW) else "expense")
                    tx = {"type":typ,"amount":float(val),"currency":"KZT","date":date.today().isoformat(),"description":row_text[:240]}
                    extracted.append(tx)
            saved = save_transactions(m.from_user.id, f"excel:{file_name}", extracted)
            index_uploaded_file(m.from_user.id, file_name, dest)
            bot.reply_to(m, KZ["file_saved"].format(count=len(saved)))
            return
        else:
            index_uploaded_file(m.from_user.id, file_name, dest)
            bot.reply_to(m, "Файл қабылданды және сақталды.")
    except Exception as e:
        traceback.print_exc()
        bot.reply_to(m, KZ["error"].format(err=str(e)))

# -------------------- 启动 --------------------
if __name__ == "__main__":
    # 安全提醒（如果 token 看起来已暴露）
    if BOT_TOKEN and "PUT_YOUR" not in BOT_TOKEN:
        print("注意：请确保 BOT_TOKEN 未在公开场合泄露。如已泄露，请在 BotFather 上重置 token。")
    print("Finance Helper Bot v4 іске қосылды. (Қазақша жауаптар, data/ 默认保存)")
    # 尝试 ping Ollama（失败不会阻塞本地解析）
    try:
        requests.get(OLLAMA_URL, timeout=1)
    except:
        print("OLLAMA 服务不可达（若不使用本地 LLM 可忽略）。")
    while True:
        try:
            bot.polling(none_stop=True)
        except Exception as e:
            traceback.print_exc()
            time.sleep(2)
