import os
import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, AudioMessage, TextSendMessage
from google import genai
from groq import Groq
import edge_tts
import asyncio

# Google Calendar 相關套件
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

app = Flask(__name__)

# 填入你的真實金鑰
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Gemini
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# 初始化 Groq (Whisper STT)
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# Google Calendar 授權範圍
SCOPES = ['https://www.googleapis.com/auth/calendar']

import json

def get_calendar_service():
    """取得 Google Calendar API 服務授權（支援 Render 雲端）"""
    creds = None
    # 優先從 Render 的環境變數讀取 token
    token_json_str = os.environ.get('GOOGLE_TOKEN_JSON')
    
    if token_json_str:
        creds = Credentials.from_authorized_user_info(json.loads(token_json_str), SCOPES)
    elif os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # 如果是在本機，可以走本機授權流程
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
            
    return build('calendar', 'v3', credentials=creds)

def create_google_calendar_event(summary, start_time_str, end_time_str=None):
    """在 Google 日曆建立行程"""
    try:
        service = get_calendar_service()
        
        # 如果使用者沒給結束時間，預設為開始時間後 1 小時
        start_dt = datetime.datetime.fromisoformat(start_time_str)
        if end_time_str:
            end_dt = datetime.datetime.fromisoformat(end_time_str)
        else:
            end_dt = start_dt + datetime.timedelta(hours=1)

        event = {
            'summary': summary,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Taipei'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Taipei'},
        }

        created_event = service.events().insert(calendarId='primary', body=event).execute()
        return f"成功建立行程：{summary}（時間：{start_dt.strftime('%m/%d %H:%M')}）"
    except Exception as e:
        print(f"建立日曆失敗: {e}")
        return "建立日曆時發生錯誤。"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=AudioMessage)
def handle_audio_message(event):
    message_id = event.message.id
    print("收到語音訊息，開始下載...")
    
    # 1. 下載語音檔
    message_content = line_bot_api.get_message_content(message_id)
    audio_path = f"{message_id}.m4a"
    with open(audio_path, 'wb') as fd:
        for chunk in message_content.iter_content():
            fd.write(chunk)
            
    # 2. Whisper 轉文字
    print("正在透過 Whisper 轉文字...")
    with open(audio_path, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(audio_path, file.read()),
            model="whisper-large-v3-turbo",
            language="zh"
        )
    user_text = transcription.text
    print(f"使用者語音轉文字: {user_text}")

    # 3. 讓 Gemini 判斷是否要建立行程或一般回覆
    print("正在傳送給 Gemini AI 思考...")
    ai_reply_text = ""
    try:
        # 我們定義一個工具讓 Gemini 可以呼叫建立日曆
        prompt = f"""
當前時間是 2026-10-11 20:11。
使用者指令：{user_text}

請判斷使用者是否想要「新增行程 / 開會 / 約定時間」。
如果使用者是要預約行程，請嚴格按照以下 JSON 格式回傳（不要包覆在 markdown 語法中，直接回傳純 JSON）：
{{"action": "create_event", "summary": "行程名稱", "start_time": "2026-10-11T21:00:00"}}

如果使用者只是普通聊天或詢問，請直接回傳一般對話文字即可。
"""
        response = gemini_client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt
        )
        res_text = response.text.strip()
        print(f"Gemini 原始回應: {res_text}")

        # 簡單檢查是否為建立行程的 JSON 指令
        if "create_event" in res_text:
            import json
            # 清理可能的 markdown 標記
            clean_json = res_text.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_json)
            # 執行建立日曆
            ai_reply_text = create_google_calendar_event(
                summary=data.get("summary", "新行程"),
                start_time_str=data.get("start_time")
            )
        else:
            ai_reply_text = res_text

    except Exception as e:
        print(f"Gemini 發生錯誤: {e}")
        ai_reply_text = f"聽懂了：{user_text}（但處理行程時發生了一點狀況）"

    # 4. edge-tts 轉語音
    output_audio_path = f"{message_id}_reply.mp3"
    async def generate_tts():
        communicate = edge_tts.Communicate(ai_reply_text, "zh-TW-HsiaoChenNeural")
        await communicate.save(output_audio_path)
    
    try:
        asyncio.run(generate_tts())
    except Exception as e:
        print(f"edge-tts 發生錯誤: {e}")

    # 5. 回傳給 LINE
    line_bot_api.reply_message(
        event.reply_token,
        [
            TextSendMessage(text=ai_reply_text),
        ]
    )
    print("回傳完畢！")
    
    # 6. 清理暫存檔
    if os.path.exists(audio_path):
        os.remove(audio_path)
    if os.path.exists(output_audio_path):
        os.remove(output_audio_path)

if __name__ == "__main__":
    app.run(port=5000)