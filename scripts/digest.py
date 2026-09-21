"""Teams -> OneDrive(digest.txt) -> AI 정리 -> GitHub 이슈

버튼(Run workflow)을 누를 때마다 digest.txt를 받아 지난 정리 이후 새 메시지를 정리한다.
AI 키가 있으면 AI로 요약하고,
없거나 실패하면 키워드로 할 일을 뽑아 과목별로 정리한다.
"""

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# AI는 선택 사항.
# COPILOT_GITHUB_TOKEN 시크릿이 있으면 GitHub Copilot으로, AI_API_KEY 등이 있으면 OpenAI 호환 API로 요약.
# 없거나 실패하면 키워드 정리 모드로 이슈를 만든다.
AI_BASE_URL = os.environ.get("AI_BASE_URL", "").rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
COPILOT_TOKEN = os.environ.get("COPILOT_GITHUB_TOKEN", "")
USE_COPILOT = bool(COPILOT_TOKEN)          # GitHub Copilot(무료 요금제 포함) 우선
AI_ENABLED = USE_COPILOT or bool(AI_BASE_URL and AI_MODEL and AI_API_KEY)
KST = timezone(timedelta(hours=9))
FALLBACK_HOURS = 6            # 이전 이슈가 없을 때만 쓰는 기본 범위
CHUNK_CHARS = 30000 if os.environ.get("COPILOT_GITHUB_TOKEN") else 7000
MIN_CHUNK_CHARS = 1500
MARKER = re.compile(r"<!-- last-message: (\S+) -->")
FIELD = re.compile(r"(분류|보낸 사람|보낸 시각|본문|첨부):\s*〔(.*?)〕(?=\s*(?:분류|보낸 사람|보낸 시각|본문|첨부):|\s*$)", re.S)
GH_API = "https://api.github.com"
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


# ---------- 3. 요약 ----------

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


class AIUnavailable(Exception):
    pass


def ask_copilot(prompt):
    """Copilot CLI를 비대화 모드로 한 번 실행. 모델은 지정하지 않는다(무료 요금제는 자동 선택만 허용)."""
    global calls_this_run
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    env.update(COPILOT_GITHUB_TOKEN=COPILOT_TOKEN, NO_COLOR="1")
    instruction = "파일을 읽거나 명령을 실행하지 말고, 아래 요청에 대한 답만 마크다운으로 출력해라.\n\n"
    with tempfile.TemporaryDirectory() as empty_dir:   # 레포 파일을 건드리지 않게 빈 폴더에서 실행
        try:
            r = subprocess.run(["copilot", "-p", instruction + prompt],
                               cwd=empty_dir, env=env, capture_output=True,
                               text=True, timeout=600)
        except FileNotFoundError:
            raise AIUnavailable("Copilot CLI가 설치되지 않았습니다 (워크플로 파일 확인)")
        except subprocess.TimeoutExpired:
            raise AIUnavailable("Copilot 응답이 10분 넘게 없었습니다")
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        raise AIUnavailable("Copilot 오류: " + (err[-1][:200] if err else f"종료 코드 {r.returncode}"))
    calls_this_run += 1
    # 끝에 붙는 사용량 통계 줄은 버린다
    cut = re.search(r"^\s*(Total usage|Usage by model|Total duration|Total code changes)", out, re.M)
    return (out[:cut.start()] if cut else out).strip()


def ask(token, prompt):
    if USE_COPILOT:
        return ask_copilot(prompt)
    global calls_this_run
    for attempt in range(6):
        try:
            r = requests.post(
                f"{AI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": 4000,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=180,
            )
        except requests.RequestException as e:
            raise AIUnavailable(f"AI 서버 연결 실패: {e}")
        if r.status_code != 429:
            break
        wait = int(r.headers.get("retry-after", "0") or 0)
        if wait > 300:
            break
        time.sleep(max(wait, 65))

    if r.status_code in (413,) or (r.status_code == 400 and "token" in r.text.lower()):
        calls_this_run += 1
        raise TooLong()
    if r.status_code == 429:
        raise AIUnavailable("오늘 AI 호출 한도에 걸렸습니다")
    if r.status_code >= 400:
        raise AIUnavailable(f"AI 응답 오류 {r.status_code}: {r.text[:200]}")
    calls_this_run += 1
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError):
        raise AIUnavailable("AI 응답 형식을 읽지 못했습니다")


def summarize_chunk(token, text, hours_label):
    prompt = (f"아래는 고등학생이 속한 Microsoft Teams 수업 채널들에서 {hours_label} 올라온 메시지다.\n"
              f"학생 본인이 빠르게 훑어볼 수 있는 한국어 정리를 만들어라.\n\n{FORMAT}\n\n원문:\n{text}")
    return ask(token, prompt)


def summarize(token, sections, hours_label):
    limit = CHUNK_CHARS
    while True:
        chunks = chunk(sections, limit)
        try:
            parts = [summarize_chunk(token, c, hours_label) for c in chunks]
            break
        except TooLong:
            if limit <= MIN_CHUNK_CHARS:
                raise AIUnavailable("메시지 하나가 너무 길어 AI가 받지 못했습니다")
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



# ---------- 3-1. AI 없이 정리 (키워드 모드) ----------

TODO_WORDS = re.compile(r"마감|제출|까지|준비물|시험|평가|수행|과제|숙제|발표|기한|지참|필참|신청|변경|취소|휴강|보강|\d{1,2}\s*/\s*\d{1,2}|\d{1,2}월\s*\d{1,2}일")


def keyword_summary(messages):
    todo = []
    for m in sorted(messages, key=lambda x: x["sent"]):
        text = m["body"].replace("\n", " ")
        if TODO_WORDS.search(text) or any(TODO_WORDS.search(f) for f in m["files"]):
            subject = re.sub(r"_?\d{2}_\d.*$", "", m["team"]).strip() or m["team"]
            short = text if len(text) <= 120 else text[:117] + "…"
            todo.append(f"- [ ] ({subject}) {short} — {m['sender']}, {m['sent']:%m-%d %H:%M}")
    head = "## 꼭 챙길 것 (키워드로 자동 추출)\n" + ("\n".join(todo) if todo else "- 없음")
    return head + "\n\n## 과목별 원문\n" + "\n\n".join(build_sections(messages)).replace("## ", "### ")

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



def find_dashboard(token, repo):
    r = requests.get(f"{GH_API}/repos/{repo}/issues",
                     headers=gh_headers(token),
                     params={"labels": "ai-usage", "state": "open", "per_page": 1},
                     timeout=60)
    r.raise_for_status()
    items = r.json()
    return items[0] if items else None


def workflow_url(repo):
    ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    name = ref.split("/.github/workflows/")[-1].split("@")[0] if ref else "teams-digest.yml"
    return f"https://github.com/{repo}/actions/workflows/{name}"


def update_dashboard(token, repo, dashboard, status):
    mode = "🤖 AI 요약" if AI_ENABLED else "📋 키워드 정리"
    body = (f"## {mode}\n\n"
            f"### [▶ 지금 요약하기]({workflow_url(repo)})\n"
            f"링크를 누른 뒤 오른쪽의 `Run workflow` → 초록 `Run workflow` 버튼\n\n"
            f"---\n마지막 실행: {status}\n")
    if dashboard:
        requests.patch(f"{GH_API}/repos/{repo}/issues/{dashboard['number']}",
                       headers=gh_headers(token), json={"body": body}, timeout=60).raise_for_status()
    else:
        requests.post(f"{GH_API}/repos/{repo}/issues",
                      headers=gh_headers(token),
                      json={"title": "▶ 요약 버튼", "body": body, "labels": ["ai-usage"]},
                      timeout=60).raise_for_status()


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")

def main():
    token, repo = env("GH_TOKEN"), env("GH_REPO")
    now = datetime.now(KST)
    dashboard = find_dashboard(token, repo)
    status = "실행 중 오류"

    try:

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
            status = f"{now:%m-%d %H:%M} · 새 메시지 없음"
            print("새 메시지가 없어 이슈를 만들지 않습니다.")
            return

        sections = build_sections(new)
        label = f"{since:%m-%d %H:%M}부터 {now:%m-%d %H:%M}까지"
        ai_note = ""
        if AI_ENABLED:
            try:
                summary, n_chunks = summarize(token, sections, label)
            except AIUnavailable as e:
                print(f"AI 요약 실패, 키워드 정리로 대체: {e}")
                summary, n_chunks = keyword_summary(new), 1
                ai_note = f"\n⚠️ AI 요약 실패({e}) → 키워드 정리로 대체"
        else:
            summary, n_chunks = keyword_summary(new), 1

        latest = max(m["sent"] for m in new)
        source = "\n\n".join(sections)
        note = f" · {n_chunks}개 조각으로 나눠 요약" if n_chunks > 1 else ""
        body = (f"{summary}\n\n---\n{label} · 새 메시지 {len(new)}건{note}\n"
                + ("AI 요약" if AI_ENABLED and not ai_note else "키워드 정리")
                + f"{ai_note}\n\n"
                + (f"<details><summary>원문 보기</summary>\n\n{source}\n\n</details>\n\n"
                   if "## 과목별 원문" not in summary else "")
                + f"<!-- last-message: {latest.isoformat()} -->")
        url = create_issue(token, repo, f"[Teams] {now:%m-%d %H:%M} 정리", body)
        status = f"{now:%m-%d %H:%M} · 새 메시지 {len(new)}건 · [결과 보기]({url})"
        print(f"이슈를 만들었습니다: {url}")
    finally:
        write_summary(f"## 실행 결과\n\n{status}")
        try:
            update_dashboard(token, repo, dashboard, status)
        except requests.RequestException as e:
            print(f"요약 버튼 이슈 갱신 실패: {e}")


if __name__ == "__main__":
    main()
