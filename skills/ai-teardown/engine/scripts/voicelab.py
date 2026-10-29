#!/usr/bin/env python3
"""聲音實驗室（voice-lab）配音接口：用自己在聲音實驗室複製的聲音配旁白，回讀也走聲音實驗室。

tts_mimo.py 在 narration.json 的 voice 寫 {"engine": "voicelab", ...} 時改用這裡；也可以單獨跑：
    python3 scripts/voicelab.py voices                     # 列出聲音實驗室裡的聲音（名稱 + referenceId）
    python3 scripts/voicelab.py say "試聽一句話" 試聽.wav [referenceId]   # 試聽

設定（不寫進任何專案檔案）：環境變數 VOICELAB_URL、STUDIO_KEY，或 ~/.config/ai-teardown/voicelab.env：
    VOICELAB_URL=https://你的聲音實驗室網址
    STUDIO_KEY=工作室通行金鑰
    VOICELAB_VOICE=預設 referenceId（可省，narration.json 的 voice.referenceId 優先）

voice 設定：
    {"engine": "voicelab", "referenceId": "…", "model": "standard" | "drama", "speed": 1.0, "temperature": 0.7, "top_p": 0.7}
    model 只有 "drama" 會用付費的 Drama 3，其餘一律免費的 S2.1。
每一拍可寫 "mood": "開心"（溫柔、興奮、生氣、嚴肅、脫口秀、廣播劇…），等於在聲音實驗室裡的 (開心)。
旁白裡的 (開心)、(停1秒) 這類指示照聲音實驗室的規則念；其他括號（例如年份註解）會照念，不會被吃掉。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ENV_FILE = Path.home() / ".config/ai-teardown/voicelab.env"

# 與聲音實驗室 src/lib/emotionParser.ts 的關鍵字一致：括號裡是這些字，聲音實驗室會當成語氣指示而不念出來
DIRECTIVE_WORDS = (
    "開心 高興 快樂 喜悅 大笑 笑 驚喜 興奮 激動 驚訝 驚呼 溫柔 輕聲 溫暖 柔和 安慰 "
    "生氣 憤怒 怒吼 咆哮 不滿 惱怒 嚴肅 正經 冷靜 沉重 緊張 嚴厲 認真 "
    "脫口秀 單口喜劇 諧星 吐槽 廣播劇 播音 旁白"
).split()
PAUSE_RE = re.compile(r"^(?:略停|停頓|停顿|頓|停)\s*(\d+(?:\.\d+)?)?\s*秒?$")
PAREN_RE = re.compile(r"[（(]([^）)]+)[）)]")


def is_directive(inner):
    inner = inner.strip()
    return bool(PAUSE_RE.match(inner)) or any(w in inner for w in DIRECTIVE_WORDS)


def split_directives(text):
    """回傳 (給字幕和對齊用的文字, 給聲音實驗室念的文字)。
    語氣指示從字幕文字拿掉、保留給聲音實驗室；其他括號在念的文字裡換成逗號，免得被聲音實驗室當指示吞掉。"""
    shown = PAREN_RE.sub(lambda m: "" if is_directive(m.group(1)) else m.group(0), text)
    said = PAREN_RE.sub(lambda m: m.group(0) if is_directive(m.group(1)) else f"，{m.group(1)}，", text)
    said = re.sub(r"，+([，。！？、；：])", r"\1", said).lstrip("，")
    return shown, said


def config():
    url, key, voice = os.environ.get("VOICELAB_URL"), os.environ.get("STUDIO_KEY"), os.environ.get("VOICELAB_VOICE")
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k == "VOICELAB_URL" and not url:
                url = v
            elif k == "STUDIO_KEY" and not key:
                key = v
            elif k == "VOICELAB_VOICE" and not voice:
                voice = v
    if not url or not key:
        sys.exit(f"缺聲音實驗室設定：VOICELAB_URL 和 STUDIO_KEY（環境變數或 {ENV_FILE}）")
    return url.rstrip("/"), key, voice


_GATE = threading.Lock()
_LAST = [0.0]
MIN_GAP = 0.5  # 兩次請求開始之間至少隔幾秒，別一次把 Fish 打爆


def _curl(args, tries=6):
    last = ""
    for k in range(tries):
        with _GATE:
            wait = _LAST[0] + MIN_GAP - time.time()
            if wait > 0:
                time.sleep(wait)
            _LAST[0] = time.time()
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            out = Path(tf.name)
        r = subprocess.run(["curl", "-sS", "--max-time", "240", "-o", str(out), "-w", "%{http_code}", *args],
                           capture_output=True, text=True)
        code = r.stdout.strip()
        body = out.read_bytes()
        out.unlink()
        if code == "200":
            return body
        last = f"HTTP {code or '?'} {(body[:300].decode('utf-8', 'replace') or r.stderr[:200])}"
        if code == "401":
            last += "（STUDIO_KEY 不對，或聲音實驗室沒設 STUDIO_KEY）"
        if code in ("400", "401", "403", "404"):  # 設定錯了，重試也沒用
            break
        wait = min(60, 5 * (k + 1)) if code == "429" else 2 * (k + 1)
        print(f"  {wait}s 後重試 {k + 1}：{last}", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"聲音實驗室呼叫失敗：{last}")


def tts(text, vcfg, mood=None):
    """合成一段，回傳 wav 位元組。"""
    url, key, default_voice = config()
    ref = vcfg.get("referenceId") or default_voice
    if not ref:
        sys.exit("沒指定聲音：narration.json 的 voice.referenceId，或 voicelab.env 的 VOICELAB_VOICE（跑 voicelab.py voices 查）")
    if mood:
        text = f"({mood}){text}"
    body = {"text": text, "referenceId": ref, "format": "wav",
            "engine": "drama" if vcfg.get("model") == "drama" else "standard",
            "speed": float(vcfg.get("speed", 1.0)),
            "temperature": float(vcfg.get("temperature", 0.7)),
            "topP": float(vcfg.get("top_p", 0.7))}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(body, tf, ensure_ascii=False)
        req = tf.name
    try:
        return _curl(["-X", "POST", f"{url}/api/studio/tts", "-H", f"x-studio-key: {key}",
                      "-H", "Content-Type: application/json", "--data-binary", f"@{req}"])
    finally:
        os.unlink(req)


def transcribe(wav):
    """回讀：把一拍的錄音交給聲音實驗室轉文字。"""
    url, key, _ = config()
    d = json.loads(_curl(["-X", "POST", f"{url}/api/studio/transcribe", "-H", f"x-studio-key: {key}",
                          "-F", f"audio=@{wav};type=audio/wav"]))
    return d.get("text", "")


def voices():
    url, key, _ = config()
    return json.loads(_curl([f"{url}/api/studio/voices", "-H", f"x-studio-key: {key}"]))["voices"]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "voices":
        for v in voices():
            print(f"{v['referenceId']}  {v['title']}")
    elif cmd == "say" and len(sys.argv) >= 4:
        vcfg = {"referenceId": sys.argv[4]} if len(sys.argv) > 4 else {}
        Path(sys.argv[3]).write_bytes(tts(split_directives(sys.argv[2])[1], vcfg))
        print(f"✓ {sys.argv[3]}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
