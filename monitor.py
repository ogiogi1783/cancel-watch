"""
DRESS PRODUCTION 予約ページのキャンセル枠ウォッチャー
- 予約ページを一定間隔で開き、カレンダーに ◎○□△ の日が新しく出たら LINE に通知する
- 予約自体は行わない（通知を見て自分で予約する）
"""
import datetime
import json
import os
import pathlib
import re
import time
import urllib.request

from playwright.sync_api import sync_playwright

URL = os.environ.get("TARGET_URL", "https://edisone.jp/dressproduction")
INTERVAL = int(os.environ.get("INTERVAL_SEC", "60"))          # チェック間隔（秒）
DURATION = int(os.environ.get("DURATION_MIN", "340")) * 60    # 1回のジョブで回す時間
MONTHS_AHEAD = int(os.environ.get("MONTHS_AHEAD", "1"))       # 今月に加えて何か月先まで見るか
DEBUG = os.environ.get("DEBUG") == "1"
TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
USER_ID = os.environ["LINE_USER_ID"]

JST = datetime.timezone(datetime.timedelta(hours=9))
DEBUG_DIR = pathlib.Path("debug")

# カレンダーのマークを拾う JS。
# テキストが1文字のマークの要素、または alt/title がマークの画像を探し、
# その日付セル（td など）の文字列をラベルにする。
EXTRACT_JS = r"""
() => {
  const OK = "◎○□△", NG = "×✕✖";
  const out = { available: [], total: 0 };
  const heading = (document.body.innerText.match(/20\d\d\s*[\/年]\s*\d{1,2}\s*月?/) || [""])[0];
  const cellOf = el => el.closest("td, li, [class*=day], [class*=date], [class*=cal]") || el.parentElement;
  const label = el => {
    const c = cellOf(el);
    return (heading + " " + (c ? c.innerText : "")).replace(/\s+/g, " ").trim();
  };
  const all = document.querySelectorAll("body *");
  for (const el of all) {
    let mark = "";
    if (el.tagName === "IMG") {
      mark = ((el.getAttribute("alt") || "") + (el.getAttribute("title") || "")).trim();
    } else if (el.children.length === 0) {
      mark = (el.textContent || "").trim();
    }
    if (mark.length !== 1) continue;
    if (OK.includes(mark)) { out.total++; out.available.push(label(el)); }
    else if (NG.includes(mark)) { out.total++; }
  }
  return out;
}
"""

NEXT_MONTH_RE = r"^\s*(次月|翌月|次の月|次の月へ|＞|>|›|»|▶|→)\s*$"


def now():
    return datetime.datetime.now(JST).strftime("%m/%d %H:%M:%S")


def line_push(text):
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=json.dumps({"to": USER_ID, "messages": [{"type": "text", "text": text[:4900]}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # 通知失敗で監視は止めない
        print(f"[{now()}] LINE送信失敗: {e}")


def check(page, n):
    page.goto(URL, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(2000)
    results = [page.evaluate(EXTRACT_JS)]
    if DEBUG:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=DEBUG_DIR / f"{n:03d}_month0.png", full_page=True)
        (DEBUG_DIR / f"{n:03d}_month0.html").write_text(page.content(), encoding="utf-8")
    for m in range(1, MONTHS_AHEAD + 1):
        btn = page.get_by_role("link", name=re.compile(NEXT_MONTH_RE)).or_(
            page.get_by_role("button", name=re.compile(NEXT_MONTH_RE)))
        if btn.count() == 0:
            break
        btn.first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        results.append(page.evaluate(EXTRACT_JS))
        if DEBUG:
            page.screenshot(path=DEBUG_DIR / f"{n:03d}_month{m}.png", full_page=True)
    total = sum(r["total"] for r in results)
    available = sorted({a for r in results for a in r["available"]})
    return total, available


def main():
    start = time.time()
    prev = set()
    misses = 0
    warned = False
    if DEBUG:
        line_push("【テスト】キャンセル監視のテスト実行を開始しました")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ja-JP", timezone_id="Asia/Tokyo")
        n = 0
        while time.time() - start < DURATION:
            n += 1
            try:
                total, available = check(page, n)
            except Exception as e:
                print(f"[{now()}] 取得エラー: {e}")
                total, available = 0, []

            if total == 0:
                # カレンダー自体が読めていない（構造変更・読み込み失敗）
                misses += 1
                print(f"[{now()}] カレンダーを検出できず ({misses}回連続)")
                if misses >= 10 and not warned:
                    line_push("⚠️ キャンセル監視：カレンダーを読み取れていません。サイトの仕様が変わった可能性があります。")
                    warned = True
            else:
                misses, warned = 0, False
                cur = set(available)
                new = cur - prev
                print(f"[{now()}] マーク{total}個 / 空き{len(cur)}件 / 新規{len(new)}件")
                if new:
                    lines = "\n".join(f"・{x}" for x in sorted(new)[:15])
                    line_push(f"🔔 空き枠が出ました！\n{lines}\n\n今すぐ予約👇\n{URL}")
                prev = cur

            time.sleep(INTERVAL)
        browser.close()


if __name__ == "__main__":
    main()
