from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
import os, json, sqlite3, bcrypt

load_dotenv()
DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT,
                password TEXT NOT NULL,
                parent_name TEXT,
                parent_phone TEXT,
                parent_relation TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                ip TEXT,
                success INTEGER NOT NULL,
                logged_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                form TEXT NOT NULL,
                score INTEGER NOT NULL,
                level TEXT NOT NULL,
                item_scores TEXT,
                taken_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sent_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.commit()

def init_db_sessions():
    """table สำหรับเก็บ chat session ข้าม request — ไม่หายเมื่อ cookie expire"""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                form TEXT NOT NULL,
                item_scores TEXT NOT NULL DEFAULT '{}',
                full_history TEXT NOT NULL DEFAULT '[]',
                greeted INTEGER NOT NULL DEFAULT 0,
                result TEXT,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_session_key ON chat_sessions(session_key)")
        conn.commit()

init_db()
init_db_sessions()

def session_key(req: Request, form: str) -> str:
    """key = username::form ถ้า login, หรือ session_id::form ถ้าไม่ได้ login"""
    user = req.session.get("user", "")
    if user:
        return f"{user}::{form}"
    # สร้าง/ดึง anonymous session id จาก cookie session
    sid = req.session.get("anon_id")
    if not sid:
        import uuid
        sid = uuid.uuid4().hex
        req.session["anon_id"] = sid
    return f"anon_{sid}::{form}"

def load_chat_session(key: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE session_key=?", (key,)
        ).fetchone()
    if not row:
        return None
    return {
        "form":         row["form"],
        "item_scores":  json.loads(row["item_scores"]),
        "full_history": json.loads(row["full_history"]),
        "greeted":      bool(row["greeted"]),
        "result":       json.loads(row["result"]) if row["result"] else None,
    }

def save_chat_session(key: str, form: str, item_scores: dict,
                      full_history: list, greeted: bool, result: dict | None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO chat_sessions (session_key, form, item_scores, full_history, greeted, result, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(session_key) DO UPDATE SET
                form=excluded.form,
                item_scores=excluded.item_scores,
                full_history=excluded.full_history,
                greeted=excluded.greeted,
                result=excluded.result,
                updated_at=excluded.updated_at
        """, (
            key, form,
            json.dumps(item_scores,  ensure_ascii=False),
            json.dumps(full_history, ensure_ascii=False),
            1 if greeted else 0,
            json.dumps(result, ensure_ascii=False) if result else None,
        ))
        conn.commit()

def clear_chat_session(key: str):
    """Archive session เก่าไว้ใน sidebar แทนการลบ"""
    import time as _time
    archived = f"{key}::arc::{int(_time.time())}"
    with get_db() as conn:
        # rename เป็น archived key
        conn.execute("UPDATE chat_sessions SET session_key=? WHERE session_key=?", (archived, key))
        conn.commit()

# --- ส่วนของ AI และการตั้งค่าแอป ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = FastAPI()

# *** จุดที่แก้: ใช้ str(BASE_DIR / "...") เพื่อระบุตำแหน่งที่แน่นอน ***
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ใช้ Secret Key จาก Env (ถ้าไม่มีจะใช้คำว่า verysecret แทน)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("MY_SUPER_SECRET_KEY", "verysecret"), max_age=60*60*24*7)

# ======================================================
# FORMS
# ======================================================
FORMS = {
    "PHQ-9": [
        "เบื่อ ไม่สนใจอยากทำอะไร",
        "ไม่สบายใจ ซึมเศร้า ท้อแท้",
        "หลับยากหรือหลับๆตื่นๆหรือนอนมากไป",
        "เหนื่อยง่ายหรือไม่ค่อยมีแรง",
        "เบื่ออาหารหรือกินมากเกินไป",
        "รู้สึกไม่ดีกับตัวเอง คิดว่าล้มเหลว",
        "สมาธิไม่ดี",
        "พูดช้าหรือกระสับกระส่าย",
        "คิดทำร้ายตัวเองหรือคิดว่าตายไปคงจะดี"
    ],
    "BDI-II": [
        "ความเศร้า","การมองโลกในแง่ร้าย","ความล้มเหลวในอดีต","การสูญเสียความสุข",
        "ความรู้สึกผิด","ความรู้สึกถูกลงโทษ","การไม่ชอบตัวเอง","การวิพากษ์วิจารณ์ตัวเอง",
        "ความคิดฆ่าตัวตาย","การร้องไห้","ความกระวนกระวายใจ","การสูญเสียความสนใจ",
        "ความไม่เด็ดขาด","ความไร้ค่า","การสูญเสียพลังงาน","การเปลี่ยนแปลงรูปแบบการนอนหลับ",
        "ความหงุดหงิด","การเปลี่ยนแปลงความอยากอาหาร","ความยากลำบากในการมีสมาธิ",
        "ความเหนื่อยล้าหรืออ่อนเพลีย","การสูญเสียความสนใจในเรื่องเพศ"
    ],
    "CES-D": [
        "คุณรู้สึกกังวลกับสิ่งที่ปกติแล้วไม่รบกวนคุณ",
        "คุณรู้สึกไม่อยากกินอาหาร หรือเบื่ออาหาร",
        "คุณรู้สึกว่าไม่สามารถสลัดความเศร้าออกไปได้",
        "คุณรู้สึกว่าคุณมีคุณค่าเท่ากับคนอื่น",
        "คุณรู้สึกลำบากในการมีสมาธิกับสิ่งที่ทำ",
        "คุณรู้สึกซึมเศร้า",
        "คุณรู้สึกว่าทุกสิ่งที่ทำต้องใช้ความพยายามมาก",
        "คุณรู้สึกมีความหวังเกี่ยวกับอนาคต",
        "คุณรู้สึกว่าชีวิตของคุณเป็นความล้มเหลว",
        "คุณรู้สึกหวาดกลัว",
        "คุณนอนหลับไม่สนิท",
        "คุณรู้สึกมีความสุข",
        "คุณพูดน้อยกว่าปกติ",
        "คุณรู้สึกโดดเดี่ยว",
        "คุณรู้สึกว่าผู้อื่นไม่เป็นมิตร",
        "คุณรู้สึกสนุกกับชีวิต",
        "คุณร้องไห้บ่อย",
        "คุณรู้สึกเศร้า",
        "คุณรู้สึกว่าผู้อื่นไม่ชอบคุณ",
        "คุณรู้สึกว่าตัวเองไม่สามารถเริ่มต้นทำสิ่งต่าง ๆ ได้"
    ],
    "HADS": [
        "ฉันรู้สึกตึงเครียดหรือกระวนกระวายใจ",
        "ฉันยังรู้สึกเพลิดเพลินใจกับสิ่งต่าง ๆ ที่เคยชอบ",
        "ฉันมีความรู้สึกกลัว คล้ายกับว่ากำลังจะมีเรื่องไม่ดีเกิดขึ้น",
        "ฉันสามารถหัวเราะและมองเห็นด้านขำขันของเรื่องต่าง ๆ ได้",
        "ฉันมีความคิดวิตกกังวลอยู่บ่อยครั้ง",
        "ฉันรู้สึกแจ่มใสเบิกบาน",
        "ฉันสามารถผ่อนคลายและรู้สึกสบายใจได้",
        "ฉันรู้สึกว่าความคิดหรือการกระทำของฉันช้าลง",
        "ฉันรู้สึกไม่สบายใจหรือปั่นป่วนในท้อง",
        "ฉันไม่ค่อยสนใจดูแลตนเองเหมือนเดิม",
        "ฉันรู้สึกกระสับกระส่าย เหมือนอยู่นิ่งไม่ได้",
        "ฉันมองอนาคตด้วยความหวัง",
        "ฉันมีความรู้สึกผวาหรือตกใจขึ้นมาอย่างฉับพลัน",
        "ฉันยังสามารถเพลิดเพลินกับการอ่านหนังสือ ฟังวิทยุ หรือดูโทรทัศน์ได้"
    ],
    "GDS-15": [
        "โดยรวมแล้ว คุณพึงพอใจกับชีวิตของคุณหรือไม่",
        "คุณลดหรือเลิกทำกิจกรรมและสิ่งที่คุณเคยสนใจหรือไม่",
        "คุณรู้สึกว่าชีวิตของคุณว่างเปล่าหรือไม่",
        "คุณรู้สึกเบื่อบ่อยหรือไม่",
        "โดยส่วนใหญ่แล้ว คุณรู้สึกมีอารมณ์ดีหรือไม่",
        "คุณกลัวว่าจะมีเรื่องร้ายเกิดขึ้นกับคุณหรือไม่",
        "คุณรู้สึกมีความสุขเป็นส่วนใหญ่ของเวลาหรือไม่",
        "คุณรู้สึกว่าตัวเองช่วยเหลือตนเองไม่ได้หรือไม่",
        "คุณมักจะชอบอยู่บ้านมากกว่าการออกไปทำกิจกรรมใหม่ ๆ หรือไม่",
        "คุณคิดว่าความจำของคุณแย่กว่าคนทั่วไปหรือไม่",
        "คุณคิดว่าการมีชีวิตอยู่เป็นสิ่งที่ดีหรือไม่",
        "คุณรู้สึกว่าตัวเองไม่มีคุณค่าหรือไม่",
        "คุณรู้สึกมีพลังหรือกระปรี้กระเปร่าอยู่หรือไม่",
        "คุณรู้สึกว่าสถานการณ์ของคุณตอนนี้สิ้นหวังหรือไม่",
        "คุณคิดว่าคนอื่น ๆ มีชีวิตที่ดีกว่าคุณหรือไม่"
    ],
    "Zung-SDS": [
        "ฉันรู้สึกซึมเศร้าและเศร้าใจ",
        "ในตอนเช้า ฉันรู้สึกดีที่สุด",
        "ฉันรู้สึกอยากร้องไห้หรือร้องไห้บ่อย",
        "ฉันนอนหลับไม่ดีในตอนกลางคืน",
        "ฉันยังรับประทานอาหารได้เท่าเดิม",
        "ฉันยังรู้สึกเพลิดเพลินกับเรื่องเพศ",
        "ฉันสังเกตว่าน้ำหนักของฉันลดลง",
        "ฉันมีอาการท้องผูก",
        "หัวใจของฉันเต้นเร็วกว่าปกติ",
        "ฉันรู้สึกเหนื่อยโดยไม่มีเหตุผล",
        "จิตใจของฉันยังปลอดโปร่งเหมือนเดิม",
        "ฉันยังสามารถทำสิ่งต่าง ๆ ได้ง่ายเหมือนเดิม",
        "ฉันรู้สึกกระวนกระวายหรืออยู่นิ่งไม่ได้",
        "ฉันมีความหวังต่ออนาคต",
        "ฉันหงุดหงิดง่ายกว่าปกติ",
        "ฉันสามารถตัดสินใจได้ง่ายเหมือนเดิม",
        "ฉันรู้สึกว่าตัวเองมีคุณค่าและเป็นที่ต้องการ",
        "ฉันรู้สึกว่าชีวิตของฉันมีความหมาย",
        "ฉันรู้สึกว่าตนเองเป็นประโยชน์ต่อผู้อื่น",
        "โดยรวมแล้ว ฉันพึงพอใจกับชีวิตของฉัน"
    ],
    "HAM-D": [
        "อารมณ์ซึมเศร้า เช่น ความเศร้า สิ้นหวัง หรือร้องไห้ง่าย",
        "ความรู้สึกผิด หรือการตำหนิตนเอง",
        "ความคิดอยากตายหรือทำร้ายตนเอง",
        "ปัญหาการนอนหลับในระยะเริ่มต้น (เข้านอนยาก)",
        "ปัญหาการนอนหลับกลางคืน (ตื่นบ่อย)",
        "ปัญหาการนอนหลับระยะท้าย (ตื่นเช้าเกินไป)",
        "ความสนใจและความสามารถในการทำงานหรือกิจกรรม",
        "การชะลอการเคลื่อนไหวหรือการพูด",
        "ความกระสับกระส่ายหรืออยู่ไม่สุข",
        "ความวิตกกังวลทางจิตใจ",
        "ความวิตกกังวลทางร่างกาย",
        "อาการทางระบบทางเดินอาหาร",
        "อาการทางกล้ามเนื้อหรือความอ่อนแรง",
        "อาการด้านความต้องการทางเพศ",
        "ความหมกมุ่นหรือกังวลเกี่ยวกับสุขภาพ",
        "การเปลี่ยนแปลงของน้ำหนักตัว",
        "ระดับการรับรู้และเข้าใจภาวะของตนเอง"
    ]
}

FORM_SCORING_CRITERIA = {
    "PHQ-9": {
        "item_range": "0-3 (0=ไม่มีเลย, 1=มีบางวัน, 2=มีบ่อย, 3=มีแทบทุกวัน)",
        "timeframe": "ใน 2 สัปดาห์ที่ผ่านมา"
    },
    "BDI-II": {
        "item_range": "0-3 (0=ไม่มี, 1=เล็กน้อย, 2=ปานกลาง, 3=รุนแรง)",
        "timeframe": "ใน 2 สัปดาห์ที่ผ่านมา"
    },
    "CES-D": {
        "item_range": "0-3 (0=ไม่มีหรือน้อยมาก, 1=บางครั้ง, 2=บ่อยครั้ง, 3=ส่วนใหญ่)",
        "timeframe": "ใน 1 สัปดาห์ที่ผ่านมา",
        "note": "ข้อ 4, 8, 12, 16 เป็น reverse items"
    },
    "HADS": {
        "item_range": "0-3 (0=ไม่มีเลย, 3=มากที่สุด)",
        "timeframe": "ใน 1 สัปดาห์ที่ผ่านมา",
        "note": "ข้อคี่=anxiety, ข้อคู่=depression"
    },
    "GDS-15": {
        "item_range": "0-1 (ใช่=1, ไม่ใช่=0 บางข้อกลับค่า)",
        "timeframe": "ช่วงนี้โดยทั่วไป"
    },
    "Zung-SDS": {
        "item_range": "1-4 (1=น้อยมาก, 2=บางครั้ง, 3=บ่อยครั้ง, 4=ส่วนใหญ่)",
        "timeframe": "ช่วงนี้โดยทั่วไป",
        "note": "ข้อ 2,5,6,11,12,14,16,17,18,19,20 เป็น positive items (กลับค่า)"
    },
    "HAM-D": {
        "item_range": "ส่วนใหญ่ 0-4 (0=ไม่มี, 4=รุนแรงมาก) บางข้อ 0-2",
        "timeframe": "1 สัปดาห์ที่ผ่านมา"
    }
}

LEVEL_RULES = {
    "PHQ-9":    [(0,4,"LOW"),(5,9,"MILD"),(10,14,"MODERATE"),(15,19,"MODERATE_HIGH"),(20,27,"HIGH")],
    "BDI-II":   [(0,13,"LOW"),(14,19,"MILD"),(20,28,"MODERATE"),(29,63,"HIGH")],
    "CES-D":    [(0,15,"LOW"),(16,25,"MEDIUM"),(26,60,"HIGH")],
    "HADS":     [(0,7,"LOW"),(8,10,"MEDIUM"),(11,21,"HIGH")],
    "GDS-15":   [(0,4,"LOW"),(5,8,"MEDIUM"),(9,15,"HIGH")],
    "Zung-SDS": [(20,39,"LOW"),(40,59,"MEDIUM"),(60,80,"HIGH")],
    "HAM-D":    [(0,7,"LOW"),(8,16,"MEDIUM"),(17,52,"HIGH")]
}

# ======================================================
# SYSTEM PROMPT — single conversation loop
# ======================================================
def build_system_prompt(form: str, all_questions: list, item_scores: dict, crisis_extra: str = "", full_history: list = []) -> str:
    criteria = FORM_SCORING_CRITERIA.get(form, {})

    missing_indices = []
    for i, q in enumerate(all_questions):
        if str(i) not in item_scores:
            missing_indices.append(f"[{i}] {q}")

    missing_str  = "\n".join(missing_indices) or "ครบแล้ว"
    scored_str   = ", ".join(f"{k}={v}" for k, v in item_scores.items()) or "ยังไม่มี"
    scored_count = len(item_scores)
    total_count  = len(all_questions)

    # ดึงคำถามที่ AI ถามไปแล้วทั้งหมด เพื่อป้องกันถามซ้ำ
    asked = [m["content"] for m in full_history if m["role"] == "assistant"]
    asked_str = "\n".join(f"- {q[:60]}" for q in asked[-10:]) if asked else "ยังไม่มี"

    return f"""คุณคือเพื่อนที่กำลังฟังใครสักคนเล่าเรื่อง
คุยภาษาไทยแบบที่คนจริงๆ คุยกัน — สั้น เป็นธรรมชาติ ไม่ตัดสิน

งานของคุณ (ห้ามบอก user):
- ช่วง: {criteria.get("timeframe", "")} | สเกล: {criteria.get("item_range", "0-3")}
{f"- note: {criteria.get('note','')}" if criteria.get("note") else ""}
- ประเมินแล้ว {scored_count}/{total_count}: {scored_str}
- ยังต้องหาข้อมูล: {missing_str}
{crisis_extra}

วิธีตอบ:
- ก่อนถามทุกครั้ง ให้ถามตัวเองก่อนว่า "user บอกเรื่องนี้ไปแล้วหรือยัง?" — ถ้าบอกแล้วในรูปแบบใดก็ตาม ให้ score เลย อย่าถาม
- แปลความ conversation ทั้งหมด: "ได้คุยกับแฟน" = มีคนรับฟัง, "เล่นกีตาร์" = มีกิจกรรมผ่อนคลาย, "บอกไปแล้วนะ" = user ตอบแล้ว ให้รับและ score
- ถ้า user ตอบเรื่องนั้นแล้วในประโยคใดก็ตาม → score แล้วถามเรื่องถัดไป ห้ามถามซ้ำ
- ฟังแล้วรับ 1 ประโยค → ถาม 1 คำถาม เท่านั้น
- ถ้าอยากเจาะเพิ่ม อ้างอิงสิ่งที่เล่าไปก่อน เช่น "เมื่อกี้ที่บอกว่า..."
- ครบทุกข้อ → ปิดอบอุ่น ใส่ done:true

ห้ามเด็ดขาด:
- ห้าม normalize: "เป็นเรื่องปกติ" / "หลายคนก็รู้สึกแบบนี้" / "มันสมเหตุสมผลนะ"
- ห้าม ปลอบโยน template: "สู้ๆ" / "คิดบวกนะ" / "ไม่ได้แย่ขนาดนั้น"
- ห้าม ขึ้นต้นด้วย "เข้าใจ..." / "ได้ยิน..." / "ฟังดู..." — ถ้าจะรับรู้ความรู้สึก ให้ใช้คำอื่น
- ห้าม พูดยาวเกิน 2 ประโยค
- ห้าม คำ clinical: แบบประเมิน / วินิจฉัย / ผิดปกติ

การรับรู้ความรู้สึก — ใช้ได้ แต่ต้องสั้นและเป็นธรรมชาติ:
✅ "ฟังดูคุณกำลังรู้สึก..." แล้วถามต่อทันที
❌ "เข้าใจครับ มันต้องหนักมากเลย บางทีเราก็รู้สึกแบบนี้ได้นะครับ..." ← ยาวเกิน ซ้ำซาก

ตัวอย่างที่ดี:
user: "วันนี้หงุดหงิดมาก" → "มีอะไรทำให้หงุดหงิดเป็นพิเศษไหมครับ?"
user: "นอนไม่ค่อยหลับ" → "หลับยากตั้งแต่แรก หรือตื่นกลางดึกครับ?"
user: "ก็ไม่ชอบตัวเองอ่ะ" → "ไม่ชอบตรงไหนเป็นพิเศษครับ?"
user: "ร้องไห้หนักมากเลย" → "เกิดอะไรขึ้นครับ?"

ตัวอย่างที่ห้ามทำ:
user: "หงุดหงิดมาก" → "เข้าใจครับ บางทีเราก็รู้สึกแบบนี้ได้..." ← normalize + ยาว
user: "เศร้าอยู่" → "เป็นเรื่องปกติที่จะรู้สึกแบบนี้นะครับ" ← normalize

กฎการ score:
- อ่าน conversation ทั้งหมดก่อนตอบทุกครั้ง — ถ้า user เพิ่งตอบอะไรไป ห้ามถามเรื่องนั้นซ้ำ
- กฎเหล็ก: ถ้า user ตอบคำถามของ AI แล้ว → score ทันที แล้วถามเรื่องถัดไปเลย ห้ามถามเรื่องเดิมซ้ำไม่ว่ากรณีใด
- AI เพิ่งถามไปแล้ว (ห้ามถามเรื่องเดิมหรือความหมายใกล้เคียงซ้ำ):
{asked_str}
- score ข้อได้ก็ต่อเมื่อ user พูดถึงเรื่องนั้นชัดเจนแล้วเท่านั้น
- ถ้ายังไม่รู้เลย → ถามก่อน
- ห้าม assume หรือใส่ 0 ให้ข้อที่ยังไม่ได้คุยถึง
- ถ้า user ตอบกว้างๆ เช่น "หลายเรื่อง" / "บ้าง" / "มีนะ" / "ก็มี" → score ได้เลย ไม่ต้องเจาะเพิ่ม
- เจาะเพิ่มก็ต่อเมื่อ user ไม่ได้พูดถึงเรื่องนั้นเลย ไม่ใช่ตอบแล้วแต่ตอบกว้าง
- ดู "ยังต้องหาข้อมูล" ด้านบน — ทุกข้อที่อยู่ในนั้นต้องถามและได้คำตอบก่อน
- done:true ได้ก็ต่อเมื่อไม่มีข้อค้างใน "ยังต้องหาข้อมูล" แล้วเท่านั้น
- ถ้ายังมีข้อค้างอยู่ → ห้ามจบ ให้ถามต่อ

Output JSON เท่านั้น:
{{"reply":"...","scores":{{"<i>":<score>}},"done":false}}"""


# ======================================================
# WORD FILTER
# ======================================================
BANNED_WORDS = ["วินิจฉัย", "ผิดปกติ", "แนวโน้ม", "แบบประเมิน", "assessment", "ซึมเศร้า", "โรคซึมเศร้า", "ภาวะซึมเศร้า"]

BORING_PREFIXES = [
    # ฟังดู...
    "ฟังดูคุณรู้สึกกำลังเศร้า","ฟังดูคุณรู้สึกกำลังเหนื่อย",
    # ขอบคุณ...
    "ขอบคุณที่เล่าให้ฟังนะครับ",
]

def strip_boring_prefix(text: str) -> str:
    import re
    t = text.strip()
    # 1. exact prefix list
    for prefix in BORING_PREFIXES:
        for sep in [" ", ", ", "\n"]:
            if t.startswith(prefix + sep):
                t = t[len(prefix)+len(sep):].strip().lstrip(",").strip()
                return t
    # 2. pattern: "มันฟังดู..." / "ฟังดู..." ทุกรูปแบบ
    t = re.sub(r"^(มัน)?ฟังดู\S*\s*", "", t).strip().lstrip(",").strip()
    return t

def sanitize(text: str) -> str:
    for w in BANNED_WORDS:
        text = text.replace(w, "")
    text = strip_boring_prefix(text)
    return text.strip()


# ======================================================
# ROUTES — pages
# ======================================================
BASE_DIR = Path(__file__).resolve().parent
@app.get("/")
async def root():
    return RedirectResponse("/intro")

@app.get("/intro")
async def intro_page(request: Request):
    return templates.TemplateResponse("intro.html", {"request": request})

@app.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    ip = request.client.host
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row and bcrypt.checkpw(password.encode(), row["password"].encode()):
        request.session["user"] = username
        with get_db() as conn:
            conn.execute("INSERT INTO login_logs (username, ip, success) VALUES (?,?,1)", (username, ip))
            conn.commit()
        return JSONResponse({"ok": True})
    else:
        with get_db() as conn:
            conn.execute("INSERT INTO login_logs (username, ip, success) VALUES (?,?,0)", (username, ip))
            conn.commit()
        return JSONResponse({"ok": False, "message": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}, status_code=401)

@app.get("/register")
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register(request: Request):
    data = await request.json()
    username        = data.get("username", "").strip()
    email           = data.get("email", "").strip()
    password        = data.get("password", "")
    parent_name     = data.get("parent_name", "").strip()
    parent_phone    = data.get("parent_phone", "").strip()
    parent_relation = data.get("parent_relation", "").strip()

    if len(username) < 3:
        return JSONResponse({"ok": False, "message": "ชื่อผู้ใช้ต้องมีอย่างน้อย 3 ตัวอักษร"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"ok": False, "message": "รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร"}, status_code=400)

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (username,email,password,parent_name,parent_phone,parent_relation) VALUES (?,?,?,?,?,?)",
                (username, email, hashed, parent_name, parent_phone, parent_relation)
            )
            conn.commit()
        return JSONResponse({"ok": True})
    except sqlite3.IntegrityError:
        return JSONResponse({"ok": False, "message": "ชื่อผู้ใช้นี้ถูกใช้ไปแล้ว"}, status_code=409)

@app.get("/index")
async def index_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/conversation")
async def conversation_page(request: Request):
    return templates.TemplateResponse("conversation.html", {"request": request})

@app.get("/post-analysis")
async def post_analysis_page(request: Request):
    return templates.TemplateResponse("post_analysis.html", {"request": request})

@app.get("/dashboard")
async def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ======================================================
# CHAT — single conversation loop
# ======================================================
@app.get("/assessment/history")
async def assessment_history(req: Request, test: str = "PHQ-9", skey: str = ""):
    """คืน full_history — รับ skey โดยตรง หรือ generate จาก test"""
    if skey:
        # ดึงจาก skey ที่ระบุมาตรงๆ (กรณี resume จาก sidebar)
        target_key = skey
    else:
        target_key = session_key(req, test)

    sess = load_chat_session(target_key)
    if not sess or not sess["full_history"]:
        return {"history": []}
    history = [m for m in sess["full_history"] if m["role"] in ("user", "assistant")]
    return {"history": history}


@app.post("/assessment/reset")
async def assessment_reset(req: Request):
    try:
        data = await req.json()
    except Exception:
        data = {}
    form = data.get("test", "PHQ-9")
    skey = session_key(req, form)
    clear_chat_session(skey)
    req.session.pop("result", None)
    return {"ok": True}

@app.get("/assessment/recent-sessions")
async def recent_sessions(req: Request):
    """ดึง session ล่าสุดของ user คนนี้ทุก form"""
    user = req.session.get("user", "")
    ip   = req.client.host if req.client else "unknown"
    base = user if user else ip
    with get_db() as conn:
        rows = conn.execute("""
            SELECT session_key, form, item_scores, full_history, greeted, result, updated_at
            FROM chat_sessions
            WHERE session_key LIKE ?
            ORDER BY updated_at DESC
            LIMIT 10
        """, (f"{base}::%",)).fetchall()
    sessions = []
    for row in rows:
        form = row["form"]
        item_scores = json.loads(row["item_scores"])
        full_history = json.loads(row["full_history"])
        result = json.loads(row["result"]) if row["result"] else None
        total = len(FORMS.get(form, []))
        scored = len(item_scores)
        last_bot = next(
            (m["content"] for m in reversed(full_history) if m["role"] == "assistant"), ""
        )
        # ดึง first user message เป็น "เรื่องที่คุย"
        first_user = next(
            (m["content"] for m in full_history if m["role"] == "user"), ""
        )
        sessions.append({
            "skey": row["session_key"],
            "form": form,
            "scored": scored,
            "total": total,
            "done": result is not None or scored >= total,
            "last_bot": last_bot[:60] + ("..." if len(last_bot) > 60 else ""),
            "first_user": first_user[:40] + ("..." if len(first_user) > 40 else ""),
            "updated_at": row["updated_at"],
        })
    return {"sessions": sessions}

@app.get("/assessment/history")
async def assessment_history(req: Request, test: str = "PHQ-9"):
    """ส่ง full_history กลับมาให้ frontend render"""
    skey = session_key(req, test)
    sess = load_chat_session(skey)
    if not sess or not sess["full_history"]:
        return {"history": []}
    return {"history": sess["full_history"]}

@app.post("/assessment/resume")
async def assessment_resume(req: Request):
    """โหลด session เก่าจาก skey เข้า active session"""
    data = await req.json()
    skey = data.get("skey", "")
    if not skey:
        return {"ok": False}
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE session_key=?", (skey,)
        ).fetchone()
    if not row:
        return {"ok": False}
    form         = row["form"]
    item_scores  = json.loads(row["item_scores"])
    full_history = json.loads(row["full_history"])
    result       = json.loads(row["result"]) if row["result"] else None
    # เขียนทับ active session key
    active_key = session_key(req, form)
    save_chat_session(active_key, form, item_scores, full_history, True, result)
    req.session["form"] = form
    return {"ok": True, "form": form}

@app.get("/assessment/check-session")
async def check_session(req: Request, test: str = "PHQ-9"):
    """ตรวจว่ามี session เก่าอยู่ไหม และมีข้อมูลพอที่จะ resume ได้"""
    skey = session_key(req, test)
    sess = load_chat_session(skey)
    if not sess or not sess["greeted"] or len(sess["full_history"]) < 2:
        return {"has_session": False}
    # ดึง last bot message เพื่อ preview
    last_bot = next(
        (m["content"] for m in reversed(sess["full_history"]) if m["role"] == "assistant"),
        ""
    )
    scored = len(sess["item_scores"])
    total  = len(FORMS.get(test, []))
    return {
        "has_session": True,
        "scored": scored,
        "total": total,
        "last_bot": last_bot[:80] + ("..." if len(last_bot) > 80 else ""),
    }

# ======================================================
# CRISIS DETECTION
# ======================================================
CRISIS_HIGH = [
    "ฆ่าตัวตาย", "อยากตาย", "ตายไปดีกว่า", "ตายดีกว่า",
    "ไม่อยากมีชีวิต", "ผูกคอ", "กรีดข้อมือ", "กรีดแขน",
    "กินยาเกินขนาด", "โดดตึก",
]
CRISIS_MEDIUM = [
    "รอยเล็บ", "จิกตัวเอง", "ตีตัวเอง", "ทุบตัวเอง",
    "ทำร้ายตัวเอง", "เจ็บตัวเอง", "ไม่อยากอยู่", "หายไปดีกว่า",
]

def detect_crisis(text: str) -> str:
    if any(kw in text for kw in CRISIS_HIGH):
        return "high"
    if any(kw in text for kw in CRISIS_MEDIUM):
        return "medium"
    return ""


# ======================================================
# GPT HELPER — retry อัตโนมัติ
# ======================================================
def call_gpt(messages: list, max_tokens: int = 300, temperature: float = 0.7) -> dict:
    FALLBACK = {"reply": "", "scores": {}, "done": False, "ok": False}
    for attempt in range(2):
        try:
            _max  = max_tokens if attempt == 0 else 200
            _temp = temperature if attempt == 0 else 0.9
            _msgs = messages if attempt == 0 else [messages[0]] + messages[-4:]
            res   = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=_msgs,
                response_format={"type": "json_object"},
                max_tokens=_max,
                temperature=_temp,
                timeout=15,
            )
            raw    = res.choices[0].message.content or ""
            reason = res.choices[0].finish_reason
            print(f"📥 attempt={attempt} finish={reason} raw={raw[:120]}")
            if not raw.strip() or reason == "length":
                print(f"⚠️ attempt={attempt} empty/length — {'retrying' if attempt == 0 else 'giving up'}")
                continue
            out = json.loads(raw.strip())
            return {
                "reply": sanitize(out.get("reply", "")),
                "scores": out.get("scores", {}),
                "done": bool(out.get("done", False)),
                "ok": True,
            }
        except json.JSONDecodeError as e:
            print(f"❌ JSON error attempt={attempt}: {e}")
            continue
        except Exception as e:
            print(f"❌ GPT error attempt={attempt}: {e}")
            break
    return FALLBACK


@app.post("/assessment/chat")
async def assessment_chat(req: Request):
    data      = await req.json()
    form      = data.get("test", "PHQ-9")
    user_text = data.get("userText", "")

    skey = session_key(req, form)
    sess = load_chat_session(skey)

    # init ถ้าไม่มี session หรือ form ใหม่
    if not sess or sess["form"] != form:
        item_scores  = {}
        full_history = []
        greeted      = False
    else:
        item_scores  = sess["item_scores"]
        full_history = sess["full_history"]
        greeted      = sess["greeted"]

    all_questions = FORMS.get(form, [])
    total_items   = len(all_questions)

    # Opening greeting
    if not user_text.strip():
        if not greeted:
            greeting = "สวัสดีครับ 😊 ดีใจที่คุณมาคุยด้วยนะครับ วันนี้เป็นยังไงบ้างครับ?"
            greeted  = True
            full_history.append({"role": "assistant", "content": greeting})
            save_chat_session(skey, form, item_scores, full_history, greeted, None)
            return {"done": False, "reply": greeting}
        # ถ้า greet ไปแล้ว — อาจเป็น user กลับมาใหม่ ส่ง last reply กลับ
        if full_history:
            last_ai = next((m["content"] for m in reversed(full_history) if m["role"] == "assistant"), "")
            return {"done": False, "reply": last_ai, "resumed": True}
        return {"done": False, "reply": ""}

    full_history.append({"role": "user", "content": user_text})
    recent_history = full_history  # ส่งทั้งหมด GPT จำ context ได้ครบ

    # Crisis detection
    crisis_level = detect_crisis(user_text)
    crisis_extra = ""
    if crisis_level == "high":
        crisis_extra = (
            "\n⚠️ user พูดถึงเรื่องน่าเป็นห่วงมาก: รับฟังก่อน อย่าตกใจ"
            "\nถามเบาๆ ว่าตอนนี้ปลอดภัยไหม แล้วเสนอสายด่วน 1323 เป็นทางเลือก (ไม่บังคับ)"
        )
    elif crisis_level == "medium":
        crisis_extra = (
            "\n⚠️ user อาจพูดถึงการเจ็บตัวเอง: รับฟังก่อน ถามเพิ่มเบาๆ"
            "\nถ้า context หนักจริง ค่อยเสนอสายด่วน 1323 แบบไม่กดดัน"
        )

    messages = [
        {"role": "system", "content": build_system_prompt(form, all_questions, item_scores, crisis_extra, full_history)}
    ] + recent_history

    result = call_gpt(messages, max_tokens=300)

    if not result["ok"]:
        fallback = (
            "หนักมากเลยนะครับ ถ้าอยากคุยกับคนที่รับสายได้ตลอด มีสายด่วน 1323 โทรฟรี 24 ชม."
            if crisis_level == "high" else
            "ขอโทษนะครับ เกิดข้อผิดพลาดเล็กน้อย ช่วยลองส่งใหม่ได้ไหมครับ"
        )
        full_history.append({"role": "assistant", "content": fallback})
        req.session["full_history"] = full_history
        return {"done": False, "reply": fallback}

    reply = result["reply"]
    done  = result["done"]

    # merge scores
    for k, v in result["scores"].items():
        item_scores[str(k)] = int(v)

    # จบได้เมื่อ score ครบทุกข้อเท่านั้น
    if len(item_scores) >= total_items:
        total_score = sum(item_scores.values())
        level = "LOW"
        for mn, mx, lvl in LEVEL_RULES.get(form, []):
            if mn <= total_score <= mx:
                level = lvl
                break
        # generate closing message เสมอ — ทิ้ง reply เดิมที่อาจยังถามค้างอยู่
        closing_msgs = [
            {"role": "system", "content": (
                "บทสนทนาจบแล้ว ให้ปิดอย่างอบอุ่น 1-2 ประโยค "
                "ขอบคุณที่เล่าให้ฟัง บอกว่าจะไปดูสรุปได้เลย "
                "ห้ามถามคำถาม ห้ามพูดถึงคะแนนหรือผล "
                'Output JSON: {"reply":"...","scores":{},"done":true}'
            )},
        ] + full_history[-6:]
        closing = call_gpt(closing_msgs, max_tokens=150)
        if closing["ok"] and closing["reply"]:
            reply = closing["reply"]
        full_history.append({"role": "assistant", "content": reply})

        result = {"form": form, "score": total_score, "level": level, "item_scores": item_scores}
        save_chat_session(skey, form, item_scores, full_history, greeted, result)
        # เก็บ result ไว้ใน cookie session ด้วย สำหรับ /assessment/result
        req.session["result"] = result
        username = req.session.get("user", "anonymous")
        print(f"💾 saving assessment: user={username} form={form} score={total_score} level={level}")
        try:
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO assessments (username,form,score,level,item_scores) VALUES (?,?,?,?,?)",
                    (username, form, total_score, level,
                     json.dumps(item_scores, ensure_ascii=False))
                )
                assessment_id = cur.lastrowid
                for msg in full_history:
                    conn.execute(
                        "INSERT INTO chat_logs (assessment_id,role,content) VALUES (?,?,?)",
                        (assessment_id, msg["role"], msg["content"])
                    )
                conn.commit()
        except Exception as e:
            print(f"❌ DB error: {e}")
        return {"done": True, "redirect": "/post-analysis", "reply": reply}

    # normal flow — append reply แล้วคุยต่อ
    full_history.append({"role": "assistant", "content": reply})
    save_chat_session(skey, form, item_scores, full_history, greeted, None)
    return {"done": False, "reply": reply}


# ======================================================
# RESULT
# ======================================================
@app.get("/assessment/result")
async def assessment_result(req: Request):
    result = req.session.get("result")
    if not result:
        return JSONResponse({"error": "no result", "form": "Unknown", "score": 0, "level": "LOW"})

    form        = result["form"]
    score       = result["score"]
    item_scores = result.get("item_scores", {})
    level       = "LOW"
    for mn, mx, lvl in LEVEL_RULES.get(form, []):
        if mn <= score <= mx:
            level = lvl
            break

    return {
        "form": form, "score": score, "level": level,
        "item_scores": item_scores,
        "form_questions": FORMS.get(form, [])
    }


# ======================================================
# DASHBOARD APIs
# ======================================================
@app.get("/api/dashboard/summary")
async def dashboard_summary(request: Request):
    with get_db() as conn:
        total_users       = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_assessments = conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]
        recent = conn.execute("""
            SELECT a.id, a.username, a.form, a.score, a.level, a.taken_at
            FROM assessments a ORDER BY a.taken_at DESC LIMIT 10
        """).fetchall()
        level_counts = conn.execute(
            "SELECT level, COUNT(*) as cnt FROM assessments GROUP BY level"
        ).fetchall()
        users_list = conn.execute("""
            SELECT u.username, u.email, u.created_at,
                   COUNT(a.id) as assessment_count,
                   MAX(a.taken_at) as last_assessment
            FROM users u LEFT JOIN assessments a ON u.username=a.username
            GROUP BY u.username ORDER BY u.created_at DESC
        """).fetchall()
    return {
        "total_users": total_users,
        "total_assessments": total_assessments,
        "recent_assessments": [dict(r) for r in recent],
        "level_counts": [dict(r) for r in level_counts],
        "users": [dict(r) for r in users_list],
    }

@app.get("/api/dashboard/user/{username}")
async def dashboard_user(username: str, request: Request):
    with get_db() as conn:
        assessments = conn.execute("""
            SELECT id, form, score, level, item_scores, taken_at
            FROM assessments WHERE username=? ORDER BY taken_at DESC
        """, (username,)).fetchall()
        user_info = conn.execute(
            "SELECT username,email,parent_name,parent_phone,parent_relation,created_at FROM users WHERE username=?",
            (username,)
        ).fetchone()
    return {
        "username": username,
        "user_info": dict(user_info) if user_info else {},
        "assessments": [dict(r) for r in assessments]
    }

@app.get("/api/dashboard/chat/{assessment_id}")
async def dashboard_chat(assessment_id: int, request: Request):
    with get_db() as conn:
        logs = conn.execute("""
            SELECT role, content, sent_at FROM chat_logs
            WHERE assessment_id=? ORDER BY sent_at ASC
        """, (assessment_id,)).fetchall()
        info = conn.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    return {"info": dict(info) if info else {}, "messages": [dict(r) for r in logs]}

@app.get("/api/dashboard/login-logs")
async def login_logs(request: Request):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM login_logs ORDER BY logged_at DESC LIMIT 200"
        ).fetchall()
    return [dict(r) for r in rows]
