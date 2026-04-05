from fastapi import FastAPI, Request
import uvicorn
import httpx
import json

app = FastAPI()


async def search_disk(token: str, query: str) -> list[dict]:
    url = "https://cloud-api.yandex.net/v1/disk/resources/files"
    headers = {"Authorization": f"OAuth {token}"}
    params = {
        "limit": 5,
        "fields": "items.name,items.path,items.type,items.created,items.public_url",
        "media_type": "document,spreadsheet,presentation"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        return []
    items = resp.json().get("items", [])
    query_lower = query.lower()
    return [item for item in items if query_lower in item.get("name", "").lower()]


async def list_recent_files(token: str) -> list[dict]:
    url = "https://cloud-api.yandex.net/v1/disk/resources/last-uploaded"
    headers = {"Authorization": f"OAuth {token}"}
    params = {
        "limit": 5,
        "fields": "items.name,items.path,items.created",
        "media_type": "document,spreadsheet,presentation"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        return []
    return resp.json().get("items", [])


async def get_public_link(token: str, path: str) -> str | None:
    url = "https://cloud-api.yandex.net/v1/disk/resources/publish"
    headers = {"Authorization": f"OAuth {token}"}
    params = {"path": path}
    async with httpx.AsyncClient() as client:
        await client.put(url, headers=headers, params=params)
        resp2 = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources",
            headers=headers,
            params={"path": path, "fields": "public_url"}
        )
    return resp2.json().get("public_url")


def format_file_list(files: list[dict]) -> str:
    if not files:
        return "Файлы не найдены."
    lines = []
    for i, f in enumerate(files[:5], 1):
        name = f.get("name", "Без названия")
        clean_name = name.rsplit(".", 1)[0] if "." in name else name
        lines.append(f"{i}. {clean_name}")
    return "Нашла следующие файлы: " + ", ".join(lines)


def extract_search_query(text: str) -> str:
    stop_words = ["найди","найти","поищи","поиск","найдите","файл","файлы",
                  "документ","документы","на диске","в диске","диске","диск",
                  "покажи","покажите","открой"]
    query = text.lower()
    for word in stop_words:
        query = query.replace(word, "")
    return query.strip()


sessions: dict[str, list[dict]] = {}


@app.get("/")
async def root():
    return {"status": "ok", "message": "Alice skill server is running"}


@app.post("/webhook")
async def alice_webhook(request: Request):
    body = await request.json()

    print("=== REQUEST ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))

    user_text: str = body.get("request", {}).get("original_utterance", "").lower().strip()
    is_new_session: bool = body.get("session", {}).get("new", False)
    session_id: str = body.get("session", {}).get("session_id", "")

    user_token: str | None = (
        body.get("session", {}).get("user", {}).get("access_token")
    )

    print(f"TOKEN: {'ДА' if user_token else 'НЕТ'}")

    # ── Нет токена — запускаем OAuth через директиву ──────────────────────────
    if not user_token:
        if is_new_session:
            # При старте новой сессии — сразу запускаем привязку аккаунта
            return {
                "response": {
                    "text": "Привет! Для работы с диском нужно войти в аккаунт Яндекса.",
                    "end_session": False
                },
                "start_account_linking": {},  # ← вот эта директива запускает OAuth
                "version": "1.0"
            }
        else:
            # В середине разговора — тоже запускаем
            return {
                "response": {
                    "text": "Нужно войти в аккаунт Яндекса.",
                    "end_session": False
                },
                "start_account_linking": {},  # ← ключевая директива
                "version": "1.0"
            }

    # ── Есть токен — работаем ─────────────────────────────────────────────────
    if is_new_session:
        return _reply("Привет! Скажи: найди файл, или: покажи последние файлы.")

    if any(w in user_text for w in ["последн", "недавн", "новые файлы", "что есть"]):
        files = await list_recent_files(user_token)
        sessions[session_id] = files
        if not files:
            return _reply("На диске не нашла ни одного документа.")
        return _reply(format_file_list(files) + ". Назови номер файла чтобы получить ссылку.")

    if any(w in user_text for w in ["найди", "поищи", "найти", "поиск", "ищи"]):
        query = extract_search_query(user_text)
        if not query:
            return _reply("Скажи как называется файл. Например: найди файл бюджет.")
        files = await search_disk(user_token, query)
        sessions[session_id] = files
        if not files:
            return _reply(f"Не нашла файлов по запросу «{query}».")
        return _reply(format_file_list(files) + ". Назови номер чтобы получить ссылку.")

    number_words = {
        "первый": 1, "первую": 1, "первое": 1, "один": 1, "1": 1,
        "второй": 2, "вторую": 2, "второе": 2, "два": 2, "2": 2,
        "третий": 3, "третью": 3, "третье": 3, "три": 3, "3": 3,
        "четвёртый": 4, "четвертый": 4, "четыре": 4, "4": 4,
        "пятый": 5, "пятую": 5, "пять": 5, "5": 5,
    }
    chosen_index = None
    for word, idx in number_words.items():
        if word in user_text:
            chosen_index = idx
            break

    if chosen_index and session_id in sessions:
        files = sessions[session_id]
        if chosen_index <= len(files):
            file = files[chosen_index - 1]
            path = file.get("path", "")
            name = file.get("name", "файл")
            clean_name = name.rsplit(".", 1)[0] if "." in name else name
            link = await get_public_link(user_token, path)
            if link:
                return _reply(f"Ссылка на файл «{clean_name}» готова: {link}")
            else:
                return _reply(f"Не смогла создать ссылку на «{clean_name}».")
        else:
            return _reply(f"У меня только {len(files)} файлов.")

    if any(w in user_text for w in ["помощь", "помоги", "что умеешь"]):
        return _reply("Скажи: найди файл отчёт. Или: покажи последние файлы.")

    if any(w in user_text for w in ["пока", "выход", "закрой", "стоп"]):
        return _reply("До встречи!", end=True)

    return _reply("Не поняла. Скажи: найди файл, покажи последние файлы, или: помощь.")


def _reply(text: str, end: bool = False) -> dict:
    return {
        "response": {"text": text, "end_session": end},
        "version": "1.0"
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)