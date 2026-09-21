"""Teams -> OneDrive(digest.txt) -> AI 정리 -> GitHub 이슈

버튼(Run workflow)을 누를 때마다 digest.txt를 받아,
지난 정리 이후 새로 올라온 메시지만 GitHub Models로 요약한다.
하루 50회 무료 한도를 1회 = 2%로 계산해 사용량 이슈에 표시한다.
"""

import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

MODEL = "openai/gpt-4.1"      # GitHub Models 무료 모델
KST = timezone(timedelta(hours=9))
FALLBACK_HOURS = 6            # 이전 이슈가 없을 때만 쓰는 기본 범위
CHUNK_CHARS = 7000            # 무료 한도(요청당 입력 8천 토큰)에 맞춘 조각 크기
MIN_CHUNK_CHARS = 1500
MARKER = re.compile(r"<!-- last-message: (\S+) -->")
FIELD = re.compile(r"(분류|보낸 사람|보낸 시각|본문|첨부):\s*〔(.*?)〕(?=\s*(?:분류|보낸 사람|보낸 시각|본문|첨부):|\s*$)", re.S)
GH_API = "https://api.github.com"
DAILY_LIMIT = 50              # GitHub Models 무료 상위 모델 하루 호출 수
PERCENT_PER_CALL = 100 // DAILY_LIMIT   # 1회 = 2%
USAGE = re.compile(r"<!-- usage: (\d{4}-\d{2}-\d{2}) (\d+) -->")
calls_this_run = 0


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"환경변수 {name} 가 비어 있습니다. GitHub Secrets를 확인하세요.")
    return value


def gh_headers(token):
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


# ---------- 1. 파일 받기 ----------

def download(url):
    if "download=1" not in url:
        url += ("&" if "?" in url else "?") + "download=1"
    r = requests.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()
    if "text/html" in r.headers.get("content-type", "") and "=====" not in r.text:
        sys.exit("파일 대신 로그인 페이지가 내려왔습니다. 공유 링크가 '링크가 있는 모든 사용자'인지 확인하세요.")
    r.encoding = "utf-8"
    return r.text


# ---------- 2. 정리 ----------

def clean_html(raw):
    text = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  lambda m: m.group(2) if m.group(1) in m.group(2) else f"{m.group(2)} ({m.group(1)})",
                  raw, flags=re.S)
    text = re.sub(r"<attachment[^>]*>\s*</attachment>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def parse_body(raw):
    try:
        return clean_html(json.loads(raw).get("content", ""))
    except (json.JSONDecodeError, AttributeError):
        return clean_html(raw)


def parse_attachments(raw):
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    names = []
    for a in items:
        kind = a.get("contentType", "")
        if kind == "reference" and a.get("name"):
            names.append(a["name"])
        elif "announcementBanner" in kind:
            try:
                title = json.loads(a.get("content") or "{}").get("title")
                if title:
                    names.insert(0, f"[공지 배너] {title}")
            except json.JSONDecodeError:
                pass
        # 링크 미리보기 카드는 본문에 링크가 이미 있으니 버린다
    return names


def parse_digest(text):
    messages = []
    for block in text.split("====="):
        fields = dict(FIELD.findall(block))
        if "보낸 시각" not in fields:
            continue
        try:
            sent = datetime.fromisoformat(fields["보낸 시각"].replace("Z", "+00:00"))
        except ValueError:
            continue
        team, _, channel = fields.get("분류", "?").partition("〕 / 〔")
        messages.append({
            "team": team.strip(),
            "channel": channel.strip() or "General",
            "sender": fields.get("보낸 사람", "?"),
            "sent": sent.astimezone(KST),
            "body": parse_body(fields.get("본문", "")),
            "files": parse_attachments(fields.get("첨부", "[]")),
        })
    return messages


def build_sections(messages):
    """채널별 텍스트 덩어리 목록."""
    groups = defaultdict(list)
    for m in sorted(messages, key=lambda x: x["sent"]):
        groups[(m["team"], m["channel"])].append(m)

    sections = []
    for (team, channel), items in groups.items():
        lines = [f"## {team} / {channel}"]
        for m in items:
            lines.append(f"- {m['sent']:%m-%d %H:%M} {m['sender']}: {m['body'] or '(내용 없음)'}")
            if m["files"]:
                lines.append(f"  첨부: {', '.join(m['files'])}")
        sections.append("\n".join(lines))
    return sections


def chunk(sections, limit):
    """채널 단위로 묶되, 한 채널이 limit보다 길면 줄 단위로 자른다."""
    chunks, current = [], ""
    for sec in sections:
        pieces = [sec] if len(sec) <= limit else split_long(sec, limit)
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > limit:
                chunks.append(current)
                current = ""
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def split_long(section, limit):
    header, *lines = section.split("\n")
    parts, buf = [], header
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            parts.append(buf)
            buf = f"{header} (이어서)"
        buf += "\n" + line[: limit - len(header) - 20]
    parts.append(buf)
    return parts


# ---------- 3. 요약 (GitHub Models, 키 불필요) ----------

FORMAT = """형식:
## 꼭 챙길 것
- 마감, 제출, 준비물, 시험처럼 행동이 필요한 것만. `- [ ] (과목) 내용 — 기한` 형태. 없으면 "없음".

## 과목별 요약
### 과목명
- 핵심 1~3줄. 올라온 자료 파일명이 있으면 함께.

## 공지
- 학년·학교 공지만 따로. 없으면 생략.

규칙:
- 원문에 없는 기한이나 내용을 지어내지 마라. 기한이 모호하면 "기한 불명확"이라고 적어라.
- 과목명은 팀 이름을 알아보기 쉽게 줄여라 (예: "심화수학(장)_26_1_1" → "심화수학(장)").
- 인사, 이모지 반응, 잡담은 버려라.
- 링크는 꼭 필요한 것만 남겨라."""


class TooLong(Exception):
    pass


def ask(token, prompt):
    # 무료 한도는 분당 10회라, 걸리면 잠깐 쉬었다가 다시 보낸다
    for attempt in range(6):
        r = requests.post(
            "https://models.github.ai/inference/chat/completions",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 4000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=180,
        )
        if r.status_code != 429:
            global calls_this_run
            calls_this_run += 1
            break
        wait = int(r.headers.get("retry-after", "0") or 0)
        if wait > 300:
            break  # 몇 분 넘게 기다리라면 하루 한도에 걸린 것
        time.sleep(max(wait, 65))
    if r.status_code == 403:
        sys.exit("GitHub Models 사용 권한이 없습니다. 워크플로에 'models: read' 권한이 있는지 확인하세요.")
    if r.status_code == 413 or (r.status_code == 400 and "token" in r.text.lower()):
        raise TooLong()
    if r.status_code == 429:
        sys.exit("GitHub Models 하루 호출 한도(50회)에 걸렸습니다. 이번 분량은 다음 실행 때 자동으로 이어서 처리됩니다.")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def summarize_chunk(token, text, hours_label):
    prompt = (f"아래는 고등학생이 속한 Microsoft Teams 수업 채널들에서 {hours_label} 올라온 메시지다.\n"
              f"학생 본인이 빠르게 훑어볼 수 있는 한국어 정리를 만들어라.\n\n{FORMAT}\n\n원문:\n{text}")
    return ask(token, prompt)


def summarize(token, sections, hours_label, remaining):
    limit = CHUNK_CHARS
    while True:
        chunks = chunk(sections, limit)
        needed = len(chunks) + (1 if len(chunks) > 1 else 0) + calls_this_run
        if needed > remaining:
            sys.exit(f"오늘 남은 호출은 {remaining}회인데 이번 요약에 {needed}회가 필요합니다. "
                     f"내일 다시 눌러주세요. 처리 못 한 메시지는 다음 실행 때 이어서 정리됩니다.")
        try:
            parts = [summarize_chunk(token, c, hours_label) for c in chunks]
            break
        except TooLong:
            if limit <= MIN_CHUNK_CHARS:
                sys.exit("메시지 하나가 너무 길어 요약할 수 없습니다.")
            limit //= 2
            print(f"입력이 길어 조각 크기를 {limit}자로 줄여 다시 시도합니다.")

    if len(parts) == 1:
        return parts[0], 1

    merged = "\n\n---\n\n".join(parts)
    prompt = (f"아래는 같은 기간의 Teams 메시지를 여러 조각으로 나눠 각각 정리한 결과다.\n"
              f"중복을 합치고 과목별로 모아 하나의 정리로 다시 써라.\n\n{FORMAT}\n\n조각별 정리:\n{merged}")
    try:
        return ask(token, prompt), len(parts)
    except TooLong:
        # 합치기도 너무 길면 조각 결과를 그대로 이어 붙인다
        return merged, len(parts)


# ---------- 4. 이슈 ----------

def last_processed(token, repo):
    r = requests.get(f"{GH_API}/repos/{repo}/issues",
                     headers=gh_headers(token),
                     params={"labels": "teams-digest", "state": "all",
                             "sort": "created", "direction": "desc", "per_page": 1},
                     timeout=60)
    r.raise_for_status()
    for issue in r.json():
        m = MARKER.search(issue.get("body") or "")
        if m:
            return datetime.fromisoformat(m.group(1))
    return None


def create_issue(token, repo, title, body):
    r = requests.post(f"{GH_API}/repos/{repo}/issues",
                      headers=gh_headers(token),
                      json={"title": title, "body": body, "labels": ["teams-digest"]},
                      timeout=60)
    r.raise_for_status()
    return r.json()["html_url"]



def bar(percent):
    filled = min(20, round(percent / 5))
    return "█" * filled + "░" * (20 - filled)


def find_dashboard(token, repo):
    r = requests.get(f"{GH_API}/repos/{repo}/issues",
                     headers=gh_headers(token),
                     params={"labels": "ai-usage", "state": "open", "per_page": 1},
                     timeout=60)
    r.raise_for_status()
    items = r.json()
    return items[0] if items else None


def usage_today(dashboard, today):
    if dashboard:
        m = USAGE.search(dashboard.get("body") or "")
        if m and m.group(1) == today:
            return int(m.group(2))
    return 0


def workflow_url(repo):
    ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    name = ref.split("/.github/workflows/")[-1].split("@")[0] if ref else "teams-digest.yml"
    return f"https://github.com/{repo}/actions/workflows/{name}"


def update_dashboard(token, repo, dashboard, today, used, status):
    percent = used * PERCENT_PER_CALL
    left = max(0, DAILY_LIMIT - used)
    body = (f"## 🤖 오늘 AI 사용량 ({today})\n\n"
            f"`{bar(percent)}` **{percent}%** · {used}회 / {DAILY_LIMIT}회\n\n"
            f"남은 호출: **{left}회** ({left * PERCENT_PER_CALL}%)\n\n"
            f"### [▶ 지금 요약하기]({workflow_url(repo)})\n"
            f"링크를 누른 뒤 오른쪽의 `Run workflow` → 초록 `Run workflow` 버튼\n\n"
            f"---\n마지막 실행: {status}\n\n"
            f"<sub>요약 1회 = {PERCENT_PER_CALL}%. 한국 시간 자정에 0%로 돌아갑니다. "
            f"실제 한도는 GitHub 기준이라 약간 다를 수 있습니다.</sub>\n\n"
            f"<!-- usage: {today} {used} -->")
    if dashboard:
        requests.patch(f"{GH_API}/repos/{repo}/issues/{dashboard['number']}",
                       headers=gh_headers(token), json={"body": body}, timeout=60).raise_for_status()
    else:
        requests.post(f"{GH_API}/repos/{repo}/issues",
                      headers=gh_headers(token),
                      json={"title": "📊 AI 사용량 · 요약 버튼", "body": body, "labels": ["ai-usage"]},
                      timeout=60).raise_for_status()


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")

def main():
    token, repo = env("GH_TOKEN"), env("GH_REPO")
    now = datetime.now(KST)
    today = f"{now:%Y-%m-%d}"

    dashboard = find_dashboard(token, repo)
    used_before = usage_today(dashboard, today)
    status = "실행 중 오류"

    try:
        remaining = DAILY_LIMIT - used_before
        if remaining <= 0:
            status = "오늘 한도를 모두 써서 요약하지 않음"
            sys.exit("오늘 AI 호출 한도(50회)를 모두 썼습니다. 내일 다시 눌러주세요.")

        since = last_processed(token, repo)
        if since is None:
            since = now - timedelta(hours=FALLBACK_HOURS)
            print(f"이전 정리가 없어 최근 {FALLBACK_HOURS}시간을 대상으로 합니다.")
        else:
            print(f"지난 정리 이후({since:%m-%d %H:%M}) 메시지를 찾습니다.")

        messages = parse_digest(download(env("DIGEST_URL")))
        # 사진만 올린 풀이 인증처럼 글도 파일명도 없는 메시지는 버린다
        new = [m for m in messages if m["sent"] > since and (m["body"] or m["files"])]
        print(f"전체 {len(messages)}건 중 새 메시지 {len(new)}건")
        if not new:
            status = f"{now:%m-%d %H:%M} · 새 메시지 없음 · 0회 사용"
            print("새 메시지가 없어 이슈를 만들지 않습니다.")
            return

        sections = build_sections(new)
        label = f"{since:%m-%d %H:%M}부터 {now:%m-%d %H:%M}까지"
        summary, n_chunks = summarize(token, sections, label, remaining)

        used = used_before + calls_this_run
        latest = max(m["sent"] for m in new)
        source = "\n\n".join(sections)
        note = f" · {n_chunks}개 조각으로 나눠 요약" if n_chunks > 1 else ""
        body = (f"{summary}\n\n---\n{label} · 새 메시지 {len(new)}건{note}\n"
                f"AI 사용 {calls_this_run}회 ({calls_this_run * PERCENT_PER_CALL}%) · "
                f"오늘 누적 {used * PERCENT_PER_CALL}%\n\n"
                f"<details><summary>원문 보기</summary>\n\n{source}\n\n</details>\n\n"
                f"<!-- last-message: {latest.isoformat()} -->")
        url = create_issue(token, repo, f"[Teams] {now:%m-%d %H:%M} 정리", body)
        status = (f"{now:%m-%d %H:%M} · 새 메시지 {len(new)}건 · "
                  f"{calls_this_run}회 사용 · [결과 보기]({url})")
        print(f"이슈를 만들었습니다: {url}")
    finally:
        used = used_before + calls_this_run
        percent = used * PERCENT_PER_CALL
        write_summary(f"## 🤖 AI 사용량\n`{bar(percent)}` **{percent}%** "
                      f"({used}/{DAILY_LIMIT}회) · 이번 실행 {calls_this_run}회\n\n{status}")
        print(f"오늘 AI 사용량: {used}/{DAILY_LIMIT}회 ({percent}%)")
        try:
            update_dashboard(token, repo, dashboard, today, used, status)
        except requests.RequestException as e:
            print(f"사용량 이슈 갱신 실패: {e}")


if __name__ == "__main__":
    main()
