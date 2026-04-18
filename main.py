from fastapi import FastAPI, Request
import uvicorn
import httpx
import json
import os
import asyncio
import uuid
from datetime import datetime, timedelta, date

app = FastAPI()

YANDEX_GPT_API_KEY = os.getenv("YANDEX_GPT_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# ── YandexGPT ────────────────────────────────────────────────────────────────

async def understand_intent(user_text: str) -> dict:
    prompt = f"""Ты — анализатор команд для голосового ассистента.
Пользователь сказал: "{user_text}"

Определи намерение и верни ТОЛЬКО JSON без пояснений:

Возможные intent:
- search_disk — поиск файла на диске (нужен параметр "query")
- list_disk — показать последние файлы на диске
- get_link — получить ссылку на файл по номеру (нужен параметр "number" от 1 до 5)
- read_mail — прочитать последние письма
- search_mail — найти письма от отправителя (нужен параметр "sender")
- calendar_today — показать встречи на сегодня
- calendar_tomorrow — показать встречи на завтра
- create_event — создать событие (нужны параметры "title", "time" в формате HH:MM)
- help — помощь
- exit — выход
- unknown — непонятная команда

Примеры:
"найди файл отчёт" -> {{"intent": "search_disk", "query": "отчёт"}}
"покажи документ про бюджет" -> {{"intent": "search_disk", "query": "бюджет"}}
"последние файлы" -> {{"intent": "list_disk"}}
"дай ссылку на первый" -> {{"intent": "get_link", "number": 1}}
"прочитай почту" -> {{"intent": "read_mail"}}
"письма от Иванова" -> {{"intent": "search_mail", "sender": "Иванов"}}
"что у меня сегодня" -> {{"intent": "calendar_today"}}
"встречи на завтра" -> {{"intent": "calendar_tomorrow"}}
"создай встречу совещание в 15:00" -> {{"intent": "create_event", "title": "совещание", "time": "15:00"}}
"пока" -> {{"intent": "exit"}}

Верни ТОЛЬКО JSON:"""

    headers = {
        "Authorization": f"Api-Key {YANDEX_GPT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite",
        "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": 100},
        "messages": [{"role": "user", "text": prompt}]
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                headers=headers, json=payload
            )
        if resp.status_code == 200:
            text = resp.json()["result"]["alternatives"][0]["message"]["text"]
            text = text.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(text)
    except Exception as e:
        print(f"GPT ERROR: {e}")

    return fallback_intent(user_text)


def fallback_intent(text: str) -> dict:
    t = text.lower()
    if any(w in t for w in ["найди", "поищи", "найти"]):
        query = t
        for w in ["найди", "поищи", "найти", "файл", "документ", "диск"]:
            query = query.replace(w, "")
        return {"intent": "search_disk", "query": query.strip()}
    if any(w in t for w in ["последн", "недавн", "все файлы"]):
        return {"intent": "list_disk"}
    if any(w in t for w in ["почт", "письм"]):
        return {"intent": "read_mail"}
    if any(w in t for w in ["сегодня", "план", "расписание"]):
        return {"intent": "calendar_today"}
    if "завтра" in t:
        return {"intent": "calendar_tomorrow"}
    if any(w in t for w in ["пока", "выход", "стоп"]):
        return {"intent": "exit"}
    if any(w in t for w in ["помощь", "помоги"]):
        return {"intent": "help"}
    numbers = {
        "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
        "первый": 1, "второй": 2, "третий": 3, "четвёртый": 4, "пятый": 5,
        "первую": 1, "вторую": 2, "третью": 3
    }
    for word, num in numbers.items():
        if word in t:
            return {"intent": "get_link", "number": num}
    return {"intent": "unknown"}


# ── Яндекс Диск ──────────────────────────────────────────────────────────────

async def list_recent_files(token: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources/last-uploaded",
            headers={"Authorization": f"OAuth {token}"},
            params={"limit": 7, "fields": "items.name,items.path,items.created"}
        )
    print(f"DISK STATUS: {resp.status_code}")
    if resp.status_code != 200:
        return []
    return resp.json().get("items", [])


async def search_disk(token: str, query: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources/files",
            headers={"Authorization": f"OAuth {token}"},
            params={"limit": 20, "fields": "items.name,items.path,items.created"}
        )
    if resp.status_code != 200:
        return []
    items = resp.json().get("items", [])
    return [i for i in items if query.lower() in i.get("name", "").lower()]


async def get_public_link(token: str, path: str) -> str | None:
    headers = {"Authorization": f"OAuth {token}"}
    async with httpx.AsyncClient() as client:
        await client.put(
            "https://cloud-api.yandex.net/v1/disk/resources/publish",
            headers=headers, params={"path": path}
        )
        resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources",
            headers=headers, params={"path": path, "fields": "public_url"}
        )
    return resp.json().get("public_url")


def format_files(files: list[dict]) -> str:
    if not files:
        return "Файлы не найдены."
    lines = [f"{i}. {f.get('name','?').rsplit('.',1)[0]}" for i, f in enumerate(files[:5], 1)]
    return "Нашла: " + ", ".join(lines)


# ── Яндекс Почта ─────────────────────────────────────────────────────────────

async def get_recent_emails(token: str) -> list[dict]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://mail.yandex.ru/api/v2/mailbox/folder/message/list",
                headers={"Authorization": f"OAuth {token}"},
                params={"fid": 1, "first": 0, "last": 5}
            )
        print(f"MAIL STATUS: {resp.status_code}, {resp.text[:200]}")
        if resp.status_code == 200:
            return resp.json().get("envelopes", [])
    except Exception as e:
        print(f"MAIL ERROR: {e}")
    return []


def format_emails(emails: list[dict]) -> str:
    if not emails:
        return "Писем не найдено."
    lines = []
    for i, m in enumerate(emails[:5], 1):
        sender = m.get("from", [{}])
        if isinstance(sender, list) and sender:
            sender_name = sender[0].get("displayName") or sender[0].get("local", "Неизвестно")
        else:
            sender_name = "Неизвестно"
        subject = m.get("subject", "Без темы")[:40]
        lines.append(f"{i}. От {sender_name}: {subject}")
    return "Последние письма: " + ". ".join(lines)


# ── Яндекс Логин ─────────────────────────────────────────────────────────────

async def get_yandex_login(token: str) -> str | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://login.yandex.ru/info?format=json",
                headers={"Authorization": f"OAuth {token}"}
            )
        if resp.status_code == 200:
            login = resp.json().get("login")
            print(f"LOGIN: {login}")
            return login
    except Exception as e:
        print(f"LOGIN ERROR: {e}")
    return None


# ── Яндекс Календарь (CalDAV) ────────────────────────────────────────────────

async def get_calendar_events(token: str, date_str: str) -> list[dict]:
    try:
        login = await get_yandex_login(token)
        if not login:
            return []

        def fetch():
            import caldav
            cal_url = f"https://caldav.yandex.ru/calendars/{login}/events/"
            print(f"CALDAV URL: {cal_url}")
            client = caldav.DAVClient(url=cal_url, username=login, password=token)
            cal = caldav.Calendar(client=client, url=cal_url)
            start = datetime.fromisoformat(date_str)
            end = start + timedelta(days=1)
            events = cal.date_search(start=start, end=end, expand=True)
            result = []
            for e in events[:5]:
                vevent = e.vobject_instance.vevent
                summary = str(getattr(vevent, "summary", "Без названия").value)
                dtstart = getattr(vevent, "dtstart", None)
                t = ""
                if dtstart and hasattr(dtstart.value, "strftime"):
                    t = dtstart.value.strftime("%H:%M")
                result.append({"name": summary, "time": t})
            return result

        result = await asyncio.get_event_loop().run_in_executor(None, fetch)
        print(f"CALENDAR EVENTS: {result}")
        return result

    except Exception as e:
        print(f"CALENDAR ERROR: {e}")
        return []


def format_events(events: list[dict], day_name: str) -> str:
    if not events:
        return f"{day_name} встреч нет. Свободен!"
    lines = []
    for e in events:
        name = e.get("name", "Без названия")
        t = e.get("time", "")
        lines.append(f"{t} — {name}" if t else name)
    return f"{day_name} у тебя: " + ", ".join(lines)


async def create_calendar_event(token: str, title: str, time_str: str, date_str: str) -> bool:
    try:
        login = await get_yandex_login(token)
        if not login:
            return False

        def create():
            import caldav
            cal_url = f"https://caldav.yandex.ru/calendars/{login}/events/"
            print(f"CALDAV CREATE URL: {cal_url}")
            client = caldav.DAVClient(url=cal_url, username=login, password=token)
            cal = caldav.Calendar(client=client, url=cal_url)

            hour, minute = 9, 0
            if ":" in time_str:
                parts = time_str.split(":")
                hour = int(parts[0])
                minute = int(parts[1]) if len(parts) > 1 else 0

            start_dt = datetime.fromisoformat(date_str).replace(hour=hour, minute=minute)
            end_dt = start_dt.replace(hour=min(hour + 1, 23))

            ical = (
                "BEGIN:VCALENDAR\r\n"
                "VERSION:2.0\r\n"
                "PRODID:-//Alice Assistant//RU\r\n"
                "BEGIN:VEVENT\r\n"
                f"UID:{uuid.uuid4()}@alice\r\n"
                f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}\r\n"
                f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}\r\n"
                f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}\r\n"
                f"SUMMARY:{title}\r\n"
                "END:VEVENT\r\n"
                "END:VCALENDAR\r\n"
            )
            cal.save_event(ical)
            return True

        result = await asyncio.get_event_loop().run_in_executor(None, create)
        print(f"CREATE EVENT RESULT: {result}")
        return result

    except Exception as e:
        print(f"CREATE EVENT ERROR: {e}")
        return False


# ── Сессии ────────────────────────────────────────────────────────────────────
sessions: dict[str, dict] = {}


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/webhook")
async def alice_webhook(request: Request):
    body = await request.json()
    print("=== REQUEST ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))

    user_text: str = body.get("request", {}).get("original_utterance", "").strip()
    is_new_session: bool = body.get("session", {}).get("new", False)
    session_id: str = body.get("session", {}).get("session_id", "")
    user_token: str | None = body.get("session", {}).get("user", {}).get("access_token")

    if not user_token:
        return {
            "response": {"text": "Для работы нужно войти в аккаунт Яндекса.", "end_session": False},
            "start_account_linking": {},
            "version": "1.0"
        }

    if is_new_session:
        return _reply(
            "Привет! Я твой ИИ-ассистент. Понимаю любые фразы. "
            "Попробуй: покажи файлы, прочитай почту, что у меня сегодня, "
            "или создай встречу совещание в 15:00."
        )

    intent_data = await understand_intent(user_text)
    intent = intent_data.get("intent", "unknown")
    print(f"INTENT: {intent_data}")

    if intent == "search_disk":
        query = intent_data.get("query", "").strip()
        if not query:
            return _reply("Скажи название файла который ищешь.")
        files = await search_disk(user_token, query)
        sessions[session_id] = {"files": files}
        if not files:
            return _reply(f"Файлов с названием «{query}» не нашла.")
        return _reply(format_files(files) + ". Назови номер чтобы получить ссылку.")

    elif intent == "list_disk":
        files = await list_recent_files(user_token)
        sessions[session_id] = {"files": files}
        if not files:
            return _reply("На диске файлов не нашла.")
        return _reply(format_files(files) + ". Назови номер чтобы получить ссылку.")

    elif intent == "get_link":
        number = intent_data.get("number", 1)
        files = sessions.get(session_id, {}).get("files", [])
        if not files:
            return _reply("Сначала скажи: покажи файлы.")
        if number > len(files):
            return _reply(f"У меня только {len(files)} файлов.")
        file = files[number - 1]
        name = file.get("name", "файл").rsplit(".", 1)[0]
        link = await get_public_link(user_token, file.get("path", ""))
        if link:
            return _reply(f"Ссылка на «{name}»: {link}")
        return _reply(f"Не смогла создать ссылку.")

    elif intent == "read_mail":
        emails = await get_recent_emails(user_token)
        if not emails:
            return _reply("Не удалось получить письма.")
        return _reply(format_emails(emails))

    elif intent == "search_mail":
        sender = intent_data.get("sender", "")
        emails = await get_recent_emails(user_token)
        filtered = [e for e in emails if sender.lower() in str(e.get("from", "")).lower()]
        if not filtered:
            return _reply(f"Писем от {sender} не нашла.")
        return _reply(format_emails(filtered))

    elif intent == "calendar_today":
        today = date.today().isoformat()
        events = await get_calendar_events(user_token, today)
        return _reply(format_events(events, "Сегодня"))

    elif intent == "calendar_tomorrow":
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        events = await get_calendar_events(user_token, tomorrow)
        return _reply(format_events(events, "Завтра"))

    elif intent == "create_event":
        title = intent_data.get("title", "Встреча")
        time_str = intent_data.get("time", "09:00")
        today = date.today().isoformat()
        success = await create_calendar_event(user_token, title, time_str, today)
        if success:
            return _reply(f"Создала событие «{title}» на {time_str}.")
        return _reply("Не смогла создать событие.")

    elif intent == "help":
        return _reply(
            "Я понимаю любые фразы. Попробуй: "
            "покажи файлы на диске, найди документ отчёт, "
            "прочитай почту, что у меня сегодня, "
            "создай встречу совещание в 15:00."
        )

    elif intent == "exit":
        return _reply("До встречи!", end=True)

    else:
        return _reply(
            f"Не поняла: «{user_text}». "
            "Попробуй: покажи файлы, прочитай почту, или что у меня сегодня."
        )


def _reply(text: str, end: bool = False) -> dict:
    return {"response": {"text": text, "end_session": end}, "version": "1.0"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
