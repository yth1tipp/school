"""Teams -> OneDrive(digest.txt) -> AI 정리 -> GitHub 이슈

Power Automate가 매일 덮어쓰는 digest.txt를 공유 링크로 받아,
최근 24시간 메시지만 골라 정리한 뒤 Claude로 요약해 이슈를 만든다.
"""

import html
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

MODEL = "claude-sonnet-5"
KST = timezone(timedelta(hours=9))
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "24"))
FIELD = re.compile(r"(분류|보낸 사람|보낸 시각|본문|첨부):\s*〔(.*?)〕(?=\s*(?:분류|보낸 사람|보낸 시각|본문|첨부):|\s*$)", re.S)


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"환경변수 {name} 가 비어 있습니다. GitHub Secrets를 확인하세요.")
    return value


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
        # 링크 미리보기 카드(adaptive 등)는 본문에 링크가 이미 있으니 버린다
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


def build_source(messages):
    groups = defaultdict(list)
    for m in sorted(messages, key=lambda x: x["sent"]):
        groups[(m["team"], m["channel"])].append(m)

    parts = []
    for (team, channel), items in groups.items():
        lines = [f"## {team} / {channel}"]
        for m in items:
            lines.append(f"- {m['sent']:%m-%d %H:%M} {m['sender']}: {m['body'] or '(내용 없음)'}")
            if m["files"]:
                lines.append(f"  첨부: {', '.join(m['files'])}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ---------- 3. 요약 ----------

def summarize(api_key, source):
    prompt = f"""아래는 고등학생이 속한 Microsoft Teams 수업 채널들에서 최근 {WINDOW_HOURS}시간 동안 올라온 메시지다.
학생 본인이 빠르게 훑어볼 수 있는 한국어 일일 정리를 만들어라.

형식:
## 오늘 꼭 챙길 것
- 마감, 제출, 준비물, 시험처럼 행동이 필요한 것만. `- [ ] (과목) 내용 — 기한` 형태. 없으면 "없음".

## 과목별 요약
### 과목명
- 핵심 1~3줄. 올라온 자료 파일명이 있으면 함께.

## 공지
- 학년·학교 공지만 따로.

규칙:
- 원문에 없는 기한이나 내용을 지어내지 마라. 기한이 모호하면 "기한 불명확"이라고 적어라.
- 과목명은 팀 이름을 알아보기 쉽게 줄여라 (예: "심화수학(장)_26_1_1" → "심화수학(장)").
- 인사, 이모지 반응, 잡담은 버려라.
- 링크는 꼭 필요한 것만 남겨라.

원문:
{source}"""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": MODEL, "max_tokens": 3000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    r.raise_for_status()
    return "\n".join(b.get("text", "") for b in r.json().get("content", [])
                     if b.get("type") == "text").strip()


# ---------- 4. 이슈 ----------

def create_issue(token, repo, title, body):
    r = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        json={"title": title, "body": body, "labels": ["teams-digest"]},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["html_url"]


def main():
    text = download(env("DIGEST_URL"))
    all_messages = parse_digest(text)

    cutoff = datetime.now(KST) - timedelta(hours=WINDOW_HOURS)
    # 사진만 올린 풀이 인증처럼 글도 파일명도 없는 메시지는 버린다
    recent = [m for m in all_messages
              if m["sent"] >= cutoff and (m["body"] or m["files"])]
    print(f"전체 {len(all_messages)}건 중 최근 {WINDOW_HOURS}시간 {len(recent)}건")

    if not recent:
        print("새 메시지가 없어 이슈를 만들지 않습니다.")
        return

    source = build_source(recent)
    summary = summarize(env("ANTHROPIC_API_KEY"), source)

    today = datetime.now(KST).strftime("%Y-%m-%d")
    body = (f"{summary}\n\n---\n<details><summary>원문 {len(recent)}건 보기</summary>\n\n"
            f"{source}\n\n</details>")
    url = create_issue(env("GH_TOKEN"), env("GH_REPO"), f"[Teams] {today} 일일 정리", body)
    print(f"이슈를 만들었습니다: {url}")


if __name__ == "__main__":
    main()
